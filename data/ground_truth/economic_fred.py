"""
data.ground_truth.economic_fred – FRED API source for economic release markets.

Uses the FRED JSON observations endpoint (requires FRED_API_KEY) to price
markets about CPI, unemployment, nonfarm payrolls, Fed rate decisions, GDP,
10-year breakeven inflation, and 30-year mortgage rates.

This source handles the same FRED data as economic.py but uses the JSON API
endpoint for more structured responses and applies per-series cache TTLs that
match the actual publication frequency (Fed rate: 6 h; monthly CPI: 12 h).

Foreign-market exclusion: FRED tracks US data exclusively.  Markets that
reference foreign economies (China, EU, UK, Japan, …) are rejected in
can_handle() to avoid spurious matches.

Requires: FRED_API_KEY env var (free at https://fred.stlouisfed.org/docs/api/api_key.html).
If the key is absent, can_handle() returns False and the source is inactive.

Confidence: 0.90 — FRED is an authoritative government data source.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
_FRED_API_KEY: str = os.environ.get("FRED_API_KEY", "")
_TIMEOUT = 3  # seconds — FRED JSON API

# Confidence for all FRED data (authoritative government source)
_CONFIDENCE = 0.90

# ── Foreign-market exclusion ───────────────────────────────────────────────────
# FRED series reflect US economic data only.  Reject markets about foreign
# economies to prevent false matches on questions like "Will China CPI exceed 2%?"
FOREIGN_COUNTRY_INDICATORS: frozenset = frozenset({
    "china", "chinese",
    "euro ", " euro", "european", "eurozone", "eu cpi", "eu gdp",
    "uk ", "united kingdom", "britain", "british",
    "japan", "japanese",
    "canada", "canadian",
    "australia", "australian",
    "germany", "german",
    "france", "french",
    "india", "indian",
    "brazil", "mexico",
    "russia", "russian",
    "south korea", "taiwan",
})

# ── Per-series metadata ────────────────────────────────────────────────────────
# cache_hours : how long to reuse a cached observation before re-fetching.
#               Set to match realistic update frequency; FRED doesn't change
#               between API calls faster than the actual publication schedule.
# lag_days    : typical delay between period end and FRED publication.
#               Used for staleness rejection: if the latest observation is older
#               than lag_days + 7 extra buffer days, the data is too stale to trade.
# keywords    : lowercased substrings to match against market.question.
#               Use US-specific phrasing to avoid matching foreign-economy markets.
FRED_SERIES: Dict[str, dict] = {
    # ── Inflation ─────────────────────────────────────────────────────────────
    "CPIAUCSL": {
        "name": "CPI All Items",
        "unit": "index",
        "keywords": [
            "us cpi", "u.s. cpi", "united states cpi",
            "cpi", "consumer price index",
            "us inflation", "u.s. inflation", "inflation rate",
        ],
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
    "T10YIE": {
        "name": "10-Year Breakeven Inflation Rate",
        "unit": "percent",
        "keywords": [
            "breakeven inflation", "10-year breakeven", "10 year breakeven",
            "inflation expectations", "tips spread",
        ],
        "frequency": "daily",
        "lag_days": 1,
        "cache_hours": 6,
    },
    # ── Employment ────────────────────────────────────────────────────────────
    "UNRATE": {
        "name": "Unemployment Rate",
        "unit": "percent",
        "keywords": [
            "us unemployment rate", "us unemployment", "u.s. unemployment",
            "unemployment rate", "jobless rate",
        ],
        "frequency": "monthly",
        "lag_days": 7,
        "cache_hours": 12,
    },
    "PAYEMS": {
        "name": "Nonfarm Payrolls",
        "unit": "thousands",
        "keywords": [
            "nonfarm payroll", "nonfarm payrolls",
            "us jobs added", "job gains", "us payrolls",
        ],
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
        "cache_hours": 24,  # monthly average changes at most 8 times a year
    },
    "DFF": {
        "name": "Fed Funds Rate (daily effective)",
        "unit": "percent",
        "keywords": ["fed rate", "federal reserve rate", "interest rate"],
        "frequency": "daily",
        "lag_days": 1,
        "cache_hours": 6,   # was 24; rate decisions can move intraday
    },
    # ── GDP ───────────────────────────────────────────────────────────────────
    "GDP": {
        "name": "GDP",
        "unit": "billions",
        "keywords": ["us gdp", "u.s. gdp", "gdp", "gross domestic product"],
        "frequency": "quarterly",
        "lag_days": 30,
        "cache_hours": 72,   # quarterly release; barely changes intra-day
    },
    # ── Housing ───────────────────────────────────────────────────────────────
    "MORTGAGE30US": {
        "name": "30-Year Fixed Rate Mortgage Average",
        "unit": "percent",
        "keywords": [
            "30 year mortgage", "30-year mortgage",
            "mortgage rate", "30yr mortgage", "fixed mortgage rate",
        ],
        "frequency": "weekly",
        "lag_days": 3,
        "cache_hours": 12,
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


# ── Ticker month parser ────────────────────────────────────────────────────────
# Matches the YYMON date segment in Kalshi market IDs.
# Examples: KXCPI-26FEB → ("26", "FEB"); KXCPICOREYOY-26FEB → ("26", "FEB")
_TICKER_MONTH_RE = re.compile(
    r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)",
    re.IGNORECASE,
)
_MONTH_ABBR: Dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Series requiring strict release-date alignment: if FRED's most recent
# observation is for an earlier month than the market target, the data has not
# been released yet and the signal must be suppressed.
_MONTHLY_ALIGNED_SERIES: frozenset = frozenset({"CPIAUCSL", "CPILFESL"})


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


class FREDEconomicSource(DataSource):
    """
    Prices economic release markets using the FRED JSON observations API.

    Covers: CPI, Core CPI, 10Y Breakeven Inflation, Unemployment, Nonfarm
    Payrolls, Fed Funds Rate, GDP, 30Y Mortgage Rate.
    Requires FRED_API_KEY env var; inactive without it.

    Confidence: 0.90 (authoritative government source).
    """

    def __init__(self) -> None:
        # Per-series datetime-based cache:
        # series_id → (value_or_None, obs_date_or_None, fetched_at: datetime)
        # value=None means the series was fetched but found stale; avoids
        # re-hitting the API until cache_hours have elapsed.
        self._cache: Dict[str, Tuple[Optional[float], Optional[datetime], datetime]] = {}
        # Cache for series that need two observations (MoM delta).
        # series_id → (latest, prior, obs_date_or_None, fetched_at: datetime)
        self._pair_cache: Dict[str, Tuple[Optional[float], Optional[float], Optional[datetime], datetime]] = {}
        logger.info(
            "FREDEconSource: initialized with %d series, datetime-based cache enabled",
            len(FRED_SERIES),
        )

    def can_handle(self, market: Market) -> bool:
        if not _FRED_API_KEY:
            return False
        text = (market.question + " " + " ".join(market.tags)).lower()
        # Reject markets about foreign economies — FRED tracks US data only
        if any(indicator in text for indicator in FOREIGN_COUNTRY_INDICATORS):
            return False
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

            threshold, is_above = self._extract_threshold_and_direction(market)
            if threshold is None:
                logger.debug(
                    "FREDEconSource: no threshold in question for %s",
                    market.market_id,
                )
                return None

            meta = FRED_SERIES[series_id]
            direction_str = "above" if is_above else "below"

            # ── PAYEMS: compare month-over-month change, not the raw level ────
            # PAYEMS reports the absolute level of total nonfarm payrolls (thousands).
            # Markets ask "were X jobs added?" — that requires the MoM delta.
            # Fetch two observations; return None if fewer than two are available.
            if series_id == "PAYEMS":
                pair = self._fetch_two_observations("PAYEMS")
                if pair is None:
                    logger.info(
                        "FREDEconSource: PAYEMS skipped for %s — "
                        "insufficient observations for month-over-month calculation",
                        market.market_id,
                    )
                    return None
                latest_level, prior_level, obs_date = pair
                if self._obs_too_old_for_market(series_id, obs_date, market):
                    return None
                monthly_change = latest_level - prior_level
                logger.info(
                    "FREDEconSource: PAYEMS: %.0f - %.0f = %+.0fk jobs added "
                    "vs threshold %.0fk (market=%s obs=%s)",
                    latest_level, prior_level, monthly_change, threshold,
                    market.market_id, obs_date.strftime("%Y-%m-%d"),
                )
                ground_truth_prob = 1.0 if (monthly_change > threshold) == is_above else 0.0
                outcome_str = "YES" if ground_truth_prob == 1.0 else "NO"
                return GroundTruthResult(
                    ground_truth_prob=ground_truth_prob,
                    confidence=_CONFIDENCE,
                    source_type=SourceType.HARD,
                    source_name="FRED/PAYEMS",
                    source_url="https://fred.stlouisfed.org/series/PAYEMS",
                    raw_data={
                        "series_id": "PAYEMS",
                        "series_name": meta["name"],
                        "latest_level": latest_level,
                        "prior_level": prior_level,
                        "monthly_change": monthly_change,
                        "obs_date": obs_date.strftime("%Y-%m-%d"),
                        "threshold": threshold,
                        "direction": direction_str,
                    },
                    reasoning=(
                        f"PAYEMS: {latest_level:.0f} - {prior_level:.0f} = "
                        f"{monthly_change:+.0f}k jobs added "
                        f"(obs {obs_date:%Y-%m-%d}), "
                        f"threshold={threshold:.0f}k ({direction_str}). "
                        f"→ {outcome_str} confidence={_CONFIDENCE:.2f}"
                    ),
                    data_published_at=obs_date.replace(tzinfo=timezone.utc),
                )

            obs = self._fetch_series(series_id)
            if obs is None:
                return None
            value, obs_date = obs

            if self._obs_too_old_for_market(series_id, obs_date, market):
                return None

            # ── Part 2: Release-date alignment for monthly CPI series ──────────
            # The 45-day resolution_date heuristic in _obs_too_old_for_market can
            # fail when a market resolves early in the target month (e.g. KXCPI-26FEB
            # resolving Feb 12: delta = 42 days ≤ 45 → not flagged).  This check
            # directly compares the observation month to the market's target month so
            # January data is never used to price a February CPI market.
            if series_id in _MONTHLY_ALIGNED_SERIES:
                target_month = self._parse_market_month(market)
                if target_month is not None:
                    obs_ym = (obs_date.year, obs_date.month)
                    tgt_ym = (target_month.year, target_month.month)
                    if obs_ym < tgt_ym:
                        logger.info(
                            "FREDEconSource: %s observation date %s does not cover %s"
                            " — data not yet released, skipping",
                            series_id,
                            obs_date.strftime("%Y-%m-%d"),
                            target_month.strftime("%B %Y"),
                        )
                        return None

            ground_truth_prob = 1.0 if (value > threshold) == is_above else 0.0
            outcome_str = "YES" if ground_truth_prob == 1.0 else "NO"

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

    def get_series_for_market(self, market: Market) -> Optional[str]:
        """Return the FRED series ID this market's ground truth depends on, or None."""
        return self._identify_series(market)

    @staticmethod
    def _parse_market_month(market: Market) -> Optional[datetime]:
        """Return the first day of the month a Kalshi market is asking about.

        Parses the YYMON segment from the market ID:
          KXCPI-26FEB          → datetime(2026, 2, 1)
          KXCPICOREYOY-26FEB   → datetime(2026, 2, 1)
          KXCPI-26MAR02-T3.5   → datetime(2026, 3, 1)
        Returns None when no month can be parsed.
        """
        m = _TICKER_MONTH_RE.search(market.market_id)
        if not m:
            return None
        year = 2000 + int(m.group(1))
        month = _MONTH_ABBR.get(m.group(2).upper())
        if month is None:
            return None
        return datetime(year, month, 1)

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

        Uses a datetime-based per-instance cache so slow-changing series
        (FEDFUNDS: 24 h; GDP: 72 h) don't hammer the API.  Stale results
        (where value is None) are also cached for cache_hours to prevent
        repeated API hits when data is known to be unavailable.
        """
        meta = FRED_SERIES.get(series_id, {})
        cache_hours = meta.get("cache_hours", 1)

        now = datetime.utcnow()
        cached = self._cache.get(series_id)
        if cached is not None:
            value, obs_date, fetched_at = cached
            age_hours = (now - fetched_at).total_seconds() / 3600
            if age_hours < cache_hours:
                if value is None:
                    logger.debug(
                        "FREDEconSource: %s known-stale in cache (age=%.1fh)",
                        series_id, age_hours,
                    )
                    return None
                logger.debug(
                    "FREDEconSource: %s from cache (age=%.1fh)",
                    series_id, age_hours,
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
                    "%dd limit (lag=%d+7); caching None (stale)",
                    series_id, obs["date"], data_age_days, max_age_days, lag_days,
                )
                self._cache[series_id] = (None, obs_date, now)
                return None

            self._cache[series_id] = (value, obs_date, now)
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

    def _fetch_two_observations(
        self, series_id: str
    ) -> Optional[Tuple[float, float, datetime]]:
        """
        Fetch the two most recent valid FRED observations for series_id.

        Returns (latest_value, prior_value, latest_obs_date), or None if fewer
        than two valid observations are available or the data is too stale.
        Used for series like PAYEMS where the market resolves on the MoM delta,
        not the absolute level.
        """
        meta = FRED_SERIES.get(series_id, {})
        cache_hours = meta.get("cache_hours", 1)

        now = datetime.utcnow()
        cached = self._pair_cache.get(series_id)
        if cached is not None:
            latest, prior, obs_date, fetched_at = cached
            age_hours = (now - fetched_at).total_seconds() / 3600
            if age_hours < cache_hours:
                if latest is None or prior is None:
                    logger.debug(
                        "FREDEconSource: %s pair known-stale in cache (age=%.1fh)",
                        series_id, age_hours,
                    )
                    return None
                logger.debug(
                    "FREDEconSource: %s pair from cache (age=%.1fh)", series_id, age_hours
                )
                return latest, prior, obs_date

        try:
            resp = requests.get(
                _FRED_OBS_URL,
                params={
                    "series_id": series_id,
                    "api_key": _FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 2,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            observations = resp.json().get("observations", [])
            valid_obs = [o for o in observations if o.get("value", "") not in (".", "")]

            if len(valid_obs) < 2:
                logger.warning(
                    "FREDEconSource: %s returned %d valid observation(s) — "
                    "need 2 for month-over-month delta; returning None",
                    series_id, len(valid_obs),
                )
                self._pair_cache[series_id] = (None, None, None, now)
                return None

            latest_val = float(valid_obs[0]["value"])
            prior_val  = float(valid_obs[1]["value"])
            obs_date   = datetime.strptime(valid_obs[0]["date"], "%Y-%m-%d")

            # Staleness check on the latest observation
            lag_days     = meta.get("lag_days", 7)
            max_age_days = lag_days + 7
            data_age_days = (datetime.utcnow() - obs_date).days
            if data_age_days > max_age_days:
                logger.warning(
                    "FREDEconSource: %s data from %s is %dd old — exceeds "
                    "%dd limit (lag=%d+7); caching None (stale)",
                    series_id, valid_obs[0]["date"], data_age_days, max_age_days, lag_days,
                )
                self._pair_cache[series_id] = (None, None, obs_date, now)
                return None

            self._pair_cache[series_id] = (latest_val, prior_val, obs_date, now)
            logger.debug(
                "FREDEconSource: fetched %s pair latest=%.0f prior=%.0f (obs %s)",
                series_id, latest_val, prior_val, valid_obs[0]["date"],
            )
            return latest_val, prior_val, obs_date

        except requests.exceptions.Timeout:
            logger.warning(
                "FREDEconSource: timeout fetching %s pair (limit=%ds)", series_id, _TIMEOUT
            )
            return None
        except Exception as exc:
            logger.warning(
                "FREDEconSource: error fetching %s pair: %s", series_id, exc
            )
            return None

    def _obs_too_old_for_market(
        self, series_id: str, obs_date: datetime, market: Market
    ) -> bool:
        """Return True if the FRED observation belongs to a prior release cycle.

        For monthly series: if obs_date is more than 45 days before the market's
        resolution date the data is from the wrong month.  Example: January
        payrolls (obs ~Jan 31) must not price a February payrolls market that
        resolves in March (~45 days later).

        Non-monthly series (daily, weekly, quarterly) are not gated here —
        their existing staleness check in _fetch_series/_fetch_two_observations
        is sufficient.

        Note: the 45-day heuristic can be insufficient for CPI markets that
        resolve early in the target month (delta ≤ 45).  The month-alignment
        check added in fetch() for _MONTHLY_ALIGNED_SERIES provides a tighter,
        ticker-based guard for those series.
        """
        meta = FRED_SERIES.get(series_id, {})
        if meta.get("frequency") != "monthly":
            return False
        try:
            resolution_date = market.resolution_date
            # Normalise to naive datetime for arithmetic with obs_date (also naive)
            if getattr(resolution_date, "tzinfo", None) is not None:
                resolution_date = resolution_date.replace(tzinfo=None)
            delta_days = (resolution_date - obs_date).days
            if delta_days > 45:
                logger.warning(
                    "FREDEconSource: %s observation %s is %dd before market "
                    "resolution %s (> 45-day limit) — stale, skipping",
                    series_id,
                    obs_date.strftime("%Y-%m-%d"),
                    delta_days,
                    market.resolution_date.strftime("%Y-%m-%d"),
                )
                return True
        except Exception:
            # Cannot determine resolution date — conservatively treat monthly
            # series as stale rather than allowing a potentially wrong-month
            # observation to pass through.
            logger.warning(
                "FREDEconSource: %s could not determine resolution date for %s"
                " — treating monthly observation as stale (safe default)",
                series_id, market.market_id,
            )
            return True
        return False

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
