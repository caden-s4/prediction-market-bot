"""
resolution.decay_monitor – position monitoring for Bot 2.

Every 5 minutes, check all open resolution drift positions.

Exit rules:
  1. EARLY EXIT (profit target)
     If the position has captured >= 80% of its theoretical maximum gain,
     exit now. This eliminates oracle dispute risk entirely and frees capital.

  2. HARD STOP (loss limit)
     If the position has moved 50%+ against entry AND time to resolution
     is still > 4 hours, exit and reassess. Either the data was wrong or the
     market knows something we don't.

  3. RESOLUTION APPROACH
     If < 15 minutes to resolution and position hasn't triggered an early
     exit, hold and let it resolve (only if source confidence was very high).
     If source confidence < 0.9, exit anyway to avoid oracle disputes.

Theoretical maximum gain (Kalshi contract economics):
  On Kalshi, buying a YES contract at price p costs p per contract.
  num_contracts = size_usd / entry_price   (for YES buy)
  num_contracts = size_usd / (1 - entry_price)   (for NO buy)

  For a YES position:
    theo_max = (ground_truth_prob - entry_price) * num_contracts
  Current gain:
    current_gain = (current_price - entry_price) * num_contracts
  Capture ratio = current_gain / theo_max
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from data.markets.base import Market

logger = logging.getLogger(__name__)

EARLY_EXIT_CAPTURE_RATIO = 0.80   # exit if 80%+ of theoretical max captured
STOP_LOSS_RATIO = 0.50             # exit if position moved 50%+ against us
STOP_LOSS_MIN_HOURS = 4.0          # only apply stop-loss if > 4h to resolution
APPROACH_THRESHOLD_HOURS = 0.25    # < 15 min to resolution = "approaching"
HIGH_CONFIDENCE_HOLD_THRESHOLD = 0.90  # hold through resolution only if this confident

# Time-based escalation: if a position is 30%+ adverse AND resolution is
# approaching fast (< 45 min), escalate to an urgent stop-loss exit regardless
# of the standard STOP_LOSS_MIN_HOURS gate.  With under 45 minutes left there
# is almost no time to recover, so cutting the loss early is strictly better.
URGENT_STOP_LOSS_RATIO = 0.30      # 30% adverse triggers escalation (softer threshold)
URGENT_STOP_LOSS_MAX_HOURS = 0.75  # escalation only active when < 45 min to resolution


class DecayAction(str, Enum):
    HOLD = "HOLD"
    EARLY_EXIT = "EARLY_EXIT"    # captured 80%+ of theoretical gain
    STOP_LOSS = "STOP_LOSS"      # moved 50%+ against and plenty of time left
    APPROACH_EXIT = "APPROACH_EXIT"  # resolving soon, exit to avoid oracle risk


@dataclass
class OpenResolutionPosition:
    """Represents a live resolution drift position."""
    market_id: str
    platform: str
    market: Market

    entry_price: float           # YES price at entry
    current_price: float         # current YES price (mark-to-market)
    ground_truth_prob: float     # ground truth probability at entry
    size_usd: float
    source_confidence: float     # confidence score at entry
    action: str                  # "buy_yes" | "buy_no"


@dataclass
class DecayDecision:
    """What to do with an open position."""
    position: OpenResolutionPosition
    action: DecayAction
    capture_ratio: float
    current_gain_usd: float
    reason: str


class DecayMonitor:
    """
    Evaluates all open resolution drift positions and emits exit signals.
    """

    def evaluate(
        self, positions: List[OpenResolutionPosition]
    ) -> List[DecayDecision]:
        """
        Evaluate all positions and return a list of decisions.
        HOLD decisions are also returned (for logging completeness).
        """
        decisions = []
        for pos in positions:
            decision = self._evaluate_one(pos)
            decisions.append(decision)
        return decisions

    def _evaluate_one(self, pos: OpenResolutionPosition) -> DecayDecision:
        hours_left = pos.market.hours_to_resolution
        current_price = pos.current_price

        # Direction-aware gain calculation using Kalshi contract economics.
        # Cost of a YES contract = entry_price per contract → num_contracts = size / entry_price.
        # Cost of a NO contract  = (1 - entry_price) per contract → num_contracts = size / (1 - entry_price).
        if pos.action == "buy_yes":
            num_contracts = pos.size_usd / pos.entry_price if pos.entry_price > 1e-9 else 0.0
            theo_max = (pos.ground_truth_prob - pos.entry_price) * num_contracts
            current_gain = (current_price - pos.entry_price) * num_contracts
        else:  # buy_no = sold YES
            no_entry = 1.0 - pos.entry_price
            num_contracts = pos.size_usd / no_entry if no_entry > 1e-9 else 0.0
            theo_max = (pos.entry_price - pos.ground_truth_prob) * num_contracts
            current_gain = (pos.entry_price - current_price) * num_contracts

        capture_ratio = current_gain / theo_max if theo_max > 1e-6 else 0.0

        logger.debug(
            "DecayMonitor: %s capture=%.2f gain=$%.2f theo=$%.2f hours=%.2f",
            pos.market_id, capture_ratio, current_gain, theo_max, hours_left,
        )

        # ── Rule 1: Early exit (80% capture) ──────────────────────────────
        if capture_ratio >= EARLY_EXIT_CAPTURE_RATIO:
            return DecayDecision(
                position=pos,
                action=DecayAction.EARLY_EXIT,
                capture_ratio=capture_ratio,
                current_gain_usd=current_gain,
                reason=(
                    f"Captured {capture_ratio:.0%} of theoretical gain "
                    f"(${current_gain:.2f} of ${theo_max:.2f}). "
                    f"Exiting early to avoid oracle dispute risk."
                ),
            )

        # ── Rule 2: Hard stop-loss (50% adverse, > 4h left) ───────────────
        if hours_left > STOP_LOSS_MIN_HOURS and capture_ratio <= -STOP_LOSS_RATIO:
            return DecayDecision(
                position=pos,
                action=DecayAction.STOP_LOSS,
                capture_ratio=capture_ratio,
                current_gain_usd=current_gain,
                reason=(
                    f"Position moved {abs(capture_ratio):.0%} adverse "
                    f"(${current_gain:.2f}) with {hours_left:.1f}h left. "
                    f"Data source may be wrong or market has new information."
                ),
            )

        # ── Rule 2b: Time-based escalation (30% adverse, < 45 min) ────────
        # With under 45 minutes to resolution there is almost no time to
        # recover a 30%+ adverse move.  Escalate to an urgent exit regardless
        # of the STOP_LOSS_MIN_HOURS gate that normally protects long-horizon
        # positions from premature stop-outs.
        if hours_left < URGENT_STOP_LOSS_MAX_HOURS and capture_ratio <= -URGENT_STOP_LOSS_RATIO:
            return DecayDecision(
                position=pos,
                action=DecayAction.STOP_LOSS,
                capture_ratio=capture_ratio,
                current_gain_usd=current_gain,
                reason=(
                    f"Urgent exit: position {abs(capture_ratio):.0%} adverse "
                    f"(${current_gain:.2f}) with only {hours_left * 60:.0f}min left — "
                    f"insufficient time to recover; cutting loss."
                ),
            )

        # ── Rule 3: Approaching resolution ────────────────────────────────
        if hours_left < APPROACH_THRESHOLD_HOURS:
            if pos.source_confidence >= HIGH_CONFIDENCE_HOLD_THRESHOLD:
                action = DecayAction.HOLD
                reason = (
                    f"<15min to resolution. Source confidence={pos.source_confidence:.2f} "
                    f">= {HIGH_CONFIDENCE_HOLD_THRESHOLD} – holding to resolution."
                )
            else:
                action = DecayAction.APPROACH_EXIT
                reason = (
                    f"<15min to resolution. Source confidence={pos.source_confidence:.2f} "
                    f"< {HIGH_CONFIDENCE_HOLD_THRESHOLD} – exiting to avoid oracle dispute."
                )
            return DecayDecision(
                position=pos,
                action=action,
                capture_ratio=capture_ratio,
                current_gain_usd=current_gain,
                reason=reason,
            )

        # ── Default: hold ─────────────────────────────────────────────────
        return DecayDecision(
            position=pos,
            action=DecayAction.HOLD,
            capture_ratio=capture_ratio,
            current_gain_usd=current_gain,
            reason=(
                f"Capture={capture_ratio:.0%} gain=${current_gain:.2f} "
                f"hours_left={hours_left:.1f}h – holding."
            ),
        )
