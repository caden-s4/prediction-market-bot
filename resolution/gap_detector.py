"""
resolution.gap_detector – detects probability mispricings.

Two signal types:

1. Cross-platform gap
   Same real-world event is listed on both Polymarket and Kalshi.
   If |poly_yes - kalshi_yes| > MIN_GAP after fees → flag both markets.
   Action: buy the underpriced side on the lagging platform.

2. Single-platform information signal
   A market on one platform has a ground truth probability that differs
   significantly from its current price. Detected after ground truth fetch.
   Action: buy the correct side on the single platform.

Fee-adjusted gap calculation:
  effective_gap = raw_gap - taker_fee_poly - taker_fee_kalshi
  (or just taker_fee_poly for single-platform signals)
  Only flag if effective_gap > MIN_GAP_THRESHOLD (default 4%).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from data.ground_truth.base import GroundTruthResult
from data.markets.base import Market
from shared.fee_cache import FeeCache

logger = logging.getLogger(__name__)

# Minimum probability gap after fees to flag (strategy spec: 4%)
MIN_GAP_THRESHOLD = 0.04

# Minimum hours remaining to act (avoid resolution chaos in last few minutes)
MIN_HOURS_TO_RESOLUTION = 0.25  # 15 minutes


@dataclass
class GapSignal:
    """A detected mispricing opportunity."""

    signal_type: str               # "cross_platform" | "information"
    market_to_buy: Market          # the underpriced market to trade
    market_reference: Optional[Market]  # the other platform market (cross-platform only)

    # Prices
    target_price: float            # current YES price on the lagging platform
    reference_price: float         # correct/reference YES price
    ground_truth_prob: Optional[float] = None  # from hard data source (if available)

    # Gap metrics
    raw_gap: float = 0.0           # |reference - target|
    effective_gap: float = 0.0     # raw_gap minus fees
    taker_fee: float = 0.0         # fee on the market_to_buy side

    # Full ground truth result – preserved from the data source so the
    # executor can use the real source confidence rather than re-computing it.
    ground_truth_result: Optional[GroundTruthResult] = None

    reasoning: str = ""

    @property
    def action(self) -> str:
        """Should we buy YES or NO on the target market?"""
        if self.reference_price > self.target_price:
            return "buy_yes"  # target is underpriced vs reference → buy YES
        return "buy_no"       # target is overpriced → buy NO (sell YES)


class GapDetector:
    """
    Detects cross-platform and information-based mispricings.
    """

    def __init__(
        self,
        fee_cache: FeeCache,
        min_gap: float = MIN_GAP_THRESHOLD,
    ) -> None:
        self._fee_cache = fee_cache
        self._min_gap = min_gap

    # ── Cross-platform detection ──────────────────────────────────────────────

    def detect_cross_platform(
        self, pairs: List[Tuple[Market, Market]]
    ) -> List[GapSignal]:
        """
        For each (poly, kalshi) pair, compute the fee-adjusted gap and flag
        any that exceed MIN_GAP_THRESHOLD.

        Returns signals sorted by effective_gap descending.
        """
        signals: List[GapSignal] = []

        for poly, kalshi in pairs:
            signal = self._evaluate_pair(poly, kalshi)
            if signal:
                signals.append(signal)

        signals.sort(key=lambda s: -s.effective_gap)
        logger.info(
            "GapDetector: %d/%d cross-platform pairs exceed %.1f%% gap",
            len(signals), len(pairs), self._min_gap * 100,
        )
        return signals

    # ── Information signal detection ──────────────────────────────────────────

    def detect_information_signal(
        self,
        market: Market,
        ground_truth_prob: float,
    ) -> Optional[GapSignal]:
        """
        For a single market, compare its current price to the ground truth
        probability. Flag if the gap (after fees) exceeds the threshold.
        """
        if not self._enough_time(market):
            return None

        fee = self._fee_cache.get_taker_fee(market.platform, market.market_id)
        raw_gap = abs(ground_truth_prob - market.yes_price)
        effective_gap = raw_gap - fee

        if effective_gap < self._min_gap:
            logger.debug(
                "GapDetector: info signal below threshold for %s (eff_gap=%.3f)",
                market.market_id, effective_gap,
            )
            return None

        reasoning = (
            f"Information signal: market_price={market.yes_price:.3f} "
            f"ground_truth={ground_truth_prob:.3f} raw_gap={raw_gap:.3f} "
            f"taker_fee={fee:.3f} effective_gap={effective_gap:.3f}"
        )
        logger.info("GapDetector: %s – %s", market.market_id, reasoning)

        return GapSignal(
            signal_type="information",
            market_to_buy=market,
            market_reference=None,
            target_price=market.yes_price,
            reference_price=ground_truth_prob,
            ground_truth_prob=ground_truth_prob,
            raw_gap=raw_gap,
            effective_gap=effective_gap,
            taker_fee=fee,
            reasoning=reasoning,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _evaluate_pair(
        self, poly: Market, kalshi: Market
    ) -> Optional[GapSignal]:
        if not self._enough_time(poly) or not self._enough_time(kalshi):
            return None

        raw_gap = abs(poly.yes_price - kalshi.yes_price)
        if raw_gap <= 0:
            return None

        fee_poly = self._fee_cache.get_taker_fee("polymarket", poly.market_id)
        fee_kalshi = self._fee_cache.get_taker_fee("kalshi", kalshi.market_id)

        # Determine which platform lags (lower price = underpriced YES = buy).
        # We only trade the lagging platform (hold to resolution, no hedge leg),
        # so only the lagging platform's taker fee applies to the effective gap.
        if poly.yes_price < kalshi.yes_price:
            lagging, reference = poly, kalshi
            lagging_fee = fee_poly
            platform_label = "polymarket lags kalshi"
        else:
            lagging, reference = kalshi, poly
            lagging_fee = fee_kalshi
            platform_label = "kalshi lags polymarket"

        effective_gap = raw_gap - lagging_fee

        if effective_gap < self._min_gap:
            logger.debug(
                "GapDetector: pair %s/%s eff_gap=%.3f below threshold",
                poly.market_id, kalshi.market_id, effective_gap,
            )
            return None

        reasoning = (
            f"Cross-platform gap: poly={poly.yes_price:.3f} kalshi={kalshi.yes_price:.3f} "
            f"raw_gap={raw_gap:.3f} lagging_fee={lagging_fee:.3f} "
            f"effective_gap={effective_gap:.3f} ({platform_label})"
        )
        logger.info(
            "GapDetector: FLAGGED %s/%s – %s",
            poly.market_id, kalshi.market_id, reasoning,
        )

        return GapSignal(
            signal_type="cross_platform",
            market_to_buy=lagging,
            market_reference=reference,
            target_price=lagging.yes_price,
            reference_price=reference.yes_price,
            raw_gap=raw_gap,
            effective_gap=effective_gap,
            taker_fee=lagging_fee,
            reasoning=reasoning,
        )

    def _enough_time(self, market: Market) -> bool:
        """Return False if market is resolving too soon to safely trade."""
        return market.hours_to_resolution >= MIN_HOURS_TO_RESOLUTION
