"""app/strategy.py - Core trading strategy logic for the Kalshi bot.

Provides pure functions for scoring and evaluating markets, plus a
SCORE_THRESHOLD constant used by the decision loop.

Design goals
------------
* Pure functions with no side-effects -> easy to unit-test.
* Score is an integer 0-100; higher == stronger trade signal.
* is_tradeable / evaluate_market are the entry-points for main.py.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Configuration constants (override via env or config.py if desired)
# ---------------------------------------------------------------------------
SCORE_THRESHOLD: int = 60          # minimum score to generate a trade signal
MIN_VOLUME: int = 1_000            # contracts traded (24 h)
MIN_OPEN_INTEREST: int = 100       # open positions
MAX_SPREAD_CENTS: int = 20         # yes_ask - yes_bid spread ceiling
UNDERPRICED_THRESHOLD: float = 0.30  # if yes_bid < 30 cents consider underpriced YES
OVERPRICED_THRESHOLD: float = 0.70   # if yes_bid > 70 cents consider overpriced YES


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_market(market: Any) -> int:
    """Return an integer score 0-100 for *market*.

    The score aggregates:
    * Liquidity  (volume + open interest) -> up to 40 pts
    * Spread     (tighter == better)       -> up to 30 pts
    * Price edge (deviation from 50 c)     -> up to 30 pts

    Parameters
    ----------
    market:
        Any object (or MagicMock in tests) exposing:
        .volume, .open_interest, .yes_bid, .yes_ask as numbers.

    Returns
    -------
    Integer score in [0, 100].
    """
    volume = getattr(market, "volume", 0) or 0
    open_interest = getattr(market, "open_interest", 0) or 0
    yes_bid = getattr(market, "yes_bid", 0) or 0
    yes_ask = getattr(market, "yes_ask", 0) or 0

    # --- Liquidity score (0-40) ---
    vol_score = min(20, int(volume / 500))          # 10 k volume -> 20 pts
    oi_score = min(20, int(open_interest / 50))     # 1 k OI      -> 20 pts
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

    return min(100, liquidity_score + spread_score + edge_score)


def is_tradeable(market: Any) -> bool:
    """Return True if *market* clears all hard liquidity/spread filters.

    These are binary knock-out checks; markets that fail are never
    evaluated regardless of their score.
    """
    if getattr(market, "status", None) != "open":
        return False

    volume = getattr(market, "volume", 0) or 0
    open_interest = getattr(market, "open_interest", 0) or 0
    yes_bid = getattr(market, "yes_bid", 0) or 0
    yes_ask = getattr(market, "yes_ask", 0) or 0

    if volume < MIN_VOLUME:
        return False
    if open_interest < MIN_OPEN_INTEREST:
        return False
    if yes_bid > 0 and yes_ask > 0 and (yes_ask - yes_bid) > MAX_SPREAD_CENTS:
        return False

    return True


def evaluate_market(market: Any) -> Optional[Dict[str, Any]]:
    """Evaluate *market* and return a trade signal dict or None.

    The full pipeline:
    1. Hard filter via is_tradeable()
    2. Score via score_market()
    3. If score >= SCORE_THRESHOLD, build and return a signal dict.

    Parameters
    ----------
    market:
        Market object with .ticker, .status, .volume, .open_interest,
        .yes_bid, .yes_ask attributes.

    Returns
    -------
    Dict with keys {ticker, action, yes_price, score} if tradeable;
    None otherwise.
    """
    if not is_tradeable(market):
        return None

    score = score_market(market)
    if score < SCORE_THRESHOLD:
        return None

    yes_bid = getattr(market, "yes_bid", 0) or 0
    yes_ask = getattr(market, "yes_ask", 0) or 0
    ticker = getattr(market, "ticker", "")

    # Determine action: buy YES when underpriced, buy NO when overpriced
    mid = (yes_bid + yes_ask) / 2.0 if (yes_bid and yes_ask) else yes_bid
    if mid / 100.0 < UNDERPRICED_THRESHOLD:
        action = "buy"
        yes_price = yes_ask  # pay the ask for YES
    elif mid / 100.0 > OVERPRICED_THRESHOLD:
        action = "buy_no"
        yes_price = 100 - yes_bid  # NO price implied
    else:
        action = "buy"
        yes_price = yes_ask

    return {
        "ticker": ticker,
        "action": action,
        "yes_price": yes_price,
        "score": score,
    }
