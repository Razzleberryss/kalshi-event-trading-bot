"""Tests for market scanner / filter logic."""
import pytest

from unittest.mock import MagicMock, AsyncMock
from app.market_scanner import MarketScanner

def make_market(
    ticker="TEST-MARKET",
    status="open",
    volume=10000,
    open_interest=500,
    yes_bid=40,
    yes_ask=60,
    category="Sports",
    close_time=None,
):
    """Helper to create a mock market object."""
    m = MagicMock()
    m.ticker = ticker
    m.status = status
    m.volume = volume
    m.open_interest = open_interest
    m.yes_bid = yes_bid
    m.yes_ask = yes_ask
    m.category = category
    m.close_time = close_time
    return m


@pytest.fixture
def mock_kalshi_client():
    client = AsyncMock()
    return client


@pytest.fixture
def scanner(mock_kalshi_client):
    return MarketScanner(client=mock_kalshi_client)


class TestMarketScanner:
    """Tests for MarketScanner class."""

    @pytest.mark.asyncio
    async def test_scan_returns_list(self, scanner, mock_kalshi_client):
        """scan() should always return a list."""
        mock_kalshi_client.get_markets.return_value = []
        result = await scanner.scan()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_scan_filters_closed_markets(self, scanner, mock_kalshi_client):
        """Closed markets should be excluded from results."""
        markets = [
            make_market(ticker="OPEN-1", status="open"),
            make_market(ticker="CLOSED-1", status="closed"),
            make_market(ticker="OPEN-2", status="open"),
        ]
        mock_kalshi_client.get_markets.return_value = markets
        result = await scanner.scan()
        tickers = [m.ticker for m in result]
        assert "CLOSED-1" not in tickers
        assert "OPEN-1" in tickers
        assert "OPEN-2" in tickers

    @pytest.mark.asyncio
    async def test_scan_filters_low_volume(self, scanner, mock_kalshi_client):
        """Markets with zero volume should be excluded."""
        markets = [
            make_market(ticker="HIGH-VOL", volume=50000),
            make_market(ticker="ZERO-VOL", volume=0),
            make_market(ticker="LOW-VOL", volume=10),
        ]
        mock_kalshi_client.get_markets.return_value = markets
        result = await scanner.scan()
        tickers = [m.ticker for m in result]
        assert "ZERO-VOL" not in tickers
        assert "HIGH-VOL" in tickers

    @pytest.mark.asyncio
    async def test_scan_returns_sorted_by_volume(self, scanner, mock_kalshi_client):
        """Results should be sorted by volume descending."""
        markets = [
            make_market(ticker="LOW", volume=100),
            make_market(ticker="HIGH", volume=100000),
            make_market(ticker="MED", volume=5000),
        ]
        mock_kalshi_client.get_markets.return_value = markets
        result = await scanner.scan()
        if len(result) >= 2:
            volumes = [m.volume for m in result]
            assert volumes == sorted(volumes, reverse=True)

    @pytest.mark.asyncio
    async def test_scan_handles_api_error(self, scanner, mock_kalshi_client):
        """Scanner should handle API errors gracefully."""
        mock_kalshi_client.get_markets.side_effect = Exception("API error")
        result = await scanner.scan()
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_handles_empty_market_list(self, scanner, mock_kalshi_client):
        """Scanner should return empty list when no markets exist."""
        mock_kalshi_client.get_markets.return_value = []
        result = await scanner.scan()
        assert result == []

    @pytest.mark.asyncio
    async def test_scan_respects_category_filter(self, scanner, mock_kalshi_client):
        """Scanner should filter by category when specified."""
        markets = [
            make_market(ticker="SPORTS-1", category="Sports"),
            make_market(ticker="POLITICS-1", category="Politics"),
            make_market(ticker="SPORTS-2", category="Sports"),
        ]
        mock_kalshi_client.get_markets.return_value = markets
        result = await scanner.scan(category="Sports")
        for m in result:
            assert m.category == "Sports"


class TestMarketScannerConfig:
    """Tests for MarketScanner configuration options."""

    def test_scanner_has_default_min_volume(self, mock_kalshi_client):
        """Scanner should have a default minimum volume threshold."""
        scanner = MarketScanner(client=mock_kalshi_client)
        assert hasattr(scanner, "min_volume")
        assert scanner.min_volume > 0

    def test_scanner_accepts_custom_min_volume(self, mock_kalshi_client):
        """Scanner should accept a custom minimum volume."""
        scanner = MarketScanner(client=mock_kalshi_client, min_volume=99999)
        assert scanner.min_volume == 99999

    def test_scanner_stores_client(self, mock_kalshi_client):
        """Scanner should store the Kalshi client reference."""
        scanner = MarketScanner(client=mock_kalshi_client)
        assert scanner.client is mock_kalshi_client
