"""app/main.py - EventTradingBot: the main async orchestrator.

Starts two concurrent tasks:
  1. ingest_loop   - uses MarketScanner to poll Kalshi for active markets every N seconds
  2. decision_loop - runs strategy.evaluate_market + executor every M seconds

Position tracking prevents doubling up on the same market.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Set

from app.balance_sync import extract_account_balance_cents
from app.market_scanner import MarketScanner
from app.strategy import evaluate_market_with_modes
from app.strategy_modes import ModelContext
from app.model_runner import run_model_once
from clients.kalshi_client import AsyncKalshiClient
from config import config
from executor.trade_executor import TradeExecutor
from monitoring.metrics import BotMetrics
from monitoring.alerting import AlertingClient
from storage.async_db_store import AsyncPostgresStore
from model.example_model import ExampleHeuristicModel
from model.interface import PredictionModel

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

    def __init__(
        self,
        *,
        client: Optional[AsyncKalshiClient] = None,
        store: Optional[AsyncPostgresStore] = None,
        scanner: Optional[MarketScanner] = None,
        metrics: Optional[BotMetrics] = None,
        alerts: Optional[AlertingClient] = None,
        model: Optional[PredictionModel] = None,
    ) -> None:
        self._client = client or AsyncKalshiClient()
        self._scanner = scanner or MarketScanner(self._client)
        self._store = store or AsyncPostgresStore()
        self._executor: Optional[TradeExecutor] = None
        self._metrics = metrics or BotMetrics()
        self._alerts = alerts or AlertingClient()
        self._running = False
        self._bot_id = os.getenv("BOT_INSTANCE_ID", "default")
        self._model: Optional[PredictionModel] = model

        # Latest raw snapshots keyed by ticker
        self._latest_snapshots: Dict[str, Dict[str, Any]] = {}
        self._previous_snapshots: Dict[str, Dict[str, Any]] = {}  # For change detection
        self._last_ingest_at: float = 0.0
        self._last_trade_at: float = 0.0
        self._last_alert_at: Dict[str, float] = {}
        self._no_change_count: int = 0  # Count of consecutive ingests with no changes

        # Position tracking: tickers we already hold a position in
        self._open_positions: Set[str] = set()
        # Exposure tracking (worst-case loss in cents)
        self._market_exposure_cents: Dict[str, int] = {}
        self._category_exposure_cents: Dict[str, int] = {}
        # Account balance used for sizing
        self._account_balance_cents: int = 0

        # Bounded concurrency for market evaluation (limit concurrent processing)
        max_concurrent = max(1, config.max_concurrent_evaluations)
        self._evaluation_semaphore = asyncio.Semaphore(max_concurrent)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open connections, warm up."""
        config.validate()
        await self._client.open()
        await self._store.connect()
        self._executor = TradeExecutor(self._client)
        if config.enable_prediction_model and self._model is None:
            self._model = ExampleHeuristicModel()
            try:
                self._model.warm_up()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Model warm_up failed: %s", exc)
        # Restore circuit breaker counters from persisted bot state (per-day)
        try:
            state = await self._store.load_bot_state(self._bot_id)
            if state and self._executor is not None:
                self._executor.restore_circuit_breaker_state(
                    trading_date=state.get("trading_date"),
                    daily_trades=state.get("daily_trades", 0),
                    daily_pnl_cents=state.get("daily_pnl_cents", 0),
                    is_tripped=state.get("is_tripped", False),
                    consecutive_failures=state.get("consecutive_failures", 0),
                )
                cb_state = self._executor.get_circuit_breaker_state()
                self._metrics.update_circuit_breaker(cb_state.is_tripped)
                self._metrics.consecutive_api_failures.set(
                    cb_state.consecutive_failures
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to restore bot state at startup: %s", exc)
        # Initialise circuit breaker P&L state from persisted trades (authoritative)
        try:
            await self._executor.sync_daily_pnl_from_store(self._store)
            cb_state = self._executor.get_circuit_breaker_state()
            self._metrics.update_circuit_breaker(cb_state.is_tripped)
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
        if self._executor is not None:
            try:
                await self._store.save_bot_state(
                    self._bot_id, self._executor.get_circuit_breaker_state()
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to persist bot state on shutdown: %s", exc)
        self._metrics.record_shutdown()
        await self._client.close()
        await self._store.close()
        await self._alerts.close()
        if self._model is not None:
            try:
                self._model.tear_down()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Model tear_down failed: %s", exc)
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
                self._cleanup_loop(),
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

                # Track if any meaningful changes occurred
                changes_detected = False
                new_snapshots: Dict[str, Dict[str, Any]] = {}

                for market in markets:
                    ticker = market.get("ticker", "")
                    if not ticker:
                        continue
                    snapshot = self._scanner.to_snapshot(market)
                    new_snapshots[ticker] = snapshot

                    # Check if this is a new ticker or if prices changed
                    prev = self._previous_snapshots.get(ticker)
                    if prev is None or (
                        prev.get("yes_bid") != snapshot.get("yes_bid")
                        or prev.get("yes_ask") != snapshot.get("yes_ask")
                        or prev.get("last_price") != snapshot.get("last_price")
                    ):
                        changes_detected = True

                    await self._store.log_market_snapshot(
                        ticker, snapshot, raise_on_error=True
                    )

                self._latest_snapshots = new_snapshots
                # Stores last ingest only (for change detection), not a history
                self._previous_snapshots = new_snapshots.copy()

                if not changes_detected and len(markets) > 0:
                    self._no_change_count += 1
                else:
                    self._no_change_count = 0

                elapsed = time.monotonic() - t0
                self._last_ingest_at = time.monotonic()
                self._metrics.markets_ingested.set(len(markets))
                logger.debug(
                    "Ingest: %d active markets cached in %.2fs (changes=%s, no_change_count=%d).",
                    len(markets),
                    elapsed,
                    changes_detected,
                    self._no_change_count,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Ingest loop error: %s", exc)
                await self._store.log_error("ingest_loop", str(exc))
                self._metrics.errors_total.inc()
                await self._alerts.post(
                    "Kalshi bot ingest loop error", extra={"error": str(exc)}
                )

            # Adaptive sleep: if no changes detected for 3+ cycles, increase interval by 50%
            base_interval = config.market_snapshot_interval
            if self._no_change_count >= 3:
                sleep_interval = base_interval * 1.5
                logger.debug(
                    "No orderbook changes detected, backing off to %.1fs interval",
                    sleep_interval,
                )
            else:
                sleep_interval = base_interval

            await asyncio.sleep(sleep_interval)

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
                await self._alerts.post(
                    "Kalshi bot position sync failed", extra={"error": str(exc)}
                )

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
                await self._alerts.post(
                    "Kalshi bot balance sync failed", extra={"error": str(exc)}
                )

    # ------------------------------------------------------------------
    # Cleanup loop - daily database maintenance
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Periodically clean up old database records to prevent unbounded growth."""
        cleanup_interval = 86400  # Run cleanup once per day (seconds)
        check_interval = 60  # Check shutdown flag every minute

        while self._running:
            # Sleep in chunks until the daily interval elapses, allowing fast shutdown
            elapsed = 0
            while self._running and elapsed < cleanup_interval:
                await asyncio.sleep(check_interval)
                elapsed += check_interval

            try:
                deleted = await self._store.cleanup_old_snapshots(days_to_keep=7)
                logger.info(
                    "Database cleanup completed: %d old snapshots removed", deleted
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Database cleanup failed: %s", exc)

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

            now = time.monotonic()
            if (
                self._last_ingest_at > 0
                and config.stale_snapshot_alert_seconds > 0
                and (now - self._last_ingest_at) > config.stale_snapshot_alert_seconds
            ):
                await self._maybe_alert(
                    "stale_snapshots",
                    "Kalshi bot snapshots are stale",
                    extra={
                        "seconds_since_last_ingest": int(now - self._last_ingest_at),
                        "latest_snapshots": len(self._latest_snapshots),
                    },
                    min_interval_seconds=config.stale_snapshot_alert_seconds,
                )

            # Alert if ingest is running but fetching zero markets
            if (
                self._last_ingest_at > 0
                and len(self._latest_snapshots) == 0
                and (now - self._last_ingest_at) < config.stale_snapshot_alert_seconds
            ):
                await self._maybe_alert(
                    "zero_markets",
                    "Kalshi bot is fetching zero markets",
                    extra={
                        "latest_snapshots": 0,
                        "no_change_count": self._no_change_count,
                        "market_snapshot_interval": config.market_snapshot_interval,
                    },
                    min_interval_seconds=300,
                )

            if (
                self._last_trade_at > 0
                and config.no_trade_alert_seconds > 0
                and (now - self._last_trade_at) > config.no_trade_alert_seconds
            ):
                await self._maybe_alert(
                    "no_trades",
                    "Kalshi bot has not traded recently",
                    extra={"seconds_since_last_trade": int(now - self._last_trade_at)},
                    min_interval_seconds=config.no_trade_alert_seconds,
                )

            if (
                self._executor is not None
                and self._executor.is_circuit_breaker_tripped()
            ):
                logger.warning(
                    "Decision loop: circuit breaker tripped; skipping trading cycle."
                )
                continue

            # Evaluate markets concurrently with bounded concurrency
            tasks = [
                self._evaluate_and_trade(ticker, snapshot)
                for ticker, snapshot in list(self._latest_snapshots.items())
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _evaluate_and_trade(self, ticker: str, snapshot: Dict[str, Any]) -> None:
        """Run strategy on one snapshot and execute if a signal is generated.

        This is the concurrency gate; core logic lives in _evaluate_and_trade_impl.
        """
        # Bounded concurrency: acquire semaphore before processing
        async with self._evaluation_semaphore:
            await self._evaluate_and_trade_impl(ticker, snapshot)

    async def _evaluate_and_trade_impl(
        self, ticker: str, snapshot: Dict[str, Any]
    ) -> None:
        """Internal implementation of market evaluation and trading logic."""
        # Skip if we already hold a position in this market
        if ticker in self._open_positions:
            logger.debug("Skipping %s - already have an open position.", ticker)
            return

        # Run the strategy
        try:
            model_probability = None
            model_confidence = None
            implied_probability = None

            if config.enable_prediction_model and self._model is not None:
                event = {
                    "id": ticker,
                    "ticker": ticker,
                    "category": snapshot.get("category", ""),
                }
                # Run model prediction in a thread to avoid blocking the async event loop
                prob, conf, implied, model_latency_s = await asyncio.to_thread(
                    run_model_once, self._model, event=event, snapshot=snapshot
                )
                model_probability = prob
                model_confidence = conf
                implied_probability = implied
                self._metrics.model_latency.observe(model_latency_s)
                if model_probability is not None and model_confidence is not None:
                    await self._store.log_model_output(
                        ticker=ticker,
                        model_name=self._model.name,
                        probability=float(model_probability),
                        confidence=float(model_confidence),
                        implied_prob=float(implied_probability),
                        raise_on_error=True,
                    )

            signal = evaluate_market_with_modes(
                snapshot,
                model=ModelContext(
                    probability=model_probability,
                    confidence=model_confidence,
                    implied_probability=implied_probability,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Strategy error for %s: %s", ticker, exc)
            return

        if signal is None:
            return

        score: float = signal.get("score", 0.0)
        side: str = signal.get("side", "yes")
        action: str = signal.get("action", "buy")
        yes_price: int = int(signal.get("yes_price", 0))
        model_probability = float(signal.get("model_probability", 0.0) or 0.0)
        model_confidence = float(signal.get("model_confidence", 0.0) or 0.0)
        implied_probability = float(signal.get("implied_probability", 0.0) or 0.0)

        if yes_price <= 0 or yes_price >= 100:
            return

        # --- Exposure-based risk checks ---
        category = snapshot.get("category", "")
        # Worst-case loss per contract based on side
        worst_loss_per_contract = yes_price if side == "yes" else 100 - yes_price
        worst_loss_per_contract = max(1, min(99, worst_loss_per_contract))
        trade_worst_loss_cents = worst_loss_per_contract  # count=1

        market_exposure = self._market_exposure_cents.get(ticker, 0)
        category_exposure = (
            self._category_exposure_cents.get(category, 0) if category else 0
        )

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
            model_probability=model_probability,
            model_confidence=model_confidence,
            implied_probability=implied_probability,
            notes=f"score={score:.4f}",
        )

        if record:
            await self._store.log_trade(record, raise_on_error=True)
            self._metrics.orders_placed.inc()
            cb_state = self._executor.get_circuit_breaker_state()
            self._metrics.update_circuit_breaker(cb_state.is_tripped)
            self._metrics.consecutive_api_failures.set(cb_state.consecutive_failures)
            self._last_trade_at = time.monotonic()
            # NOTE: Position will be reflected in next position_sync_loop cycle.
            # We don't optimistically add to avoid false positives if order fails to fill.
            logger.info("Order logged: %s | %s", record.id, ticker)
        else:
            cb_state = self._executor.get_circuit_breaker_state()
            self._metrics.update_circuit_breaker(cb_state.is_tripped)
            self._metrics.consecutive_api_failures.set(cb_state.consecutive_failures)
            if cb_state.is_tripped:
                await self._alerts.post(
                    "Kalshi bot circuit breaker TRIPPED",
                    extra={
                        "ticker": ticker,
                        "consecutive_failures": cb_state.consecutive_failures,
                        "daily_trades": cb_state.daily_trades,
                        "daily_pnl_cents": cb_state.daily_pnl_cents,
                    },
                )

    async def _maybe_alert(
        self,
        key: str,
        text: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        min_interval_seconds: int = 300,
    ) -> None:
        now = time.monotonic()
        last = self._last_alert_at.get(key, 0.0)
        if (now - last) < float(max(1, min_interval_seconds)):
            return
        self._last_alert_at[key] = now
        await self._alerts.post(text, extra=extra)


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
