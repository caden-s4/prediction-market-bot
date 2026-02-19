"""
data.markets.base – shared types and abstract base for prediction market clients.

All market clients expose the same interface so the pipeline stages can
be written once and work across Polymarket, Kalshi, and any future platforms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    PARTIAL = "PARTIAL"


@dataclass
class PriceLevel:
    """A single price level in an order book."""
    price: float     # decimal [0–1] (cents / 100)
    size: float      # available size in USD (or contracts)


@dataclass
class OrderBook:
    """Snapshot of the YES-side order book for one market."""
    market_id: str
    platform: str
    yes_bids: List[PriceLevel]   # sorted descending (best bid first)
    yes_asks: List[PriceLevel]   # sorted ascending (best ask first)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def best_yes_ask(self) -> Optional[float]:
        """Lowest YES ask price – cost to buy YES."""
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def best_yes_bid(self) -> Optional[float]:
        """Highest YES bid – revenue from selling YES (= buying NO)."""
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def implied_no_ask(self) -> Optional[float]:
        """
        Cost to buy NO = 1 - best YES bid.
        (Buying NO is equivalent to selling YES at the bid.)
        """
        bid = self.best_yes_bid
        return (1.0 - bid) if bid is not None else None

    @property
    def mid_price(self) -> Optional[float]:
        ask = self.best_yes_ask
        bid = self.best_yes_bid
        if ask is not None and bid is not None:
            return (ask + bid) / 2.0
        return ask or bid

    def slippage_adjusted_price(self, side: Side, size_usd: float) -> float:
        """
        Estimate the volume-weighted average fill price for a given USD notional,
        accounting for order-book depth.  Returns the ask/bid price if depth
        exceeds size (no slippage).
        """
        levels = self.yes_asks if side == Side.YES else list(reversed(self.yes_bids))
        remaining = size_usd
        total_cost = 0.0
        for level in levels:
            available_usd = level.size
            fill = min(remaining, available_usd)
            total_cost += fill * level.price
            remaining -= fill
            if remaining <= 0:
                break
        if remaining > 0:
            # Not enough depth; fill the rest at worst level price
            last_price = levels[-1].price if levels else 1.0
            total_cost += remaining * last_price
        return total_cost / size_usd


@dataclass
class Market:
    """
    Normalised market representation, platform-agnostic.

    Attributes
    ----------
    market_id      : platform-native ID
    platform       : "polymarket" | "kalshi"
    question       : human-readable question text
    category       : e.g. "weather", "sports", "politics"
    tags           : list of tag strings from the platform
    resolution_date: UTC datetime when the market resolves
    yes_price      : current best YES ask price [0–1]
    no_price       : current best NO ask price [0–1]
    volume_usd     : total volume traded to date
    open_interest  : current open interest in USD
    location       : optional dict with lat/lon if weather market
    """

    market_id: str
    platform: str
    question: str
    category: str
    tags: List[str]
    resolution_date: datetime
    yes_price: float       # [0–1]
    no_price: float        # [0–1]
    volume_usd: float = 0.0
    open_interest: float = 0.0
    location: Optional[dict] = None   # {"lat": ..., "lon": ..., "city": ...}
    raw: dict = field(default_factory=dict)

    @property
    def implied_prob(self) -> float:
        """Market-implied probability from mid of YES ask/NO ask spread."""
        return self.yes_price

    @property
    def hours_to_resolution(self) -> float:
        delta = self.resolution_date - datetime.utcnow()
        return max(delta.total_seconds() / 3600, 0.0)

    def is_weather_market(self) -> bool:
        return (
            self.category.lower() == "weather"
            or any(t.lower() in ("weather", "precipitation", "rain", "snow", "temperature")
                   for t in self.tags)
        )


@dataclass
class Order:
    """Represents a placed or simulated order."""
    market_id: str
    platform: str
    side: Side
    price: float       # limit price [0–1]
    size_usd: float    # USD notional
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.OPEN
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    dry_run: bool = False


class BaseMarketClient(ABC):
    """Abstract base class all platform clients must implement."""

    PLATFORM: str = "unknown"
    # Category tags this platform supports for weather markets
    WEATHER_CATEGORY_TAGS: List[str] = ["weather"]

    # ── Required interface ────────────────────────────────────────────────────

    @abstractmethod
    def get_markets(
        self,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Market]:
        """Fetch a list of open markets, optionally filtered."""
        ...

    @abstractmethod
    def get_order_book(self, market_id: str) -> OrderBook:
        """Fetch the current order book for a specific market."""
        ...

    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """Submit an order. If dry_run=True, simulate and return immediately."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str, market_id: str) -> bool:
        """Cancel an open order. Returns True if successful."""
        ...

    @abstractmethod
    def get_positions(self) -> List[Order]:
        """Fetch current open positions."""
        ...
