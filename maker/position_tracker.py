"""
maker.position_tracker – per-market exposure and position state for the maker bot.

Tracks:
  - Outstanding bid/ask order IDs per market
  - Net position (cumulative fills) per market
  - Whether we're quoting both sides, one side, or paused

Cap rule:
  If net position on one side exceeds the per-market cap, stop quoting that
  side until it rebalances via fills on the other side or manual reset.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class MarketPosition:
    """Tracks maker state for a single market."""
    market_id: str
    cap_usd: float                  # per-side exposure cap

    # Current outstanding order IDs (None = no live quote)
    bid_order_id: Optional[str] = None
    ask_order_id: Optional[str] = None

    # Cumulative fill amounts (positive = long YES, negative = long NO)
    net_yes_usd: float = 0.0
    net_no_usd: float = 0.0

    # Last quote prices (for stale-quote detection)
    last_bid_price: float = 0.0
    last_ask_price: float = 0.0
    last_quoted_at: float = field(default_factory=time.monotonic)

    # Rebate accrual
    rebates_earned_usd: float = 0.0

    @property
    def yes_capped(self) -> bool:
        """True if YES (bid) side is at or over the cap → stop bidding."""
        return self.net_yes_usd >= self.cap_usd

    @property
    def no_capped(self) -> bool:
        """True if NO (ask) side is at or over the cap → stop asking."""
        return self.net_no_usd >= self.cap_usd

    @property
    def quote_age_seconds(self) -> float:
        return time.monotonic() - self.last_quoted_at

    def record_bid_fill(self, size_usd: float) -> None:
        self.net_yes_usd += size_usd
        self.bid_order_id = None

    def record_ask_fill(self, size_usd: float) -> None:
        self.net_no_usd += size_usd
        self.ask_order_id = None

    def record_rebate(self, amount_usd: float) -> None:
        self.rebates_earned_usd += amount_usd


class PositionTracker:
    """
    Registry of per-market maker positions.

    Thread-safe; both the async quoter and the fill callback can update it.
    """

    def __init__(self, default_cap_usd: float = 75.0) -> None:
        self._default_cap = default_cap_usd
        self._lock = RLock()
        self._markets: Dict[str, MarketPosition] = {}

    def get_or_create(self, market_id: str, cap_usd: Optional[float] = None) -> MarketPosition:
        with self._lock:
            if market_id not in self._markets:
                self._markets[market_id] = MarketPosition(
                    market_id=market_id,
                    cap_usd=cap_usd or self._default_cap,
                )
            return self._markets[market_id]

    def get(self, market_id: str) -> Optional[MarketPosition]:
        with self._lock:
            return self._markets.get(market_id)

    def all_markets(self) -> list[MarketPosition]:
        with self._lock:
            return list(self._markets.values())

    def total_exposure_usd(self) -> float:
        with self._lock:
            return sum(p.net_yes_usd + p.net_no_usd for p in self._markets.values())

    def total_rebates_usd(self) -> float:
        with self._lock:
            return sum(p.rebates_earned_usd for p in self._markets.values())

    def stale_markets(self, max_age_seconds: float = 0.5) -> list[MarketPosition]:
        """Return markets whose quotes are older than max_age_seconds."""
        with self._lock:
            return [
                p for p in self._markets.values()
                if (p.bid_order_id or p.ask_order_id)
                and p.quote_age_seconds > max_age_seconds
            ]
