"""
Stage 4 – Risk Management and Position Sizing.

Inputs  : EdgeResult + portfolio state + bot config
Outputs : RiskResult dataclass

Logic (from spec):
- Calculate full Kelly position size
- Apply fractional Kelly multiplier (default 0.25)
- Hard-cap at max_position_fraction of bankroll
- Apply correlation penalty for related positions
- Enforce max total exposure ceiling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pipeline.stage3_edge import EdgeResult

logger = logging.getLogger(__name__)


@dataclass
class RiskResult:
    """
    Output of the risk management stage.

    kelly_fraction        : raw Kelly fraction suggested
    fractional_kelly      : after multiplier applied
    position_size_usd     : final recommended position size in USD
    passes_exposure_check : False if bankroll fully deployed or at ceiling
    correlation_penalty   : 0–1 multiplier applied for correlated positions
    reject_reason         : human-readable reason if rejected
    """
    kelly_fraction: float
    fractional_kelly: float
    position_size_usd: float
    passes_exposure_check: bool
    correlation_penalty: float = 1.0
    reject_reason: str = ""


class Stage4Risk:
    """
    Applies Kelly Criterion, fractional multiplier, and exposure limits.
    """

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        max_position_fraction: float = 0.08,
        max_total_exposure: float = 0.25,
    ) -> None:
        self._kelly_fraction = kelly_fraction
        self._max_pos_frac = max_position_fraction
        self._max_exposure = max_total_exposure

    def run(
        self,
        edge_result: EdgeResult,
        bankroll: float,
        current_exposure_usd: float,
        correlated_positions: int = 0,
    ) -> RiskResult:
        """
        Parameters
        ----------
        edge_result           : from Stage 3
        bankroll              : total bankroll in USD
        current_exposure_usd  : total USD currently deployed across all positions
        correlated_positions  : # of open positions correlated with this trade
        """
        if bankroll <= 0:
            return RiskResult(
                kelly_fraction=0.0, fractional_kelly=0.0, position_size_usd=0.0,
                passes_exposure_check=False, reject_reason="Bankroll is zero"
            )

        p = edge_result.our_prob      # P(WIN)
        q = 1.0 - p                   # P(LOSS)
        # b = net odds received on win; for binary prediction markets b = (1/entry - 1)
        if edge_result.direction == 1:
            entry = max(edge_result.net_edge + edge_result.tx_cost + p, 1e-6)
        else:
            entry = max(1.0 - edge_result.our_prob, 1e-6)

        # Binary Kelly: f* = (b*p - q) / b where b = (1 - entry) / entry
        b = (1.0 - entry) / entry if entry < 1.0 else 0.0
        kelly = (b * p - q) / b if b > 0 else 0.0
        kelly = max(kelly, 0.0)   # never short

        # ── Fractional Kelly ───────────────────────────────────────────────
        fractional = kelly * self._kelly_fraction

        # ── Correlation penalty ────────────────────────────────────────────
        # Reduce size by 20% per correlated position (min 40% of original)
        penalty = max(1.0 - 0.20 * correlated_positions, 0.40)
        fractional *= penalty

        # ── Hard caps ─────────────────────────────────────────────────────
        max_pos_usd = bankroll * self._max_pos_frac
        pos_usd = min(fractional * bankroll, max_pos_usd)

        # ── Total exposure ceiling ─────────────────────────────────────────
        remaining_capacity = bankroll * self._max_exposure - current_exposure_usd
        if remaining_capacity <= 0:
            return RiskResult(
                kelly_fraction=kelly,
                fractional_kelly=fractional,
                position_size_usd=0.0,
                passes_exposure_check=False,
                correlation_penalty=penalty,
                reject_reason=(
                    f"Total exposure ceiling reached "
                    f"({current_exposure_usd:.0f}/{bankroll * self._max_exposure:.0f} USD)"
                ),
            )

        pos_usd = min(pos_usd, remaining_capacity)
        pos_usd = max(pos_usd, 0.0)

        logger.info(
            "Stage4: kelly=%.3f frac=%.3f corr_penalty=%.2f final_size=$%.2f",
            kelly, fractional, penalty, pos_usd,
        )
        return RiskResult(
            kelly_fraction=kelly,
            fractional_kelly=fractional,
            position_size_usd=pos_usd,
            passes_exposure_check=True,
            correlation_penalty=penalty,
        )
