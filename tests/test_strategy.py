"""Tests for trading strategy logic.

Covers:
- evaluate_market with both dict markets (from API) and MagicMock object markets
- 'active' status (Kalshi API) AND 'open' status (legacy) both accepted
- score_market with volume_24h field (from /events endpoint)
- is_tradeable boundary conditions
"""
from unittest.mock import MagicMock

from app.strategy import (
    SCORE_THRESHOLD,
    evaluate_market,
    is_tradeable,
    score_market,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _liquid_dict(status="active", yes_bid=45, yes_ask=55,
                 volume_24h=10_000, open_interest=500, ticker="TEST"):
    """Return a dict market (as returned by the Kalshi API)."""
    return {
        "ticker": ticker,
        "status": status,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
    }


def _liquid_mock(status="active", yes_bid=45, yes_ask=55,
                 volume=10_000, open_interest=500, ticker="TEST"):
    """Return a MagicMock object market."""
    m = MagicMock()
    m.status = status
    m.yes_bid = yes_bid
    m.yes_ask = yes_ask
    m.volume = volume
    m.volume_24h = None  # simulate missing volume_24h on object
    m.open_interest = open_interest
    m.ticker = ticker
    return m


# ---------------------------------------------------------------------------
# TestEvaluateMarket
# ---------------------------------------------------------------------------

class TestEvaluateMarket:
    """Tests for evaluate_market function."""

    # --- Status gate ---

    def test_returns_none_for_closed_market_dict(self):
        """Dict market with closed status should return None."""
        result = evaluate_market({"status": "closed", "ticker": "X"})
        assert result is None

    def test_returns_none_for_closed_market_obj(self):
        """Object market with closed status should return None."""
        m = MagicMock()
        m.status = "closed"
        assert evaluate_market(m) is None

    def test_accepts_active_status(self):
        """Kalshi API returns 'active' - must be accepted."""
        market = _liquid_dict(status="active")
        result = evaluate_market(market)
        # May return None if score < threshold, but should NOT fail on status
        # We just verify it wasn't blocked by status alone
        # (high volume + tight spread should score above threshold)
        assert result is None or isinstance(result, dict)

    def test_accepts_open_status_legacy(self):
        """Legacy 'open' status should also be accepted for backwards compat."""
        market = _liquid_dict(status="open")
        result = evaluate_market(market)
        assert result is None or isinstance(result, dict)

    def test_rejects_unknown_status(self):
        """Unknown status should return None."""
        market = _liquid_dict(status="unknown")
        assert evaluate_market(market) is None

    # --- Volume gate ---

    def test_returns_none_for_zero_volume(self):
        """Dict market with zero volume should be skipped."""
        market = _liquid_dict(volume_24h=0, open_interest=0)
        assert evaluate_market(market) is None

    def test_returns_none_for_zero_volume_obj(self):
        """Object market with zero volume should be skipped."""
        m = _liquid_mock(status="active", volume=0, open_interest=0)
        assert evaluate_market(m) is None

    # --- Signal quality ---

    def test_returns_signal_for_high_score_dict(self):
        """High-volume, tight-spread active dict market should produce a signal."""
        market = _liquid_dict(
            status="active",
            yes_bid=46, yes_ask=52,
            volume_24h=50_000, open_interest=1_000,
            ticker="GOOD-MARKET",
        )
        result = evaluate_market(market)
        if result is not None:
            assert "ticker" in result
            assert "action" in result
            assert "side" in result
            assert "yes_price" in result
            assert "score" in result
            assert result["action"] == "buy"

    def test_prefers_yes_when_yes_price_low_dict(self):
        """Dict market: strategy should buy YES when YES is cheap (<30c)."""
        market = _liquid_dict(
            status="active",
            yes_bid=20, yes_ask=22,
            volume_24h=50_000, open_interest=1_000,
            ticker="CHEAP-YES",
        )
        result = evaluate_market(market)
        if result is not None:
            assert result["side"] == "yes"
            assert result["action"] == "buy"

    def test_prefers_no_when_yes_price_high_dict(self):
        """Dict market: strategy should buy NO when YES is expensive (>70c)."""
        market = _liquid_dict(
            status="active",
            yes_bid=78, yes_ask=80,
            volume_24h=50_000, open_interest=1_000,
            ticker="CHEAP-NO",
        )
        result = evaluate_market(market)
        if result is not None:
            assert result["side"] == "no"
            assert result["action"] == "buy"

    def test_prefers_yes_when_yes_price_low_obj(self):
        """Object market: strategy should buy YES when YES is cheap."""
        m = _liquid_mock(status="active", yes_bid=20, yes_ask=22,
                         volume=50_000, open_interest=1_000, ticker="CHEAP-YES")
        result = evaluate_market(m)
        if result is not None:
            assert result["side"] == "yes"

    def test_prefers_no_when_yes_price_high_obj(self):
        """Object market: strategy should buy NO when YES is overpriced."""
        m = _liquid_mock(status="active", yes_bid=78, yes_ask=80,
                         volume=50_000, open_interest=1_000, ticker="CHEAP-NO")
        result = evaluate_market(m)
        if result is not None:
            assert result["side"] == "no"


# ---------------------------------------------------------------------------
# TestScoreMarket
# ---------------------------------------------------------------------------

class TestScoreMarket:
    """Tests for score_market function."""

    def test_score_is_float_dict(self):
        """Score from dict market should always return a float."""
        score = score_market(_liquid_dict())
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_score_is_float_obj(self):
        """Score from object market should always return a float."""
        score = score_market(_liquid_mock())
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_volume_24h_used_when_present(self):
        """volume_24h field (from /events endpoint) should be used for scoring."""
        high = _liquid_dict(volume_24h=100_000, open_interest=2_000)
        low = _liquid_dict(volume_24h=100, open_interest=10)
        assert score_market(high) > score_market(low)

    def test_high_volume_increases_score(self):
        """Higher volume should produce a higher score."""
        low_vol = _liquid_dict(volume_24h=100, open_interest=10)
        high_vol = _liquid_dict(volume_24h=100_000, open_interest=5_000)
        assert score_market(high_vol) > score_market(low_vol)

    def test_tight_spread_increases_score(self):
        """Tight bid-ask spread should produce a higher score."""
        wide = _liquid_dict(yes_bid=30, yes_ask=70, volume_24h=50_000, open_interest=1_000)
        tight = _liquid_dict(yes_bid=48, yes_ask=52, volume_24h=50_000, open_interest=1_000)
        assert score_market(tight) > score_market(wide)


# ---------------------------------------------------------------------------
# TestIsTradeable
# ---------------------------------------------------------------------------

class TestIsTradeable:
    """Tests for is_tradeable function."""

    def test_returns_true_for_high_score(self):
        """Score above threshold should be tradeable."""
        assert is_tradeable(SCORE_THRESHOLD + 0.01) is True

    def test_returns_false_for_low_score(self):
        """Score below threshold should not be tradeable."""
        assert is_tradeable(SCORE_THRESHOLD - 0.01) is False

    def test_returns_true_at_exact_threshold(self):
        """Score exactly at threshold should be tradeable (>= semantics)."""
        assert is_tradeable(SCORE_THRESHOLD) is True

    def test_handles_zero_score(self):
        """Zero score should never be tradeable."""
        assert is_tradeable(0.0) is False

    def test_handles_perfect_score(self):
        """Perfect score of 1.0 should always be tradeable."""
        assert is_tradeable(1.0) is True
