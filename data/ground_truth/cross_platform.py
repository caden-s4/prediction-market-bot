"""
data.ground_truth.cross_platform – uses one platform's price as ground truth
for the equivalent market on the other platform.

How it works:
  1. build_pairs(kalshi_markets, polymarket_markets) fuzzy-matches titles using
     SequenceMatcher after normalising abbreviations, dates, and platform noise.
  2. For a matched pair, the Polymarket price is returned as ground truth for
     the Kalshi market (and vice versa).
  3. Confidence = POLYMARKET_AS_GT_CONFIDENCE × similarity_score (≈ 0.46–0.78).
     This sits below the 0.80 auto-trade gate, so cross-platform signals appear
     as ghost trades for human review.  Raise the confidence floor once a week
     of ghost trades validates pair matching accuracy.

Rationale: Polymarket generally has more sophisticated traders than Kalshi,
making Kalshi the better market to trade against when they diverge.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from data.markets.base import Market

logger = logging.getLogger(__name__)

# Minimum title similarity to consider two markets the same underlying event.
# 0.60 is permissive enough to match differently-phrased questions about the
# same underlying (e.g. "Will S&P 500 close above 6550 today?" ↔ "S&P above 6550"),
# while still rejecting clearly different strikes/events.
# Diagnostic top-10 logging helps tune this threshold over time.
MIN_SIMILARITY = 0.60

# Confidence when using Polymarket price as ground truth.
# Real market price, but not an authoritative data source.
# Multiply by similarity → 0.60×0.78 = 0.47 (min) … 1.0×0.78 = 0.78 (max).
# Both sit below the 0.80 auto-trade gate — intentional until pairs are validated.
POLYMARKET_AS_GT_CONFIDENCE = 0.78

# How long cached pairs remain valid before triggering a rebuild.
_PAIR_CACHE_TTL = timedelta(minutes=30)


def _normalize_title(title: str) -> str:
    """
    Strip dates, platform-specific language, and punctuation for comparison.
    Normalizes common financial abbreviations before stripping punctuation
    so "S&P 500" and "S&P500" both reduce to the same token.

    Examples:
      "Will the S&P 500 close above 6550 on Mar 2 at 4pm EST?"
      → "will sp500 close above 6550"

      "S&P500 above 6550 March 2?" → "sp500 above 6550"
    """
    t = title.lower()

    # Normalize common financial index abbreviations before stripping punctuation
    t = t.replace("s&p 500", "sp500")
    t = t.replace("s&p500", "sp500")
    t = t.replace("s & p 500", "sp500")
    t = t.replace("s&p", "sp")
    t = t.replace("nasdaq-100", "nasdaq100")
    t = t.replace("nasdaq 100", "nasdaq100")
    t = t.replace("russell 2000", "russell2000")
    t = t.replace("dow jones industrial average", "djia")
    t = t.replace("dow jones", "djia")
    t = t.replace("&", "and")

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
        self._last_built: Optional[datetime] = None

    def needs_rebuild(self) -> bool:
        """True if pairs have never been built or the cache has expired.

        Deliberately does NOT check whether _pairs is non-empty: if a build
        ran and found zero matches (legitimately no cross-platform pairs this
        cycle), _last_built is still set and the TTL guard still applies.
        The old `not self._pairs` check made an empty result indistinguishable
        from "never built", causing a rebuild on every cycle whenever no pairs
        were found.
        """
        if self._last_built is None:
            return True
        return datetime.utcnow() - self._last_built >= _PAIR_CACHE_TTL

    def build_pairs(
        self, kalshi_markets: List[Market], polymarket_markets: List[Market]
    ) -> int:
        """
        Fuzzy-match each Kalshi market against all Polymarket markets.
        Rebuilds the pairs cache in place; returns the number of pairs found.

        For each Kalshi market, all Polymarket similarities are computed and
        sorted.  The best match is taken if it exceeds MIN_SIMILARITY.  The
        top-10 candidates are logged at DEBUG level for unmatched markets to
        help tune the threshold over time.
        """
        pairs: Dict[str, Tuple[str, float]] = {}

        for km in kalshi_markets:
            # Score all Polymarket markets against this Kalshi market
            scored: List[Tuple[float, str]] = [
                (_similarity(km.question, pm.question), pm.market_id)
                for pm in polymarket_markets
            ]
            scored.sort(key=lambda x: -x[0])

            best_score, best_pm_id = scored[0] if scored else (0.0, "")

            if best_score > MIN_SIMILARITY and best_pm_id:
                pairs[km.market_id] = (best_pm_id, best_score)
                logger.info(
                    "CrossPlatform: paired %s ↔ %s (similarity=%.2f)",
                    km.market_id, best_pm_id, best_score,
                )
            else:
                # Diagnostic: log top-10 candidates to help tune MIN_SIMILARITY
                top10 = [(pm_id, f"{sc:.2f}") for sc, pm_id in scored[:10]]
                logger.debug(
                    "CrossPlatform: no match for %s (best=%.2f < %.2f). Top-10: %s",
                    km.market_id, best_score, MIN_SIMILARITY, top10,
                )

        self._pairs = pairs
        self._last_built = datetime.utcnow()
        next_rebuild = self._last_built + _PAIR_CACHE_TTL
        logger.info(
            "CrossPlatform: built %d cross-platform pairs "
            "(next rebuild ~%s UTC)",
            len(pairs), next_rebuild.strftime("%H:%M"),
        )
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
