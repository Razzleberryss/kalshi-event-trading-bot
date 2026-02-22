"""clients/kalshi_client.py - Async REST client for the Kalshi Trade API v2.

Handles auth, pagination, retries with exponential backoff, and idempotency.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional

import aiohttp

from config import config

logger = logging.getLogger(__name__)


class KalshiAPIError(Exception):
    """Raised when Kalshi API returns a non-2xx response."""
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"Kalshi API error {status}: {body}")


class AsyncKalshiClient:
    """Async HTTP client for Kalshi Trade API v2.

    Usage:
        async with AsyncKalshiClient() as client:
            markets = await client.get_markets()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        self._api_key = api_key or config.kalshi_api_key
        self._api_secret = api_secret or config.kalshi_api_secret
        self._base_url = (base_url or config.kalshi_base_url).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=config.kalshi_timeout_seconds)
        self._session: Optional[aiohttp.ClientSession] = None
        self._consecutive_failures = 0

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "AsyncKalshiClient":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def open(self) -> None:
        """Create the underlying aiohttp session."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        self._session = aiohttp.ClientSession(
            headers=headers,
            timeout=self._timeout,
        )
        logger.info("AsyncKalshiClient session opened (base=%s)", self._base_url)

    async def close(self) -> None:
        """Close the underlying aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.info("AsyncKalshiClient session closed.")

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute an HTTP request with exponential back-off retry."""
        if self._session is None:
            raise RuntimeError("Session not open. Use 'async with AsyncKalshiClient()'.")

        url = f"{self._base_url}{path}"
        for attempt in range(1, config.kalshi_max_retries + 1):
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=extra_headers or {},
                ) as resp:
                    body = await resp.text()
                    if resp.status >= 400:
                        raise KalshiAPIError(resp.status, body)
                    self._consecutive_failures = 0
                    return await resp.json(content_type=None)
            except KalshiAPIError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._consecutive_failures += 1
                wait = 2 ** attempt
                logger.warning(
                    "Request failed (attempt %d/%d) %s %s: %s. Retrying in %ds.",
                    attempt,
                    config.kalshi_max_retries,
                    method,
                    url,
                    exc,
                    wait,
                )
                if attempt == config.kalshi_max_retries:
                    raise
                await asyncio.sleep(wait)
        return None  # unreachable

    # ------------------------------------------------------------------
    # Market endpoints
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all open markets, handling pagination automatically."""
        markets: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if cursor:
            params["cursor"] = cursor

        while True:
            data = await self._request("GET", "/markets", params=params)
            batch = data.get("markets", [])
            markets.extend(batch)
            next_cursor = data.get("cursor")
            if not next_cursor or len(batch) < limit:
                break
            params["cursor"] = next_cursor

        logger.debug("Fetched %d markets (status=%s).", len(markets), status)
        return markets

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Fetch a single market by ticker."""
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_market_orderbook(
        self, ticker: str, depth: int = 10
    ) -> Dict[str, Any]:
        """Fetch the order book for a market."""
        return await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )

    # ------------------------------------------------------------------
    # Order endpoints
    # ------------------------------------------------------------------

    async def place_order(
        self,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,  # cents 1–99
        no_price: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place an order on Kalshi.

        An idempotency key is generated automatically if not provided,
        preventing duplicate orders on retried requests.
        """
        key = idempotency_key or str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            payload["yes_price"] = yes_price
        if no_price is not None:
            payload["no_price"] = no_price

        logger.info(
            "Placing order: ticker=%s side=%s action=%s count=%d type=%s key=%s",
            ticker, side, action, count, order_type, key,
        )
        return await self._request(
            "POST",
            "/portfolio/orders",
            json=payload,
            extra_headers={"Idempotency-Key": key},
        )

    async def get_portfolio_balance(self) -> Dict[str, Any]:
        """Fetch the current portfolio cash balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Fetch all current positions."""
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])
