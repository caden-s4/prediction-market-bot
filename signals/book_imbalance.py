"""
signals.book_imbalance – order book imbalance directional signal.

Logic
-----
Compute the bid-to-total volume ratio (imbalance score) within a shallow
depth band near the best bid and ask.

    imbalance = bid_volume / (bid_volume + ask_volume)

Interpretation:
    > bullish_threshold (e.g. 0.65)  →  strong buy pressure  →  BUY_YES
    < bearish_threshold (e.g. 0.35)  →  strong sell pressure →  BUY_NO

Edge estimate is derived from how far the imbalance exceeds the threshold:
    edge = (imbalance - 0.5) × sensitivity

This is a momentum/order-flow signal.  It does not use weather data.
Best used to time entry and avoid adverse fills near model update windows.

References
----------
Cont, Kukanov, Stoikov (2014) – The Price Impact of Order Book Events.
Gould, Porter, Williams, McDonald, Fenn, Howison (2013) – Limit Order Books.
"""

from __future__ import annotations

import logging
from typing import Optional

from adapters.base import LiveOrderBook
from .base import BaseSignal, Signal, SignalDirection, SignalType

logger = logging.getLogger(__name__)


class BookImbalanceSignal(BaseSignal):
    """
    Fires when the order book is heavily skewed toward bids or asks.

    Parameters
    ----------
    bullish_threshold : imbalance score above which we signal BUY_YES
                        (default 0.65 = 65% of near-touch volume is on bid side)
    bearish_threshold : imbalance score below which we signal BUY_NO
                        (default 0.35)
    depth_pct         : how far from the best bid/ask to include in volume calc
                        as a fraction (default 0.05 = 5%)
    min_depth_usd     : minimum total book depth (USD) required to trust the signal
                        (avoids signaling in illiquid books with 2 lots)
    sensitivity       : scaling factor converting imbalance excess to edge estimate
    """

    @property
    def signal_type(self) -> SignalType:
        return SignalType.BOOK_IMBALANCE

    def __init__(
        self,
        bullish_threshold: float = 0.65,
        bearish_threshold: float = 0.35,
        depth_pct: float = 0.05,
        min_depth_usd: float = 500.0,
        sensitivity: float = 0.20,
    ) -> None:
        self._bullish = bullish_threshold
        self._bearish = bearish_threshold
        self._depth_pct = depth_pct
        self._min_depth = min_depth_usd
        self._sensitivity = sensitivity

    def evaluate(
        self,
        book: LiveOrderBook,
    ) -> Optional[Signal]:
        """
        Evaluate imbalance for a single market's order book.

        Parameters
        ----------
        book : the LiveOrderBook to evaluate
        """
        if not book.is_synced:
            return None

        bid_vol = book.get_bid_depth(self._depth_pct)
        ask_vol = book.get_ask_depth(self._depth_pct)
        total_vol = bid_vol + ask_vol

        if total_vol < self._min_depth:
            logger.debug(
                "BookImbalance: %s depth $%.0f below min $%.0f – skip",
                book.market_id, total_vol, self._min_depth,
            )
            return None

        imbalance = bid_vol / total_vol   # 0 = all asks, 1 = all bids

        if imbalance > self._bullish:
            direction = SignalDirection.BUY_YES
            # Edge estimate: how far above 0.5 neutral, scaled by sensitivity
            edge = (imbalance - 0.5) * self._sensitivity
            strength = (imbalance - self._bullish) / (1.0 - self._bullish)

        elif imbalance < self._bearish:
            direction = SignalDirection.BUY_NO
            edge = (0.5 - imbalance) * self._sensitivity
            strength = (self._bearish - imbalance) / self._bearish

        else:
            return None  # neutral zone – no signal

        strength = min(max(strength, 0.0), 1.0)
        edge = min(edge, 0.15)  # cap at 15% – imbalance alone can't justify more

        logger.info(
            "BookImbalance: %s imbalance=%.3f direction=%s edge=%.2f%%",
            book.market_id, imbalance, direction.value, edge * 100,
        )

        return Signal(
            signal_type=self.signal_type,
            direction=direction,
            platform=book.platform,
            market_id=book.market_id,
            edge_estimate=edge,
            strength=strength,
            metadata={
                "imbalance": imbalance,
                "bid_vol": bid_vol,
                "ask_vol": ask_vol,
                "total_vol": total_vol,
                "best_bid": book.get_best_bid(),
                "best_ask": book.get_best_ask(),
                "spread": book.get_spread(),
                "depth_pct": self._depth_pct,
            },
        )
