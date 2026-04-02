"""
resolution.confidence – two-dimensional confidence scorer for resolution drift trades.

Before firing on any signal, score it on two dimensions:

1. Source confidence (0-1)
   How authoritative is the data source?
   - 0.9+  Official API returning a final result (live score, published release)
   - 0.8+  Government/regulatory primary document
   - 0.5+  Aggregated secondary structured sources
   - < 0.5 News / interpretation – skip

   A freshness multiplier is applied based on how long ago the data was
   published (ground_truth.data_published_at):
     < 15 min  → 1.00 (full score)
     15–60 min → 0.90
     1–2 hrs   → 0.80
     > 2 hrs   → 0.75
   Stale data has had time to be priced in by other traders; the edge is
   likely already gone. If data_published_at is None, no penalty is applied.

   For cross-platform signals without ground truth, source confidence is
   estimated from the gap size, with a liquidity penalty applied when
   signal.depth_ratio is set:
     0.70 + min(gap * 2, 0.15) - max(0, 0.10 * (1 - depth_ratio))

2. Resolution clarity (0-1)
   How unambiguous is the market's resolution criteria given the data?
   - 1.0  Binary YES/NO with no room for interpretation
   - 0.8  Clear criteria, minor edge case risk
   - 0.5  Some interpretation required
   - 0.0  Vague or subjective

   Clarity is scored via a category/tag lookup first (most reliable for
   common market types), falling back to regex patterns.

ONLY trade if BOTH scores >= 0.80. One weak dimension = skip.

Combined floor check: if both scores pass the 0.80 gate but are both
below 0.85 (marginal on both axes), requires_depth_check is set True.
The executor must verify order-book depth >= 3x intended position size.

Oracle dispute keywords – two tiers:
  ORACLE_HARD_BLOCK_KEYWORDS: cap clarity at 0.50 (will fail the gate)
  ORACLE_SOFT_CAP_KEYWORDS:   cap clarity at 0.60 (will fail the gate)

Directional confidence: if ground_truth.directional_confidence == "ambiguous",
the trade is blocked immediately regardless of other scores.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from data.ground_truth.base import GroundTruthResult, SourceType
from data.markets.base import Market
from resolution.gap_detector import GapSignal

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.80   # default; overridable via MIN_CONFIDENCE_THRESHOLD env var
MARGINAL_THRESHOLD   = 0.85   # below this on both axes → requires_depth_check

# ── Category / tag clarity lookup ─────────────────────────────────────────────
# Checked before regex. Tags are more specific, checked first.

CATEGORY_CLARITY: dict = {
    "sports":       0.95,   # winner/loser determined by scoreboard
    "sport":        0.95,
    "economics":    0.85,   # numeric threshold (CPI, GDP, rate decision)
    "economy":      0.85,
    "economic":     0.85,
    "financial":    0.80,
    "finance":      0.80,
    "legal":        0.85,   # conviction, ruling, approval
    "crypto":       0.80,   # price-threshold, machine-resolvable
    "science":      0.80,
    "politics":     0.70,   # often involves certification or interpretation
    "political":    0.70,
    "geopolitical": 0.65,
    "entertainment":0.75,
    "culture":      0.70,
}

TAG_CLARITY: dict = {
    # Sports leagues – always definitively resolved
    "nfl": 0.95, "nba": 0.95, "mlb": 0.95, "nhl": 0.95,
    "soccer": 0.95, "tennis": 0.95, "mma": 0.95, "boxing": 0.95,
    "golf": 0.95, "f1": 0.95, "formula1": 0.95,
    # Economic indicators
    "fed": 0.85, "fomc": 0.85, "cpi": 0.85, "gdp": 0.85,
    "unemployment": 0.85, "nonfarm": 0.85, "payrolls": 0.85,
    # Legal
    "conviction": 0.85, "ruling": 0.85, "verdict": 0.85,
    "legislation": 0.80, "appeal": 0.80,
    # Elections – resolution depends on certification
    "election": 0.70, "primary": 0.70,
}

# ── Regex fallbacks ────────────────────────────────────────────────────────────

HIGH_CLARITY_PATTERNS = [
    r"\bwill .+ win\b",
    r"\bwill .+ beat\b",
    r"\bwill .+ score\b",
    r"\bwill .+ reach\b",
    r"\bwill .+ exceed\b",
    r"\bwill .+ above\b",
    r"\bwill .+ below\b",
    r"\bwill .+ (be )?approved\b",
    r"\bwill .+ (be )?rejected\b",
    r"\bwill .+ (be )?signed\b",
    r"\bwill .+ (be )?enacted\b",
    r"\bwill .+ qualify\b",
    r"\bwill .+ advance\b",
]

LOW_CLARITY_PATTERNS = [
    r"\bmore than .+ times\b",
    r"\bsignificantly\b",
    r"\bsubstantially\b",
    r"\bbefore or after\b",
    r"\binterpret\b",
    r"\bconsider\b",
    r"\bsufficient\b",
    r"\bbetter than\b",
    r"\bworse than\b",
]

# ── Oracle dispute keyword tiers ───────────────────────────────────────────────

# Tier 1 – hard block: cap clarity at 0.50 (guaranteed gate failure)
ORACLE_HARD_BLOCK_KEYWORDS = [
    "popular vote",        # election share disputes are common
    "seat projection",     # probabilistic; sources disagree
    "polling average",     # not an official outcome
    "widely considered",   # inherently subjective
    "generally regarded",  # inherently subjective
    "majority of",         # ambiguous threshold unless explicitly defined
    "estimated to",        # not a final verified number
    "projected to",        # forward estimate, not resolved fact
]

# Tier 2 – soft cap: cap clarity at 0.60 (also fails gate, but logged separately)
ORACLE_SOFT_CAP_KEYWORDS = [
    "as reported by",         # introduces reporter interpretation
    "as certified by",        # depends on certifier judgment
    "as determined by",       # delegated judgment
    "according to",           # source-dependent interpretation
    "per official statement", # statement may be ambiguous or later revised
    "as announced by",        # announcement may be corrected
    "as confirmed by",        # confirmation source may be contested
    "as declared by",         # declaration may be informal
]


@dataclass
class ConfidenceScore:
    """Output of the two-dimensional confidence scorer."""
    source_confidence: float        # 0-1, after freshness multiplier
    resolution_clarity: float       # 0-1
    passes: bool                    # True iff both >= CONFIDENCE_THRESHOLD
    skip_reason: Optional[str]      # set when passes=False
    requires_depth_check: bool = False  # True when both scores pass but are both < 0.85
    freshness_multiplier: float = 1.0   # for audit logging


class ConfidenceScorer:
    """
    Scores a (market, ground_truth_result, signal) triple on source confidence
    and resolution clarity. Returns a ConfidenceScore.
    """

    def __init__(self, threshold: float = CONFIDENCE_THRESHOLD) -> None:
        self._threshold = threshold

    def score(
        self,
        market: Market,
        ground_truth: Optional[GroundTruthResult],
        signal: GapSignal,
    ) -> ConfidenceScore:
        # ── 0. Directional confidence check ───────────────────────────────
        # If the data source explicitly flags the direction as ambiguous,
        # block immediately — we don't know which side to trade.
        if (
            ground_truth is not None
            and ground_truth.directional_confidence == "ambiguous"
        ):
            logger.info(
                "ConfidenceScorer: SKIP %s – ground truth direction is ambiguous",
                market.market_id,
            )
            return ConfidenceScore(
                source_confidence=0.0,
                resolution_clarity=0.0,
                passes=False,
                skip_reason="ground truth data is directionally ambiguous",
            )

        # ── 1. Source confidence ───────────────────────────────────────────
        gt_has_signal = (
            ground_truth is not None
            and ground_truth.ground_truth_prob is not None
        )
        freshness_mult = 1.0

        if not gt_has_signal:
            if signal.signal_type == "cross_platform":
                # Estimate from gap size, discounted by thin-book liquidity.
                depth_pen = _depth_penalty(signal)
                source_conf = 0.70 + min(signal.effective_gap * 2, 0.15) - depth_pen
                source_conf = round(min(max(source_conf, 0.0), 0.85), 4)
            else:
                source_conf = 0.0
        else:
            source_conf = ground_truth.confidence
            freshness_mult = _freshness_multiplier(ground_truth)
            source_conf = round(source_conf * freshness_mult, 4)

        # ── 2. Resolution clarity ──────────────────────────────────────────
        resolution_clarity = self._score_resolution_clarity(market)

        # ── 3. Oracle dispute risk – two tiers ────────────────────────────
        q = market.question.lower()
        has_hard_dispute = any(kw in q for kw in ORACLE_HARD_BLOCK_KEYWORDS)
        has_soft_dispute = (
            not has_hard_dispute
            and any(kw in q for kw in ORACLE_SOFT_CAP_KEYWORDS)
        )

        if has_hard_dispute:
            resolution_clarity = min(resolution_clarity, 0.50)
            logger.info(
                "ConfidenceScorer: hard oracle dispute keyword in %s – "
                "capping clarity at 0.50",
                market.market_id,
            )
        elif has_soft_dispute:
            resolution_clarity = min(resolution_clarity, 0.60)
            logger.info(
                "ConfidenceScorer: soft oracle dispute keyword in %s – "
                "capping clarity at 0.60",
                market.market_id,
            )

        # ── 4. Gate ────────────────────────────────────────────────────────
        passes = (
            source_conf >= self._threshold
            and resolution_clarity >= self._threshold
        )

        # ── 5. Combined floor check ────────────────────────────────────────
        # Both dimensions pass but neither is strong — combined uncertainty
        # is meaningfully higher. Flag for executor's depth guard.
        requires_depth_check = (
            passes
            and source_conf < MARGINAL_THRESHOLD
            and resolution_clarity < MARGINAL_THRESHOLD
        )

        # ── Build skip_reason ──────────────────────────────────────────────
        skip_reason: Optional[str] = None
        if not passes:
            parts = []
            if source_conf < self._threshold:
                parts.append(
                    f"source_confidence={source_conf:.2f} < {self._threshold}"
                )
            if resolution_clarity < self._threshold:
                parts.append(
                    f"resolution_clarity={resolution_clarity:.2f} < {self._threshold}"
                )
            if has_hard_dispute:
                parts.append("hard oracle dispute keyword detected")
            elif has_soft_dispute:
                parts.append("soft oracle dispute keyword detected")
            skip_reason = "; ".join(parts)

        level = "PASS" if passes else "SKIP"
        depth_note = " [marginal-both→depth-check]" if requires_depth_check else ""
        logger.info(
            "ConfidenceScorer: %s%s %s source=%.2f clarity=%.2f freshness=%.2f%s",
            level, depth_note, market.market_id,
            source_conf, resolution_clarity, freshness_mult,
            f" – {skip_reason}" if skip_reason else "",
        )

        return ConfidenceScore(
            source_confidence=source_conf,
            resolution_clarity=resolution_clarity,
            passes=passes,
            skip_reason=skip_reason,
            requires_depth_check=requires_depth_check,
            freshness_multiplier=freshness_mult,
        )

    # ── Resolution clarity ─────────────────────────────────────────────────────

    def _score_resolution_clarity(self, market: Market) -> float:
        q = market.question.lower()

        # 1. Category/tag lookup — more reliable than regex for known market types
        cat_score = _category_based_clarity(market)
        if cat_score is not None:
            return cat_score

        # 2. Low score for vague language
        if any(re.search(p, q) for p in LOW_CLARITY_PATTERNS):
            return 0.40

        # 3. High score for clean binary patterns
        high_matches = sum(1 for p in HIGH_CLARITY_PATTERNS if re.search(p, q))
        if high_matches >= 2:
            return 0.95
        if high_matches == 1:
            return 0.85

        # 4. Explicit numeric threshold (highly unambiguous)
        if re.search(r"\b\d+(\.\d+)?\s*(%|k|m|b|billion|million|thousand)?\b", q):
            return 0.80

        # 5. Fallback
        return 0.65


# ── Module-level helpers ───────────────────────────────────────────────────────

def _freshness_multiplier(ground_truth: GroundTruthResult) -> float:
    """
    Penalise stale ground-truth data. The older the publication, the more
    likely other traders have already priced in the information.
    Returns 1.0 if data_published_at is not set (no penalty).
    """
    published_at = ground_truth.data_published_at
    if published_at is None:
        return 1.0
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_minutes = (now - published_at).total_seconds() / 60.0
    if age_minutes < 0:
        return 1.0  # clock skew – don't penalise
    if age_minutes < 15:
        return 1.00
    if age_minutes < 60:
        return 0.90
    if age_minutes < 120:
        return 0.80
    return 0.75


def _category_based_clarity(market: Market) -> Optional[float]:
    """
    Returns a clarity score from the category/tag lookup, or None if
    no match found (caller falls back to regex).
    Tags are checked first as they are more specific.
    """
    for tag in (market.tags or []):
        score = TAG_CLARITY.get(tag.lower())
        if score is not None:
            return score
    return CATEGORY_CLARITY.get(market.category.lower())


def _depth_penalty(signal: GapSignal) -> float:
    """
    Liquidity penalty for cross-platform signals where depth_ratio is known.
    Formula: max(0, 0.10 * (1 - depth_ratio))
    depth_ratio = 1.0 means deep book (no penalty); 0.0 means empty (−0.10).
    Returns 0.0 when depth_ratio has not been computed yet.
    """
    depth_ratio = getattr(signal, "depth_ratio", None)
    if depth_ratio is None:
        return 0.0
    return max(0.0, 0.10 * (1.0 - depth_ratio))
