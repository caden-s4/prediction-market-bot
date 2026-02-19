"""
Stage 6 – Continuous Re-evaluation of Open Positions.

Inputs  : List of open positions + current forecasts + portfolio state
Outputs : List of ReevaluationDecision (HOLD / EXIT / REBALANCE)

Logic (from spec):
- Re-run probability estimation on all open positions
- If edge has gone negative → signal EXIT
- If better opportunity exists and at max exposure → signal REBALANCE (exit
  lower-edge position to fund higher-edge one)
- Track opportunity cost vs remaining edge
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from data.markets.base import Market, Order, OrderBook, Side
from data.weather.aggregator import ConsensusForecast
from pipeline.stage2_market import Stage2Market
from pipeline.stage3_edge import EdgeResult, Stage3Edge

logger = logging.getLogger(__name__)


class ReevalAction(str, Enum):
    HOLD = "HOLD"
    EXIT = "EXIT"
    REBALANCE = "REBALANCE"   # exit this, reallocate to better opportunity


@dataclass
class ReevaluationDecision:
    """Decision for a single open position."""
    order: Order
    market: Market
    action: ReevalAction
    current_edge: float
    original_edge: float
    reason: str
    metadata: dict = field(default_factory=dict)


class Stage6Reevaluation:
    """
    Continuously monitors open positions and generates exit / rebalance signals.
    """

    def __init__(
        self,
        min_edge_to_hold: float = 0.0,   # exit if edge drops below this
        rebalance_improvement: float = 0.03,  # only swap if new edge is this much better
    ) -> None:
        self._min_edge_to_hold = min_edge_to_hold
        self._rebalance_delta = rebalance_improvement
        self._stage2 = Stage2Market()
        self._stage3 = Stage3Edge(min_edge_threshold=min_edge_to_hold)

    def run(
        self,
        open_positions: List[Order],
        position_markets: Dict[str, Market],
        position_forecasts: Dict[str, Optional[ConsensusForecast]],
        order_books: Dict[str, OrderBook],
        original_edges: Dict[str, float],
        best_new_edge: float = 0.0,
        at_max_exposure: bool = False,
    ) -> List[ReevaluationDecision]:
        """
        Parameters
        ----------
        open_positions     : current open orders from portfolio
        position_markets   : market_id → Market for each open position
        position_forecasts : market_id → ConsensusForecast (freshly fetched)
        order_books        : market_id → OrderBook (current)
        original_edges     : market_id → edge at entry
        best_new_edge      : the highest edge seen in new opportunities this cycle
        at_max_exposure    : True if total exposure is at the ceiling

        Returns
        -------
        List of ReevaluationDecision – caller handles the actual order cancellation.
        """
        decisions: List[ReevaluationDecision] = []

        for position in open_positions:
            mid = position.market_id
            market = position_markets.get(mid)
            if not market:
                logger.warning("Stage6: market %s not found – skipping", mid)
                continue

            forecast = position_forecasts.get(mid)
            ob = order_books.get(mid)
            original_edge = original_edges.get(mid, 0.0)

            if forecast is None or ob is None:
                logger.warning("Stage6: missing forecast/ob for %s – HOLDing", mid)
                decisions.append(ReevaluationDecision(
                    order=position, market=market,
                    action=ReevalAction.HOLD,
                    current_edge=original_edge, original_edge=original_edge,
                    reason="Missing fresh data – holding conservatively",
                ))
                continue

            # Re-run Stage 2 + Stage 3 with fresh data
            analysis = self._stage2.run(
                market=market,
                order_book=ob,
                our_prob=forecast.consensus_prob,
                target_size_usd=position.size_usd,
            )
            edge_result = self._stage3.run(
                forecast=forecast,
                analysis=analysis,
                platform=market.platform,
            )

            if edge_result is None:
                action = ReevalAction.HOLD
                current_edge = original_edge
                reason = "Could not recalculate edge – holding"
            else:
                current_edge = edge_result.net_edge

                if current_edge < self._min_edge_to_hold:
                    action = ReevalAction.EXIT
                    reason = (
                        f"Edge decayed to {current_edge:.2%} "
                        f"(below hold threshold {self._min_edge_to_hold:.2%})"
                    )
                elif at_max_exposure and best_new_edge > current_edge + self._rebalance_delta:
                    action = ReevalAction.REBALANCE
                    reason = (
                        f"Rebalancing: new opportunity edge {best_new_edge:.2%} "
                        f"vs current {current_edge:.2%} "
                        f"(delta={best_new_edge - current_edge:.2%} ≥ {self._rebalance_delta:.2%})"
                    )
                else:
                    action = ReevalAction.HOLD
                    reason = f"Edge holding at {current_edge:.2%} – no action needed"

            logger.info(
                "Stage6: market=%s action=%s current_edge=%.3f original=%.3f",
                mid, action, current_edge, original_edge,
            )
            decisions.append(ReevaluationDecision(
                order=position,
                market=market,
                action=action,
                current_edge=current_edge,
                original_edge=original_edge,
                reason=reason,
            ))

        return decisions
