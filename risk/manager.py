"""
risk.manager – pre-trade risk gate for the live WebSocket trading loop.

Checks (in order)
-----------------
1. Already in position?        Prevents duplicate entries in the same market.
2. Daily loss limit hit?       Hard-stops trading if drawdown exceeds threshold.
3. Minimum edge threshold?     Signal edge must exceed the configured floor.
4. Fractional Kelly sizing?    Computes safe position size; returns 0 if too small.
5. Position size ceiling?      Single trade ≤ max_position_fraction of bankroll.
6. Total exposure ceiling?     All open positions ≤ max_total_exposure of bankroll.

RiskDecision
------------
approved    : bool    – True if all checks passed
reject_reason : str   – human-readable explanation when approved=False
position_size_usd : float – recommended size when approved=True

This module is intentionally synchronous (no async/await) so it can be
called from the signal callback without bridging event loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Optional

from signals.base import Signal, SignalDirection

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """Output of the risk manager for a single candidate trade."""
    approved: bool
    reject_reason: str = ""
    position_size_usd: float = 0.0
    kelly_fraction: float = 0.0
    check_log: str = ""            # pipe-delimited trace of all checks


class RiskManager:
    """
    Stateful pre-trade risk manager.

    State tracked internally:
        - Set of currently open market IDs (prevent duplicate positions)
        - Daily PnL  (reset at UTC midnight)
        - Total USD currently at risk

    Parameters
    ----------
    bankroll_usd          : total capital to size against
    kelly_fraction        : fractional Kelly multiplier (0.10–0.50 recommended)
    max_position_fraction : single trade hard cap as fraction of bankroll
    max_total_exposure    : all open trades hard cap as fraction of bankroll
    min_edge_threshold    : minimum net edge to approve a trade (e.g. 0.015)
    max_daily_loss_usd    : halt trading if daily losses exceed this amount
    """

    def __init__(
        self,
        bankroll_usd: float,
        kelly_fraction: float = 0.25,
        max_position_fraction: float = 0.08,
        max_total_exposure: float = 0.25,
        min_edge_threshold: float = 0.015,
        max_daily_loss_usd: float = 0.0,   # 0 = disabled
    ) -> None:
        self._bankroll = bankroll_usd
        self._kelly_fraction = kelly_fraction
        self._max_pos_frac = max_position_fraction
        self._max_exposure = max_total_exposure
        self._min_edge = min_edge_threshold
        self._max_daily_loss = max_daily_loss_usd

        self._open_positions: Dict[str, float] = {}   # market_id → USD at risk
        self._daily_pnl: float = 0.0
        self._pnl_date: date = datetime.now(timezone.utc).date()

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def bankroll(self) -> float:
        return self._bankroll

    @property
    def total_exposure_usd(self) -> float:
        return sum(self._open_positions.values())

    @property
    def daily_pnl(self) -> float:
        self._maybe_reset_daily_pnl()
        return self._daily_pnl

    def check(self, signal: Signal) -> RiskDecision:
        """
        Run all risk checks for a candidate trade signal.
        Returns a RiskDecision with approved=True and a position size,
        or approved=False with the first-failing reason.
        """
        self._maybe_reset_daily_pnl()
        checks: list[str] = []

        # ── Check 1: Duplicate position ────────────────────────────────────
        if signal.market_id in self._open_positions:
            reason = f"Already in position for {signal.market_id}"
            return RiskDecision(approved=False, reject_reason=reason,
                                check_log="FAIL:duplicate_position")

        checks.append("PASS:no_duplicate")

        # ── Check 2: Daily loss limit ──────────────────────────────────────
        if self._max_daily_loss > 0 and self._daily_pnl <= -self._max_daily_loss:
            reason = (
                f"Daily loss limit hit: ${-self._daily_pnl:.2f} "
                f"≥ ${self._max_daily_loss:.2f}"
            )
            return RiskDecision(approved=False, reject_reason=reason,
                                check_log="|".join(checks) + "|FAIL:daily_loss_limit")

        checks.append("PASS:daily_loss_ok")

        # ── Check 3: Minimum edge ──────────────────────────────────────────
        if signal.edge_estimate < self._min_edge:
            reason = (
                f"Edge {signal.edge_estimate:.2%} below threshold {self._min_edge:.2%}"
            )
            return RiskDecision(approved=False, reject_reason=reason,
                                check_log="|".join(checks) + "|FAIL:edge_below_min")

        checks.append(f"PASS:edge={signal.edge_estimate:.3f}")

        # ── Check 4: Kelly sizing ──────────────────────────────────────────
        kelly_frac = self._kelly(signal)
        fractional = kelly_frac * self._kelly_fraction
        checks.append(f"kelly={kelly_frac:.4f} frac_kelly={fractional:.4f}")

        # ── Check 5: Single position cap ───────────────────────────────────
        max_pos_usd = self._bankroll * self._max_pos_frac
        pos_usd = min(fractional * self._bankroll, max_pos_usd)

        if pos_usd <= 0:
            return RiskDecision(approved=False, reject_reason="Kelly sizing produced $0 position",
                                check_log="|".join(checks) + "|FAIL:zero_size")

        checks.append(f"PASS:single_cap max=${max_pos_usd:.0f} pos=${pos_usd:.0f}")

        # ── Check 6: Total exposure cap ────────────────────────────────────
        max_total_usd = self._bankroll * self._max_exposure
        remaining = max_total_usd - self.total_exposure_usd
        if remaining <= 0:
            reason = (
                f"Total exposure ceiling reached "
                f"(${self.total_exposure_usd:.0f} / ${max_total_usd:.0f})"
            )
            return RiskDecision(approved=False, reject_reason=reason,
                                check_log="|".join(checks) + "|FAIL:exposure_ceiling")

        pos_usd = min(pos_usd, remaining)
        checks.append(f"PASS:total_cap remaining=${remaining:.0f} final=${pos_usd:.0f}")

        logger.info(
            "RiskManager: APPROVED %s  size=$%.2f  kelly=%.4f  checks=%s",
            signal.market_id, pos_usd, kelly_frac, "|".join(checks),
        )
        return RiskDecision(
            approved=True,
            position_size_usd=pos_usd,
            kelly_fraction=kelly_frac,
            check_log="|".join(checks),
        )

    # ── Position lifecycle ─────────────────────────────────────────────────────

    def record_open(self, market_id: str, size_usd: float) -> None:
        """Call after a trade is successfully placed."""
        self._open_positions[market_id] = size_usd
        logger.debug("RiskManager: opened %s $%.2f", market_id, size_usd)

    def record_close(self, market_id: str, pnl_usd: float) -> None:
        """Call after a position resolves or is manually exited."""
        self._open_positions.pop(market_id, None)
        self._daily_pnl += pnl_usd
        self._bankroll += pnl_usd
        logger.debug(
            "RiskManager: closed %s pnl=$%.2f  daily_pnl=$%.2f  bankroll=$%.2f",
            market_id, pnl_usd, self._daily_pnl, self._bankroll,
        )

    def update_bankroll(self, new_bankroll: float) -> None:
        """Sync external bankroll changes (e.g. deposits/withdrawals)."""
        self._bankroll = new_bankroll

    # ── Kelly sizing ───────────────────────────────────────────────────────────

    def _kelly(self, signal: Signal) -> float:
        """
        Binary Kelly fraction: f* = (b·p − q) / b
        where p = P(win), q = 1−p, b = net odds on a win.

        For a prediction market at price `entry`:
            b = (1 − entry) / entry
            p = our estimated probability of the trade resolving in our favour
        """
        edge = signal.edge_estimate
        # Approximate entry price from edge + a mid-market estimate
        # edge ≈ our_prob − entry  →  entry ≈ our_prob − edge
        # Without a full weather model here, use a conservative mid of 0.5
        # (the weather pipeline stages set the real sizing; this is for WS signals)
        mid = signal.metadata.get("best_bid", 0.45)
        if signal.direction == SignalDirection.BUY_NO:
            entry = 1.0 - float(mid or 0.55)
        else:
            entry = float(mid or 0.45)

        entry = max(min(entry, 0.99), 0.01)
        b = (1.0 - entry) / entry
        p = min(entry + edge, 0.99)
        q = 1.0 - p

        kelly = (b * p - q) / b if b > 0 else 0.0
        return max(kelly, 0.0)

    # ── Daily PnL reset ────────────────────────────────────────────────────────

    def _maybe_reset_daily_pnl(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._pnl_date:
            logger.info(
                "RiskManager: new UTC day – resetting daily PnL (was $%.2f)",
                self._daily_pnl,
            )
            self._daily_pnl = 0.0
            self._pnl_date = today
