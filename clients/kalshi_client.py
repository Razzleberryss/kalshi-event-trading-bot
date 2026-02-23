"""clients/kalshi_client.py - Async Kalshi REST API client."""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class AsyncKalshiClient:
    """Async HTTP client for the Kalshi v2 REST API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        raw_secret = api_secret or os.getenv("KALSHI_API_SECRET", "")
        self._private_key = serialization.load_pem_private_key(
            raw_secret.encode(), password=None
        )
        self._client = httpx.AsyncClient(base_url=KALSHI_API_BASE, timeout=10.0)

    def _sign(self, method: str, path: str) -> Dict[str, str]:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode()
        sig = self._private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        }

    async def get_markets(
        self,
        status: str = "active",
        limit: int = 200,
        category: Optional[str] = None,
        **kwargs: Any,
    ) -> List[Any]:
        path = "/markets"
        params: Dict[str, Any] = {"limit": limit}
        if category:
            params["category"] = category
        headers = self._sign("GET", path)
        resp = await self._client.get(path, params=params, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        markets = data.get("markets", [])
        logger.info("Fetched %d markets from Kalshi.", len(markets))
        return markets

    async def close(self) -> None:
        await self._client.aclose()
