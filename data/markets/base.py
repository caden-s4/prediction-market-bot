"""
data.markets.base – shared types and abstract base for prediction market clients.

All market clients expose the same interface so the pipeline stages can
be written once and work across Polymarket, Kalshi, and any future platforms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    def slippage_adjusted_price(self, side: Side, size_usd: float) -> "FillResult":
        """
        Walk the book level-by-level for the requested side and clamp at
        exhausted depth.  Returns a FillResult describing how much actually
        filled, the volume-weighted average price across consumed levels, the
        number of levels touched, and whether the request was clamped.

        Side semantics:
          Side.YES → walk yes_asks ascending (cheapest seller first; buying YES)
          Side.NO  → walk yes_bids descending (highest bidder first; selling
                     YES is equivalent to buying NO)

        Units: size_usd and level.size are interpreted in the same units; the
        existing convention (matched by the legacy stage2 pipeline and the
        gap-detector calls) treats them as USD-equivalent depth.

        Edge cases:
          - Empty book on the requested side → FillResult(0.0, 0.0, 0, True).
          - size_usd <= 0 → FillResult(0.0, 0.0, 0, False) (trivially full).
          - Requested size exceeds total depth → fills everything available,
            clamped=True. Residual is NOT charged at the worst-level price
            (that was the historical over-fill bug).
        """
        if size_usd <= 0:
            return FillResult(filled_size_usd=0.0, vwap=0.0,
                              levels_consumed=0, clamped=False)

        # Asks are stored ascending (best ask first); bids are stored descending
        # (best bid first). Either walk in natural order — best-priced first.
        levels = self.yes_asks if side == Side.YES else self.yes_bids
        if not levels:
            return FillResult(filled_size_usd=0.0, vwap=0.0,
                              levels_consumed=0, clamped=True)

        remaining = size_usd
        total_cost = 0.0
        filled = 0.0
        consumed = 0
        for level in levels:
            if remaining <= 0:
                break
            fill = min(remaining, level.size)
            if fill <= 0:
                continue
            total_cost += fill * level.price
            filled += fill
            remaining -= fill
            consumed += 1

        clamped = remaining > 0
        vwap = (total_cost / filled) if filled > 0 else 0.0
        return FillResult(filled_size_usd=filled, vwap=vwap,
                          levels_consumed=consumed, clamped=clamped)


@dataclass(frozen=True)
class FillResult:
    """
    Outcome of walking an order book against a requested fill.

    filled_size_usd : how much of the request actually fills given book depth
    vwap            : volume-weighted average YES price across consumed levels
                      (0.0 when nothing filled)
    levels_consumed : number of price levels touched (0 when empty / no fill)
    clamped         : True iff requested size exceeded available depth
    """
    filled_size_usd: float
    vwap: float
    levels_consumed: int
    clamped: bool


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
        now = datetime.now(timezone.utc)
        # Handle both timezone-aware and naive resolution_date
        rd = self.resolution_date
        if rd.tzinfo is None:
            rd = rd.replace(tzinfo=timezone.utc)
        return max((rd - now).total_seconds() / 3600, 0.0)

    def is_weather_market(self) -> bool:
        if self.category.lower() == "weather":
            return True
        if any(t.lower() in ("weather", "precipitation", "rain", "snow", "temperature")
               for t in self.tags):
            return True
        # Kalshi daily high-temperature markets (e.g. KXHIGHAUS, KXHIGHCHI, KXHIGHDEN)
        # are filed under "general" in Kalshi's API but are weather contracts.
        # The pattern: market_id starts with KXHIGH and ends with a city code.
        mid_upper = self.market_id.upper()
        if mid_upper.startswith("KXHIGH") or mid_upper.startswith("KXLOW"):
            return True
        return False


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
    # Polymarket EIP-712: feeRateBps MUST be included in the signed payload.
    # Omitting it causes silent order rejection on fee-enabled markets.
    # Set this from FeeCache before calling place_order on Polymarket.
    fee_rate_bps: int = 0


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

    def get_open_orders(self) -> List[Order]:
        """
        Fetch open (unfilled) orders resting on the book.
        Returns an empty list if the platform client doesn't override this.
        """
        return []

    def close_position(self, market_id: str) -> None:
        """
        Attempt to close / cancel any open orders for a market.
        Subclasses should override this to implement exchange-specific exit logic.
        For fully-filled contracts the default behaviour is to log a warning and
        let them resolve naturally (appropriate for a resolution-drift strategy).
        """
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "close_position not implemented for platform %s – "
            "market %s will hold to resolution",
            self.PLATFORM, market_id,
        )

    def get_market(self, market_id: str) -> Optional[Market]:
        """
        Fetch a single market by ID.
        Returns None if not found, the platform doesn't support it, or the
        fetch fails.  Subclasses should override this for platforms that
        expose a per-market endpoint.
        """
        return None

    def get_balance(self) -> Optional[float]:
        """
        Fetch the current cash / USDC balance for this account.
        Returns USD float, or None if unavailable (public mode, API error, etc.).
        """
        return None
