"""
adapters.kalshi_ws – Kalshi WebSocket adapter.

Endpoint
--------
prod : wss://trading-api.kalshi.com/trade-api/ws/v2
demo : wss://demo-api.kalshi.co/trade-api/ws/v2

Authentication
--------------
Same HMAC-SHA256 signature scheme as the REST API; passed as HTTP upgrade
headers (KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE).

Order book convention
---------------------
Kalshi returns two sides in every snapshot / delta:
  "yes"  – YES bid levels.  Price p (cents) = willingness to buy YES at p¢.
  "no"   – NO  bid levels.  Price p (cents) = willingness to buy NO  at p¢.
              A NO bid at p¢ is equivalent to a YES ask at (100-p)¢.

We translate to a standard bid/ask book in the LiveOrderBook.

Message formats
---------------
Subscribe:
    {"id": N, "cmd": "subscribe",
     "params": {"channels": ["orderbook_delta"], "market_tickers": [...]}}

Snapshot response:
    {"type": "orderbook_snapshot",
     "msg": {"market_ticker": "...",
             "yes": [[price_cents, size], ...],
             "no":  [[price_cents, size], ...]}}

Delta response:
    {"type": "orderbook_delta",
     "msg": {"market_ticker": "...", "price": cents, "delta": qty, "side": "yes"|"no"}}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, List

from .base import BaseMarketAdapter

logger = logging.getLogger(__name__)


class KalshiWSAdapter(BaseMarketAdapter):
    """Kalshi WebSocket adapter with HMAC auth and live order book."""

    PLATFORM = "kalshi"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        env: str = "prod",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key
        self._api_secret = api_secret
        self._env = env
        self._seq = 0  # monotonically increasing command ID

    # ── Connection ─────────────────────────────────────────────────────────────

    def _ws_url(self) -> str:
        if self._env == "prod":
            return "wss://trading-api.kalshi.com/trade-api/ws/v2"
        return "wss://demo-api.kalshi.co/trade-api/ws/v2"

    def _build_auth_headers(self) -> Dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        path = "/trade-api/ws/v2"
        message = ts_ms + "GET" + path
        sig = hmac.new(
            self._api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        }

    async def _send_subscribe(self, ws, market_ids: List[str]) -> None:
        if not market_ids:
            logger.warning("KalshiWSAdapter: no markets to subscribe to")
            return
        self._seq += 1
        payload = {
            "id": self._seq,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": market_ids,
            },
        }
        await ws.send(json.dumps(payload))
        logger.info("Kalshi WS: subscribed to %d markets", len(market_ids))

    # ── Message dispatch ───────────────────────────────────────────────────────

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("Kalshi WS: non-JSON frame ignored: %.80s", raw)
            return

        msg_type = data.get("type", "")
        msg = data.get("msg", {})

        if msg_type == "orderbook_snapshot":
            self._handle_snapshot(msg)
        elif msg_type == "orderbook_delta":
            self._handle_delta(msg)
        elif msg_type == "subscribed":
            logger.debug("Kalshi WS: subscription confirmed: %s", data)
        elif msg_type == "error":
            logger.error("Kalshi WS error message: %s", data)
        # heartbeat / ack / other types are silently ignored

    # ── Book update handlers ───────────────────────────────────────────────────

    def _handle_snapshot(self, msg: dict) -> None:
        ticker = msg.get("market_ticker", "")
        if not ticker:
            return

        book = self._get_or_create_book(ticker)

        yes_levels = msg.get("yes", [])   # [[price_cents, size], ...]
        no_levels = msg.get("no", [])     # [[price_cents, size], ...]

        # YES bid levels: price = cents to buy YES
        bids = [(float(p) / 100.0, float(s)) for p, s in yes_levels]
        # NO bid at p cents → YES ask at (100-p) cents
        asks = [((100.0 - float(p)) / 100.0, float(s)) for p, s in no_levels]

        book.apply_snapshot(bids=bids, asks=asks)
        logger.debug(
            "Kalshi snapshot: %s  bid=%.4f  ask=%.4f  spread=%.4f",
            ticker,
            book.get_best_bid() or 0,
            book.get_best_ask() or 0,
            book.get_spread() or 0,
        )

    def _handle_delta(self, msg: dict) -> None:
        ticker = msg.get("market_ticker", "")
        if not ticker:
            return

        book = self._get_or_create_book(ticker)
        side = msg.get("side", "yes")           # "yes" or "no"
        price_cents = float(msg.get("price", 0))
        delta = float(msg.get("delta", 0))       # change in quantity (can be negative)

        if side == "yes":
            # YES bid level change
            book.apply_delta_increment("bid", price_cents / 100.0, delta)
        else:
            # NO bid level change → YES ask level change (inverted price)
            yes_ask_price = (100.0 - price_cents) / 100.0
            book.apply_delta_increment("ask", yes_ask_price, delta)
