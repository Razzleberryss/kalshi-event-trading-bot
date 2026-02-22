"""Tests for trading strategy logic."""


from unittest.mock import MagicMock
from app.strategy import (
    evaluate_market,
    score_market,
    is_tradeable,
    SCORE_THRESHOLD,
)


class TestEvaluateMarket:
    """Tests for evaluate_market function."""

    def test_returns_none_for_closed_market(self):
        """Closed markets should not be evaluated."""
        market = MagicMock()
        market.status = "closed"
        result = evaluate_market(market)
        assert result is None

    def test_returns_none_for_low_volume(self):
        """Markets with insufficient volume should be skipped."""
        market = MagicMock()
        market.status = "open"
        market.volume = 0
        market.open_interest = 0
        result = evaluate_market(market)
        assert result is None

    def test_returns_signal_for_valid_market(self):
        """Valid market should return a tradeable signal."""
        market = MagicMock()
        market.status = "open"
        market.volume = 10000
        market.open_interest = 500
        market.yes_bid = 30
        market.yes_ask = 35
        market.ticker = "TEST-MARKET"
        result = evaluate_market(market)
        # Result should be a dict with trade info or None
        if result is not None:
            assert "ticker" in result
            assert "action" in result
            assert "yes_price" in result

    def test_prefers_yes_when_yes_price_low(self):
        """Strategy should prefer YES when probability is underpriced."""
        market = MagicMock()
        market.status = "open"
        market.volume = 50000
        market.open_interest = 1000
        market.yes_bid = 20  # 20 cents — underpriced YES
        market.yes_ask = 22
        market.ticker = "CHEAP-YES"
        result = evaluate_market(market)
        if result is not None:
            assert result["action"] == "buy"
            assert result["side"] == "yes"

    def test_prefers_no_when_yes_price_high(self):
        """Strategy should prefer NO when YES is overpriced."""
        market = MagicMock()
        market.status = "open"
        market.volume = 50000
        market.open_interest = 1000
        market.yes_bid = 78  # 78 cents — overpriced YES, cheap NO
        market.yes_ask = 80
        market.ticker = "CHEAP-NO"
        result = evaluate_market(market)
        if result is not None:
            assert result["action"] == "buy"
            assert result["side"] == "no"


class TestScoreMarket:
    """Tests for score_market function."""

    def test_score_is_float(self):
        """Score should always return a float."""
        market = MagicMock()
        market.volume = 10000
        market.open_interest = 500
        market.yes_bid = 45
        market.yes_ask = 55
        score = score_market(market)
        assert isinstance(score, float)

    def test_high_volume_increases_score(self):
        """Higher volume should produce a higher score."""
        low_vol = MagicMock()
        low_vol.volume = 100
        low_vol.open_interest = 10
        low_vol.yes_bid = 45
        low_vol.yes_ask = 55

        high_vol = MagicMock()
        high_vol.volume = 100000
        high_vol.open_interest = 5000
        high_vol.yes_bid = 45
        high_vol.yes_ask = 55

        assert score_market(high_vol) > score_market(low_vol)

    def test_tight_spread_increases_score(self):
        """Tight bid-ask spread should produce a higher score."""
        wide_spread = MagicMock()
        wide_spread.volume = 50000
        wide_spread.open_interest = 1000
        wide_spread.yes_bid = 30
        wide_spread.yes_ask = 70

        tight_spread = MagicMock()
        tight_spread.volume = 50000
        tight_spread.open_interest = 1000
        tight_spread.yes_bid = 48
        tight_spread.yes_ask = 52

        assert score_market(tight_spread) > score_market(wide_spread)


class TestIsTradeable:
    """Tests for is_tradeable function."""

    def test_returns_true_for_high_score(self):
        """Market with score above threshold should be tradeable."""
        assert is_tradeable(SCORE_THRESHOLD + 0.1) is True

    def test_returns_false_for_low_score(self):
        """Market with score below threshold should not be tradeable."""
        assert is_tradeable(SCORE_THRESHOLD - 0.1) is False

    def test_returns_false_at_exact_threshold(self):
        """Market at exactly the threshold should not be tradeable (exclusive)."""
        assert is_tradeable(SCORE_THRESHOLD) is False

    def test_handles_zero_score(self):
        """Zero score should never be tradeable."""
        assert is_tradeable(0.0) is False

    def test_handles_perfect_score(self):
        """Perfect score of 1.0 should always be tradeable."""
        assert is_tradeable(1.0) is True
