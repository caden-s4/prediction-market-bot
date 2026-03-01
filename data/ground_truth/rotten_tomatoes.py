"""
data.ground_truth.rotten_tomatoes – Rotten Tomatoes Tomatometer score source.

Handles markets asking whether a film's Rotten Tomatoes Tomatometer score
will exceed a threshold, e.g.:
  "Scream 7 Rotten Tomatoes score? Above 90"
  "Will Deadpool & Wolverine score above 85 on Rotten Tomatoes?"

Uses the undocumented RT internal search API (no key required) to look up the
film's current Tomatometer score.  Returns None gracefully when the film is
not found or the score has not yet been published (< 5 reviews).

Confidence scale (based on review count):
  0.90  ≥ 40 reviews  (Certified score; stable)
  0.80  10–39 reviews (Building; can still move)
  0.60  5–9 reviews   (Early; high volatility — below the 0.80 trade gate)

A score with fewer than 5 reviews is treated as unpublished and returns None.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional, Tuple

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_RT_SEARCH_URL = "https://www.rottentomatoes.com/api/private/v2.0/movies"
_RT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; prediction-market-bot/1.0)",
    "Accept": "application/json",
}
_TIMEOUT = 10

# Cache: title_lower → (fetched_at, (score_int, num_reviews))
# RT scores update slowly; 30-minute TTL is appropriate.
_RT_CACHE: dict = {}
_RT_CACHE_TTL = 1800  # seconds

# Minimum review count to consider a score tradeable
_MIN_REVIEWS = 5


class RottenTomatoesSource(DataSource):
    """
    Fetches Tomatometer scores from Rotten Tomatoes for film score markets.

    Markets must explicitly mention "Rotten Tomatoes", "tomatometer", or "RT score"
    in the question or tags to be claimed by this source.
    """

    def can_handle(self, market: Market) -> bool:
        text = (market.question + " " + " ".join(market.tags)).lower()
        return any(kw in text for kw in (
            "rotten tomatoes", "tomatometer", "rt score", "tomato score",
        ))

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            title = self._extract_title(market.question)
            if not title:
                logger.debug(
                    "RTSource: could not extract film title from '%s'",
                    market.question,
                )
                return None

            score, num_reviews = self._fetch_score(title)
            if score is None:
                logger.debug(
                    "RTSource: no Tomatometer score found for '%s' (market %s)",
                    title, market.market_id,
                )
                return None

            if num_reviews < _MIN_REVIEWS:
                logger.info(
                    "RTSource: '%s' score=%d but only %d reviews — too few to trade (%s)",
                    title, score, num_reviews, market.market_id,
                )
                return None

            threshold = self._extract_threshold(market.question)
            prob = self._compute_prob(score, threshold, market)

            if num_reviews >= 40:
                confidence = 0.90
            elif num_reviews >= 10:
                confidence = 0.80
            else:
                confidence = 0.60   # 5–9 reviews: below the 0.80 trade gate

            return GroundTruthResult(
                ground_truth_prob=prob,
                confidence=confidence,
                source_type=SourceType.HARD,
                source_name="RottenTomatoes/tomatometer",
                source_url=(
                    f"https://www.rottentomatoes.com/search/?q="
                    + title.replace(" ", "+")
                ),
                raw_data={
                    "title":       title,
                    "score":       score,
                    "num_reviews": num_reviews,
                    "threshold":   threshold,
                },
                reasoning=(
                    f"Rotten Tomatoes Tomatometer: '{title}' score={score}% "
                    f"({num_reviews} reviews). "
                    + (f"Threshold={threshold}. " if threshold is not None else "No threshold. ")
                    + (f"prob={prob:.2f}." if prob is not None else "prob=None.")
                ),
            )

        except Exception as exc:
            logger.warning("RTSource: error for %s: %s", market.market_id, exc)
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_title(self, question: str) -> Optional[str]:
        """Extract the film title from the market question.

        Handles formats Kalshi uses:
          "Scream 7 Rotten Tomatoes score? Above 90"
          "Will Scream 7 have a Rotten Tomatoes score above 90?"
          "Will Deadpool & Wolverine score above 85 on Rotten Tomatoes?"
        """
        # "Will TITLE have/score/get a ... Rotten Tomatoes"
        m = re.search(
            r"\bWill\s+(.+?)\s+(?:have|score|get|receive)\b",
            question, re.IGNORECASE,
        )
        if m:
            title = m.group(1).strip()
            if 1 <= len(title) <= 80:
                return title

        # "TITLE Rotten Tomatoes score" or "TITLE tomatometer"
        m = re.search(
            r"^(.+?)\s+(?:Rotten\s+Tomatoes|tomatometer|RT\s+score)",
            question, re.IGNORECASE,
        )
        if m:
            title = m.group(1).strip()
            if 1 <= len(title) <= 80:
                return title

        # "TITLE score above/below N on Rotten Tomatoes"
        m = re.search(
            r"^(.+?)\s+score\s+(?:above|below|over|under)",
            question, re.IGNORECASE,
        )
        if m:
            title = m.group(1).strip()
            if 1 <= len(title) <= 80:
                return title

        return None

    def _extract_threshold(self, question: str) -> Optional[int]:
        """Extract the numeric Tomatometer threshold from the market question."""
        m = re.search(
            r"(?:above|over|greater\s+than|exceed|>)\s*(\d+)",
            question, re.IGNORECASE,
        )
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        m = re.search(
            r"(?:below|under|less\s+than|<)\s*(\d+)",
            question, re.IGNORECASE,
        )
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        return None

    def _compute_prob(
        self,
        score: int,
        threshold: Optional[int],
        market: Market,
    ) -> Optional[float]:
        if threshold is None:
            return None
        question_lower = market.question.lower()
        above = any(w in question_lower for w in ("above", "over", "exceed", "greater", ">"))
        below = any(w in question_lower for w in ("below", "under", "less than", "<"))
        if above:
            return 1.0 if score > threshold else 0.0
        if below:
            return 1.0 if score < threshold else 0.0
        # Near-equality: within 2 percentage points
        return 1.0 if abs(score - threshold) <= 2 else 0.0

    def _fetch_score(self, title: str) -> Tuple[Optional[int], int]:
        """Fetch the Tomatometer score for a film title.

        Returns (score_int, num_reviews) or (None, 0) if not found or on error.
        Results are module-level cached for _RT_CACHE_TTL seconds.
        """
        cache_key = title.lower().strip()
        now = time.monotonic()
        cached = _RT_CACHE.get(cache_key)
        if cached:
            fetched_at, result = cached
            if now - fetched_at < _RT_CACHE_TTL:
                return result

        try:
            resp = requests.get(
                _RT_SEARCH_URL,
                params={"q": title, "page_limit": 5, "page": 1},
                headers=_RT_HEADERS,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            # Handle both v1 (data.movies) and v2 (results) response shapes
            movies = (
                data.get("results")
                or (data.get("data") or {}).get("movies")
                or []
            )

            title_lower = title.lower()
            for movie in movies:
                movie_title = str(movie.get("title", "")).lower()

                # Accept exact substring match or all-words match
                if title_lower not in movie_title and movie_title not in title_lower:
                    search_words = [w for w in title_lower.split() if len(w) > 2]
                    if not search_words or not all(w in movie_title for w in search_words):
                        continue

                # Try field names across API versions
                score_raw = (
                    movie.get("tomatoMeter")
                    or movie.get("tomatoScore")
                    or movie.get("meterScore")
                    or (movie.get("tomato_rating") or {}).get("score")
                )
                reviews_raw = (
                    movie.get("numReviews")
                    or movie.get("reviewCount")
                    or movie.get("tomatoNumReviews")
                    or (movie.get("tomato_rating") or {}).get("count")
                    or 0
                )
                if score_raw is not None:
                    try:
                        result = (int(score_raw), int(reviews_raw))
                        _RT_CACHE[cache_key] = (now, result)
                        return result
                    except (TypeError, ValueError):
                        continue

            _RT_CACHE[cache_key] = (now, (None, 0))
            return None, 0

        except Exception as exc:
            logger.debug("RTSource: fetch failed for '%s': %s", title, exc)
            return None, 0
