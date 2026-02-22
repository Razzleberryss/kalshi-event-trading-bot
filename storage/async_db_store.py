"""storage/async_db_store.py - Async Postgres storage using asyncpg.

Tables: orders, events, market_snapshots, model_outputs
All writes are fire-and-forget safe (errors are logged, not raised).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncpg

from config import config
from executor.trade_executor import TradeRecord

logger = logging.getLogger(__name__)


CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    action          TEXT NOT NULL,
    count           INTEGER NOT NULL,
    price_cents     INTEGER NOT NULL,
    pnl_cents       INTEGER DEFAULT 0,
    mode            TEXT NOT NULL,
    order_id        TEXT,
    model_prob      FLOAT,
    model_conf      FLOAT,
    implied_prob    FLOAT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    yes_bid         INTEGER,
    yes_ask         INTEGER,
    last_price      INTEGER,
    volume          INTEGER,
    open_interest   INTEGER,
    raw_json        JSONB
);

CREATE TABLE IF NOT EXISTS model_outputs (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL,
    ticker          TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    probability     FLOAT NOT NULL,
    confidence      FLOAT NOT NULL,
    implied_prob    FLOAT,
    edge            FLOAT
);

CREATE TABLE IF NOT EXISTS errors (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,
    message         TEXT NOT NULL,
    details         JSONB
);

CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_ticker ON market_snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_model_outputs_ticker ON model_outputs(ticker);
"""


class AsyncPostgresStore:
    """Async Postgres storage layer via asyncpg connection pool."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Create the connection pool and ensure schema is initialised."""
        self._pool = await asyncpg.create_pool(
            dsn=config.database_url,
            min_size=config.db_pool_min_size,
            max_size=config.db_pool_max_size,
        )
        await self._init_schema()
        logger.info("AsyncPostgresStore connected (pool min=%d max=%d).",
                    config.db_pool_min_size, config.db_pool_max_size)

    async def close(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("AsyncPostgresStore pool closed.")

    async def _init_schema(self) -> None:
        """Create tables if they don't exist."""
        async with self._pool.acquire() as conn:
            await conn.execute(CREATE_TABLES_SQL)
        logger.debug("Database schema initialised.")

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    async def log_trade(self, record: TradeRecord) -> None:
        """Persist a TradeRecord to the orders table."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO orders
                        (id, timestamp, ticker, side, action, count, price_cents,
                         pnl_cents, mode, order_id, model_prob, model_conf,
                         implied_prob, notes)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    record.id,
                    record.timestamp,
                    record.ticker,
                    record.side,
                    record.action,
                    record.count,
                    record.price_cents,
                    record.pnl_cents,
                    record.mode,
                    record.order_id,
                    record.model_probability,
                    record.model_confidence,
                    record.implied_probability,
                    record.notes,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to log trade %s: %s", record.id, exc)

    async def log_market_snapshot(
        self, ticker: str, snapshot: Dict[str, Any]
    ) -> None:
        """Store a raw market snapshot."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO market_snapshots
                        (captured_at, ticker, yes_bid, yes_ask, last_price,
                         volume, open_interest, raw_json)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                    """,
                    datetime.now(tz=timezone.utc),
                    ticker,
                    snapshot.get("yes_bid"),
                    snapshot.get("yes_ask"),
                    snapshot.get("last_price"),
                    snapshot.get("volume"),
                    snapshot.get("open_interest"),
                    json.dumps(snapshot),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to log snapshot for %s: %s", ticker, exc)

    async def log_model_output(
        self,
        ticker: str,
        model_name: str,
        probability: float,
        confidence: float,
        implied_prob: float,
    ) -> None:
        """Store a model prediction."""
        edge = probability - implied_prob
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO model_outputs
                        (captured_at, ticker, model_name, probability,
                         confidence, implied_prob, edge)
                    VALUES ($1,$2,$3,$4,$5,$6,$7)
                    """,
                    datetime.now(tz=timezone.utc),
                    ticker,
                    model_name,
                    probability,
                    confidence,
                    implied_prob,
                    edge,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to log model output for %s: %s", ticker, exc)

    async def log_error(
        self, source: str, message: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Store an error event for forensic audit."""
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO errors (captured_at, source, message, details)
                    VALUES ($1,$2,$3,$4)
                    """,
                    datetime.now(tz=timezone.utc),
                    source,
                    message,
                    json.dumps(details or {}),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to log error from %s: %s", source, exc)

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    async def get_daily_pnl_cents(self) -> int:
        """Sum of pnl_cents for today's orders."""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT COALESCE(SUM(pnl_cents), 0) AS total "
                    "FROM orders WHERE timestamp::date = CURRENT_DATE"
                )
                return int(row["total"])
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch daily P&L: %s", exc)
            return 0

    async def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch the most recent trades."""
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM orders ORDER BY timestamp DESC LIMIT $1", limit
                )
                return [dict(r) for r in rows]
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fetch recent trades: %s", exc)
            return []
