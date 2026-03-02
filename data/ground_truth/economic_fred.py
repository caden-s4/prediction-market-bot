"""
data.ground_truth.economic_fred – FRED API source for economic release markets.

Uses the FRED JSON observations endpoint (requires FRED_API_KEY) to price
markets about CPI, unemployment, nonfarm payrolls, Fed rate decisions, and GDP.

This source handles the same FRED data as economic.py but uses the JSON API
endpoint for more structured responses and applies per-series cache TTLs that
match the actual publication frequency (Fed rate: 24 h; monthly CPI: 12 h).

Requires: FRED_API_KEY env var (free at https://fred.stlouisfed.org/docs/api/api_key.html).
If the key is absent, can_handle() returns False and the source is inactive.

Confidence: 0.90 — FRED is an authoritative government data source.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_API_KEY: str = os.environ.get("FRED_API_KEY", "")
_TIMEOUT = 3  # seconds — FRED JSON API; same headroom as economic.py

# Confidence for all FRED data (authoritative government source)
_CONFIDENCE = 0.90

# ── Per-series metadata ────────────────────────────────────────────────────────
# cache_hours : how long to reuse a cached observation before re-fetching.
#               Set to match realistic update frequency; FRED doesn't change
#               between API calls faster than the actual publication schedule.
# lag_days    : typical delay between period end and FRED publication.
#               Used for staleness rejection: if the latest observation is older
#               than lag_days + 7 extra buffer days, the data is too stale to trade.
# keywords    : lowercased substrings to match against market.question.
FRED_SERIES: Dict[str, dict] = {
    # ── Inflation ─────────────────────────────────────────────────────────────
    "CPIAUCSL": {
        "name": "CPI All Items",
        "unit": "index",
        "keywords": ["cpi", "consumer price index", "inflation"],
        "frequency": "monthly",
        "lag_days": 14,   # BLS releases ~2 weeks after month end
        "cache_hours": 12,
    },
    "CPILFESL": {
        "name": "Core CPI (ex food/energy)",
        "unit": "index",
        "keywords": ["core cpi", "core inflation", "core consumer price"],
        "frequency": "monthly",
        "lag_days": 14,
        "cache_hours": 12,
    },
    # ── Employment ────────────────────────────────────────────────────────────
    "UNRATE": {
        "name": "Unemployment Rate",
        "unit": "percent",
        "keywords": ["unemployment rate", "unemployment", "jobless rate"],
        "frequency": "monthly",
        "lag_days": 7,
        "cache_hours": 12,
    },
    "PAYEMS": {
        "name": "Nonfarm Payrolls",
        "unit": "thousands",
        "keywords": ["nonfarm payroll", "nonfarm payrolls", "jobs added", "job gains", "payrolls"],
        "frequency": "monthly",
        "lag_days": 7,
        "cache_hours": 6,   # jobs Friday is high-stakes; refresh more often
    },
    # ── Federal Reserve ───────────────────────────────────────────────────────
    "FEDFUNDS": {
        "name": "Federal Funds Rate (monthly avg)",
        "unit": "percent",
        "keywords": ["fed funds rate", "federal funds rate", "fomc rate"],
        "frequency": "monthly",
        "lag_days": 1,
        "cache_hours": 24,  # changes at most 8 times a year
    },
    "DFF": {
        "name": "Fed Funds Rate (daily effective)",
        "unit": "percent",
        "keywords": ["fed rate", "federal reserve rate", "interest rate"],
        "frequency": "daily",
        "lag_days": 1,
        "cache_hours": 24,
    },
    # ── GDP ───────────────────────────────────────────────────────────────────
    "GDP": {
        "name": "GDP",
        "unit": "billions",
        "keywords": ["gdp", "gross domestic product"],
        "frequency": "quarterly",
        "lag_days": 30,
        "cache_hours": 72,   # quarterly release; barely changes intra-day
    },
    # ── Gas (fallback; EIADataSource is preferred when EIA_API_KEY is set) ───
    "GASREGCOVW": {
        "name": "Regular Gasoline Price (EIA/FRED)",
        "unit": "dollars_per_gallon",
        "keywords": ["gas price", "gasoline price", "average gas price"],
        "frequency": "weekly",
        "lag_days": 3,
        "cache_hours": 4,
    },
}

# Module-level observation cache:
# series_id → (value: float, obs_date: datetime, fetched_at: float [monotonic])
_OBS_CACHE: Dict[str, Tuple[float, datetime, float]] = {}

# Regex to pull a numeric threshold from Kalshi market IDs and question text.
# Kalshi format: KXCPI-26MAR02-T3.5 → above 3.5; -B3.5 → below 3.5 (bucket).
_TICKER_ABOVE_RE = re.compile(r"-T([\d.]+)$")
_TICKER_BELOW_RE = re.compile(r"-B([\d.]+)$")
_TEXT_ABOVE_RE = re.compile(
    r"(?:above|over|exceed|higher than|at least|greater than)\s*\$?\s*([\d,]+\.?\d*)\s*%?",
    re.IGNORECASE,
)
_TEXT_BELOW_RE = re.compile(
    r"(?:below|under|less than|not exceed|at most)\s*\$?\s*([\d,]+\.?\d*)\s*%?",
    re.IGNORECASE,
)
_SUFFIX_BELOW_RE = re.compile(
    r"\$?\s*([\d,]+\.?\d*)\s*%?\s+or\s+(?:below|less|under)\b",
    re.IGNORECASE,
)
_SUFFIX_ABOVE_RE = re.compile(
    r"\$?\s*([\d,]+\.?\d*)\s*%?\s+or\s+(?:above|more|higher|over)\b",
    re.IGNORECASE,
)


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


class FREDEconomicSource(DataSource):
    """
    Prices economic release markets using the FRED JSON observations API.

    Covers: CPI, Core CPI, Unemployment, Nonfarm Payrolls, Fed Funds Rate,
    GDP.  Requires FRED_API_KEY env var; inactive without it.

    Confidence: 0.90 (authoritative government source).
    """

    def can_handle(self, market: Market) -> bool:
        if not _FRED_API_KEY:
            return False
        text = (market.question + " " + " ".join(market.tags)).lower()
        return any(
            kw in text
            for meta in FRED_SERIES.values()
            for kw in meta["keywords"]
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            series_id = self._identify_series(market)
            if not series_id:
                logger.debug(
                    "FREDEconSource: no series matched for %s", market.market_id
                )
                return None

            obs = self._fetch_series(series_id)
            if obs is None:
                return None
            value, obs_date = obs

            threshold, is_above = self._extract_threshold_and_direction(market)
            if threshold is None:
                logger.debug(
                    "FREDEconSource: no threshold in question for %s",
                    market.market_id,
                )
                return None

            ground_truth_prob = 1.0 if (value > threshold) == is_above else 0.0
            direction_str = "above" if is_above else "below"
            outcome_str = "YES" if ground_truth_prob == 1.0 else "NO"
            meta = FRED_SERIES[series_id]

            logger.info(
                "FREDEconSource: %s series=%s value=%.4f threshold=%.4f "
                "direction=%s → %s (obs_date=%s)",
                market.market_id, series_id, value, threshold,
                direction_str.upper(), outcome_str,
                obs_date.strftime("%Y-%m-%d"),
            )

            return GroundTruthResult(
                ground_truth_prob=ground_truth_prob,
                confidence=_CONFIDENCE,
                source_type=SourceType.HARD,
                source_name=f"FRED/{series_id}",
                source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                raw_data={
                    "series_id": series_id,
                    "series_name": meta["name"],
                    "value": value,
                    "obs_date": obs_date.strftime("%Y-%m-%d"),
                    "threshold": threshold,
                    "direction": direction_str,
                },
                reasoning=(
                    f"{meta['name']}: latest={value:.4f} (obs {obs_date:%Y-%m-%d}), "
                    f"threshold={threshold:.4f} ({direction_str}). "
                    f"→ {outcome_str} confidence={_CONFIDENCE:.2f}"
                ),
                data_published_at=obs_date.replace(tzinfo=timezone.utc),
            )

        except Exception as exc:
            logger.warning(
                "FREDEconSource: error for %s: %s", market.market_id, exc
            )
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _identify_series(self, market: Market) -> Optional[str]:
        """Return the best FRED series ID for this market."""
        text = (market.question + " " + " ".join(market.tags)).lower()
        # Longest keyword wins (avoids "cpi" matching before "core cpi")
        best_series = None
        best_len = 0
        for series_id, meta in FRED_SERIES.items():
            for kw in meta["keywords"]:
                if kw in text and len(kw) > best_len:
                    best_series = series_id
                    best_len = len(kw)
        return best_series

    def _fetch_series(self, series_id: str) -> Optional[Tuple[float, datetime]]:
        """
        Fetch the most recent FRED observation.  Returns (value, obs_date).

        Results are cached per-series according to the series' cache_hours so
        slow-changing series (Fed rate: 24 h; GDP: 72 h) don't hammer the API.
        """
        meta = FRED_SERIES.get(series_id, {})
        cache_hours = meta.get("cache_hours", 1)

        now_mono = time.monotonic()
        cached = _OBS_CACHE.get(series_id)
        if cached:
            value, obs_date, fetched_at = cached
            if now_mono - fetched_at < cache_hours * 3600:
                logger.debug(
                    "FREDEconSource: %s from cache (age=%.0fs)",
                    series_id, now_mono - fetched_at,
                )
                return value, obs_date

        try:
            resp = requests.get(
                _FRED_OBS_URL,
                params={
                    "series_id": series_id,
                    "api_key": _FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            observations = resp.json().get("observations", [])
            if not observations:
                logger.warning(
                    "FREDEconSource: no observations returned for %s", series_id
                )
                return None

            obs = observations[0]
            raw_value = obs.get("value", "")
            if raw_value in (".", ""):
                logger.debug(
                    "FREDEconSource: %s has missing-value sentinel; skipping", series_id
                )
                return None

            value = float(raw_value)
            obs_date = datetime.strptime(obs["date"], "%Y-%m-%d")

            # Staleness check: reject data older than lag_days + 7-day buffer
            lag_days = meta.get("lag_days", 7)
            max_age_days = lag_days + 7
            data_age_days = (datetime.utcnow() - obs_date).days
            if data_age_days > max_age_days:
                logger.warning(
                    "FREDEconSource: %s data from %s is %dd old — exceeds "
                    "%dd limit (lag=%d+7); returning None (stale)",
                    series_id, obs["date"], data_age_days, max_age_days, lag_days,
                )
                return None

            _OBS_CACHE[series_id] = (value, obs_date, now_mono)
            logger.debug(
                "FREDEconSource: fetched %s=%.4f (obs %s)", series_id, value, obs["date"]
            )
            return value, obs_date

        except requests.exceptions.Timeout:
            logger.warning(
                "FREDEconSource: timeout fetching %s (limit=%ds)", series_id, _TIMEOUT
            )
            return None
        except Exception as exc:
            logger.warning("FREDEconSource: error fetching %s: %s", series_id, exc)
            return None

    @staticmethod
    def _extract_threshold_and_direction(
        market: Market,
    ) -> Tuple[Optional[float], bool]:
        """
        Return (threshold, is_above) from the market ID suffix or question text.

        Kalshi suffix -T{val} means above; -B{val} means below/bucket.
        Question text is tried next using the same patterns as financial.py.
        Returns (None, True) if no threshold can be extracted.
        """
        mid = market.market_id
        q = market.question

        # Kalshi ticker convention
        m = _TICKER_ABOVE_RE.search(mid)
        if m:
            return _parse_float(m.group(1)), True

        m = _TICKER_BELOW_RE.search(mid)
        if m:
            return _parse_float(m.group(1)), False

        # Question text — standard keyword-before-number patterns
        m = _TEXT_ABOVE_RE.search(q)
        if m:
            return _parse_float(m.group(1)), True

        m = _TEXT_BELOW_RE.search(q)
        if m:
            return _parse_float(m.group(1)), False

        # Reverse patterns: "3.84% or less", "4.0 or above"
        m = _SUFFIX_BELOW_RE.search(q)
        if m:
            return _parse_float(m.group(1)), False

        m = _SUFFIX_ABOVE_RE.search(q)
        if m:
            return _parse_float(m.group(1)), True

        return None, True
