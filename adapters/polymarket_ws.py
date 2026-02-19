"""
adapters.polymarket_ws – Polymarket CLOB WebSocket adapter.

Endpoint
--------
wss://ws-subscriptions-clob.polymarket.com/ws/

Authentication
--------------
None required for public order book data.  Prices are public on Polymarket.

Subscription format
-------------------
After connecting, send one JSON message per asset group:
    {"assets_ids": ["<token_id_1>", ...], "type": "Market"}

Polymarket uses token IDs (not condition IDs) for the CLOB.  A binary
market has two tokens: YES (token[0]) and NO (token[1]).  The order book
for each token is independent; we subscribe to the YES token.

Message formats
---------------
Full book snapshot (event_type = "book"):
    {
      "event_type": "book",
      "asset_id": "<token_id>",
      "market": "<condition_id>",
      "bids": [{"price": "0.45", "size": "150.00"}, ...],
      "asks": [{"price": "0.46", "size": "200.00"}, ...]
    }

Incremental price change (event_type = "price_change"):
    {
      "event_type": "price_change",
      "asset_id": "<token_id>",
      "changes": [
          {"side": "BUY",  "price": "0.45", "size": "50.00"},
          {"side": "SELL", "price": "0.46", "size": "0.00"},
          ...
      ]
    }

Note: size = "0.00" in a price_change means the level is removed.
BUY changes → bid levels; SELL changes → ask levels.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List

from .base import BaseMarketAdapter

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"


class PolymarketWSAdapter(BaseMarketAdapter):
    """Polymarket CLOB WebSocket adapter (no auth required for public data)."""

    PLATFORM = "polymarket"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Map condition_id (market ID) → YES token_id for CLOB subscriptions
        self._token_to_market: Dict[str, str] = {}   # token_id → market_id
        self._market_to_token: Dict[str, str] = {}   # market_id → token_id

    # ── Connection ─────────────────────────────────────────────────────────────

    def _ws_url(self) -> str:
        return _WS_URL

    def _build_auth_headers(self) -> Dict[str, str]:
        # No auth required for public Polymarket CLOB data
        return {}

    async def _send_subscribe(self, ws, market_ids: List[str]) -> None:
        """
        Subscribe using token IDs.  market_ids here are YES token IDs.
        Call register_token_mapping() first to map market→token if needed.
        """
        token_ids = [
            self._market_to_token.get(mid, mid)   # fallback: assume market_id IS token_id
            for mid in market_ids
        ]
        if not token_ids:
            return
        payload = {
            "assets_ids": token_ids,
            "type": "Market",
        }
        await ws.send(json.dumps(payload))
        logger.info("Polymarket WS: subscribed to %d token IDs", len(token_ids))

    # ── Token ↔ market mapping ─────────────────────────────────────────────────

    def register_token_mapping(self, market_id: str, yes_token_id: str) -> None:
        """
        Register the YES token ID for a given condition/market ID.
        Polymarket's CLOB works with token IDs, but the rest of the bot
        uses condition IDs as market_id.  Call this during setup.
        """
        self._market_to_token[market_id] = yes_token_id
        self._token_to_market[yes_token_id] = market_id

    # ── Message dispatch ───────────────────────────────────────────────────────

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Polymarket WS: non-JSON frame: %.80s", raw)
            return

        # Polymarket sends arrays or single objects
        if isinstance(data, list):
            for item in data:
                self._dispatch(item)
        elif isinstance(data, dict):
            self._dispatch(data)

    def _dispatch(self, msg: dict) -> None:
        event_type = msg.get("event_type", msg.get("type", ""))

        if event_type == "book":
            self._handle_book_snapshot(msg)
        elif event_type == "price_change":
            self._handle_price_change(msg)
        elif event_type == "tick_size_change":
            pass   # informational only
        else:
            logger.debug("Polymarket WS: unhandled event_type=%s", event_type)

    # ── Book update handlers ───────────────────────────────────────────────────

    def _handle_book_snapshot(self, msg: dict) -> None:
        token_id = msg.get("asset_id", "")
        # Resolve to the canonical market_id used throughout the bot
        market_id = self._token_to_market.get(token_id, token_id)
        book = self._get_or_create_book(market_id)

        bids = [
            (float(b["price"]), float(b["size"]))
            for b in msg.get("bids", [])
        ]
        asks = [
            (float(a["price"]), float(a["size"]))
            for a in msg.get("asks", [])
        ]
        book.apply_snapshot(bids=bids, asks=asks)
        logger.debug(
            "Polymarket snapshot: %s  bid=%.4f  ask=%.4f  spread=%.4f",
            market_id,
            book.get_best_bid() or 0,
            book.get_best_ask() or 0,
            book.get_spread() or 0,
        )

    def _handle_price_change(self, msg: dict) -> None:
        token_id = msg.get("asset_id", "")
        market_id = self._token_to_market.get(token_id, token_id)
        book = self._get_or_create_book(market_id)

        for change in msg.get("changes", []):
            side_raw = change.get("side", "").upper()  # "BUY" or "SELL"
            side = "bid" if side_raw == "BUY" else "ask"
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))
            # size = 0 means remove the level (apply_delta handles this)
            book.apply_delta(side=side, price=price, new_size=size)
