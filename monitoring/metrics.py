"""monitoring/metrics.py - Prometheus metrics for the Kalshi trading bot.

Exposes metrics on http://localhost:{PROMETHEUS_PORT}/metrics
All metrics follow naming convention: kalshi_bot_*
"""

from __future__ import annotations

import logging
from typing import Optional

from prometheus_client import Counter, Gauge, Histogram, start_http_server

from config import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Histogram buckets (seconds)
# ---------------------------------------------------------------------------
LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class BotMetrics:
    """Container for all Prometheus metrics.

    Instantiate once and share across the bot.
    Call start_server() during bot startup.
    """

    def __init__(self) -> None:
        # --- Order metrics ---
        self.orders_placed = Counter(
            "kalshi_bot_orders_placed_total",
            "Total number of orders placed (paper or live).",
        )
        self.order_errors = Counter(
            "kalshi_bot_order_errors_total",
            "Total number of order placement failures.",
        )

        # --- P&L metrics ---
        self.daily_pnl_cents = Gauge(
            "kalshi_bot_daily_pnl_cents",
            "Running daily P&L in cents (positive = profit).",
        )
        self.total_pnl_cents = Gauge(
            "kalshi_bot_total_pnl_cents",
            "All-time accumulated P&L in cents.",
        )

        # --- Position metrics ---
        self.open_positions = Gauge(
            "kalshi_bot_open_positions",
            "Number of open positions currently held.",
        )
        self.markets_ingested = Gauge(
            "kalshi_bot_markets_ingested",
            "Number of open markets ingested in the latest poll.",
        )

        # --- Circuit breaker ---
        self.circuit_breaker_trips = Counter(
            "kalshi_bot_circuit_breaker_trips_total",
            "Total number of times the circuit breaker has tripped.",
        )
        self.circuit_breaker_active = Gauge(
            "kalshi_bot_circuit_breaker_active",
            "1 if circuit breaker is currently tripped, 0 otherwise.",
        )

        # --- Latency metrics ---
        self.model_latency = Histogram(
            "kalshi_bot_model_latency_seconds",
            "Time taken by the prediction model to compute a single prediction.",
            buckets=LATENCY_BUCKETS,
        )
        self.order_latency = Histogram(
            "kalshi_bot_order_latency_seconds",
            "Round-trip time for order placement (live mode only).",
            buckets=LATENCY_BUCKETS,
        )
        self.api_latency = Histogram(
            "kalshi_bot_api_latency_seconds",
            "Round-trip time for Kalshi API calls.",
            buckets=LATENCY_BUCKETS,
        )

        # --- Error metrics ---
        self.errors_total = Counter(
            "kalshi_bot_errors_total",
            "Total number of errors encountered by the bot.",
        )
        self.consecutive_api_failures = Gauge(
            "kalshi_bot_consecutive_api_failures",
            "Current count of consecutive API call failures.",
        )

        # --- Uptime ---
        self.bot_up = Gauge(
            "kalshi_bot_up",
            "1 if the bot is running, 0 if it is down.",
        )

        self._server_started = False

    def start_server(self, port: Optional[int] = None) -> None:
        """Start the Prometheus HTTP metrics server.

        Safe to call multiple times; will only start once.
        """
        if self._server_started:
            return
        p = port or config.prometheus_port
        try:
            start_http_server(p)
            self._server_started = True
            self.bot_up.set(1)
            logger.info("Prometheus metrics server started on port %d.", p)
        except OSError as exc:
            logger.warning("Could not start Prometheus server on port %d: %s", p, exc)

    def record_shutdown(self) -> None:
        """Mark the bot as down in metrics."""
        self.bot_up.set(0)

    def update_circuit_breaker(self, is_tripped: bool) -> None:
        """Update circuit breaker gauge."""
        self.circuit_breaker_active.set(1 if is_tripped else 0)
        if is_tripped:
            self.circuit_breaker_trips.inc()
