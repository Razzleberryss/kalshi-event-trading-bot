"""app/market_scanner.py - MarketScanner: filters and ranks Kalshi markets.

Fetches open markets from the Kalshi client, applies liquidity and
spread filters, and returns a ranked list of tradeable candidates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_LIMIT: int = 200
MIN_VOLUME: int = 1_000          # minimum 24-h traded contracts
MIN_OPEN_INTEREST: int = 100     # minimum open positions
MAX_SPREAD_CENTS: int = 20       # yes_ask - yes_bid must be <= this


class MarketScanner:
    """Scans Kalshi for liquid, well-priced markets worth modelling.

    Parameters
    ----------
    client:
        An instance of AsyncKalshiClient (duck-typed to avoid circular import).
    min_volume:
        Minimum 24-h volume in contracts for a market to qualify.
    min_open_interest:
        Minimum open interest in contracts.
    max_spread_cents:
        Maximum bid-ask spread in cents (0-100 scale).
    limit:
        Maximum number of markets to fetch from the API per call.
    """

    def __init__(
        self,
        client: Any,
        *,
        min_volume: int = MIN_VOLUME,
        min_open_interest: int = MIN_OPEN_INTEREST,
        max_spread_cents: int = MAX_SPREAD_CENTS,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.client = client
        self.min_volume = min_volume
        self.min_open_interest = min_open_interest
        self.max_spread_cents = max_spread_cents
        self.limit = limit

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def scan(
        self,
        *,
        status: str = "open",
        category: Optional[str] = None,
    ) -> List[Any]:
        """Return a filtered + ranked list of market objects.

        Parameters
        ----------
        status:
            Kalshi market status filter (default: 'open').
        category:
            Optional category slug to narrow the search (e.g. 'Sports').

        Returns
        -------
        List of market objects that pass all filters, sorted by descending
        open interest (highest liquidity first).
        """
        params: Dict[str, Any] = {"status": status, "limit": self.limit}
        if category:
            params["category"] = category

        try:
            markets: List[Any] = await self.client.get_markets(**params)
        except Exception as exc:  # noqa: BLE001
            logger.error("MarketScanner.scan - API error: %s", exc)
            return []

        candidates = [m for m in markets if self._passes_filters(m)]
        candidates.sort(
            key=lambda m: getattr(m, "open_interest", 0) or 0,
            reverse=True,
        )
        logger.debug(
            "MarketScanner: %d/%d markets passed filters.",
            len(candidates),
            len(markets),
        )
        return candidates

    # -----------------------------------------------------------------------
    # Filter helpers
    # -----------------------------------------------------------------------

    def _passes_filters(self, market: Any) -> bool:
        """Return True if *market* satisfies all quality thresholds."""
        # Must be open
        if getattr(market, "status", None) != "open":
            return False

        volume = getattr(market, "volume", 0) or 0
        open_interest = getattr(market, "open_interest", 0) or 0
        yes_bid = getattr(market, "yes_bid", 0) or 0
        yes_ask = getattr(market, "yes_ask", 0) or 0

        # Liquidity filters
        if volume < self.min_volume:
            return False
        if open_interest < self.min_open_interest:
            return False

        # Spread filter (only when both sides are quoted)
        if yes_bid > 0 and yes_ask > 0:
            spread = yes_ask - yes_bid
            if spread > self.max_spread_cents:
                return False

        return True

    # -----------------------------------------------------------------------
    # Convenience
    # -----------------------------------------------------------------------

    def to_snapshot(self, market: Any) -> Dict[str, Any]:
        """Convert a Kalshi market object to the canonical snapshot dict."""
        return {
            "ticker": getattr(market, "ticker", ""),
            "yes_bid": getattr(market, "yes_bid", 0),
            "yes_ask": getattr(market, "yes_ask", 0),
            "last_price": getattr(market, "last_price", 0),
            "volume": getattr(market, "volume", 0),
            "open_interest": getattr(market, "open_interest", 0),
            "status": getattr(market, "status", ""),
            "category": getattr(market, "category", ""),
            "close_time": getattr(market, "close_time", None),
        }
