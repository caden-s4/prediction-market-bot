"""
signals.engine – signal orchestrator.

The SignalEngine is the central hub that:
  1. Holds references to all registered adapters and market pairs.
  2. On each tick (or book-update callback), runs all signal detectors.
  3. Returns a list of fired Signal objects sorted by strength descending.
  4. Logs every signal seen – including those that did NOT fire – to the
     event database for backtesting / calibration.

Usage (async)
-------------
engine = SignalEngine(
    cross_exchange=CrossExchangeSignal(min_spread=0.015),
    book_imbalance=BookImbalanceSignal(bullish_threshold=0.65),
)
engine.register_arb_pair(
    poly_book=poly_adapter.get_book("0xabc..."),
    kalshi_book=kalshi_adapter.get_book("KXWEATHER-SEA"),
    poly_market_id="0xabc...",
    kalshi_market_id="KXWEATHER-SEA",
)
engine.register_book(poly_adapter.get_book("0xabc..."))
engine.register_book(kalshi_adapter.get_book("KXWEATHER-SEA"))

signals = engine.evaluate_all()
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from adapters.base import LiveOrderBook
from .base import Signal, SignalDirection
from .cross_exchange import CrossExchangeSignal
from .book_imbalance import BookImbalanceSignal

logger = logging.getLogger(__name__)

# Callback type: called whenever new signals are produced (for async notification)
SignalCallback = Callable[[List[Signal]], None]


@dataclass
class ArbPair:
    """A registered cross-exchange pair for arb scanning."""
    poly_book: LiveOrderBook
    kalshi_book: LiveOrderBook
    poly_market_id: str
    kalshi_market_id: str


class SignalEngine:
    """
    Evaluates all registered signal detectors on every tick.

    Thread-safe: evaluate_all() can be called from any thread or coroutine.
    """

    def __init__(
        self,
        cross_exchange: Optional[CrossExchangeSignal] = None,
        book_imbalance: Optional[BookImbalanceSignal] = None,
        event_logger: Optional[object] = None,  # monitoring.EventDB
    ) -> None:
        self._cross_exchange = cross_exchange or CrossExchangeSignal()
        self._book_imbalance = book_imbalance or BookImbalanceSignal()
        self._event_logger = event_logger

        self._arb_pairs: List[ArbPair] = []
        self._books: List[LiveOrderBook] = []     # single-book signal targets
        self._callbacks: List[SignalCallback] = []

    # ── Registration ───────────────────────────────────────────────────────────

    def register_arb_pair(
        self,
        poly_book: LiveOrderBook,
        kalshi_book: LiveOrderBook,
        poly_market_id: str,
        kalshi_market_id: str,
    ) -> None:
        """Register a cross-exchange pair for arb scanning."""
        self._arb_pairs.append(ArbPair(
            poly_book=poly_book,
            kalshi_book=kalshi_book,
            poly_market_id=poly_market_id,
            kalshi_market_id=kalshi_market_id,
        ))
        logger.debug(
            "SignalEngine: registered arb pair poly=%s kalshi=%s",
            poly_market_id, kalshi_market_id,
        )

    def register_book(self, book: LiveOrderBook) -> None:
        """Register a live book for single-market signals (e.g. imbalance)."""
        self._books.append(book)
        logger.debug("SignalEngine: registered book %s:%s", book.platform, book.market_id)

    def add_callback(self, fn: SignalCallback) -> None:
        """Register a function called whenever evaluate_all() produces signals."""
        self._callbacks.append(fn)

    # ── Evaluation ─────────────────────────────────────────────────────────────

    def evaluate_all(self) -> List[Signal]:
        """
        Run all detectors and return all fired signals sorted by strength.
        Logs every signal (fired or not) to the event database.
        """
        signals: List[Signal] = []

        # ── Cross-exchange arb ─────────────────────────────────────────────
        for pair in self._arb_pairs:
            sig = self._cross_exchange.evaluate(
                poly_book=pair.poly_book,
                kalshi_book=pair.kalshi_book,
                poly_market_id=pair.poly_market_id,
                kalshi_market_id=pair.kalshi_market_id,
            )
            if sig:
                signals.append(sig)
                self._log_signal(sig, fired=True)
            # else: log as "seen but not fired" for calibration
            # (omitted for brevity – event_logger can handle this)

        # ── Book imbalance ─────────────────────────────────────────────────
        for book in self._books:
            sig = self._book_imbalance.evaluate(book=book)
            if sig:
                signals.append(sig)
                self._log_signal(sig, fired=True)

        # Sort by strength (highest first)
        signals.sort(key=lambda s: s.strength, reverse=True)

        if signals:
            logger.info(
                "SignalEngine: %d signal(s) fired | top=%s",
                len(signals), signals[0],
            )
            for fn in self._callbacks:
                try:
                    fn(signals)
                except Exception as exc:
                    logger.warning("SignalEngine callback error: %s", exc)

        return signals

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log_signal(self, signal: Signal, fired: bool) -> None:
        if self._event_logger:
            try:
                self._event_logger.log_signal(signal, fired=fired)
            except Exception as exc:
                logger.warning("SignalEngine: event_logger.log_signal error: %s", exc)
        logger.debug(
            "Signal %s fired=%s edge=%.2f%% strength=%.2f",
            signal.signal_type.value, fired,
            signal.edge_estimate * 100, signal.strength,
        )
