"""
shared.bankroll – unified bankroll manager for both bots.

Allocation:
  60% → Maker bot (capital posted as limit orders in crypto markets)
  40% → Resolution drift bot (capital available for arb positions)

Rules:
  - Never let one bot starve the other beyond its allocation ceiling
  - Hard daily loss limit across both bots combined; if hit, both stop
  - Track per-bot PnL separately for analysis

The bankroll manager is the single source of truth for capital state.
Both bots call reserve() before placing orders and release() on close/fill.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class BotAllocation:
    """Capital state for a single bot."""
    name: str
    ceiling_fraction: float        # e.g. 0.60 for maker
    reserved_usd: float = 0.0     # currently deployed
    realized_pnl_usd: float = 0.0
    daily_pnl_usd: float = 0.0
    _day_start: float = field(default_factory=time.time, repr=False)

    def reset_daily(self) -> None:
        self.daily_pnl_usd = 0.0
        self._day_start = time.time()


class Bankroll:
    """
    Thread-safe shared bankroll manager.

    Parameters
    ----------
    total_usd          : total capital to manage
    maker_fraction     : fraction allocated to maker bot (default 0.60)
    resolution_fraction: fraction allocated to resolution bot (default 0.40)
    max_daily_loss_usd : hard daily stop across both bots (0 = disabled)
    """

    def __init__(
        self,
        total_usd: float,
        maker_fraction: float = 0.60,
        resolution_fraction: float = 0.40,
        max_daily_loss_usd: float = 0.0,
    ) -> None:
        assert abs(maker_fraction + resolution_fraction - 1.0) < 1e-6, \
            "maker_fraction + resolution_fraction must equal 1.0"

        self._lock = RLock()
        self._total_usd = total_usd
        self._max_daily_loss = max_daily_loss_usd

        self._bots: Dict[str, BotAllocation] = {
            "maker": BotAllocation("maker", maker_fraction),
            "resolution": BotAllocation("resolution", resolution_fraction),
        }

        # Per-market reservation tracking: market_id → (bot_name, reserved_usd)
        self._reservations: Dict[str, tuple] = {}

    # ── Capital queries ───────────────────────────────────────────────────────

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._total_usd

    def ceiling_usd(self, bot: str) -> float:
        """Maximum capital the named bot is allowed to deploy."""
        with self._lock:
            alloc = self._bots[bot]
            return self._total_usd * alloc.ceiling_fraction

    def available_usd(self, bot: str) -> float:
        """Capital available to deploy right now for the named bot."""
        with self._lock:
            alloc = self._bots[bot]
            ceiling = self._total_usd * alloc.ceiling_fraction
            return max(0.0, ceiling - alloc.reserved_usd)

    def reserved_usd(self, bot: str) -> float:
        with self._lock:
            return self._bots[bot].reserved_usd

    def is_halted(self) -> bool:
        """True if the daily loss limit has been breached."""
        if self._max_daily_loss <= 0:
            return False
        with self._lock:
            total_daily_loss = sum(
                max(0.0, -b.daily_pnl_usd) for b in self._bots.values()
            )
            return total_daily_loss >= self._max_daily_loss

    # ── Reservation lifecycle ─────────────────────────────────────────────────

    def reserve(self, bot: str, market_id: str, amount_usd: float) -> bool:
        """
        Reserve capital for a pending order.

        Returns True if the reservation succeeded, False if insufficient
        capacity or daily limit breached.
        """
        if self.is_halted():
            logger.warning("Bankroll: HALTED – daily loss limit reached")
            return False

        with self._lock:
            alloc = self._bots[bot]
            ceiling = self._total_usd * alloc.ceiling_fraction
            if alloc.reserved_usd + amount_usd > ceiling:
                logger.warning(
                    "Bankroll: %s insufficient capacity (need=%.2f avail=%.2f)",
                    bot, amount_usd, ceiling - alloc.reserved_usd,
                )
                return False
            alloc.reserved_usd += amount_usd
            self._reservations[market_id] = (bot, amount_usd)
            logger.debug(
                "Bankroll: reserve %s/%s $%.2f (total_reserved=%.2f)",
                bot, market_id, amount_usd, alloc.reserved_usd,
            )
            return True

    def release(self, market_id: str, realized_pnl_usd: float = 0.0) -> None:
        """
        Release a reservation and record PnL on close.
        """
        with self._lock:
            entry = self._reservations.pop(market_id, None)
            if entry is None:
                return
            bot_name, reserved = entry
            alloc = self._bots[bot_name]
            alloc.reserved_usd = max(0.0, alloc.reserved_usd - reserved)
            alloc.realized_pnl_usd += realized_pnl_usd
            alloc.daily_pnl_usd += realized_pnl_usd
            self._total_usd += realized_pnl_usd
            logger.info(
                "Bankroll: released %s/%s pnl=%.2f (bot_daily=%.2f total=%.2f)",
                bot_name, market_id, realized_pnl_usd,
                alloc.daily_pnl_usd, self._total_usd,
            )

    def record_rebate(self, amount_usd: float) -> None:
        """Record a maker rebate payment (positive PnL, no reservation needed)."""
        with self._lock:
            alloc = self._bots["maker"]
            alloc.realized_pnl_usd += amount_usd
            alloc.daily_pnl_usd += amount_usd
            self._total_usd += amount_usd

    # ── Daily reset ───────────────────────────────────────────────────────────

    def reset_daily_stats(self) -> None:
        with self._lock:
            for alloc in self._bots.values():
                alloc.reset_daily()
        logger.info("Bankroll: daily stats reset")

    def summary(self) -> dict:
        with self._lock:
            return {
                "total_usd": round(self._total_usd, 4),
                "halted": self.is_halted(),
                "bots": {
                    name: {
                        "ceiling_usd": round(self._total_usd * b.ceiling_fraction, 2),
                        "reserved_usd": round(b.reserved_usd, 2),
                        "available_usd": round(
                            max(0, self._total_usd * b.ceiling_fraction - b.reserved_usd), 2
                        ),
                        "realized_pnl_usd": round(b.realized_pnl_usd, 4),
                        "daily_pnl_usd": round(b.daily_pnl_usd, 4),
                    }
                    for name, b in self._bots.items()
                },
            }
