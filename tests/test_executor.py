"""tests/test_executor.py - Unit tests for the trade executor."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from clients.kalshi_client import AsyncKalshiClient
from executor.trade_executor import CircuitBreakerState, TradeExecutor


@pytest.fixture()
def mock_client() -> AsyncKalshiClient:
    client = MagicMock(spec=AsyncKalshiClient)
    client.place_order = AsyncMock(
        return_value={"order": {"id": "test-order-id", "status": "resting"}}
    )
    return client


@pytest.fixture()
def executor(mock_client: AsyncKalshiClient) -> TradeExecutor:
    return TradeExecutor(kalshi_client=mock_client)


class TestCircuitBreakerState:
    def test_reset_on_new_day(self) -> None:
        """Circuit breaker should reset counters on a new day."""
        import datetime
        cb = CircuitBreakerState()
        cb.daily_trades = 10
        cb.daily_pnl_cents = -1000
        cb.is_tripped = True
        # Simulate a new day
        cb.date = datetime.date(2020, 1, 1)
        cb.reset_if_new_day()
        assert cb.daily_trades == 0
        assert cb.daily_pnl_cents == 0
        assert cb.is_tripped is False

    def test_no_reset_same_day(self) -> None:
        """Circuit breaker should NOT reset if still the same day."""
        import datetime
        cb = CircuitBreakerState()
        cb.daily_trades = 5
        cb.date = datetime.date.today()
        cb.reset_if_new_day()
        assert cb.daily_trades == 5


class TestTradeExecutorPaperMode:
    def test_paper_execute_returns_record(self, executor: TradeExecutor) -> None:
        """Paper mode should return a TradeRecord without calling the API."""
        record = asyncio.run(
            executor.execute(
                ticker="TEST-MARKET",
                side="yes",
                action="buy",
                count=1,
                yes_price=55,
            )
        )
        assert record is not None
        assert record.ticker == "TEST-MARKET"
        assert record.mode == "PAPER"
        assert record.price_cents == 55

    def test_circuit_breaker_blocks_after_max_trades(self, executor: TradeExecutor) -> None:
        """Executor should block trades after max_daily_trades is exceeded."""
        from config import config
        executor._cb.daily_trades = config.max_daily_trades
        record = asyncio.run(
            executor.execute(
                ticker="TEST-MARKET",
                side="yes",
                action="buy",
                count=1,
                yes_price=55,
            )
        )
        assert record is None
        assert executor._cb.is_tripped is True

    def test_circuit_breaker_blocks_when_tripped(self, executor: TradeExecutor) -> None:
        """Executor should immediately block when circuit breaker is tripped."""
        executor._cb.is_tripped = True
        record = asyncio.run(
            executor.execute(
                ticker="ANOTHER-MARKET",
                side="yes",
                action="buy",
                count=1,
                yes_price=60,
            )
        )
        assert record is None

    def test_order_size_capped(self, executor: TradeExecutor) -> None:
        """Orders exceeding max size should be capped."""
        from config import config
        # Set very low limit to trigger capping
        original_max = config.max_order_size_cents
        config.max_order_size_cents = 100  # 1 dollar
        try:
            record = asyncio.run(
                executor.execute(
                    ticker="TEST-MARKET",
                    side="yes",
                    action="buy",
                    count=50,  # 50 * 50 cents = 2500 cents > 100 limit
                    yes_price=50,
                )
            )
            assert record is not None
            assert record.count == 2  # 100 // 50 = 2
        finally:
            config.max_order_size_cents = original_max

    def test_daily_pnl_trips_circuit_breaker(self, executor: TradeExecutor) -> None:
        """Daily loss exceeding limit should trip the circuit breaker."""
        from config import config
        executor._cb.daily_pnl_cents = -(config.daily_loss_limit_cents + 1)
        record = asyncio.run(
            executor.execute(
                ticker="TEST-MARKET",
                side="yes",
                action="buy",
                count=1,
                yes_price=40,
            )
        )
        assert record is None
        assert executor._cb.is_tripped is True

    def test_get_daily_summary(self, executor: TradeExecutor) -> None:
        """daily_summary should return correct structure."""
        summary = executor.get_daily_summary()
        assert "date" in summary
        assert "trades" in summary
        assert "pnl_cents" in summary
        assert "circuit_breaker_tripped" in summary
