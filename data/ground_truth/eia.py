"""
data.ground_truth.eia – US Energy Information Administration weekly gas prices.

Source: EIA Open Data API v2  (https://api.eia.gov/v2/)
API key: free, register at https://www.eia.gov/opendata/register.php
Set the key in .env as  EIA_API_KEY=<your_key>

Without a key the source returns None on every fetch (disabled gracefully).
Set EIA_API_KEY and the source becomes active on the next restart.

Series: EMM_EPMR_PTE_NUS_DPG
  EIA Weekly U.S. Regular Conventional Retail Gasoline Prices ($/gal)
  Published every Monday ~5 pm ET.
  Used as ground truth for Kalshi KXAAAGASW bracket markets.

Why EIA over FRED/GASREGCOVW:
  - Same underlying series but fetched directly from EIA for faster updates
    (FRED can lag a few hours behind the EIA release).
  - JSON response is cleaner and includes explicit period strings.
  - Confidence = 0.90 when data is ≤ 7 days old (weekly cadence).

Note on AAA vs EIA conventional:
  KXAAAGASW resolves against AAA national average (all formulations).
  EIA Regular Conventional excludes reformulated-fuel cities and runs
  ~$0.10-0.20/gal below AAA.  For near-money brackets (gap < 40%) the EIA
  price is a useful lower-bound signal; the LARGE_DIVERGENCE gate (gap > 40%)
  blocks auto-trading when the spread is too wide.  For a tighter match use
  EIA series EMM_EPM0U_PTE_NUS_DPG (all formulations) if available on your key.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_EIA_BASE    = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"
_EIA_SERIES  = "EPM0"    # All formulations (regular + reformulated); tracks AAA closely
_EIA_AREA    = "NUS"     # National U.S. (duoarea facet code)
_TIMEOUT     = 10        # seconds

# Module-level cache: (fetched_at, (value, period_str)) or (fetched_at, (None, None))
_EIA_CACHE: dict = {}
_EIA_CACHE_TTL = 300  # seconds — same as FRED cache

_EIA_API_KEY: str = os.environ.get("EIA_API_KEY", "")

# Gas-price market keywords (mirrors economic.py _INDICATOR_MAP gas entries)
_GAS_KEYWORDS = (
    "gas", "gas price", "gas prices", "gasoline", "gasoline price", "average gas",
    "AAA gas",
)


class EIADataSource(DataSource):
    """
    Fetches weekly U.S. retail gasoline prices from the EIA Open Data API.

    Disabled (returns None) when EIA_API_KEY is not set in the environment.
    """

    def can_handle(self, market: Market) -> bool:
        if not _EIA_API_KEY:
            return False
        text = (market.question + " " + " ".join(market.tags)).lower()
        return (
            market.market_id.startswith("KXAAAGASW")
            or any(kw in text for kw in _GAS_KEYWORDS)
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        if not _EIA_API_KEY:
            return None
        try:
            value, period = self._fetch_latest()
            if value is None:
                return None

            # Determine staleness
            hours_since = self._hours_since_period(period)
            if hours_since is None or hours_since > 168:
                logger.debug(
                    "EIASource: data from period '%s' is too stale for %s",
                    period, market.market_id,
                )
                return None
            confidence = 0.90 if hours_since < 24 else 0.80

            threshold = self._extract_threshold(market.question)
            prob      = self._compute_prob(value, threshold, market)

            return GroundTruthResult(
                ground_truth_prob=prob,
                confidence=confidence,
                source_type=SourceType.HARD,
                source_name=f"EIA/{_EIA_SERIES}",
                source_url=_EIA_BASE,
                raw_data={
                    "series":     _EIA_SERIES,
                    "area":       _EIA_AREA,
                    "latest_value": value,
                    "period":     period,
                    "threshold":  threshold,
                },
                reasoning=(
                    f"EIA {_EIA_SERIES} (all-formulations) weekly gas price: {value:.3f} $/gal "
                    f"(period {period}, {hours_since:.0f}h old). "
                    + (f"Threshold={threshold:.3f}. " if threshold is not None else "No threshold. ")
                    + (f"prob={prob:.2f}." if prob is not None else "prob=None.")
                ),
            )
        except Exception as exc:
            logger.warning("EIASource: error for %s: %s", market.market_id, exc)
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch_latest(self) -> tuple:
        """Fetch the most recent weekly observation from EIA v2 API.

        Returns (value_float, period_str) or (None, None) on failure.
        Results are cached for _EIA_CACHE_TTL seconds so all bracket markets
        in one cycle share a single HTTP call.
        """
        now = time.monotonic()
        cached = _EIA_CACHE.get(_EIA_SERIES)
        if cached:
            fetched_at, result = cached
            if now - fetched_at < _EIA_CACHE_TTL:
                return result

        try:
            resp = requests.get(
                _EIA_BASE,
                params={
                    "api_key":              _EIA_API_KEY,
                    "frequency":            "weekly",
                    "data[0]":              "value",
                    f"facets[product][]":   _EIA_SERIES,
                    f"facets[duoarea][]":   _EIA_AREA,
                    "sort[0][column]":      "period",
                    "sort[0][direction]":   "desc",
                    "offset":               "0",
                    "length":               "5",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            rows = (data.get("response") or {}).get("data") or []
            for row in rows:
                try:
                    val = float(row["value"])
                    period = str(row["period"])
                    result = (val, period)
                    _EIA_CACHE[_EIA_SERIES] = (now, result)
                    return result
                except (KeyError, ValueError, TypeError):
                    continue
            _EIA_CACHE[_EIA_SERIES] = (now, (None, None))
            return None, None
        except Exception as exc:
            logger.debug("EIASource: fetch failed: %s", exc)
            return None, None

    @staticmethod
    def _hours_since_period(period: str) -> Optional[float]:
        """Return hours elapsed since the EIA period string (YYYY-MM-DD)."""
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(period, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        except Exception:
            return None

    @staticmethod
    def _extract_threshold(question: str) -> Optional[float]:
        """Extract a dollar-per-gallon threshold from the market question."""
        m = re.search(
            r"(?:above|over|below|under|greater than|less than|>|<)\s*\$?\s*([\d,]+\.?\d*)",
            question, re.IGNORECASE,
        )
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    @staticmethod
    def _compute_prob(
        value: float, threshold: Optional[float], market: Market
    ) -> Optional[float]:
        """Return YES probability from the EIA value and market threshold."""
        if threshold is None:
            return None
        q = market.question.lower()
        above = any(w in q for w in ("above", "over", "exceed", "higher", "greater", ">"))
        below = any(w in q for w in ("below", "under", "less than", "lower", "<"))
        if above:
            return 1.0 if value > threshold else 0.0
        if below:
            return 1.0 if value < threshold else 0.0
        # No direction phrase — near-equality check
        if abs(value - threshold) / max(abs(threshold), 1e-6) < 0.02:
            return 1.0
        return 0.0
