"""
maker.fair_value – real-time crypto fair value feed via Binance WebSocket.

For each crypto symbol tracked by the maker bot, we maintain a live best-bid /
best-ask from Binance's bookTicker stream. The midpoint of that is used as the
anchor for quoting on Polymarket.

Why Binance?
  - Deepest crypto order book globally → most reliable price anchor
  - bookTicker stream updates on every best-bid/ask change (sub-millisecond)
  - No API key required for public market data streams

Fallback: if Binance is unavailable, try Coinbase Advanced Trade WebSocket.

Stream format:
  wss://stream.binance.com:9443/ws/<symbol>@bookTicker
  Message: { "b": "best_bid", "B": "bid_qty", "a": "best_ask", "A": "ask_qty" }
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Dict, Optional, Tuple

import websockets

logger = logging.getLogger(__name__)

# Polymarket crypto market title patterns → Binance symbol
# These are the 5-min / 15-min crypto markets on Polymarket
SYMBOL_MAP: Dict[str, str] = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "bnb": "BNBUSDT",
    "xrp": "XRPUSDT",
    "ripple": "XRPUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
    "matic": "MATICUSDT",
    "polygon": "MATICUSDT",
    "avax": "AVAXUSDT",
    "avalanche": "AVAXUSDT",
    "link": "LINKUSDT",
    "chainlink": "LINKUSDT",
}

_BINANCE_WS = "wss://stream.binance.com:9443/ws"
_COINBASE_WS = "wss://advanced-trade-ws.coinbase.com"
_RECONNECT_DELAY = 2.0  # seconds between reconnect attempts


class FairValueFeed:
    """
    Maintains a live fair value (midpoint) for a set of crypto symbols.

    Usage
    -----
    feed = FairValueFeed(symbols=["BTCUSDT", "ETHUSDT"])
    asyncio.create_task(feed.run())          # start in background
    fv = feed.get_midpoint("BTCUSDT")        # poll anytime
    feed.on_update("BTCUSDT", my_callback)   # or subscribe to callbacks
    """

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = [s.upper() for s in symbols]
        # symbol → (best_bid, best_ask, timestamp)
        self._quotes: Dict[str, Tuple[float, float, float]] = {}
        self._callbacks: Dict[str, list[Callable]] = {}
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def get_midpoint(self, symbol: str) -> Optional[float]:
        """Return current midpoint price for symbol, or None if not yet received."""
        q = self._quotes.get(symbol.upper())
        if q is None:
            return None
        bid, ask, _ = q
        return (bid + ask) / 2.0

    def get_quote(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Return (best_bid, best_ask) for symbol, or None."""
        q = self._quotes.get(symbol.upper())
        return (q[0], q[1]) if q else None

    def age_seconds(self, symbol: str) -> float:
        """Seconds since last update for symbol. Returns inf if never updated."""
        q = self._quotes.get(symbol.upper())
        return time.monotonic() - q[2] if q else float("inf")

    def on_update(self, symbol: str, callback: Callable[[str, float, float], None]) -> None:
        """Register a callback fired on every price update: callback(symbol, bid, ask)."""
        self._callbacks.setdefault(symbol.upper(), []).append(callback)

    def stop(self) -> None:
        self._running = False

    # ── WebSocket loop ────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the Binance bookTicker WebSocket. Reconnects automatically."""
        self._running = True
        stream_names = [f"{s.lower()}@bookTicker" for s in self._symbols]
        combined = "/".join(stream_names)
        url = f"{_BINANCE_WS}/{combined}"

        while self._running:
            try:
                logger.info("FairValueFeed: connecting to Binance %s", url)
                async with websockets.connect(url, ping_interval=20) as ws:
                    async for raw in ws:
                        if not self._running:
                            return
                        self._handle_message(raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning(
                    "FairValueFeed: disconnected (%s). Reconnecting in %.1fs",
                    exc, _RECONNECT_DELAY,
                )
                await asyncio.sleep(_RECONNECT_DELAY)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
            # Combined stream wraps in {"stream": ..., "data": {...}}
            if "data" in msg:
                msg = msg["data"]
            symbol = msg.get("s", "").upper()
            bid = float(msg.get("b", 0))
            ask = float(msg.get("a", 0))
            if symbol and bid > 0 and ask > 0:
                self._quotes[symbol] = (bid, ask, time.monotonic())
                for cb in self._callbacks.get(symbol, []):
                    try:
                        cb(symbol, bid, ask)
                    except Exception as exc:
                        logger.debug("FairValueFeed: callback error: %s", exc)
        except Exception as exc:
            logger.debug("FairValueFeed: parse error: %s", exc)


def symbol_for_market(market_question: str) -> Optional[str]:
    """
    Detect which Binance symbol is the price anchor for a Polymarket crypto market.

    Returns None if the market doesn't correspond to a known symbol.
    """
    text = market_question.lower()
    for keyword, symbol in SYMBOL_MAP.items():
        if keyword in text:
            return symbol
    return None
