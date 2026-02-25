"""executor/trade_executor.py - Trade execution engine with circuit breakers.

Supports PAPER (simulated) and LIVE modes.
Enforces daily loss limits, trade count limits, and circuit breakers.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from clients.kalshi_client import AsyncKalshiClient
from config import TradingMode, config

if TYPE_CHECKING:
    from storage.async_db_store import AsyncPostgresStore

logger = logging.getLogger(__name__)


@dataclass
class TradeRecord:
    """Immutable record of a single executed trade."""
    id: str
    timestamp: datetime
    ticker: str
    side: str          # "yes" / "no"
    action: str        # "buy" / "sell"
    count: int
    price_cents: int   # fill price in cents
    pnl_cents: int = 0
    mode: str = "PAPER"
    order_id: Optional[str] = None
    model_probability: float = 0.0
    model_confidence: float = 0.0
    implied_probability: float = 0.0
    notes: str = ""


@dataclass
class CircuitBreakerState:
    """Tracks daily risk counters and trip state."""
    date: date = field(default_factory=lambda: date.today())
    daily_trades: int = 0
    daily_pnl_cents: int = 0
    is_tripped: bool = False
    consecutive_failures: int = 0

    def reset_if_new_day(self) -> None:
        today = date.today()
        if self.date != today:
            logger.info("New trading day %s - resetting circuit breaker.", today)
            self.date = today
            self.daily_trades = 0
            self.daily_pnl_cents = 0
            self.is_tripped = False
            self.consecutive_failures = 0


class TradeExecutor:
    """Routes orders to PAPER simulator or live Kalshi API.

    Circuit breakers automatically halt trading when:
        - Daily P&L loss exceeds ``config.daily_loss_limit_cents``
        - Daily trade count exceeds ``config.max_daily_trades``
        - Consecutive API failures exceed ``config.circuit_breaker_threshold``
    """

    def __init__(self, kalshi_client: AsyncKalshiClient) -> None:
        self._client = kalshi_client
        self._mode = config.mode
        self._cb = CircuitBreakerState()
        self._trades: List[TradeRecord] = []
        self._account_balance_cents: int = 0
        logger.info("TradeExecutor initialized in %s mode.", self._mode.value)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int,
        model_probability: float = 0.0,
        model_confidence: float = 0.0,
        implied_probability: float = 0.0,
        notes: str = "",
    ) -> Optional[TradeRecord]:
        """Execute a trade after circuit breaker checks.

        Returns a TradeRecord on success, None if blocked.
        """
        self._cb.reset_if_new_day()

        # --- Circuit breaker checks ---
        if self._cb.is_tripped:
            logger.warning("Circuit breaker TRIPPED. Blocking trade on %s.", ticker)
            return None

        if self._cb.daily_trades >= config.max_daily_trades:
            logger.warning(
                "Max daily trades (%d) reached. Blocking trade.",
                config.max_daily_trades,
            )
            self._trip_circuit_breaker("max_daily_trades")
            return None

        if self._cb.daily_pnl_cents <= -config.daily_loss_limit_cents:
            logger.warning(
                "Daily loss limit (%d cents) hit. Blocking trade.",
                config.daily_loss_limit_cents,
            )
            self._trip_circuit_breaker("daily_loss_limit")
            return None

        # --- Account balance based per-trade cap ---
        worst_loss_per_contract = yes_price if side == "yes" else 100 - yes_price
        worst_loss_per_contract = max(1, min(99, worst_loss_per_contract))
        if self._account_balance_cents > 0:
            max_loss_for_trade = int(
                self._account_balance_cents * config.max_risk_fraction_per_trade
            )
            max_contracts_by_balance = max_loss_for_trade // worst_loss_per_contract
            if max_contracts_by_balance <= 0:
                logger.warning(
                    "Account balance too low to risk any contracts on %s; blocking trade.",
                    ticker,
                )
                return None
            if count > max_contracts_by_balance:
                logger.warning(
                    "Requested count %d exceeds balance-based max %d for %s; capping.",
                    count,
                    max_contracts_by_balance,
                    ticker,
                )
                count = max_contracts_by_balance

        # --- Order size cap ---
        order_value = count * yes_price
        if order_value > config.max_order_size_cents:
            logger.warning(
                "Order value %d cents exceeds max %d. Capping count.",
                order_value,
                config.max_order_size_cents,
            )
            count = max(1, config.max_order_size_cents // yes_price)

        # --- Route to PAPER or LIVE ---
        if self._mode == TradingMode.PAPER:
            record = self._paper_execute(
                ticker, side, action, count, yes_price,
                model_probability, model_confidence, implied_probability, notes,
            )
        else:
            record = await self._live_execute(
                ticker, side, action, count, yes_price,
                model_probability, model_confidence, implied_probability, notes,
            )

        if record:
            self._cb.daily_trades += 1
            self._trades.append(record)
            logger.info(
                "Trade executed: %s %s %s x%d @ %d cents (mode=%s)",
                action, side, ticker, count, yes_price, self._mode.value,
            )
        return record

    def get_daily_summary(self) -> Dict[str, Any]:
        """Return today's trading summary."""
        self._cb.reset_if_new_day()
        return {
            "date": str(self._cb.date),
            "trades": self._cb.daily_trades,
            "pnl_cents": self._cb.daily_pnl_cents,
            "circuit_breaker_tripped": self._cb.is_tripped,
        }

    def register_pnl(self, pnl_cents: int) -> None:
        """Update daily P&L (called by storage layer after settlement)."""
        self._cb.reset_if_new_day()
        self._cb.daily_pnl_cents += pnl_cents
        if self._cb.daily_pnl_cents <= -config.daily_loss_limit_cents:
            self._trip_circuit_breaker("pnl_update")

    def update_account_balance(self, balance_cents: int) -> None:
        """Update cached account balance in cents."""
        if balance_cents < 0:
            logger.warning("Ignoring negative account balance value: %d", balance_cents)
            return
        self._account_balance_cents = balance_cents

    async def sync_daily_pnl_from_store(self, store: "AsyncPostgresStore") -> None:
        """Initialise daily P&L from persistent storage.

        This is typically called once at startup so that the in-memory
        circuit breaker state reflects any realised P&L already recorded
        in the database for the current trading day.
        """
        self._cb.reset_if_new_day()
        try:
            pnl_cents = await store.get_daily_pnl_cents()
        except Exception:  # noqa: BLE001
            logger.error("Failed to sync daily P&L from store; leaving at %d cents.", self._cb.daily_pnl_cents)
            return

        self._cb.daily_pnl_cents = pnl_cents
        if self._cb.daily_pnl_cents <= -config.daily_loss_limit_cents:
            self._trip_circuit_breaker("startup_daily_loss_limit")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _trip_circuit_breaker(self, reason: str) -> None:
        self._cb.is_tripped = True
        logger.error("CIRCUIT BREAKER TRIPPED: reason=%s", reason)

    def _paper_execute(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int,
        model_probability: float,
        model_confidence: float,
        implied_probability: float,
        notes: str,
    ) -> TradeRecord:
        """Simulate order fill using bid/ask mid-price."""
        logger.debug("[PAPER] Simulating fill: %s %s x%d @ %d", action, ticker, count, yes_price)
        return TradeRecord(
            id=str(uuid.uuid4()),
            timestamp=datetime.now(tz=timezone.utc),
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            price_cents=yes_price,
            mode="PAPER",
            model_probability=model_probability,
            model_confidence=model_confidence,
            implied_probability=implied_probability,
            notes=notes,
        )

    async def _live_execute(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int,
        model_probability: float,
        model_confidence: float,
        implied_probability: float,
        notes: str,
    ) -> Optional[TradeRecord]:
        """Place a real order via the Kalshi API."""
        try:
            resp = await self._client.place_order(
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                yes_price=yes_price,
            )
            order_id = resp.get("order", {}).get("id", "unknown")
            self._cb.consecutive_failures = 0
            return TradeRecord(
                id=str(uuid.uuid4()),
                timestamp=datetime.now(tz=timezone.utc),
                ticker=ticker,
                side=side,
                action=action,
                count=count,
                price_cents=yes_price,
                mode="LIVE",
                order_id=order_id,
                model_probability=model_probability,
                model_confidence=model_confidence,
                implied_probability=implied_probability,
                notes=notes,
            )
        except Exception as exc:  # noqa: BLE001
            self._cb.consecutive_failures += 1
            logger.error("Live order failed for %s: %s", ticker, exc)
            if self._cb.consecutive_failures >= config.circuit_breaker_threshold:
                self._trip_circuit_breaker("consecutive_api_failures")
            return None
