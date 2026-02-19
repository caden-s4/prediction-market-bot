"""
adapters.base – abstract WebSocket adapter and live in-memory order book.

The LiveOrderBook is updated in-place as snapshot / delta messages arrive
from an exchange.  It is thread-safe so the async WebSocket loop can write
while the signal engine reads from a different thread or coroutine.

Public interface per adapter instance:
    subscribe(market_ids)             register IDs before connecting
    run()                             async loop – run inside asyncio.Task
    stop()                            graceful shutdown
    get_book(market_id)    -> Optional[LiveOrderBook]
    get_best_bid(market_id) -> Optional[float]
    get_best_ask(market_id) -> Optional[float]
    get_spread(market_id)   -> Optional[float]
    get_mid(market_id)      -> Optional[float]
    add_global_callback(fn)           fires on every book update

LiveOrderBook public interface:
    apply_snapshot(bids, asks)
    apply_delta(side, price, new_size)
    apply_delta_increment(side, price, delta)
    get_best_bid()  get_best_ask()  get_spread()  get_mid()
    get_imbalance(depth_pct)   bid_vol / total_vol near the touch
    snapshot()                 serialisable dict (for recorder)
"""

from __future__ import annotations

import asyncio
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Callback type: receives the updated LiveOrderBook after every change
BookUpdateCallback = Callable[["LiveOrderBook"], None]


# ── Live Order Book ────────────────────────────────────────────────────────────

class LiveOrderBook:
    """
    Thread-safe in-memory order book maintained via WebSocket snapshots
    and incremental delta updates.

    Prices are stored as Python floats in [0, 1].
    Sizes are in the platform's native unit (USD notional on both exchanges).
    """

    def __init__(self, market_id: str, platform: str) -> None:
        self.market_id = market_id
        self.platform = platform
        self.last_updated: Optional[datetime] = None
        self.is_synced: bool = False   # True after the first snapshot arrives

        self._bids: Dict[float, float] = {}   # price → size
        self._asks: Dict[float, float] = {}   # price → size
        self._lock = threading.RLock()
        self._callbacks: List[BookUpdateCallback] = []

    # ── Snapshot / delta application ──────────────────────────────────────────

    def apply_snapshot(
        self,
        bids: List[tuple],   # [(price_float, size_float), ...]
        asks: List[tuple],
    ) -> None:
        """Replace the full book with a snapshot.  Triggers callbacks."""
        with self._lock:
            self._bids = {float(p): float(s) for p, s in bids if float(s) > 0}
            self._asks = {float(p): float(s) for p, s in asks if float(s) > 0}
            self.last_updated = datetime.now(timezone.utc)
            self.is_synced = True
        self._fire_callbacks()

    def apply_delta(self, side: str, price: float, new_size: float) -> None:
        """
        Set a price level to an absolute new size.
        side: "bid" or "ask".  new_size=0 removes the level.
        """
        with self._lock:
            book = self._bids if side == "bid" else self._asks
            if new_size <= 0:
                book.pop(price, None)
            else:
                book[price] = new_size
            self.last_updated = datetime.now(timezone.utc)
        self._fire_callbacks()

    def apply_delta_increment(self, side: str, price: float, delta: float) -> None:
        """
        Increment (or decrement) an existing price level.
        side: "bid" or "ask".  Negative delta reduces; reaching ≤ 0 removes.
        """
        with self._lock:
            book = self._bids if side == "bid" else self._asks
            new_size = book.get(price, 0.0) + delta
            if new_size <= 0:
                book.pop(price, None)
            else:
                book[price] = new_size
            self.last_updated = datetime.now(timezone.utc)
        self._fire_callbacks()

    # ── Book queries ──────────────────────────────────────────────────────────

    def get_best_bid(self) -> Optional[float]:
        with self._lock:
            return max(self._bids.keys()) if self._bids else None

    def get_best_ask(self) -> Optional[float]:
        with self._lock:
            return min(self._asks.keys()) if self._asks else None

    def get_spread(self) -> Optional[float]:
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is not None and ask is not None:
            return ask - bid
        return None

    def get_mid(self) -> Optional[float]:
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        if bid is not None and ask is not None:
            return (bid + ask) / 2.0
        return ask if ask is not None else bid

    def get_bid_depth(self, depth_pct: float = 0.05) -> float:
        """Total bid-side size within `depth_pct` (fractional) of the best bid."""
        best = self.get_best_bid()
        if best is None:
            return 0.0
        cutoff = best * (1.0 - depth_pct)
        with self._lock:
            return sum(s for p, s in self._bids.items() if p >= cutoff)

    def get_ask_depth(self, depth_pct: float = 0.05) -> float:
        """Total ask-side size within `depth_pct` (fractional) of the best ask."""
        best = self.get_best_ask()
        if best is None:
            return 0.0
        cutoff = best * (1.0 + depth_pct)
        with self._lock:
            return sum(s for p, s in self._asks.items() if p <= cutoff)

    def get_imbalance(self, depth_pct: float = 0.05) -> float:
        """
        Bid volume / total volume within `depth_pct` of the best bid/ask.
        > 0.5  →  more buy pressure (bullish for YES)
        < 0.5  →  more sell pressure (bearish for YES)
        Returns 0.5 when the book is empty.
        """
        bid_vol = self.get_bid_depth(depth_pct)
        ask_vol = self.get_ask_depth(depth_pct)
        total = bid_vol + ask_vol
        return bid_vol / total if total > 0.0 else 0.5

    def snapshot(self) -> dict:
        """Return a serialisable dict of the current book state (used by recorder)."""
        with self._lock:
            return {
                "market_id": self.market_id,
                "platform": self.platform,
                "bids": sorted(self._bids.items(), key=lambda x: -x[0]),
                "asks": sorted(self._asks.items(), key=lambda x: x[0]),
                "last_updated": self.last_updated.isoformat() if self.last_updated else None,
                "is_synced": self.is_synced,
            }

    # ── Callback support ───────────────────────────────────────────────────────

    def add_callback(self, fn: BookUpdateCallback) -> None:
        """Register a function to call on every book update."""
        self._callbacks.append(fn)

    def _fire_callbacks(self) -> None:
        for fn in self._callbacks:
            try:
                fn(self)
            except Exception as exc:
                logger.warning("LiveOrderBook callback error: %s", exc)

    def __repr__(self) -> str:
        bid = self.get_best_bid()
        ask = self.get_best_ask()
        return (
            f"LiveOrderBook({self.platform}:{self.market_id} "
            f"bid={bid:.4f if bid else 'N/A'} "
            f"ask={ask:.4f if ask else 'N/A'})"
        )


