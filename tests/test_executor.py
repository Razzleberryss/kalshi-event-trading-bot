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

    def test_circuit_breaker_blocks_after_max_trades(
        self, executor: TradeExecutor
    ) -> None:
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


class TestTradeExecutorLiveRetries:
    def test_live_execute_uses_client_order_id_and_retries(
        self, executor: TradeExecutor
    ) -> None:
        """Live execute should retry with same client_order_id on transient errors."""
        from config import config, TradingMode

        executor._mode = TradingMode.LIVE
        # Fail first call, succeed second
        calls = {"n": 0, "client_order_ids": []}

        async def place_order(**kwargs):
            calls["n"] += 1
            calls["client_order_ids"].append(kwargs.get("client_order_id"))
            if calls["n"] == 1:
                raise Exception("timeout")  # noqa: TRY002
            return {"order": {"id": "ok"}}

        executor._client.place_order = AsyncMock(side_effect=place_order)
        original = config.kalshi_max_retries
        config.kalshi_max_retries = 2
        try:
            record = asyncio.run(
                executor.execute(
                    ticker="MKT",
                    side="yes",
                    action="buy",
                    count=1,
                    yes_price=50,
                )
            )
            assert record is not None
            assert calls["n"] == 2
            assert calls["client_order_ids"][0] is not None
            assert calls["client_order_ids"][0] == calls["client_order_ids"][1]
        finally:
            config.kalshi_max_retries = original


class TestTradeExecutorRiskInvariants:
    def test_balance_risk_fraction_caps_contracts(
        self, executor: TradeExecutor
    ) -> None:
        """Balance-based risk should cap contract count."""
        from config import config

        executor.update_account_balance(10_000)  # $100
        original_fraction = config.max_risk_fraction_per_trade
        config.max_risk_fraction_per_trade = 0.01  # risk $1 max
        try:
            record = asyncio.run(
                executor.execute(
                    ticker="TEST",
                    side="yes",
                    action="buy",
                    count=100,
                    yes_price=50,  # worst loss per contract = 50c
                )
            )
            assert record is not None
            # max_loss_for_trade = 100c; 100 // 50 = 2 contracts
            assert record.count == 2
        finally:
            config.max_risk_fraction_per_trade = original_fraction
