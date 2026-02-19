"""
execution.portfolio – in-memory portfolio state tracker.

Tracks:
  - Open positions (Order objects) with associated entry edge
  - Current bankroll (updated after each resolved trade)
  - Total deployed exposure
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from data.markets.base import Order, OrderStatus

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Enriched order tracked by the portfolio."""
    order: Order
    market_id: str
    platform: str
    entry_edge: float
    entry_prob: float
    trade_id: Optional[int] = None   # calibration DB row ID
    entered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Portfolio:
    """
    Manages the set of open positions and running bankroll.
    Thread-safety is NOT guaranteed; use single-threaded event loop.
    """

    def __init__(self, starting_bankroll: float) -> None:
        self._bankroll = starting_bankroll
        self._positions: Dict[str, Position] = {}   # key: market_id

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def open_positions(self) -> List[Position]:
        return list(self._positions.values())

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.order.size_usd for p in self._positions.values())

    @property
    def available_capital(self) -> float:
        return max(self._bankroll - self.total_exposure_usd, 0.0)

    # ── Position management ───────────────────────────────────────────────────

    def add_position(self, position: Position) -> None:
        if position.market_id in self._positions:
            logger.warning(
                "Portfolio: already have position in %s – skipping duplicate",
                position.market_id,
            )
            return
        self._positions[position.market_id] = position
        logger.info(
            "Portfolio: opened position in %s $%.2f (edge=%.1f%%)",
            position.market_id, position.order.size_usd, position.entry_edge * 100,
        )

    def remove_position(self, market_id: str) -> Optional[Position]:
        pos = self._positions.pop(market_id, None)
        if pos:
            logger.info("Portfolio: closed position in %s", market_id)
        return pos

    def resolve_position(
        self,
        market_id: str,
        outcome: float,      # 0.0 or 1.0
        final_price: float,  # price at resolution
    ) -> float:
        """
        Mark a position as resolved.  Computes PnL and updates bankroll.
        Returns PnL in USD.
        """
        pos = self.remove_position(market_id)
        if not pos:
            logger.warning("Portfolio: resolve_position called for unknown %s", market_id)
            return 0.0

        size = pos.order.size_usd
        entry_price = pos.order.price or pos.entry_prob

        if pos.order.side.value == "YES":
            pnl = size * (outcome / entry_price - 1.0) if entry_price > 0 else 0.0
        else:
            pnl = size * ((1 - outcome) / (1 - entry_price) - 1.0) if entry_price < 1.0 else 0.0

        self._bankroll += pnl
        logger.info(
            "Portfolio: resolved %s outcome=%.0f pnl=$%.2f bankroll=$%.2f",
            market_id, outcome, pnl, self._bankroll,
        )
        return pnl

    def get_position(self, market_id: str) -> Optional[Position]:
        return self._positions.get(market_id)

    def summary(self) -> dict:
        return {
            "bankroll": self._bankroll,
            "total_exposure": self.total_exposure_usd,
            "available_capital": self.available_capital,
            "n_open_positions": len(self._positions),
            "positions": [
                {
                    "market_id": p.market_id,
                    "platform": p.platform,
                    "size_usd": p.order.size_usd,
                    "side": p.order.side.value,
                    "entry_edge": p.entry_edge,
                }
                for p in self._positions.values()
            ],
        }
