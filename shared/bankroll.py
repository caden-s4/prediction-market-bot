"""
shared.bankroll – capital manager for the resolution drift bot.

Tracks:
  - Total capital (bankroll)
  - Per-market reservations (capital deployed in open positions)
  - Realized PnL (lifetime and daily)
  - Daily halt: stops all trading if combined daily loss hits the limit

100% of the bankroll is available for resolution drift trades, subject to
the per-position cap (default 20% of total) and fractional Kelly sizing.
"""

from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Dict

logger = logging.getLogger(__name__)


class Bankroll:
    """
    Thread-safe capital manager.

    Parameters
    ----------
    total_usd          : total capital to manage
    max_daily_loss_usd : hard daily stop (0 = disabled)
    """

    def __init__(
        self,
        total_usd: float,
        max_daily_loss_usd: float = 0.0,
    ) -> None:
        self._lock = RLock()
        self._total_usd = total_usd
        self._max_daily_loss = max_daily_loss_usd

        self._reserved_usd: float = 0.0
        self._realized_pnl_usd: float = 0.0
        self._daily_pnl_usd: float = 0.0
        self._day_start: float = time.time()

        # market_id → reserved_usd
        self._reservations: Dict[str, float] = {}

    # ── Capital queries ───────────────────────────────────────────────────────

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._total_usd

    @property
    def available_usd(self) -> float:
        with self._lock:
            return max(0.0, self._total_usd - self._reserved_usd)

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return self._reserved_usd

    def set_total(self, amount_usd: float) -> None:
        """Override the total bankroll (for dry-run virtual capital)."""
        with self._lock:
            self._total_usd = amount_usd
        logger.info("Bankroll: total overridden to $%.2f", amount_usd)

    def is_halted(self) -> bool:
        """True if the daily loss limit has been breached."""
        with self._lock:
            if self._max_daily_loss <= 0:
                return False
            return -self._daily_pnl_usd >= self._max_daily_loss

    # ── Reservation lifecycle ─────────────────────────────────────────────────

    def reserve(self, market_id: str, amount_usd: float) -> bool:
        """
        Reserve capital for a pending order.
        Returns True on success, False if insufficient capital or halted.
        """
        if self.is_halted():
            logger.warning("Bankroll: HALTED – daily loss limit reached")
            return False

        with self._lock:
            if self._reserved_usd + amount_usd > self._total_usd:
                logger.warning(
                    "Bankroll: insufficient capital (need=%.2f avail=%.2f)",
                    amount_usd, self._total_usd - self._reserved_usd,
                )
                return False
            self._reserved_usd += amount_usd
            self._reservations[market_id] = amount_usd
            logger.debug(
                "Bankroll: reserve %s $%.2f (reserved=%.2f total=%.2f)",
                market_id, amount_usd, self._reserved_usd, self._total_usd,
            )
            return True

    def release(self, market_id: str, realized_pnl_usd: float = 0.0) -> None:
        """Release a reservation and record realized PnL."""
        with self._lock:
            reserved = self._reservations.pop(market_id, 0.0)
            self._reserved_usd = max(0.0, self._reserved_usd - reserved)
            self._realized_pnl_usd += realized_pnl_usd
            self._daily_pnl_usd += realized_pnl_usd
            self._total_usd += realized_pnl_usd
            logger.info(
                "Bankroll: released %s pnl=%.4f (daily=%.4f total=%.2f)",
                market_id, realized_pnl_usd, self._daily_pnl_usd, self._total_usd,
            )

    # ── Daily reset ───────────────────────────────────────────────────────────

    def reset_daily_stats(self) -> None:
        with self._lock:
            self._daily_pnl_usd = 0.0
            self._day_start = time.time()
        logger.info("Bankroll: daily stats reset")

    def summary(self) -> dict:
        with self._lock:
            return {
                "total_usd": round(self._total_usd, 4),
                "reserved_usd": round(self._reserved_usd, 2),
                "available_usd": round(max(0.0, self._total_usd - self._reserved_usd), 2),
                "realized_pnl_usd": round(self._realized_pnl_usd, 4),
                "daily_pnl_usd": round(self._daily_pnl_usd, 4),
                "halted": self.is_halted(),
            }
