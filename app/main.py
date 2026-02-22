"""app/main.py - EventTradingBot: the main async orchestrator.

Starts two concurrent tasks:
  1. ingest_loop  - polls Kalshi for market snapshots every N seconds
  2. decision_loop - runs model + decision engine every M seconds
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any, Dict, List

from clients.kalshi_client import AsyncKalshiClient
from config import config
from executor.trade_executor import TradeExecutor
from model.example_model import ExampleHeuristicModel
from model.interface import PredictionModel
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

    Wires together the Kalshi client, prediction model, trade executor,
    Postgres storage, and Prometheus metrics into a single async runtime.
    """

    def __init__(self, model: PredictionModel | None = None) -> None:
        self._model = model or ExampleHeuristicModel()
        self._client = AsyncKalshiClient()
        self._store = AsyncPostgresStore()
        self._executor: TradeExecutor | None = None
        self._metrics = BotMetrics()
        self._running = False
        self._latest_snapshots: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Open connections and warm up the model."""
        config.validate()
        await self._client.open()
        await self._store.connect()
        self._executor = TradeExecutor(self._client)
        self._model.warm_up()
        self._metrics.start_server()
        self._running = True
        logger.info(
            "EventTradingBot started | mode=%s model=%s",
            config.mode.value,
            self._model.name,
        )

    async def shutdown(self) -> None:
        """Gracefully close all resources."""
        self._running = False
        self._model.tear_down()
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
            )
        except asyncio.CancelledError:
            logger.info("Bot tasks cancelled.")
        finally:
            await self.shutdown()

    # ------------------------------------------------------------------
    # Ingest loop - fast ticker (every N seconds)
    # ------------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        """Poll open markets and store snapshots at high frequency."""
        while self._running:
            try:
                markets: List[Dict[str, Any]] = await self._client.get_markets(
                    status="open", limit=200
                )
                for market in markets:
                    ticker = market.get("ticker", "")
                    if not ticker:
                        continue
                    snapshot = {
                        "ticker": ticker,
                        "yes_bid": market.get("yes_bid", 0),
                        "yes_ask": market.get("yes_ask", 0),
                        "last_price": market.get("last_price", 0),
                        "volume": market.get("volume", 0),
                        "open_interest": market.get("open_interest", 0),
                        "status": market.get("status", ""),
                    }
                    self._latest_snapshots[ticker] = snapshot
                    await self._store.log_market_snapshot(ticker, snapshot)
                self._metrics.markets_ingested.set(len(markets))
                logger.debug("Ingest: %d open markets cached.", len(markets))
            except Exception as exc:  # noqa: BLE001
                logger.error("Ingest loop error: %s", exc)
                await self._store.log_error("ingest_loop", str(exc))
                self._metrics.errors_total.inc()

            await asyncio.sleep(config.market_snapshot_interval)

    # ------------------------------------------------------------------
    # Decision loop - slower ticker (every M seconds)
    # ------------------------------------------------------------------

    async def _decision_loop(self) -> None:
        """Run the model on every snapshot and execute trades if edge found."""
        while self._running:
            await asyncio.sleep(config.decision_loop_interval)
            if not self._latest_snapshots:
                logger.debug("Decision loop: no snapshots yet, waiting.")
                continue

            for ticker, snapshot in list(self._latest_snapshots.items()):
                await self._evaluate_market(ticker, snapshot)

    async def _evaluate_market(
        self, ticker: str, snapshot: Dict[str, Any]
    ) -> None:
        """Run model on a single market and execute if criteria are met."""
        event: Dict[str, Any] = {"id": ticker, "ticker": ticker}

        try:
            import time
            t0 = time.monotonic()
            prediction = self._model.predict(event, snapshot)
            latency = time.monotonic() - t0
            self._metrics.model_latency.observe(latency)
        except Exception as exc:  # noqa: BLE001
            logger.error("Model predict failed for %s: %s", ticker, exc)
            return

        probability: float = prediction.get("probability", 0.5)
        confidence: float = prediction.get("confidence", 0.0)

        yes_bid: float = float(snapshot.get("yes_bid", 50)) / 100.0
        yes_ask: float = float(snapshot.get("yes_ask", 50)) / 100.0
        implied_prob = (yes_bid + yes_ask) / 2.0
        edge = probability - implied_prob

        await self._store.log_model_output(
            ticker=ticker,
            model_name=self._model.name,
            probability=probability,
            confidence=confidence,
            implied_prob=implied_prob,
        )

        # --- Decision rule: buy YES if edge > threshold and confidence > min ---
        if edge > config.min_edge_threshold and confidence >= config.min_confidence:
            yes_price_cents = int(yes_ask * 100)  # pay the ask
            if yes_price_cents <= 0 or yes_price_cents >= 100:
                return

            logger.info(
                "SIGNAL: %s | model=%.3f implied=%.3f edge=%.3f conf=%.3f -> BUY YES @ %d",
                ticker, probability, implied_prob, edge, confidence, yes_price_cents,
            )

            record = await self._executor.execute(
                ticker=ticker,
                side="yes",
                action="buy",
                count=1,
                yes_price=yes_price_cents,
                model_probability=probability,
                model_confidence=confidence,
                implied_probability=implied_prob,
                notes=f"edge={edge:.4f}",
            )

            if record:
                await self._store.log_trade(record)
                self._metrics.orders_placed.inc()
                logger.info("Order logged: %s", record.id)


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
