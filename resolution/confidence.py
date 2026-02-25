"""
resolution.confidence – two-dimensional confidence scorer for resolution drift trades.

Before firing on any signal, score it on two dimensions:

1. Source confidence (0-1)
   How authoritative is the data source?
   - 0.9+  Official API returning a final result (live score, published release)
   - 0.8+  Government/regulatory primary document
   - 0.5+  Aggregated secondary structured sources
   - < 0.5 News / interpretation – skip

2. Resolution clarity (0-1)
   How unambiguous is the market's resolution criteria given the data?
   - 1.0  Binary YES/NO with no room for interpretation
   - 0.8  Clear criteria, minor edge case risk
   - 0.5  Some interpretation required
   - 0.0  Vague or subjective

ONLY trade if BOTH scores >= 0.8. One weak dimension = skip.

The biggest risk in resolution drift trading is oracle disputes: Polymarket
has contested resolutions even when the underlying fact was clear. We reduce
this by filtering aggressively on resolution clarity.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from data.ground_truth.base import GroundTruthResult, SourceType
from data.markets.base import Market
from resolution.gap_detector import GapSignal

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.80    # both dimensions must meet this

# Patterns that signal HIGH resolution clarity (binary, unambiguous)
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

# Patterns that signal LOW resolution clarity – skip these
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

# Oracle dispute risk: market question keywords that predict Polymarket oracle disputes.
# Kept deliberately narrow — only include phrases that are inherently subjective or
# that Polymarket has historically contested.  Do NOT add objective economic terms like
# "estimated", "adjusted", "revised", or "projected" — these appear in clearly-resolvable
# FRED/BLS data questions and were causing legitimate signals to be capped at 0.50.
ORACLE_DISPUTE_KEYWORDS = [
    "popular vote",        # election share disputes are common
    "seat projection",     # probabilistic; different sources disagree
    "polling average",     # not an official outcome
]


@dataclass
class ConfidenceScore:
    """Output of the two-dimensional confidence scorer."""
    source_confidence: float         # 0-1
    resolution_clarity: float        # 0-1
    passes: bool                     # True if both >= CONFIDENCE_THRESHOLD
    skip_reason: Optional[str]       # set if passes=False


class ConfidenceScorer:
    """
    Scores a (market, ground_truth_result) pair on source confidence and
    resolution clarity. Returns a ConfidenceScore.
    """

    def score(
        self,
        market: Market,
        ground_truth: Optional[GroundTruthResult],
        signal: GapSignal,
    ) -> ConfidenceScore:
        """
        Evaluate whether this trade meets the dual-confidence bar.

        Parameters
        ----------
        market       : the market we're considering trading
        ground_truth : result from GroundTruthRouter (None for cross-platform signals)
        signal       : the gap signal that flagged this market
        """
        # ── Source confidence ──────────────────────────────────────────────
        if ground_truth is None:
            # Cross-platform signal with no ground truth data
            # Confidence based purely on price divergence magnitude
            if signal.signal_type == "cross_platform":
                source_conf = 0.70 + min(signal.effective_gap * 2, 0.15)
                source_conf = round(min(source_conf, 0.85), 4)
            else:
                source_conf = 0.0
        else:
            source_conf = ground_truth.confidence

        # ── Resolution clarity ─────────────────────────────────────────────
        resolution_clarity = self._score_resolution_clarity(market)

        # ── Oracle dispute risk override ───────────────────────────────────
        q = market.question.lower()
        has_dispute_risk = any(kw in q for kw in ORACLE_DISPUTE_KEYWORDS)
        if has_dispute_risk:
            resolution_clarity = min(resolution_clarity, 0.50)
            logger.info(
                "ConfidenceScorer: oracle dispute risk detected for %s – "
                "capping resolution_clarity at 0.50",
                market.market_id,
            )

        # ── Final gate ─────────────────────────────────────────────────────
        passes = (
            source_conf >= CONFIDENCE_THRESHOLD
            and resolution_clarity >= CONFIDENCE_THRESHOLD
        )

        skip_reason: Optional[str] = None
        if not passes:
            parts = []
            if source_conf < CONFIDENCE_THRESHOLD:
                parts.append(
                    f"source_confidence={source_conf:.2f} < {CONFIDENCE_THRESHOLD}"
                )
            if resolution_clarity < CONFIDENCE_THRESHOLD:
                parts.append(
                    f"resolution_clarity={resolution_clarity:.2f} < {CONFIDENCE_THRESHOLD}"
                )
            if has_dispute_risk:
                parts.append("oracle dispute risk keyword detected")
            skip_reason = "; ".join(parts)

        level = "PASS" if passes else "SKIP"
        logger.info(
            "ConfidenceScorer: %s %s source=%.2f clarity=%.2f%s",
            level, market.market_id, source_conf, resolution_clarity,
            f" – {skip_reason}" if skip_reason else "",
        )

        return ConfidenceScore(
            source_confidence=source_conf,
            resolution_clarity=resolution_clarity,
            passes=passes,
            skip_reason=skip_reason,
        )

    # ── Resolution clarity scoring ─────────────────────────────────────────────

    def _score_resolution_clarity(self, market: Market) -> float:
        q = market.question.lower()

        # Instant low score for vague language
        if any(re.search(p, q) for p in LOW_CLARITY_PATTERNS):
            return 0.40

        # High score for clean binary patterns
        high_matches = sum(
            1 for p in HIGH_CLARITY_PATTERNS if re.search(p, q)
        )
        if high_matches >= 2:
            return 0.95
        if high_matches == 1:
            return 0.85

        # Check for explicit numeric threshold (highly unambiguous)
        has_number = bool(re.search(r"\b\d+(\.\d+)?\s*(%|k|m|b|billion|million|thousand)?\b", q))
        if has_number:
            return 0.80

        # Fallback: moderate clarity
        return 0.65
