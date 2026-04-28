"""
signals.base – shared Signal dataclass and abstract BaseSignal.

Every signal detector returns a Signal or None.  The SignalEngine
collects all fired signals and passes them to the risk manager before
any trade is placed.

Signal fields
-------------
signal_type   : "cross_exchange_arb" | "book_imbalance" | ...
direction     : "BUY_YES" | "BUY_NO" | "SKIP"
platform      : which platform to trade on ("kalshi" | "polymarket" | "both")
market_id     : canonical market ID on the chosen platform
edge_estimate : estimated net edge AFTER fees [0–1]
strength      : normalised signal strength [0–1] for ranking
fired         : set to True by SignalEngine when it passes risk checks
metadata      : arbitrary extra info for logging / calibration
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class SignalType(str, Enum):
    CROSS_EXCHANGE_ARB = "cross_exchange_arb"
    BOOK_IMBALANCE = "book_imbalance"


class SignalDirection(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SKIP = "SKIP"


@dataclass
class Signal:
    """A single trading signal emitted by a signal detector."""

    signal_type: SignalType
    direction: SignalDirection
    platform: str                   # exchange to trade on
    market_id: str                  # canonical ID on that exchange
    edge_estimate: float            # net edge after fees [0–1]
    strength: float                 # normalised ranking score [0–1]

    # Optional arb fields (populated by CrossExchangeSignal)
    buy_platform: Optional[str] = None
    sell_platform: Optional[str] = None
    buy_market_id: Optional[str] = None
    sell_market_id: Optional[str] = None
    buy_price: Optional[float] = None
    sell_price: Optional[float] = None

    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fired: bool = False            # True after passing risk checks

    def __str__(self) -> str:
        return (
            f"Signal({self.signal_type.value} {self.direction.value} "
            f"{self.platform}:{self.market_id} "
            f"edge={self.edge_estimate:.2%} strength={self.strength:.2f})"
        )


class BaseSignal(ABC):
    """
    Abstract signal detector.

    Subclasses implement evaluate() which receives the live order books
    for one market (or a pair of markets for cross-exchange signals) and
    returns a Signal if conditions are met, or None otherwise.
    """

    @property
    @abstractmethod
    def signal_type(self) -> SignalType: ...

    @abstractmethod
    def evaluate(self, **kwargs) -> Optional[Signal]:
        """
        Evaluate conditions and return a Signal or None.
        kwargs depends on the signal type (see subclass docstrings).
        """
        ...
