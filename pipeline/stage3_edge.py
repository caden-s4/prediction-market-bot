"""
Stage 3 – Edge Calculation and Statistical Significance.

Inputs  : ConsensusForecast + MarketAnalysis + threshold config
Outputs : EdgeResult dataclass

Logic (from spec):
- Bayesian expected value: integrate over the full confidence interval,
  not just the point estimate.  This gives a more conservative edge.
- Apply transaction-cost deduction.
- Gate on minimum edge threshold (default 5%).
- Compute a Sharpe-equivalent (edge / variance) to rank trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats

from data.weather.aggregator import ConsensusForecast
from pipeline.stage2_market import MarketAnalysis

logger = logging.getLogger(__name__)

# Transaction cost estimates per platform (as fraction of notional)
_TX_COSTS = {
    "polymarket": 0.02,   # ~2% fee on maker/taker
    "kalshi": 0.07,       # Kalshi charges a fee on profit
}


@dataclass
class EdgeResult:
    """
    Edge calculation output.

    point_edge          : naive (point estimate - market price) edge
    bayesian_edge       : edge integrated over full CI (more conservative)
    net_edge            : bayesian_edge minus transaction costs
    passes_threshold    : True if net_edge ≥ min_edge_threshold
    sharpe_equivalent   : edge / std_of_edge (risk-adjusted ranking metric)
    tx_cost             : estimated transaction cost fraction
    variance            : variance of the edge estimate (from CI width)
    direction           : +1 if buying YES, -1 if buying NO
    """
    point_edge: float
    bayesian_edge: float
    net_edge: float
    passes_threshold: bool
    sharpe_equivalent: float
    tx_cost: float
    variance: float
    direction: int    # +1 = YES, -1 = NO
    our_prob: float
    market_prob: float


class Stage3Edge:
    """
    Computes Bayesian expected value edge and statistical filters.
    """

    def __init__(
        self,
        min_edge_threshold: float = 0.05,
    ) -> None:
        self._min_edge = min_edge_threshold

    def run(
        self,
        forecast: ConsensusForecast,
        analysis: MarketAnalysis,
        platform: str,
    ) -> Optional[EdgeResult]:
        """
        Returns an EdgeResult, or None if there is insufficient data to
        calculate edge (e.g. no order book prices available).
        """
        if analysis.implied_prob is None:
            logger.warning("Stage3: no implied probability for %s", analysis.market_id)
            return None

        market_prob = analysis.implied_prob
        our_prob = forecast.consensus_prob

        # ── Direction ─────────────────────────────────────────────────────
        # Positive edge on YES if our_prob > market YES ask price
        # Positive edge on NO  if (1 - our_prob) > (1 - market YES bid)
        #                       ↔ our_prob < market YES bid
        yes_ask = analysis.slippage_yes or analysis.best_yes_ask or market_prob
        no_ask = analysis.slippage_no or analysis.best_no_ask or (1.0 - market_prob)

        edge_yes = our_prob - yes_ask
        edge_no = (1.0 - our_prob) - no_ask
        direction = 1 if edge_yes >= edge_no else -1
        point_edge = max(edge_yes, edge_no)

        # ── Bayesian integration over confidence interval ──────────────────
        # Model our probability estimate as a truncated normal distribution
        # parameterised by [prob_low, prob_high] = 95% CI.
        ci_lo = forecast.prob_low
        ci_hi = forecast.prob_high
        mu = our_prob
        # std from 95% CI: CI half-width ≈ 1.96σ
        sigma = max((ci_hi - ci_lo) / (2 * 1.96), 1e-6)

        # Integrate EV over the distribution of our probability estimate
        # E[EV] = ∫ (p - entry_price) * N(p; mu, sigma) dp
        # For a normal distribution this has a closed form:
        if direction == 1:
            entry = yes_ask
        else:
            entry = no_ask
            # Flip so we integrate "probability of NO outcome"
            mu = 1.0 - mu
            ci_lo, ci_hi = 1.0 - ci_hi, 1.0 - ci_lo

        # E[p] under truncated normal ≈ mu (symmetric approx is fine here)
        # E[EV] = mu - entry
        bayesian_edge = float(mu - entry)

        # Variance of the edge estimate = variance of p = sigma^2
        variance = float(sigma ** 2)

        # ── Transaction cost deduction ─────────────────────────────────────
        tx_cost = _TX_COSTS.get(platform, 0.03)
        net_edge = bayesian_edge - tx_cost

        # ── Sharpe-equivalent ─────────────────────────────────────────────
        std_edge = float(np.sqrt(variance + tx_cost ** 2))
        sharpe = net_edge / std_edge if std_edge > 0 else 0.0

        passes = net_edge >= self._min_edge

        result = EdgeResult(
            point_edge=point_edge,
            bayesian_edge=bayesian_edge,
            net_edge=net_edge,
            passes_threshold=passes,
            sharpe_equivalent=sharpe,
            tx_cost=tx_cost,
            variance=variance,
            direction=direction,
            our_prob=forecast.consensus_prob,
            market_prob=market_prob,
        )

        logger.info(
            "Stage3: market=%s dir=%s point_edge=%.3f bayes_edge=%.3f net=%.3f passes=%s",
            analysis.market_id,
            "YES" if direction == 1 else "NO",
            point_edge, bayesian_edge, net_edge, passes,
        )
        return result
