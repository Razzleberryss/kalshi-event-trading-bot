"""clients/kalshi_client.py - Async REST client for the Kalshi Trade API v2.

Handles RSA auth (PKCS#1 v1.5 + SHA-256), pagination, retries with
exponential backoff, and idempotency keys.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

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
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "AsyncKalshiClient":
        self._session = aiohttp.ClientSession()
        logger.info(
            "AsyncKalshiClient session opened (base=%s)", self._base_url
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session:
            await self._session.close()

    def _build_headers(self, method: str, path: str) -> Dict[str, str]:
        """Build RSA-signed request headers for Kalshi API v2."""
        timestamp = str(int(time.time() * 1000))
        # Message to sign: timestamp + method + path (no query string)
        msg_to_sign = f"{timestamp}{method.upper()}{path}"

        try:
            private_key = serialization.load_pem_private_key(
                self._api_secret.encode(),
                password=None,
            )
            signature = base64.b64encode(
                private_key.sign(
                    msg_to_sign.encode("utf-8"),
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
            ).decode()
        except Exception as exc:
            logger.error("Failed to sign request: %s", exc)
            raise

        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        retries: int = 3,
    ) -> Any:
        """Execute a signed HTTP request with retries and backoff."""
        if self._session is None:
            raise RuntimeError("Client session not started. Use async with.")

        url = f"{self._base_url}{path}"
        headers = self._build_headers(method, path)
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(retries):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                ) as resp:
                    body = await resp.text()
                    if resp.status == 200:
                        return await resp.json(content_type=None)
                    if resp.status == 429:
                        wait = 2 ** attempt
                        logger.warning(
                            "Rate limited (429). Waiting %ds before retry %d/%d.",
                            wait, attempt + 1, retries,
                        )
                        await asyncio.sleep(wait)
                        # Rebuild headers with fresh timestamp for retry
                        headers = self._build_headers(method, path)
                        if extra_headers:
                            headers.update(extra_headers)
                        continue
                    raise KalshiAPIError(resp.status, body)
            except KalshiAPIError:
                raise
            except Exception as exc:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)
                logger.warning("Request error on attempt %d: %s", attempt + 1, exc)

        raise KalshiAPIError(429, "Max retries exceeded due to rate limiting.")

    # ------------------------------------------------------------------
    # Market endpoints
    # ------------------------------------------------------------------

    async def get_markets(
        self,
        status: str = "open",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Fetch all open markets, handling pagination automatically."""
        markets: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"status": status, "limit": limit}
            if cursor:
                params["cursor"] = cursor

            data = await self._request("GET", "/markets", params=params)
            batch = data.get("markets", [])
            markets.extend(batch)

            cursor = data.get("cursor")
            if not cursor or not batch:
                break

            # Respect rate limit: small delay between pagination calls
            await asyncio.sleep(0.5)

        logger.debug("Fetched %d markets (status=%s).", len(markets), status)
        return markets

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Fetch a single market by ticker."""
        data = await self._request("GET", f"/markets/{ticker}")
        return data.get("market", data)

    async def get_orderbook(
        self, ticker: str, depth: int = 10
    ) -> Dict[str, Any]:
        """Fetch the order book for a market."""
        return await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )

    # ------------------------------------------------------------------
    # Portfolio endpoints
    # ------------------------------------------------------------------

    async def get_balance(self) -> Dict[str, Any]:
        """Get account balance."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        data = await self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    # ------------------------------------------------------------------
    # Order endpoints
    # ------------------------------------------------------------------

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        order_type: str = "limit",
        yes_price: Optional[int] = None,
        no_price: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place a new order.

        Args:
            ticker:          Market ticker (e.g. "INXY-23-B4.5T").
            side:            "yes" or "no".
            action:          "buy" or "sell".
            count:           Number of contracts.
            order_type:      "limit" or "market".
            yes_price:       Limit price in cents for YES contracts.
            no_price:        Limit price in cents for NO contracts.
            idempotency_key: Optional dedup key; auto-generated if None.

        Returns:
            The order dict returned by Kalshi.
        """
        key = idempotency_key or str(uuid.uuid4())
        logger.info(
            "Placing order: ticker=%s side=%s action=%s count=%d type=%s key=%s",
            ticker, side, action, count, order_type, key,
        )
        payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            payload["yes_price"] = yes_price
        if no_price is not None:
            payload["no_price"] = no_price

        return await self._request(
            "POST",
            "/portfolio/orders",
            json=payload,
            extra_headers={"Idempotency-Key": key},
        )

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an existing order."""
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        ticker: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch orders, optionally filtered by ticker and/or status."""
        params: Dict[str, Any] = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        data = await self._request("GET", "/portfolio/orders", params=params)
        return data.get("orders", [])
