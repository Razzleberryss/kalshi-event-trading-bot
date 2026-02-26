"""clients/kalshi_client.py - Async Kalshi REST API client."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import config

logger = logging.getLogger(__name__)


class AsyncKalshiClient:
    """Async HTTP client for the Kalshi v2 REST API with rate limiting."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        raw_secret = api_secret or os.getenv("KALSHI_API_SECRET", "")
        self._private_key = None
        if raw_secret:
            self._private_key = serialization.load_pem_private_key(
                raw_secret.encode(), password=None
            )
        self._client = httpx.AsyncClient(
            base_url=config.kalshi_base_url, timeout=float(config.kalshi_timeout_seconds)
        )
        # Rate limiting: max 20 concurrent requests
        self._rate_limiter = asyncio.Semaphore(20)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        """No-op lifecycle hook — client is ready after __init__."""
        logger.debug("AsyncKalshiClient.open() called (no-op).")

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()
        logger.debug("AsyncKalshiClient closed.")

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        if not self.api_key or self._private_key is None:
            raise ValueError(
                "Kalshi API credentials are not configured (missing key or secret)."
            )
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        status: str = "active",
        limit: int = 200,
        category: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Any]:
        """Fetch markets via /events with nested markets for real volume data.

        Supports pagination to fetch all markets even if count exceeds API limit.
        """
        path = "/events"
        all_markets: List[Any] = []
        cursor: Optional[str] = None

        # Fetch all pages until we have all markets
        while True:
            params: Dict[str, Any] = {
                "limit": min(limit, 200),
                "with_nested_markets": "true",
            }
            if category:
                params["category"] = category
            if cursor:
                params["cursor"] = cursor

            # Rate-limited API call
            async with self._rate_limiter:
                headers = self._sign("GET", path)
                resp = await self._client.get(path, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            events = data.get("events", [])

            # Flatten nested markets and attach category from parent event
            for event in events:
                cat = event.get("category", "")
                for market in event.get("markets", []):
                    market["category"] = cat
                    all_markets.append(market)

            # Check if there are more pages
            cursor = data.get("cursor")
            if not cursor or len(events) < params["limit"]:
                # No more pages or last page had fewer items than limit
                break

            # Safety check: limit total markets to prevent infinite loops
            if len(all_markets) >= limit * 10:  # max 10 pages
                logger.warning("Pagination safety limit reached, stopping at %d markets", len(all_markets))
                break

        logger.info(
            "Fetched %d markets from Kalshi via pagination.",
            len(all_markets),
        )
        return all_markets

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch current open positions for the account."""
        path = "/portfolio/positions"
        async with self._rate_limiter:
            headers = self._sign("GET", path)
            resp = await self._client.get(path, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return data.get("market_positions", [])

    async def get_balance(self) -> Dict[str, Any]:
        """Fetch current account balance."""
        path = "/portfolio/balance"
        async with self._rate_limiter:
            headers = self._sign("GET", path)
            resp = await self._client.get(path, headers=headers)
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        yes_price: int,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place an order on the Kalshi exchange.

        Parameters
        ----------
        ticker : str
            Market ticker (e.g. 'KXBTCD-25FEB2314-T65000').
        side : str
            'yes' or 'no'.
        action : str
            'buy' or 'sell'.
        count : int
            Number of contracts.
        yes_price : int
            Limit price in cents on the yes side (1-99).
        order_type : str
            'limit' (default) or 'market'.
        client_order_id : str, optional
            Idempotency key; auto-generated if not provided.

        Returns
        -------
        dict containing the Kalshi order response.
        """
        path = "/portfolio/orders"
        body: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "yes_price": yes_price,
            "client_order_id": client_order_id or str(uuid.uuid4()),
        }
        async with self._rate_limiter:
            headers = self._sign("POST", path)
            headers["Content-Type"] = "application/json"
            resp = await self._client.post(path, json=body, headers=headers)
            resp.raise_for_status()
            data: Dict[str, Any] = resp.json()
        logger.info(
            "Order placed: %s %s %s x%d @ %d -> order_id=%s",
            action,
            side,
            ticker,
            count,
            yes_price,
            data.get("order", {}).get("id", "?"),
        )
        return data

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an open order by order_id."""
        path = f"/portfolio/orders/{order_id}"
        async with self._rate_limiter:
            headers = self._sign("DELETE", path)
            resp = await self._client.delete(path, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Fetch the account's orders, optionally filtered."""
        path = "/portfolio/orders"
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        async with self._rate_limiter:
            headers = self._sign("GET", path)
            resp = await self._client.get(path, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json().get("orders", [])
