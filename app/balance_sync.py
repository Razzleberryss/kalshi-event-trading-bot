from __future__ import annotations

from typing import Dict, Any


def extract_account_balance_cents(balance_payload: Dict[str, Any]) -> int:
    """Best-effort extraction of account balance in cents from Kalshi payload.

    The Kalshi balance endpoint returns a JSON object that at minimum includes
    a ``balance`` field (see tests). If additional fields are present (such as
    available buying power), they can be incorporated here without touching
    the main bot logic.
    """
    raw = balance_payload.get("balance", 0) or 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0
