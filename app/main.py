"""app/main.py - EventTradingBot: the main async orchestrator.

Starts two concurrent tasks:
  1. ingest_loop   - uses MarketScanner to poll Kalshi for active markets every N seconds
  2. decision_loop - runs strategy.evaluate_market + executor every M seconds

Position tracking prevents doubling up on the same market.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set

from app.balance_sync import extract_account_balance_cents
from app.market_scanner import MarketScanner
from app.strategy import evaluate_market
from clients.kalshi_client import AsyncKalshiClient
from config import config
from executor.trade_executor import TradeExecutor
from monitoring.metrics import BotMetrics
from storage.async_db_store import AsyncPostgresStore

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=config.log_level,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


class EventTradingBot:
    """Top-level bot orchestrator.

    Wires together:
      - AsyncKalshiClient  : fetches live market data
      - MarketScanner      : filters to only liquid, well-priced markets
      - strategy module    : scores each market and decides YES/NO direction
      - TradeExecutor      : routes to PAPER or LIVE with circuit breakers
      - AsyncPostgresStore : logs every trade and snapshot to Postgres
      - BotMetrics         : Prometheus counters / gauges
    """

    def __init__(self) -> None:
        self._client = AsyncKalshiClient()
        self._scanner = MarketScanner(self._client)
        self._store = AsyncPostgresStore()
        self._executor: Optional[TradeExecutor] = None
        self._metrics = BotMetrics()
        self._running = False

        # Latest raw snapshots keyed by ticker
        self._latest_snapshots: Dict[str, Dict[str, Any]] = {}

        # Position tracking: tickers we already hold a position in
        self._open_positions: Set[str] = set()
        # Exposure tracking (worst-case loss in cents)
        self._market_exposure_cents: Dict[str, int] = {}
        self._category_exposure_cents: Dict[str, int] = {}
        # Account balance used for sizing
        self._account_balance_cents: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open connections, warm up."""
        config.validate()
        await self._client.open()
        await self._store.connect()
        self._executor = TradeExecutor(self._client)
        # Initialise circuit breaker P&L state from persisted trades
        try:
            await self._executor.sync_daily_pnl_from_store(self._store)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync daily P&L from store at startup: %s", exc)
        self._metrics.start_server()
        self._running = True
        logger.info(
            "EventTradingBot started | mode=%s",
            config.mode.value,
        )

    async def shutdown(self) -> None:
        """Gracefully close all resources."""
        self._running = False
        await self._client.close()
        await self._store.close()
        logger.info("EventTradingBot shut down.")

    async def run(self) -> None:
        """Main entry point: starts ingest + decision tasks concurrently."""
        await self.startup()
        try:
            await asyncio.gather(
                self._ingest_loop(),
                self._decision_loop(),
                self._position_sync_loop(),
                self._balance_sync_loop(),
            )
        except asyncio.CancelledError:
            logger.info("Bot tasks cancelled.")
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Ingest loop - fast ticker (every N seconds)
    # ------------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        """Use MarketScanner to fetch liquid active markets and cache snapshots."""
        while self._running:
            try:
                t0 = time.monotonic()
                markets: List[Dict[str, Any]] = await self._scanner.scan(
                    status="active"
                )
                for market in markets:
                    ticker = market.get("ticker", "")
                    if not ticker:
                        continue
                    snapshot = self._scanner.to_snapshot(market)
                    self._latest_snapshots[ticker] = snapshot
                    await self._store.log_market_snapshot(ticker, snapshot)

                elapsed = time.monotonic() - t0
                self._metrics.markets_ingested.set(len(markets))
                logger.debug(
                    "Ingest: %d active markets cached in %.2fs.",
                    len(markets),
                    elapsed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Ingest loop error: %s", exc)
                await self._store.log_error("ingest_loop", str(exc))
                self._metrics.errors_total.inc()

            await asyncio.sleep(config.market_snapshot_interval)

    # ------------------------------------------------------------------
    # Position sync loop - keeps _open_positions current
    # ------------------------------------------------------------------

    async def _position_sync_loop(self) -> None:
        """Periodically refresh open positions from Kalshi.

        Prevents us from doubling up on markets we already hold.
        Runs every 60 seconds (same cadence as the decision loop).
        """
        while self._running:
            await asyncio.sleep(config.decision_loop_interval)
            try:
                positions = await self._client.get_positions()
                open_positions: Set[str] = set()
                market_exposure: Dict[str, int] = {}
                category_exposure: Dict[str, int] = {}

                for pos in positions:
                    ticker = pos.get("ticker", "")
                    position = int(pos.get("position", 0) or 0)
                    if not ticker or position == 0:
                        continue

                    open_positions.add(ticker)
                    # Worst-case loss per contract on Kalshi is 100 cents.
                    worst_loss_cents = abs(position) * 100
                    market_exposure[ticker] = worst_loss_cents

                    snapshot = self._latest_snapshots.get(ticker, {})
                    category = snapshot.get("category") or pos.get("category", "")
                    if category:
                        category_exposure[category] = (
                            category_exposure.get(category, 0) + worst_loss_cents
                        )

                self._open_positions = open_positions
                self._market_exposure_cents = market_exposure
                self._category_exposure_cents = category_exposure
                self._metrics.open_positions.set(len(self._open_positions))

                logger.debug(
                    "Position sync: %d open positions.",
                    len(self._open_positions),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Position sync failed: %s", exc)

    # ------------------------------------------------------------------
    # Balance sync loop - keeps account balance current for sizing
    # ------------------------------------------------------------------

    async def _balance_sync_loop(self) -> None:
        """Periodically refresh account balance for risk-based sizing."""
        while self._running:
            await asyncio.sleep(config.decision_loop_interval)
            try:
                balance_payload = await self._client.get_balance()
                balance_cents = extract_account_balance_cents(balance_payload)
                if balance_cents < 0:
                    logger.warning(
                        "Ignoring negative balance value from API: %d",
                        balance_cents,
                    )
                    continue
                self._account_balance_cents = balance_cents
                if self._executor is not None:
                    self._executor.update_account_balance(balance_cents)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Balance sync failed: %s", exc)

    # ------------------------------------------------------------------
    # Decision loop - slower ticker (every M seconds)
    # ------------------------------------------------------------------

    async def _decision_loop(self) -> None:
        """Run strategy.evaluate_market on every snapshot, execute if signal found."""
        while self._running:
            await asyncio.sleep(config.decision_loop_interval)

            if not self._latest_snapshots:
                logger.debug("Decision loop: no snapshots yet, waiting.")
                continue

            for ticker, snapshot in list(self._latest_snapshots.items()):
                await self._evaluate_and_trade(ticker, snapshot)

    async def _evaluate_and_trade(
        self, ticker: str, snapshot: Dict[str, Any]
    ) -> None:
        """Run strategy on one snapshot and execute if a signal is generated."""
        # Skip if we already hold a position in this market
        if ticker in self._open_positions:
            logger.debug("Skipping %s - already have an open position.", ticker)
            return

        # Run the strategy
        try:
            signal = evaluate_market(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.error("Strategy error for %s: %s", ticker, exc)
            return

        if signal is None:
            return

        score: float = signal.get("score", 0.0)
        side: str = signal.get("side", "yes")
        action: str = signal.get("action", "buy")
        yes_price: int = int(signal.get("yes_price", 0))

        if yes_price <= 0 or yes_price >= 100:
            return

        # --- Exposure-based risk checks ---
        category = snapshot.get("category", "")
        # Worst-case loss per contract based on side
        worst_loss_per_contract = yes_price if side == "yes" else 100 - yes_price
        worst_loss_per_contract = max(1, min(99, worst_loss_per_contract))
        trade_worst_loss_cents = worst_loss_per_contract  # count=1

        market_exposure = self._market_exposure_cents.get(ticker, 0)
        category_exposure = self._category_exposure_cents.get(category, 0) if category else 0

        new_market_exposure = market_exposure + trade_worst_loss_cents
        new_category_exposure = category_exposure + trade_worst_loss_cents

        if new_market_exposure > config.max_notional_per_market_cents:
            logger.info(
                "Blocking trade on %s - market exposure limit exceeded "
                "(current=%d, new=%d, max=%d).",
                ticker,
                market_exposure,
                new_market_exposure,
                config.max_notional_per_market_cents,
            )
            return

        if category and new_category_exposure > config.max_notional_per_category_cents:
            logger.info(
                "Blocking trade on %s (category=%s) - category exposure limit exceeded "
                "(current=%d, new=%d, max=%d).",
                ticker,
                category,
                category_exposure,
                new_category_exposure,
                config.max_notional_per_category_cents,
            )
            return

        logger.info(
            "SIGNAL: %s | side=%s action=%s price=%d score=%.3f",
            ticker,
            side,
            action,
            yes_price,
            score,
        )

        assert self._executor is not None
        record = await self._executor.execute(
            ticker=ticker,
            side=side,
            action=action,
            count=1,
            yes_price=yes_price,
            notes=f"score={score:.4f}",
        )

        if record:
            await self._store.log_trade(record)
            self._metrics.orders_placed.inc()
            # Optimistically add to open positions until next sync
            self._open_positions.add(ticker)
            logger.info("Order logged: %s | %s", record.id, ticker)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _handle_signal(bot: EventTradingBot, loop: asyncio.AbstractEventLoop) -> None:
    logger.info("Shutdown signal received.")
    loop.create_task(bot.shutdown())


async def main() -> None:
    bot = EventTradingBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, bot, loop)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
