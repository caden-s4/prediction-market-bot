"""
shared.bankroll – capital manager for the resolution drift bot.

Tracks:
  - Total capital (bankroll)
  - Per-market reservations (capital deployed in open positions)
  - Realized PnL (lifetime and daily)
  - Daily halt: stops all trading if combined daily loss hits the limit

100% of the bankroll is available for resolution drift trades, subject to
the per-position cap (default 20% of total) and fractional Kelly sizing.

Sports-specific risk controls (applied via sports_size_usd()):
  max_exposure_per_game  : 8% of bankroll on any single game
  max_exposure_per_sport : 20% of bankroll on any single sport simultaneously
  edge_scaled_sizing     : base_size × (shock_magnitude / 0.15), capped at 2×
"""

from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Dict

logger = logging.getLogger(__name__)

# Sports exposure limits
_MAX_GAME_EXPOSURE_FRAC = 0.08    # 8% per game
_MAX_SPORT_EXPOSURE_FRAC = 0.20   # 20% per sport
_SHOCK_BASE = 0.15                # shock of 0.15 = 1× base size
_SHOCK_MAX_MULT = 2.0             # cap edge multiplier at 2×


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

        # Sports exposure tracking: game_id → reserved_usd, sport → reserved_usd
        # These are separate from _reservations to allow sport-level aggregation.
        self._game_exposure: Dict[str, float] = {}    # game_id → usd
        self._sport_exposure: Dict[str, float] = {}   # sport → usd

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

    def reset_virtual(self, amount_usd: float) -> None:
        """
        Full blank-slate reset — wipes all reservations, P&L, and exposure
        tracking, then sets the total to amount_usd.

        Only for ghost/dry-run resets.  Live mode should never call this.
        """
        with self._lock:
            self._total_usd = amount_usd
            self._reserved_usd = 0.0
            self._reservations.clear()
            self._realized_pnl_usd = 0.0
            self._daily_pnl_usd = 0.0
            self._day_start = time.time()
            self._game_exposure.clear()
            self._sport_exposure.clear()
        logger.info("Bankroll: reset to $%.2f (blank slate)", amount_usd)

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

    # ── Sports risk controls ──────────────────────────────────────────────────

    def sports_size_usd(
        self,
        base_size_usd: float,
        game_id: str,
        sport: str,
        shock_magnitude: float = 0.15,
    ) -> float:
        """
        Compute the allowable sports position size after applying:
          1. Edge-scaled sizing: base × (shock / 0.15), capped at 2×
          2. Per-game exposure cap: never exceed 8% of total bankroll on one game
          3. Per-sport exposure cap: never exceed 20% of total bankroll on one sport

        Returns the final clamped size in USD (may be 0.0 if caps are exhausted).
        Does NOT reserve capital — call reserve() separately.
        """
        with self._lock:
            # Edge-scaled sizing
            mult = min(shock_magnitude / _SHOCK_BASE, _SHOCK_MAX_MULT)
            edge_sized = base_size_usd * mult

            # Per-game cap
            game_used = self._game_exposure.get(game_id, 0.0)
            game_limit = self._total_usd * _MAX_GAME_EXPOSURE_FRAC
            game_headroom = max(0.0, game_limit - game_used)

            # Per-sport cap
            sport_used = self._sport_exposure.get(sport, 0.0)
            sport_limit = self._total_usd * _MAX_SPORT_EXPOSURE_FRAC
            sport_headroom = max(0.0, sport_limit - sport_used)

            final = min(edge_sized, game_headroom, sport_headroom)

            logger.debug(
                "Bankroll.sports_size_usd: game=%s sport=%s shock=%.2f mult=%.1f "
                "edge_sized=%.2f game_headroom=%.2f sport_headroom=%.2f → %.2f",
                game_id, sport, shock_magnitude, mult,
                edge_sized, game_headroom, sport_headroom, final,
            )
            return final

    def reserve_sports(
        self,
        market_id: str,
        game_id: str,
        sport: str,
        amount_usd: float,
    ) -> bool:
        """
        Reserve capital for a sports trade, tracking game and sport exposure.

        Calls the base reserve() first; if that succeeds, updates the game/sport
        exposure counters. Returns False if reserve() fails.
        """
        if not self.reserve(market_id, amount_usd):
            return False
        with self._lock:
            self._game_exposure[game_id] = (
                self._game_exposure.get(game_id, 0.0) + amount_usd
            )
            self._sport_exposure[sport] = (
                self._sport_exposure.get(sport, 0.0) + amount_usd
            )
            logger.debug(
                "Bankroll.reserve_sports: %s game=%s sport=%s +$%.2f "
                "(game_total=%.2f sport_total=%.2f)",
                market_id, game_id, sport, amount_usd,
                self._game_exposure[game_id],
                self._sport_exposure[sport],
            )
        return True

    def release_sports(
        self,
        market_id: str,
        game_id: str,
        sport: str,
        realized_pnl_usd: float = 0.0,
    ) -> None:
        """
        Release a sports trade reservation and update game/sport exposure counters.
        """
        with self._lock:
            reserved = self._reservations.get(market_id, 0.0)
            self._game_exposure[game_id] = max(
                0.0, self._game_exposure.get(game_id, 0.0) - reserved
            )
            self._sport_exposure[sport] = max(
                0.0, self._sport_exposure.get(sport, 0.0) - reserved
            )
        # Delegate to base release for the bookkeeping
        self.release(market_id, realized_pnl_usd)

    def sports_exposure_summary(self) -> dict:
        """Return current game and sport exposure for monitoring."""
        with self._lock:
            return {
                "game_exposure": dict(self._game_exposure),
                "sport_exposure": dict(self._sport_exposure),
                "max_game_pct": _MAX_GAME_EXPOSURE_FRAC * 100,
                "max_sport_pct": _MAX_SPORT_EXPOSURE_FRAC * 100,
            }

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
