"""config.py - Central configuration for the Kalshi Event Trading Bot.

All settings are loaded from environment variables with sane defaults.
Copy .env.example to .env and fill in your values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TradingMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass
class TradingConfig:
    # ---------------------------------------------------------------------------
    # Runtime mode
    # ---------------------------------------------------------------------------
    mode: TradingMode = field(
        default_factory=lambda: TradingMode(
            os.getenv("TRADING_MODE", "PAPER").upper()
        )
    )

    # ---------------------------------------------------------------------------
    # Kalshi API
    # ---------------------------------------------------------------------------
    kalshi_api_key: str = field(
        default_factory=lambda: os.getenv("KALSHI_API_KEY", "")
    )
    kalshi_api_secret: str = field(
        default_factory=lambda: os.getenv("KALSHI_API_SECRET", "")
    )
    kalshi_base_url: str = field(
        default_factory=lambda: os.getenv(
            "KALSHI_BASE_URL", "https://trading-api.kalshi.com/trade-api/v2"
        )
    )
    kalshi_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("KALSHI_TIMEOUT_SECONDS", "10"))
    )
    kalshi_max_retries: int = field(
        default_factory=lambda: int(os.getenv("KALSHI_MAX_RETRIES", "3"))
    )

    # ---------------------------------------------------------------------------
    # Risk limits
    # ---------------------------------------------------------------------------
    max_order_size_cents: int = field(
        default_factory=lambda: int(os.getenv("MAX_ORDER_SIZE_CENTS", "1000"))  # $10
    )
    max_risk_fraction_per_trade: float = field(
        default_factory=lambda: float(
            os.getenv("MAX_RISK_FRACTION_PER_TRADE", "0.02")
        )
    )
    max_notional_per_market_cents: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_NOTIONAL_PER_MARKET_CENTS", "5000")
        )
    )
    max_notional_per_category_cents: int = field(
        default_factory=lambda: int(
            os.getenv("MAX_NOTIONAL_PER_CATEGORY_CENTS", "20000")
        )
    )
    daily_loss_limit_cents: int = field(
        default_factory=lambda: int(os.getenv("DAILY_LOSS_LIMIT_CENTS", "5000"))  # $50
    )
    max_daily_trades: int = field(
        default_factory=lambda: int(os.getenv("MAX_DAILY_TRADES", "50"))
    )
    circuit_breaker_threshold: int = field(
        default_factory=lambda: int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
    )

    # ---------------------------------------------------------------------------
    # Decision engine thresholds
    # ---------------------------------------------------------------------------
    min_edge_threshold: float = field(
        default_factory=lambda: float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))
    )
    min_confidence: float = field(
        default_factory=lambda: float(os.getenv("MIN_CONFIDENCE", "0.60"))
    )

    # ---------------------------------------------------------------------------
    # Poll intervals (seconds)
    # ---------------------------------------------------------------------------
    market_snapshot_interval: int = field(
        default_factory=lambda: int(os.getenv("MARKET_SNAPSHOT_INTERVAL", "5"))
    )
    decision_loop_interval: int = field(
        default_factory=lambda: int(os.getenv("DECISION_LOOP_INTERVAL", "60"))
    )

    # ---------------------------------------------------------------------------
    # Database
    # ---------------------------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/kalshi_bot",
        )
    )
    db_pool_min_size: int = field(
        default_factory=lambda: int(os.getenv("DB_POOL_MIN_SIZE", "2"))
    )
    db_pool_max_size: int = field(
        default_factory=lambda: int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    )

    # ---------------------------------------------------------------------------
    # Monitoring / alerting
    # ---------------------------------------------------------------------------
    prometheus_port: int = field(
        default_factory=lambda: int(os.getenv("PROMETHEUS_PORT", "8000"))
    )
    slack_webhook_url: Optional[str] = field(
        default_factory=lambda: os.getenv("SLACK_WEBHOOK_URL", None)
    )
    alert_on_consecutive_failures: int = field(
        default_factory=lambda: int(
            os.getenv("ALERT_ON_CONSECUTIVE_FAILURES", "3")
        )
    )

    # ---------------------------------------------------------------------------
    # Logging
    # ---------------------------------------------------------------------------
    log_level: str = field(
        default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper()
    )

    def validate(self) -> None:
        """Raise ValueError for any obviously wrong config values."""
        if self.mode == TradingMode.LIVE:
            if not self.kalshi_api_key:
                raise ValueError("KALSHI_API_KEY must be set when TRADING_MODE=LIVE")
            if not self.kalshi_api_secret:
                raise ValueError("KALSHI_API_SECRET must be set when TRADING_MODE=LIVE")
        if self.max_order_size_cents <= 0:
            raise ValueError("MAX_ORDER_SIZE_CENTS must be positive")
        if not (0 < self.max_risk_fraction_per_trade < 1):
            raise ValueError("MAX_RISK_FRACTION_PER_TRADE must be between 0 and 1")
        if self.max_notional_per_market_cents <= 0:
            raise ValueError("MAX_NOTIONAL_PER_MARKET_CENTS must be positive")
        if self.max_notional_per_category_cents <= 0:
            raise ValueError("MAX_NOTIONAL_PER_CATEGORY_CENTS must be positive")
        if self.daily_loss_limit_cents <= 0:
            raise ValueError("DAILY_LOSS_LIMIT_CENTS must be positive")
        if not (0 < self.min_edge_threshold < 1):
            raise ValueError("MIN_EDGE_THRESHOLD must be between 0 and 1")
        if not (0 < self.min_confidence <= 1):
            raise ValueError("MIN_CONFIDENCE must be between 0 and 1")


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere else
# ---------------------------------------------------------------------------
config = TradingConfig()