# ── Abstract WebSocket adapter ─────────────────────────────────────────────────

class BaseMarketAdapter(ABC):
    """
    Abstract async WebSocket market adapter.

    Subclasses implement:
        _ws_url()              WebSocket endpoint URL
        _build_auth_headers()  HTTP headers for the upgrade request
        _send_subscribe(ws, market_ids)  subscription commands
        _handle_message(raw)   parse + dispatch a raw text message

    The base class handles:
        - Connection lifecycle
        - Exponential backoff reconnect on any error
        - Book registry (get_book / get_best_bid / get_spread / etc.)
        - Global callback registration
    """

    PLATFORM: str = "unknown"

    def __init__(
        self,
        reconnect_delay_initial: float = 1.0,
        reconnect_delay_max: float = 64.0,
    ) -> None:
        self._books: Dict[str, LiveOrderBook] = {}
        self._subscriptions: Set[str] = set()
        self._reconnect_delay_initial = reconnect_delay_initial
        self._reconnect_delay_max = reconnect_delay_max
        self._running = False
        self._global_callbacks: List[BookUpdateCallback] = []

    # ── Subclass interface ─────────────────────────────────────────────────────

    @abstractmethod
    def _ws_url(self) -> str: ...

    @abstractmethod
    def _build_auth_headers(self) -> Dict[str, str]: ...

    @abstractmethod
    async def _send_subscribe(self, ws, market_ids: List[str]) -> None: ...

    @abstractmethod
    async def _handle_message(self, raw: str) -> None: ...

    # ── Book access ────────────────────────────────────────────────────────────

    def get_book(self, market_id: str) -> Optional[LiveOrderBook]:
        return self._books.get(market_id)

    def get_best_bid(self, market_id: str) -> Optional[float]:
        book = self._books.get(market_id)
        return book.get_best_bid() if book else None

    def get_best_ask(self, market_id: str) -> Optional[float]:
        book = self._books.get(market_id)
        return book.get_best_ask() if book else None

    def get_spread(self, market_id: str) -> Optional[float]:
        book = self._books.get(market_id)
        return book.get_spread() if book else None

    def get_mid(self, market_id: str) -> Optional[float]:
        book = self._books.get(market_id)
        return book.get_mid() if book else None

    def all_books(self) -> Dict[str, LiveOrderBook]:
        """Return a copy of the book registry."""
        return dict(self._books)

    # ── Subscription management ────────────────────────────────────────────────

    def subscribe(self, market_ids: List[str]) -> None:
        """Register market IDs to subscribe to (effective on next connect)."""
        for mid in market_ids:
            self._subscriptions.add(mid)
            if mid not in self._books:
                book = LiveOrderBook(mid, self.PLATFORM)
                for fn in self._global_callbacks:
                    book.add_callback(fn)
                self._books[mid] = book
        logger.debug(
            "%s adapter: %d markets registered",
            self.PLATFORM, len(self._subscriptions),
        )

    def add_global_callback(self, fn: BookUpdateCallback) -> None:
        """Register a callback that fires on every book update on any market."""
        self._global_callbacks.append(fn)
        for book in self._books.values():
            book.add_callback(fn)

    # ── Main connection loop ───────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Async main loop.  Connect → subscribe → read messages → reconnect.
        Run this inside an asyncio.Task:
            task = asyncio.create_task(adapter.run())
        """
        import websockets

        self._running = True
        delay = self._reconnect_delay_initial

        while self._running:
            try:
                headers = self._build_auth_headers()
                async with websockets.connect(
                    self._ws_url(),
                    additional_headers=headers,
                    ping_interval=20,
                    ping_timeout=15,
                    close_timeout=5,
                ) as ws:
                    logger.info("%s WebSocket connected → %s", self.PLATFORM, self._ws_url())
                    delay = self._reconnect_delay_initial  # reset after clean connect
                    await self._send_subscribe(ws, list(self._subscriptions))
                    async for message in ws:
                        if not self._running:
                            break
                        await self._handle_message(message)

            except Exception as exc:
                if not self._running:
                    break
                logger.warning(
                    "%s WebSocket error: %s – reconnecting in %.0fs",
                    self.PLATFORM, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_delay_max)

        logger.info("%s WebSocket adapter stopped", self.PLATFORM)

    def stop(self) -> None:
        """Signal the run loop to exit after the current message."""
        self._running = False

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_or_create_book(self, market_id: str) -> LiveOrderBook:
        if market_id not in self._books:
            book = LiveOrderBook(market_id, self.PLATFORM)
            for fn in self._global_callbacks:
                book.add_callback(fn)
            self._books[market_id] = book
        return self._books[market_id]
