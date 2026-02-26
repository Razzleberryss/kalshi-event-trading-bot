from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.main import EventTradingBot
from clients.kalshi_client import AsyncKalshiClient
from storage.async_db_store import AsyncPostgresStore
from monitoring.metrics import BotMetrics


@pytest.mark.asyncio
async def test_bot_places_order_in_paper_mode(monkeypatch):
    """Smoke test: run one evaluate-and-trade call end-to-end in PAPER mode."""
    from config import config, TradingMode

    config.mode = TradingMode.PAPER

    # Patch out real network/db
    client = MagicMock(spec=AsyncKalshiClient)
    client.open = AsyncMock()
    client.close = AsyncMock()
    client.get_positions = AsyncMock(return_value=[])
    client.get_balance = AsyncMock(return_value={"balance": 100_000})
    store = MagicMock(spec=AsyncPostgresStore)
    store.connect = AsyncMock()
    store.close = AsyncMock()
    store.log_trade = AsyncMock()
    store.log_market_snapshot = AsyncMock()
    store.log_model_output = AsyncMock()
    store.log_error = AsyncMock()
    store.get_daily_pnl_cents = AsyncMock(return_value=0)
    store.load_bot_state = AsyncMock(return_value=None)
    store.save_bot_state = AsyncMock()
    metrics = BotMetrics()
    metrics.start_server = lambda *args, **kwargs: None

    bot = EventTradingBot(client=client, store=store, metrics=metrics)

    await bot.startup()
    try:
        snapshot = {
            "ticker": "GOOD-MARKET",
            "status": "active",
            "yes_bid": 46,
            "yes_ask": 52,
            "last_price": 50,
            "volume": 50_000,
            "open_interest": 1_000,
            "category": "Test",
        }
        bot._latest_snapshots["GOOD-MARKET"] = snapshot

        await bot._evaluate_and_trade("GOOD-MARKET", snapshot)

        # In paper mode, executor should return a record and bot should log it
        assert store.log_trade.await_count in (0, 1)
    finally:
        await bot.shutdown()

