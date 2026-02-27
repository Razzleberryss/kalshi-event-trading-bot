"""tests/test_kalshi_client.py - Unit tests for AsyncKalshiClient.

All network I/O and cryptographic signing is mocked so tests run
completely offline without real Kalshi credentials.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx


# ---------------------------------------------------------------------------
# Helpers - patch load_pem_private_key so __init__ never tries real crypto
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_private_key():
    """A MagicMock standing in for an RSA private key."""
    key = MagicMock()
    key.sign.return_value = b"fake-signature-bytes"
    return key


@pytest.fixture()
def client(mock_private_key):
    """AsyncKalshiClient instance with mocked crypto + HTTP transport."""
    with patch(
        "clients.kalshi_client.serialization.load_pem_private_key",
        return_value=mock_private_key,
    ):
        from clients.kalshi_client import AsyncKalshiClient

        c = AsyncKalshiClient(api_key="test-key", api_secret="fake-pem")
        # Replace the internal httpx client with an AsyncMock
        c._client = AsyncMock()
        return c


# ---------------------------------------------------------------------------
# _sign
# ---------------------------------------------------------------------------


class TestSign:
    def test_returns_three_headers(self, client):
        headers = client._sign("GET", "/events")
        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers

    def test_access_key_matches_api_key(self, client):
        headers = client._sign("GET", "/events")
        assert headers["KALSHI-ACCESS-KEY"] == "test-key"

    def test_signature_is_base64(self, client):
        headers = client._sign("POST", "/portfolio/orders")
        sig = headers["KALSHI-ACCESS-SIGNATURE"]
        # Should not raise
        decoded = base64.b64decode(sig)
        assert len(decoded) > 0

    def test_query_string_stripped_from_signed_message(self, client, mock_private_key):
        """Kalshi requires signing the path without query parameters."""
        client._sign("GET", "/events?limit=200&with_nested_markets=true")

        signed_msg = mock_private_key.sign.call_args[0][0].decode()
        assert "?" not in signed_msg
        assert "limit=200" not in signed_msg
        assert "with_nested_markets" not in signed_msg

    def test_full_trade_api_path_used_for_signing(self, client, mock_private_key):
        """The signed string must include /trade-api/v2/events, not just /events."""
        client._sign("GET", "/events")

        signed_msg = mock_private_key.sign.call_args[0][0].decode()
        assert "/trade-api/v2/events" in signed_msg

    def test_query_string_stripped_and_full_path_used(self, client, mock_private_key):
        """Combined: strip query params AND use full /trade-api/v2 prefix."""
        client._sign("GET", "/events?limit=200&with_nested_markets=true")

        signed_msg = mock_private_key.sign.call_args[0][0].decode()
        assert "/trade-api/v2/events" in signed_msg
        assert "?" not in signed_msg

    def test_timestamp_is_milliseconds(self, client, mock_private_key):
        """KALSHI-ACCESS-TIMESTAMP must be in milliseconds (13-digit integer)."""
        import time
        before_ms = int(time.time() * 1000)
        headers = client._sign("GET", "/events")
        after_ms = int(time.time() * 1000)

        ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
        assert before_ms <= ts <= after_ms


# ---------------------------------------------------------------------------
# get_markets
# ---------------------------------------------------------------------------


class TestGetMarkets:
    @pytest.mark.asyncio
    async def test_returns_flat_market_list(self, client):
        """Events with nested markets are flattened into a single list."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "events": [
                {
                    "category": "Politics",
                    "markets": [
                        {"ticker": "MKT-A", "status": "active"},
                        {"ticker": "MKT-B", "status": "active"},
                    ],
                },
                {
                    "category": "Economics",
                    "markets": [
                        {"ticker": "MKT-C", "status": "active"},
                    ],
                },
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        markets = await client.get_markets()
        assert len(markets) == 3

    @pytest.mark.asyncio
    async def test_category_propagated_from_event(self, client):
        """Each market should inherit 'category' from its parent event."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "events": [
                {
                    "category": "Sports",
                    "markets": [{"ticker": "SPORT-1", "status": "active"}],
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        markets = await client.get_markets()
        assert markets[0]["category"] == "Sports"

    @pytest.mark.asyncio
    async def test_empty_events_returns_empty_list(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"events": []}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        markets = await client.get_markets()
        assert markets == []

    @pytest.mark.asyncio
    async def test_http_error_propagates(self, client):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
        client._client.get = AsyncMock(return_value=mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_markets()


# ---------------------------------------------------------------------------
# place_order
# ---------------------------------------------------------------------------


class TestPlaceOrder:
    @pytest.mark.asyncio
    async def test_returns_order_dict(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "order": {"id": "order-xyz", "status": "resting"}
        }
        mock_response.raise_for_status = MagicMock()
        client._client.post = AsyncMock(return_value=mock_response)

        result = await client.place_order(
            ticker="KXBTCD-25FEB",
            side="yes",
            action="buy",
            count=1,
            yes_price=45,
        )
        assert result["order"]["id"] == "order-xyz"

    @pytest.mark.asyncio
    async def test_auto_generates_client_order_id(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"order": {"id": "o1"}}
        mock_response.raise_for_status = MagicMock()

        captured_body = {}

        async def capture_post(path, json=None, headers=None):
            captured_body.update(json or {})
            return mock_response

        client._client.post = capture_post

        await client.place_order(
            ticker="MKT-X", side="no", action="buy", count=2, yes_price=60
        )
        assert "client_order_id" in captured_body
        assert len(captured_body["client_order_id"]) > 0


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_calls_delete_with_order_id(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "cancelled"}
        mock_response.raise_for_status = MagicMock()
        client._client.delete = AsyncMock(return_value=mock_response)

        result = await client.cancel_order("order-abc")
        assert result["status"] == "cancelled"
        client._client.delete.assert_called_once()
        call_args = client._client.delete.call_args
        assert "order-abc" in call_args[0][0]


# ---------------------------------------------------------------------------
# get_positions
# ---------------------------------------------------------------------------


class TestGetPositions:
    @pytest.mark.asyncio
    async def test_returns_market_positions_list(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "market_positions": [
                {"ticker": "MKT-A", "position": 3},
                {"ticker": "MKT-B", "position": -1},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        positions = await client.get_positions()
        assert len(positions) == 2
        assert positions[0]["ticker"] == "MKT-A"

    @pytest.mark.asyncio
    async def test_empty_positions(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"market_positions": []}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        positions = await client.get_positions()
        assert positions == []


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_returns_balance_dict(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"balance": 50_000, "pnl": 1_200}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        bal = await client.get_balance()
        assert bal["balance"] == 50_000


# ---------------------------------------------------------------------------
# get_orders
# ---------------------------------------------------------------------------


class TestGetOrders:
    @pytest.mark.asyncio
    async def test_returns_orders_list(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "orders": [
                {"id": "o1", "status": "resting"},
                {"id": "o2", "status": "filled"},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        orders = await client.get_orders()
        assert len(orders) == 2

    @pytest.mark.asyncio
    async def test_empty_orders_list(self, client):
        mock_response = MagicMock()
        mock_response.json.return_value = {"orders": []}
        mock_response.raise_for_status = MagicMock()
        client._client.get = AsyncMock(return_value=mock_response)

        orders = await client.get_orders(ticker="MISSING")
        assert orders == []


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_open_is_noop(self, client):
        """open() should complete without raising."""
        await client.open()  # no-op, should not raise

    @pytest.mark.asyncio
    async def test_close_calls_aclose(self, client):
        client._client.aclose = AsyncMock()
        await client.close()
        client._client.aclose.assert_called_once()
