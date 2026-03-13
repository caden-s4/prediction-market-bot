"""
resolution.gap_detector – detects probability mispricings.

Two signal types:

1. Cross-platform gap
   Same real-world event is listed on both Polymarket and Kalshi.
   If |poly_yes - kalshi_yes| > min_gap after fees → flag both markets.
   Action: buy the underpriced side on the lagging platform.

2. Single-platform information signal
   A market on one platform has a ground truth probability that differs
   significantly from its current price. Detected after ground truth fetch.
   Action: buy the correct side on the single platform.

Fee-adjusted gap calculation:
  effective_gap = raw_gap - taker_fee_poly - taker_fee_kalshi
  (or just taker_fee_poly for single-platform signals)

Time-adjusted minimum gap:
  min_gap = min(BASE_GAP_THRESHOLD + hours_to_resolution × TIME_GAP_PREMIUM, MAX_MIN_GAP)
  BASE_GAP_THRESHOLD = 0.04  (4% floor — minimum gap at any horizon)
  TIME_GAP_PREMIUM   = 0.030 (extra 3.0% per hour remaining)
  MAX_MIN_GAP        = 0.25  (cap — near-certain signals must never be blocked)

  Examples:
    4 h remaining  → min(0.04 + 4.00 × 0.030, 0.25) = 0.160  (16.0%)
    1 h remaining  → min(0.04 + 1.00 × 0.030, 0.25) = 0.070  ( 7.0%)
    15 min (0.25h) → min(0.04 + 0.25 × 0.030, 0.25) = 0.048  ( 4.8%)
    15 h remaining → min(0.04 + 15.0 × 0.030, 0.25) = 0.250  (25.0%) ← capped

  Rationale: the closer to resolution, the less time for the world to change.
  A 9.5% gap at 3.8 hours is not the same edge as a 9.5% gap at 30 minutes.
  The cap prevents the formula from blocking near-certain signals (e.g. gap=99.5%)
  on long-duration markets where hours_left × 0.030 would otherwise exceed 25%.
  Early entries require substantially larger mispricings to justify the time risk.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from data.ground_truth.base import GroundTruthResult, SourceType
from data.ground_truth.cross_platform import CrossPlatformSource
from data.markets.base import Market
from shared.fee_cache import FeeCache

logger = logging.getLogger(__name__)

# Time-adjusted minimum gap: min_gap = BASE_GAP_THRESHOLD + hours × TIME_GAP_PREMIUM
BASE_GAP_THRESHOLD = 0.04    # 4% floor — applies even at t=0
TIME_GAP_PREMIUM   = 0.030   # additional 3.0% required per hour of remaining time
MAX_MIN_GAP        = 0.25    # cap — near-certain signals must not be blocked by duration

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

    # Liquidity ratio for cross-platform signals.
    # Populated by the executor after fetching the live order book, before the
    # confidence gate.  Value: shallower-side book depth / max position size,
    # capped at 1.0.  None = not yet computed; confidence scorer skips penalty.
    depth_ratio: Optional[float] = None

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

    # If detect_information_signal() hasn't been called for this many seconds,
    # treat the next call as the start of a new batch and flush the previous
    # cycle's accumulated stats.  Must be shorter than TIER_1_INTERVAL (15s)
    # but longer than the per-market stagger (~0.25s) so intra-batch gaps
    # don't accidentally flush mid-cycle.
    _BATCH_GAP_S: float = 3.0

    def __init__(
        self,
        fee_cache: FeeCache,
        base_gap: float = BASE_GAP_THRESHOLD,
        force_test: bool = False,
    ) -> None:
        self._fee_cache = fee_cache
        self._base_gap = base_gap
        self._force_test = force_test
        # Signal cache for the fuzzy cross-platform scan.
        # The gap evaluation loop (K Kalshi × P Poly) dominates cycle time:
        # build_pairs() is O(K×P) SequenceMatcher work, and the fee-cache
        # lookups for matched pairs add further latency on cold starts.
        # We cache the computed signal list with a monotonic-clock TTL that
        # matches _PAIR_CACHE_TTL (8 h).  On cache-hit cycles the function
        # returns in O(1) with a single float comparison — no loop, no I/O.
        # Signals are stale by at most 8 h, which is fine: _try_execute()
        # re-validates live order-book prices before any real placement.
        _FUZZY_SIGNAL_CACHE_TTL = 8 * 3600  # seconds — must match _PAIR_CACHE_TTL
        self._fuzzy_signal_cache_ttl: float = _FUZZY_SIGNAL_CACHE_TTL
        self._fuzzy_signals_cache: List[GapSignal] = []
        self._fuzzy_signals_cached_at: float = 0.0  # time.monotonic() stamp

        # ── Per-cycle signal stats (accumulated across detect_information_signal calls)
        # Flushed to a summary log at the start of the next batch, detected by
        # a gap of > _BATCH_GAP_S seconds since the last call.
        self._cycle_actionable: int = 0
        self._cycle_blocked: int = 0
        self._cycle_near_miss: int = 0   # blocked but within 20% of threshold
        self._last_info_signal_at: float = 0.0  # monotonic timestamp of last call

    # ── Cross-platform detection ──────────────────────────────────────────────

    def detect_cross_platform(
        self, pairs: List[Tuple[Market, Market]]
    ) -> List[GapSignal]:
        """
        For each (poly, kalshi) pair, compute the fee-adjusted gap and flag
        any that exceed the time-adjusted minimum gap threshold.

        Returns signals sorted by effective_gap descending.
        """
        signals: List[GapSignal] = []

        for poly, kalshi in pairs:
            signal = self._evaluate_pair(poly, kalshi)
            if signal:
                signals.append(signal)

        signals.sort(key=lambda s: -s.effective_gap)
        logger.info(
            "GapDetector: %d/%d cross-platform pairs exceed time-adjusted gap threshold",
            len(signals), len(pairs),
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
        # ── Batch boundary: flush previous cycle's summary ────────────────────
        # detect_information_signal() is called per-market in a tight loop.
        # A gap of > _BATCH_GAP_S since the last call means we're at the start
        # of a new scan batch — emit the previous cycle's accumulated stats now.
        now_mono = time.monotonic()
        if (
            self._last_info_signal_at > 0
            and now_mono - self._last_info_signal_at > self._BATCH_GAP_S
            and (self._cycle_actionable + self._cycle_blocked) > 0
        ):
            logger.info(
                "[SIGNAL] Cycle summary: %d actionable, %d blocked "
                "(%d near-miss within 20%% of threshold)",
                self._cycle_actionable, self._cycle_blocked, self._cycle_near_miss,
            )
            self._cycle_actionable = 0
            self._cycle_blocked = 0
            self._cycle_near_miss = 0
        self._last_info_signal_at = now_mono

        if not self._enough_time(market):
            return None

        fee = self._fee_cache.get_taker_fee(market.platform, market.market_id)
        raw_gap = abs(ground_truth_prob - market.yes_price)
        effective_gap = raw_gap - fee

        min_gap = self._time_adjusted_min_gap(market.hours_to_resolution)
        side = "YES" if ground_truth_prob > market.yes_price else "NO"

        if effective_gap < min_gap:
            shortfall = min_gap - effective_gap
            near_miss = shortfall < min_gap * 0.20
            logger.info(
                "[SIGNAL] BLOCKED %s | gt_prob=%.3f mkt_price=%.3f "
                "gap=%.3f (%.1f%%) min_gap=%.3f shortfall=%.3f | side=%s",
                market.market_id, ground_truth_prob, market.yes_price,
                effective_gap, effective_gap * 100, min_gap, shortfall, side,
            )
            self._cycle_blocked += 1
            if near_miss:
                self._cycle_near_miss += 1
            return None

        _uncapped_gap = self._base_gap + market.hours_to_resolution * TIME_GAP_PREMIUM
        _cap_note = f" capped@{MAX_MIN_GAP:.2f}" if _uncapped_gap > MAX_MIN_GAP else ""
        reasoning = (
            f"Information signal: market_price={market.yes_price:.3f} "
            f"ground_truth={ground_truth_prob:.3f} raw_gap={raw_gap:.3f} "
            f"taker_fee={fee:.3f} effective_gap={effective_gap:.3f} "
            f"min_gap={min_gap:.3f} "
            f"({self._base_gap:.2f}+{market.hours_to_resolution:.2f}h\u00d7{TIME_GAP_PREMIUM:.3f}{_cap_note})"
        )
        logger.info(
            "[SIGNAL] ACTIONABLE %s | gt_prob=%.3f mkt_price=%.3f "
            "gap=%.3f (%.1f%%) min_gap=%.3f | side=%s",
            market.market_id, ground_truth_prob, market.yes_price,
            effective_gap, effective_gap * 100, min_gap, side,
        )
        logger.info("GapDetector: %s – %s", market.market_id, reasoning)
        self._cycle_actionable += 1

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

        hours = min(poly.hours_to_resolution, kalshi.hours_to_resolution)
        min_gap = self._time_adjusted_min_gap(hours)
        if effective_gap < min_gap:
            logger.debug(
                "GapDetector: pair %s/%s eff_gap=%.3f below threshold %.3f "
                "(%.2fh remaining)",
                poly.market_id, kalshi.market_id, effective_gap, min_gap, hours,
            )
            return None

        _uncapped_cross = self._base_gap + hours * TIME_GAP_PREMIUM
        _cap_note_cross = f" capped@{MAX_MIN_GAP:.2f}" if _uncapped_cross > MAX_MIN_GAP else ""
        reasoning = (
            f"Cross-platform gap: poly={poly.yes_price:.3f} kalshi={kalshi.yes_price:.3f} "
            f"raw_gap={raw_gap:.3f} lagging_fee={lagging_fee:.3f} "
            f"effective_gap={effective_gap:.3f} ({platform_label}) "
            f"min_gap={min_gap:.3f} "
            f"({self._base_gap:.2f}+{hours:.2f}h\u00d7{TIME_GAP_PREMIUM:.3f}{_cap_note_cross})"
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

    def _time_adjusted_min_gap(self, hours: float) -> float:
        """
        Return the minimum effective gap required given hours to resolution.

        min_gap = min(base_gap + hours × TIME_GAP_PREMIUM, MAX_MIN_GAP)

        The further from resolution, the larger the gap must be to justify
        entering: early positions have more time to go wrong.  The cap at
        MAX_MIN_GAP (0.25) ensures that near-certain signals (e.g. gas price
        gap=99.5%) are never blocked purely because the market has many hours
        left — the formula would otherwise demand min_gap=0.499 at 15.3h.

        In force-test mode the threshold is overridden to 1% so almost any
        gap passes — this exists solely for end-to-end pipeline validation
        and must never be used in production.
        """
        if self._force_test:
            min_gap = 0.01  # 1% — let almost everything through for testing
            logger.debug("[FORCE-TEST] min_gap overridden to %.2f", min_gap)
            return min_gap
        return min(self._base_gap + hours * TIME_GAP_PREMIUM, MAX_MIN_GAP)

    def _enough_time(self, market: Market) -> bool:
        """Return False if market is resolving too soon to safely trade."""
        return market.hours_to_resolution >= MIN_HOURS_TO_RESOLUTION

    # ── Fuzzy cross-platform scan ─────────────────────────────────────────────

    def run_cross_platform_scan(
        self,
        kalshi_markets: List[Market],
        polymarket_markets: List[Market],
    ) -> List[GapSignal]:
        """
        Discover cross-platform mispricings using fuzzy title matching.

        Rebuilds the Kalshi↔Polymarket pairs cache if it is stale (empty or
        older than 30 minutes).  Each matched pair is evaluated for a gap; any
        pair exceeding the time-adjusted minimum threshold produces a GapSignal.

        Signals are returned as BUY on the Kalshi market (we always trade the
        less-efficient Kalshi side).  Confidence = POLYMARKET_AS_GT_CONFIDENCE
        × similarity ≈ 0.56–0.78, which sits below the 0.80 auto-trade gate so
        these appear as ghost signals until pair accuracy is validated.
        """
        if not kalshi_markets or not polymarket_markets:
            return []

        # ── Signal cache fast-path ────────────────────────────────────────────
        # A single float comparison.  If the cache is warm, return immediately
        # — no pair rebuild check, no loop, no I/O.  This is the path taken on
        # every normal cycle between discovery rebuilds.
        now = time.monotonic()
        if now - self._fuzzy_signals_cached_at < self._fuzzy_signal_cache_ttl:
            logger.debug(
                "GapDetector[cross]: cache hit — returning %d signal(s) "
                "(%.0fs remaining in %dh window)",
                len(self._fuzzy_signals_cache),
                self._fuzzy_signal_cache_ttl - (now - self._fuzzy_signals_cached_at),
                int(self._fuzzy_signal_cache_ttl / 3600),
            )
            return self._fuzzy_signals_cache

        # ── Cache miss: rebuild pairs if stale, then re-evaluate gaps ────────
        # Lazy-initialise
        if not hasattr(self, "_cross_platform"):
            self._cross_platform = CrossPlatformSource()

        # build_pairs() is the expensive O(K×P) SequenceMatcher step.
        # needs_rebuild() is a pure in-memory TTL check (no I/O).
        if self._cross_platform.needs_rebuild():
            self._cross_platform.build_pairs(kalshi_markets, polymarket_markets)

        pm_by_id: Dict[str, Market] = {m.market_id: m for m in polymarket_markets}
        signals: List[GapSignal] = []

        for km in kalshi_markets:
            if not self._enough_time(km):
                continue

            pm_prob, confidence = self._cross_platform.get_probability(km, pm_by_id)
            if pm_prob is None:
                continue

            raw_gap = abs(km.yes_price - pm_prob)
            if raw_gap <= 0:
                continue

            fee = self._fee_cache.get_taker_fee("kalshi", km.market_id)
            effective_gap = raw_gap - fee

            min_gap = self._time_adjusted_min_gap(km.hours_to_resolution)
            if effective_gap < min_gap:
                shortfall = min_gap - effective_gap
                side = "YES" if pm_prob > km.yes_price else "NO"
                logger.info(
                    "[SIGNAL] BLOCKED %s | gt_prob=%.3f mkt_price=%.3f "
                    "gap=%.3f (%.1f%%) min_gap=%.3f shortfall=%.3f | side=%s | cross_platform",
                    km.market_id, pm_prob, km.yes_price,
                    effective_gap, effective_gap * 100, min_gap, shortfall, side,
                )
                continue

            if pm_prob > km.yes_price:
                action_label = "buy_yes"
                reference_price = pm_prob
            else:
                action_label = "buy_no"
                reference_price = pm_prob

            gt_result = GroundTruthResult(
                ground_truth_prob=pm_prob,
                confidence=confidence,
                source_type=SourceType.AGGREGATED,
                source_name=f"Polymarket/cross-platform",
                reasoning=(
                    f"Cross-platform: kalshi={km.yes_price:.3f} "
                    f"polymarket={pm_prob:.3f} raw_gap={raw_gap:.3f} "
                    f"effective_gap={effective_gap:.3f} confidence={confidence:.2f}"
                ),
            )

            reasoning = (
                f"Cross-platform (fuzzy): kalshi={km.yes_price:.3f} "
                f"polymarket={pm_prob:.3f} raw_gap={raw_gap:.3f} "
                f"fee={fee:.3f} effective_gap={effective_gap:.3f} "
                f"min_gap={min_gap:.3f} confidence={confidence:.2f}"
            )
            logger.info(
                "[SIGNAL] ACTIONABLE %s | gt_prob=%.3f mkt_price=%.3f "
                "gap=%.3f (%.1f%%) min_gap=%.3f | side=%s | cross_platform",
                km.market_id, pm_prob, km.yes_price,
                effective_gap, effective_gap * 100, min_gap,
                "YES" if pm_prob > km.yes_price else "NO",
            )
            logger.info(
                "GapDetector[cross]: FLAGGED %s — %s", km.market_id, reasoning
            )

            signals.append(GapSignal(
                signal_type="cross_platform",
                market_to_buy=km,
                market_reference=None,
                target_price=km.yes_price,
                reference_price=reference_price,
                ground_truth_prob=pm_prob,
                raw_gap=raw_gap,
                effective_gap=effective_gap,
                taker_fee=fee,
                ground_truth_result=gt_result,
                reasoning=reasoning,
            ))

        signals.sort(key=lambda s: -s.effective_gap)
        logger.info(
            "GapDetector[cross]: %d fuzzy cross-platform signal(s) found "
            "from %d Kalshi × %d Polymarket markets",
            len(signals), len(kalshi_markets), len(polymarket_markets),
        )

        # ── Store in signal cache ─────────────────────────────────────────────
        # Stamp the cache with the current monotonic time.  All cycles within
        # the next _fuzzy_signal_cache_ttl seconds will hit the fast-path above.
        self._fuzzy_signals_cache = signals
        self._fuzzy_signals_cached_at = time.monotonic()

        return signals
