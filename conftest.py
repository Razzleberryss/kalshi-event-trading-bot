"""conftest.py - Root-level pytest configuration for kalshi-event-trading-bot.

Adds the project root to sys.path so all packages are importable without
having to install the package first.
"""
from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure repo root is on sys.path so that `import app`, `import clients`, etc.
# all resolve correctly when running pytest from any directory.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_client() -> AsyncMock:
    """An AsyncMock of AsyncKalshiClient for use across all test files."""
    client = AsyncMock()
    client.get_markets = AsyncMock(return_value=[])
    client.place_order = AsyncMock(return_value={"order_id": "test-order-123"})
    client.get_portfolio = AsyncMock(return_value={"balance": 10_000})
    return client


@pytest.fixture
def sample_market() -> MagicMock:
    """A MagicMock representing a typical open, liquid Kalshi market."""
    m = MagicMock()
    m.ticker = "SAMPLE-MARKET"
    m.status = "open"
    m.volume = 10_000
    m.open_interest = 500
    m.yes_bid = 45
    m.yes_ask = 55
    m.last_price = 50
    m.category = "Economics"
    m.close_time = None
    return m


@pytest.fixture
def closed_market() -> MagicMock:
    """A MagicMock representing a closed market (should be filtered out)."""
    m = MagicMock()
    m.ticker = "CLOSED-MARKET"
    m.status = "closed"
    m.volume = 10_000
    m.open_interest = 500
    m.yes_bid = 45
    m.yes_ask = 55
    m.last_price = 50
    m.category = "Economics"
    m.close_time = None
    return m


@pytest.fixture
def low_volume_market() -> MagicMock:
    """A MagicMock representing an illiquid market (should be filtered out)."""
    m = MagicMock()
    m.ticker = "ILLIQUID-MARKET"
    m.status = "open"
    m.volume = 10          # below MIN_VOLUME
    m.open_interest = 5   # below MIN_OPEN_INTEREST
    m.yes_bid = 45
    m.yes_ask = 55
    m.last_price = 50
    m.category = "Sports"
    m.close_time = None
    return m
