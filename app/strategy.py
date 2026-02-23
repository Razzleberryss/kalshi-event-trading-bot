"""app/strategy.py - Core trading strategy logic for the Kalshi bot.

Provides pure functions for scoring and evaluating markets, plus a
SCORE_THRESHOLD constant used by the decision loop.

Design goals
------------
* Pure functions with no side-effects -> easy to unit-test.
* Score is a float in [0.0, 1.0]; higher == stronger trade signal.
* is_tradeable(score) takes a numeric score and compares to SCORE_THRESHOLD.
* evaluate_market is the main entry-point for main.py.
* Supports both dict markets (from API) and object markets.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
SCORE_THRESHOLD: float = 0.6  # minimum score (0-1) to generate a trade signal
MIN_VOLUME: int = 1_000        # contracts traded (24 h)
MIN_OPEN_INTEREST: int = 100   # open positions
MAX_SPREAD_CENTS: int = 20     # yes_ask - yes_bid spread ceiling
UNDERPRICED_THRESHOLD: float = 0.30  # if yes_bid < 30 cents: underpriced YES
OVERPRICED_THRESHOLD: float = 0.70   # if yes_bid > 70 cents: overpriced YES


# ---------------------------------------------------------------------------
# Helper: works with both dict markets and object markets
# ---------------------------------------------------------------------------
def _get(market: Any, key: str, default: Any = None) -> Any:
    """Get attribute from a dict or object market."""
    if isinstance(market, dict):
        return market.get(key, default)
    return getattr(market, key, default)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_market(market: Any) -> float:
    """Return a float score in [0.0, 1.0] for *market*.

    The score aggregates:
    * Liquidity (volume + open interest) -> up to 40 pts
    * Spread (tighter == better)         -> up to 30 pts
    * Price edge (deviation from 50c)    -> up to 30 pts

    All points are normalised to [0.0, 1.0] before returning.

    Parameters
    ----------
    market:
        Any object or dict with volume/volume_24h, open_interest,
        yes_bid, yes_ask fields.

    Returns
    -------
    float score in [0.0, 1.0].
    """
    volume = _get(market, "volume_24h") or _get(market, "volume", 0) or 0
    open_interest = _get(market, "open_interest", 0) or 0
    yes_bid = _get(market, "yes_bid", 0) or 0
    yes_ask = _get(market, "yes_ask", 0) or 0

    # --- Liquidity score (0-40) ---
    vol_score = min(20, int(volume / 500))          # 10k volume -> 20 pts
    oi_score = min(20, int(open_interest / 50))     # 1k OI -> 20 pts
    liquidity_score = vol_score + oi_score

    # --- Spread score (0-30): tighter spread earns more points ---
    if yes_bid > 0 and yes_ask > 0:
        spread = yes_ask - yes_bid
        spread_score = max(0, 30 - int(spread * 1.5))
    else:
        spread_score = 0

    # --- Price edge score (0-30): favour markets near 50c ---
    mid = (yes_bid + yes_ask) / 2.0 if (yes_bid and yes_ask) else yes_bid
    deviation_from_fair = abs(mid - 50)
    if deviation_from_fair <= 5:
        edge_score = 30
    elif deviation_from_fair <= 15:
        edge_score = 20
    elif deviation_from_fair <= 25:
        edge_score = 10
    else:
        edge_score = 0

    raw_score = min(100, liquidity_score + spread_score + edge_score)
    return float(raw_score) / 100.0


# ---------------------------------------------------------------------------
# Tradeable gate
# ---------------------------------------------------------------------------
def is_tradeable(score: float) -> bool:
    """Return True if *score* meets or exceeds SCORE_THRESHOLD.

    Parameters
    ----------
    score:
        Numeric score in [0.0, 1.0] returned by score_market().

    Returns
    -------
    bool - True only when score >= SCORE_THRESHOLD.
    """
    return score >= SCORE_THRESHOLD


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def evaluate_market(market: Any) -> Optional[Dict[str, Any]]:
    """Evaluate *market* and return a trade signal dict or None.

    Pipeline:
    1. Basic status/volume checks.
    2. Score via score_market().
    3. If score >= SCORE_THRESHOLD, build and return a signal dict.

    Parameters
    ----------
    market:
        Market dict or object with ticker, status, volume/volume_24h,
        open_interest, yes_bid, yes_ask.

    Returns
    -------
    Dict with keys {ticker, action, side, yes_price, score} or None.
    """
    # Hard gate: market must be active (Kalshi API returns 'active', not 'open')
    status = _get(market, "status", None)
    if status not in ("active", "open"):  # accept both for safety
        return None

    volume = _get(market, "volume_24h") or _get(market, "volume", 0) or 0
    open_interest = _get(market, "open_interest", 0) or 0
    if volume <= 0 or open_interest <= 0:
        return None

    score = score_market(market)
    if not is_tradeable(score):
        return None

    yes_bid = _get(market, "yes_bid", 0) or 0
    yes_ask = _get(market, "yes_ask", 0) or 0
    ticker = _get(market, "ticker", "")

    # Determine direction: buy YES when underpriced, buy NO when overpriced
    mid = (yes_bid + yes_ask) / 2.0 if (yes_bid and yes_ask) else yes_bid
    mid_frac = mid / 100.0

    if mid_frac < UNDERPRICED_THRESHOLD:
        # YES is cheap - buy YES
        action = "buy"
        side = "yes"
        yes_price = yes_ask
    elif mid_frac > OVERPRICED_THRESHOLD:
        # YES is expensive - buy NO
        action = "buy"
        side = "no"
        yes_price = 100 - yes_bid
    else:
        # Near fair value - default to YES
        action = "buy"
        side = "yes"
        yes_price = yes_ask

    return {
        "ticker": ticker,
        "action": action,
        "side": side,
        "yes_price": yes_price,
        "score": score,
    }
