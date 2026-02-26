from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from config import config

logger = logging.getLogger(__name__)


class AlertingClient:
    """Minimal Slack webhook alerting."""

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self._webhook_url = webhook_url or config.slack_webhook_url
        self._client = httpx.AsyncClient(timeout=5.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, text: str, *, extra: Optional[Dict[str, Any]] = None) -> None:
        if not self._webhook_url:
            return
        payload: Dict[str, Any] = {"text": text}
        if extra:
            payload["attachments"] = [{"text": json.dumps(extra, default=str)}]
        try:
            resp = await self._client.post(self._webhook_url, json=payload)
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send Slack alert: %s", exc)

