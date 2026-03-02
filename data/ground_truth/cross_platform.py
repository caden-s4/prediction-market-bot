"""
data.ground_truth.cross_platform – uses one platform's price as ground truth
for the equivalent market on the other platform.

How it works:
  1. build_pairs(kalshi_markets, polymarket_markets) fuzzy-matches titles using
     SequenceMatcher after stripping dates and platform noise.
  2. For a matched pair, the Polymarket price is returned as ground truth for
     the Kalshi market (and vice versa).
  3. Confidence = POLYMARKET_AS_GT_CONFIDENCE × similarity_score (≈ 0.56–0.78).
     This sits below the 0.80 auto-trade gate, so cross-platform signals appear
     as ghost trades for human review.  Raise the confidence floor once a week
     of ghost trades validates pair matching accuracy.

Rationale: Polymarket generally has more sophisticated traders than Kalshi,
making Kalshi the better market to trade against when they diverge.
"""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from data.markets.base import Market

logger = logging.getLogger(__name__)

# Minimum title similarity to consider two markets the same underlying event.
# 0.72 matches "Will S&P 500 close above 6550 today?" ↔ "S&P above 6550 March 2?"
# while rejecting "Will S&P 500 close above 6500?" ↔ "Will S&P 500 close above 6550?"
MIN_SIMILARITY = 0.72

# Confidence when using Polymarket price as ground truth.
# Real market price, but not an authoritative data source.
# Multiply by similarity → 0.72×0.72 = 0.52 (min) … 0.78×1.0 = 0.78 (max).
# Both sit below the 0.80 auto-trade gate — intentional until pairs are validated.
POLYMARKET_AS_GT_CONFIDENCE = 0.78

# How long cached pairs remain valid before triggering a rebuild (seconds).
_PAIR_CACHE_TTL = 1800  # 30 minutes — pairs rarely change within a discovery cycle


def _normalize_title(title: str) -> str:
    """
    Strip dates, platform-specific language, and punctuation for comparison.

    Example:
      "Will the S&P 500 close above 6550 on Mar 2 at 4pm EST?"
      → "will s p 500 close above 6550"
    """
    t = title.lower()
    # Remove date patterns: "on Mar 2", "by March 2026", "as of 3/2"
    t = re.sub(r"\b(?:on|by|as of|before|after)\s+\w+\s+\d+,?\s*\d*", "", t)
    t = re.sub(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", "", t)
    # Remove time references: "at 4pm", "at close", "at 4:00 ET"
    t = re.sub(r"\bat\s+[\d:]+\s*(?:am|pm|et|utc)?", "", t)
    # Remove platform noise
    t = re.sub(r"\b(?:kalshi|polymarket|at the end of|at close)\b", "", t)
    # Normalise punctuation and whitespace
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _similarity(a: str, b: str) -> float:
    """Return title similarity after normalisation (0–1)."""
    return SequenceMatcher(None, _normalize_title(a), _normalize_title(b)).ratio()


class CrossPlatformSource:
    """
    Fuzzy-match Kalshi and Polymarket markets by title, then use one
    platform's price as ground truth for the other.

    Not a DataSource subclass — it operates on two market lists rather than
    a single Market object.  Invoked by GapDetector.run_cross_platform_scan().

    Lifecycle:
      build_pairs()        — one-time fuzzy matching; cached for _PAIR_CACHE_TTL.
      get_probability()    — Kalshi → Polymarket price lookup.
      get_reverse_probability() — Polymarket → Kalshi price lookup.
    """

    def __init__(self) -> None:
        # {kalshi_id: (polymarket_id, similarity_score)}
        self._pairs: Dict[str, Tuple[str, float]] = {}
        self._built_at: float = 0.0

    def needs_rebuild(self) -> bool:
        """True if pairs cache is empty or expired."""
        return (
            not self._pairs
            or time.monotonic() - self._built_at >= _PAIR_CACHE_TTL
        )

    def build_pairs(
        self, kalshi_markets: List[Market], polymarket_markets: List[Market]
    ) -> int:
        """
        Fuzzy-match each Kalshi market against all Polymarket markets.
        Rebuilds the pairs cache in place; returns the number of pairs found.
        """
        pairs: Dict[str, Tuple[str, float]] = {}

        for km in kalshi_markets:
            best_id: Optional[str] = None
            best_score = MIN_SIMILARITY

            for pm in polymarket_markets:
                score = _similarity(km.question, pm.question)
                if score > best_score:
                    best_score = score
                    best_id = pm.market_id

            if best_id is not None:
                pairs[km.market_id] = (best_id, best_score)
                logger.info(
                    "CrossPlatform: paired %s ↔ %s (similarity=%.2f)",
                    km.market_id, best_id, best_score,
                )

        self._pairs = pairs
        self._built_at = time.monotonic()
        logger.info("CrossPlatform: built %d cross-platform pairs", len(pairs))
        return len(pairs)

    def get_probability(
        self,
        kalshi_market: Market,
        polymarket_by_id: Dict[str, Market],
    ) -> Tuple[Optional[float], float]:
        """
        Return (polymarket_yes_price, confidence) for a Kalshi market.

        confidence = POLYMARKET_AS_GT_CONFIDENCE × similarity_score.
        Returns (None, 0.0) if no pair is found or the Polymarket market has
        been evicted from the price registry.
        """
        pair = self._pairs.get(kalshi_market.market_id)
        if pair is None:
            return None, 0.0

        pm_id, similarity = pair
        pm_market = polymarket_by_id.get(pm_id)
        if pm_market is None:
            return None, 0.0

        confidence = POLYMARKET_AS_GT_CONFIDENCE * similarity
        logger.info(
            "CrossPlatformSource: %s → Polymarket/%s "
            "price=%.3f similarity=%.2f confidence=%.2f",
            kalshi_market.market_id, pm_id,
            pm_market.yes_price, similarity, confidence,
        )
        return pm_market.yes_price, confidence

    def get_reverse_probability(
        self,
        polymarket_market: Market,
        kalshi_by_id: Dict[str, Market],
    ) -> Tuple[Optional[float], float]:
        """
        Reverse direction: use Kalshi price as ground truth for a Polymarket
        market.  Returns (kalshi_yes_price, confidence) or (None, 0.0).
        """
        reverse = {pm_id: k_id for k_id, (pm_id, _) in self._pairs.items()}
        kalshi_id = reverse.get(polymarket_market.market_id)
        if kalshi_id is None:
            return None, 0.0

        kalshi_market = kalshi_by_id.get(kalshi_id)
        if kalshi_market is None:
            return None, 0.0

        _, similarity = self._pairs[kalshi_id]
        confidence = POLYMARKET_AS_GT_CONFIDENCE * similarity
        logger.info(
            "CrossPlatformSource: %s → Kalshi/%s "
            "price=%.3f confidence=%.2f",
            polymarket_market.market_id, kalshi_id,
            kalshi_market.yes_price, confidence,
        )
        return kalshi_market.yes_price, confidence

    @property
    def pair_count(self) -> int:
        return len(self._pairs)
