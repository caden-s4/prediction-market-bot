"""
Stage 5 – Timing and Execution Logic.

Inputs  : market + edge result + current UTC time
Outputs : TimingResult dataclass

Logic (from spec):
- Check whether we're in a high-information-flow window (right after a
  model update cycle) or mid-cycle.
- Apply a decay function: as time-to-resolution decreases, the edge
  threshold required to trade increases.
- Decide whether to execute NOW or wait for the next model update.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from data.markets.base import Market
from pipeline.stage3_edge import EdgeResult

logger = logging.getLogger(__name__)

# Minutes since last full-hour when each model updates (UTC)
# GFS:  every 6 h at 00Z, 06Z, 12Z, 18Z  → use "minutes since last multiple of 6h"
# ECMWF: every 12 h at 00Z, 12Z
# NAM:  every 6 h at 00Z, 06Z, 12Z, 18Z
# HRRR: every 1 h

_HIGH_INFO_WINDOW_MINUTES = 45   # minutes after a model update that count as "fresh"


def _minutes_since_last_update(now_utc: datetime, interval_hours: float) -> float:
    """Return how many minutes have elapsed since the last model update."""
    total_minutes = now_utc.hour * 60 + now_utc.minute
    interval_minutes = interval_hours * 60
    return total_minutes % interval_minutes


@dataclass
class TimingResult:
    """
    Output of the timing stage.

    execute_now       : True if the bot should trade immediately
    wait_reason       : human-readable reason for waiting (if execute_now=False)
    in_info_window    : True if we're within _HIGH_INFO_WINDOW_MINUTES of a model update
    adjusted_threshold: edge threshold adjusted for time-to-resolution decay
    min_since_update  : minutes elapsed since the last HRRR update (as proxy)
    """
    execute_now: bool
    wait_reason: str
    in_info_window: bool
    adjusted_threshold: float
    min_since_update: float


class Stage5Timing:
    """
    Decides whether to execute a trade now or wait.
    """

    def __init__(
        self,
        base_edge_threshold: float = 0.05,
    ) -> None:
        self._base_threshold = base_edge_threshold

    def run(
        self,
        market: Market,
        edge_result: EdgeResult,
        now_utc: Optional[datetime] = None,
    ) -> TimingResult:
        now_utc = now_utc or datetime.now(timezone.utc)

        # ── Time-to-resolution decay ───────────────────────────────────────
        # As resolution approaches, we need higher confidence because
        # the remaining uncertainty is proportionally larger.
        # Decay function:  threshold = base + decay_extra * exp(-hours / tau)
        # This means threshold rises as hours_to_resolution shrinks.
        hours_left = market.hours_to_resolution
        decay_extra = 0.04   # maximum additional threshold from decay
        tau = 12.0           # hours at which extra threshold = decay_extra / e
        import math
        adjusted_threshold = self._base_threshold + decay_extra * math.exp(-hours_left / tau)

        # ── Information-flow window check ──────────────────────────────────
        # HRRR updates every hour → check minutes since last hour mark
        min_since_hrrr = _minutes_since_last_update(now_utc, interval_hours=1.0)
        min_since_gfs = _minutes_since_last_update(now_utc, interval_hours=6.0)

        # We're in a high-info window if ANY major model just updated
        in_info_window = (
            min_since_hrrr <= _HIGH_INFO_WINDOW_MINUTES
            or min_since_gfs <= _HIGH_INFO_WINDOW_MINUTES
        )

        # ── Decision logic ─────────────────────────────────────────────────
        net_edge = edge_result.net_edge

        # Always trade immediately if edge is large enough regardless of timing
        if net_edge >= adjusted_threshold + 0.05:
            return TimingResult(
                execute_now=True,
                wait_reason="",
                in_info_window=in_info_window,
                adjusted_threshold=adjusted_threshold,
                min_since_update=min_since_hrrr,
            )

        # Marginal edge: only execute in high-info window
        if net_edge >= adjusted_threshold:
            if in_info_window:
                reason = ""
                execute = True
            else:
                reason = (
                    f"Marginal edge ({net_edge:.2%}) – waiting for next model update "
                    f"({min_since_hrrr:.0f} min since last HRRR refresh)"
                )
                execute = False
        else:
            reason = (
                f"Edge {net_edge:.2%} below adjusted threshold {adjusted_threshold:.2%} "
                f"({hours_left:.0f}h to resolution)"
            )
            execute = False

        logger.info(
            "Stage5: market=%s execute=%s edge=%.3f threshold=%.3f info_window=%s",
            market.market_id, execute, net_edge, adjusted_threshold, in_info_window,
        )
        return TimingResult(
            execute_now=execute,
            wait_reason=reason,
            in_info_window=in_info_window,
            adjusted_threshold=adjusted_threshold,
            min_since_update=min_since_hrrr,
        )
