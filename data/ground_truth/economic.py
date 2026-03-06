"""
data.ground_truth.economic – economic data release fetcher.

Sources:
  1. FRED (Federal Reserve Economic Data) – free, no key needed for reading.
     Covers: CPI, GDP, unemployment, Fed rate decisions, PPI, PCE, etc.
  2. BLS (Bureau of Labor Statistics) public API – no key required for basic use.

For each flagged market, we:
  1. Detect the economic indicator being referenced (CPI, unemployment, etc.)
  2. Fetch the latest observation from FRED/BLS
  3. Compare to the market's implied threshold to determine YES/NO probability

Confidence: 0.95 for published data releases; 0.0 before release.
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

_FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_FRED_API_BASE = "https://api.stlouisfed.org/fred"
_TIMEOUT = 3      # seconds — FRED/API calls; 3 s gives headroom without stalling a cycle

# Module-level FRED response cache: series_id → (fetched_at, (value, date_str))
# All bracket markets in the same series (KXAAAGASW-26MAR02-2.888, -2.898, …)
# share the same underlying indicator — one HTTP call should cover all of them.
# TTL of 5 minutes is well within any FRED update frequency.
_FRED_CACHE: dict = {}
_FRED_CACHE_TTL = 300  # seconds

# Optional FRED API key — required for the release calendar check.
# Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html
_FRED_API_KEY: str = os.environ.get("FRED_API_KEY", "")

# Per-series staleness windows: (fresh_hours, max_hours).
# fresh_hours  → confidence 0.95
# max_hours    → confidence 0.80
# > max_hours  → None (too stale)
#
# The defaults (24h fresh, 168h/7d max) are chosen for monthly releases.
# Weekly series (ICSA) have tighter windows; quarterly series (GDP) have
# wider ones so we don't reject data that's legitimately 60+ days old.
_SERIES_STALENESS: Dict[str, Tuple[int, int]] = {
    # Daily / near-real-time
    "DFF":          (24,    72),   # Fed Funds Rate: daily; stale after 3 days
    # Weekly
    "GASREGCOVW":   (24,   144),   # US avg retail gas price: weekly (EIA/Monday); 6-day max
    "ICSA":         (24,   168),   # Initial Claims: weekly; stale after 7 days
    # Monthly
    "UNRATE":   (24,  1080),   # Unemployment: monthly; allow up to 45 days
    "PAYEMS":   (24,   744),   # Nonfarm Payroll: monthly
    "CPIAUCSL": (24,   744),   # CPI: monthly
    "PPIACO":   (24,   744),   # PPI: monthly
    "PCE":      (24,   744),   # PCE: monthly
    "PCEPILFE": (24,   744),   # Core PCE: monthly
    "RSAFS":    (24,   744),   # Retail Sales: monthly
    "HOUST":    (24,   744),   # Housing Starts: monthly
    "BOPGSTB":  (24,  1440),   # Trade Balance: monthly, often 60-day lag
    "NAPM":     (24,   744),   # ISM PMI: monthly
    # Quarterly
    "GDP":      (24,  2208),   # GDP: quarterly; allow up to 92 days
}

# Map keyword → (FRED series ID, human name)
_INDICATOR_MAP: Dict[str, Tuple[str, str]] = {
    "cpi": ("CPIAUCSL", "Consumer Price Index"),
    "inflation": ("CPIAUCSL", "Consumer Price Index"),
    "unemployment": ("UNRATE", "Unemployment Rate"),
    "unemployment rate": ("UNRATE", "Unemployment Rate"),
    "jobless": ("ICSA", "Initial Jobless Claims"),
    "initial claims": ("ICSA", "Initial Jobless Claims"),
    "gdp": ("GDP", "Gross Domestic Product"),
    "ppi": ("PPIACO", "Producer Price Index"),
    "pce": ("PCE", "Personal Consumption Expenditures"),
    "core pce": ("PCEPILFE", "Core PCE Price Index"),
    "federal funds rate": ("DFF", "Federal Funds Rate"),
    "fed funds": ("DFF", "Federal Funds Rate"),
    "interest rate": ("DFF", "Federal Funds Rate"),
    "retail sales": ("RSAFS", "Retail Sales"),
    "housing starts": ("HOUST", "Housing Starts"),
    "nonfarm payroll": ("PAYEMS", "Nonfarm Payroll Employment"),
    "payrolls": ("PAYEMS", "Nonfarm Payroll Employment"),
    "jobs": ("PAYEMS", "Nonfarm Payroll Employment"),
    "trade balance": ("BOPGSTB", "Trade Balance"),
    "ism manufacturing": ("NAPM", "ISM Manufacturing PMI"),
    "pmi": ("NAPM", "ISM Manufacturing PMI"),
    # Retail gasoline — EIA Regular Conventional series via FRED (GASREGCOVW).
    #
    # GASREGCOVW = EIA Weekly U.S. Regular Conventional Retail Gasoline Prices
    # ($/gal), updated every Monday ~5 pm ET.  This is a FRED-native series
    # served reliably by the fredgraph.csv endpoint (no API key required).
    #
    # Note on accuracy vs AAA: GASREGCOVW excludes reformulated-fuel (RFG) cities
    # (LA, NYC, Chicago) and runs $0.10–0.20/gal below the AAA national average
    # that Kalshi KXAAAGASW resolves against.  The LARGE_DIVERGENCE gate (gap >
    # 40%) blocks auto-trading when the spread is too wide.  When EIA_API_KEY is
    # set, EIADataSource provides a more accurate reading and takes precedence
    # (confidence=0.90 vs 0.80 here) so this source acts as a fallback only.
    #
    # Why NOT APU000074714 (BLS CPI city-avg): that series ID is only accessible
    # via the FRED website — the fredgraph.csv shortcut endpoint returns HTTP 400
    # for BLS APU sub-item IDs, causing _fetch_fred_latest to return (None, None)
    # for every gas market and inflating no_source counts.
    "gas": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "gas price": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "gas prices": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "gasoline": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "gasoline price": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "average gas": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
    "AAA gas": ("GASREGCOVW", "US Weekly Retail Gasoline Price (EIA conv.)"),
}


# Foreign country/region indicators — markets mentioning these are about non-US
# economic data that FRED US series cannot resolve correctly.  Matched as
# substrings so "european" catches "euro", "eu " catches "eu gdp", etc.
_ECON_FOREIGN_INDICATORS = frozenset({
    "china", "chinese", "euro ", "european", "uk ", "united kingdom",
    "britain", "british", "japan", "japanese", "canada", "canadian",
    "australia", "australian", "germany", "german", "france", "french",
    "india", "indian", "brazil", "mexico", "russia", "russian",
    "south korea", "taiwan",
})


class EconomicDataSource(DataSource):
    """
    Fetches economic indicator data from FRED public API.
    """

    def can_handle(self, market: Market) -> bool:
        text = (market.question + " " + " ".join(market.tags)).lower()
        # Reject foreign-country markets: US FRED series cannot answer them.
        if any(ind in text for ind in _ECON_FOREIGN_INDICATORS):
            return False
        return (
            market.category.lower() in ("economics", "economy", "finance", "macro")
            or any(k in text for k in _INDICATOR_MAP)
            or market.market_id.startswith("KXAAAGASW")  # AAA weekly gas bracket series
            or any(word in text for word in (
                "federal reserve", "fomc", "rate hike", "rate cut",
                "basis points", "bps", "economic", "recession",
            ))
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            indicator_key, series_id, indicator_name = self._detect_indicator(market)
            if not series_id:
                logger.debug("EconSource: no indicator detected for %s", market.market_id)
                return None

            latest_value, latest_date = self._fetch_fred_latest(series_id)
            if latest_value is None:
                return None

            # Debug log: always emit the raw fetched value for GASREGCOVW so we can
            # diagnose whether stale data, a unit issue, or a series mismatch is the
            # root cause when prob=0.00 contradicts a near-certain market price.
            if series_id == "GASREGCOVW":
                logger.info(
                    "GASREGCOVW raw value: %s (date: %s) for %s",
                    latest_value, latest_date, market.market_id,
                )
                # Hard staleness cutoff for weekly series: if data is older than 6 days
                # (144h) we are at the tail end of the weekly cycle (publication is
                # expected the next Monday ~5pm ET).  Trading on week-old gas prices
                # that are about to be superseded produces wrong directional signals.
                # This check runs before _compute_prob so that even within the
                # _compute_confidence window (< 144h) we don't bet against a near-
                # certain market price that is anticipating the newer publication.
                if latest_date:
                    try:
                        release_dt = datetime.strptime(latest_date, "%Y-%m-%d").replace(
                            tzinfo=timezone.utc
                        )
                        hours_since_data = (
                            datetime.now(timezone.utc) - release_dt
                        ).total_seconds() / 3600
                        if hours_since_data >= 144:
                            logger.info(
                                "EconSource: GASREGCOVW data from %s is %.0fh old — "
                                "exceeds 6-day weekly-series limit; skipping %s",
                                latest_date, hours_since_data, market.market_id,
                            )
                            return None
                    except Exception:
                        pass

            # Suppress the signal if the next scheduled release is within 24 hours.
            # Trading on 6-day-old data the day before the monthly release is
            # pointless — the market has already anticipated the update.
            if self._next_release_within_hours(series_id, 24):
                logger.info(
                    "EconSource: %s has a scheduled release within 24h — "
                    "suppressing stale signal for %s",
                    series_id, market.market_id,
                )
                return None

            # Determine if the latest release is "fresh" (within the market window)
            threshold = self._extract_threshold(market.question, indicator_key)
            ground_truth_prob = self._compute_prob(latest_value, threshold, market)

            # Asymmetric series-mismatch buffer: GASREGCOVW (EIA Regular Conventional)
            # excludes reformulated-fuel cities (LA, NYC, Chicago) and reliably runs
            # $0.10–$0.20/gal BELOW the AAA national average that KXAAAGASW resolves
            # against.  Because AAA ≥ EIA, when the threshold is above (fetched − $0.20)
            # the AAA price might be above the threshold even if EIA is below it — we
            # would generate a false NO signal.  Suppress prob in that risky direction.
            #
            # Safe direction: when threshold < fetched − $0.20 (EIA is clearly above
            # threshold, and AAA is even higher), return prob=1.0 as normal.
            #
            # threshold > fetched − 0.20  →  suppress (prob=None): too close to call
            # threshold ≤ fetched − 0.20  →  allow:   gas is well above threshold
            if (
                series_id == "GASREGCOVW"
                and market.market_id.startswith("KXAAAGASW")
                and threshold is not None
                and threshold > (latest_value - 0.20)
            ):
                logger.info(
                    "EconSource: GASREGCOVW/KXAAAGASW asymmetric buffer — "
                    "threshold=%.3f > fetched=%.3f − $0.20 (=%.3f); "
                    "AAA/EIA spread could flip outcome; returning prob=None for %s",
                    threshold, latest_value, latest_value - 0.20, market.market_id,
                )
                ground_truth_prob = None

            confidence = self._compute_confidence(latest_date, market, series_id)

            if confidence is None:
                fresh_h, max_h = _SERIES_STALENESS.get(series_id, (24, 168))
                logger.debug(
                    "EconSource: %s data from %s exceeds max staleness (%dh) — skipping %s",
                    series_id, latest_date, max_h, market.market_id,
                )
                return None

            return GroundTruthResult(
                ground_truth_prob=ground_truth_prob,
                confidence=confidence,
                source_type=SourceType.HARD,
                source_name=f"FRED/{series_id}",
                source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                raw_data={
                    "series_id": series_id,
                    "indicator": indicator_name,
                    "latest_value": latest_value,
                    "latest_date": str(latest_date),
                    "threshold": threshold,
                },
                reasoning=(
                    f"FRED {indicator_name} ({series_id}): latest={latest_value:.4f} "
                    f"as of {latest_date}. "
                    + (f"Threshold={threshold:.4f}. " if threshold is not None else "No threshold extracted. ")
                    + (f"Derived prob={ground_truth_prob:.2f} " if ground_truth_prob is not None else "prob=None (no signal) ")
                    + f"confidence={confidence:.2f}."
                ),
            )

        except Exception as exc:
            logger.warning("EconSource: error for %s: %s", market.market_id, exc)
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _detect_indicator(self, market: Market) -> Tuple[str, str, str]:
        """Return (keyword, series_id, human_name) or ('', '', '')."""
        text = (market.question + " " + " ".join(market.tags)).lower()
        for keyword, (series_id, name) in _INDICATOR_MAP.items():
            if keyword in text:
                return keyword, series_id, name
        return "", "", ""

    def _fetch_fred_latest(self, series_id: str) -> Tuple[Optional[float], Optional[str]]:
        """Fetch most recent observation from FRED CSV endpoint.

        Results are cached for _FRED_CACHE_TTL seconds so that bracket-series
        markets (e.g. KXAAAGASW-26MAR02-2.888, -2.898, -2.908 …) share a
        single HTTP call rather than hitting FRED once per bracket level.
        """
        now = time.monotonic()
        cached = _FRED_CACHE.get(series_id)
        if cached:
            fetched_at, result = cached
            if now - fetched_at < _FRED_CACHE_TTL:
                return result

        url = f"{_FRED_BASE}?id={series_id}"
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            lines = resp.text.strip().split("\n")
            # Lines: header + data rows (DATE,VALUE)
            # Find last non-missing value
            for line in reversed(lines[1:]):
                parts = line.strip().split(",")
                if len(parts) == 2 and parts[1] not in (".", ""):
                    try:
                        result = (float(parts[1]), parts[0])
                        _FRED_CACHE[series_id] = (now, result)
                        return result
                    except ValueError:
                        continue
            _FRED_CACHE[series_id] = (now, (None, None))
            return None, None
        except Exception as exc:
            logger.debug("EconSource: FRED fetch failed for %s: %s", series_id, exc)
            return None, None

    def _extract_threshold(self, question: str, indicator_key: str) -> Optional[float]:
        """Extract numeric threshold from the market question if present.

        Handles currency-prefixed values like "$2.888" and "$3.50" in addition
        to bare numerics and percentage/k-suffix formats.
        """
        # \$? allows an optional dollar sign between the comparison word and the
        # number — Kalshi gas/price markets use "above $2.888" formatting.
        patterns = [
            r"(?:above|over|greater than|exceed|more than|>)\s*\$?\s*([\d,]+\.?\d*)\s*%?k?",
            r"(?:below|under|less than|<)\s*\$?\s*([\d,]+\.?\d*)\s*%?k?",
            r"(?:at least|>=)\s*\$?\s*([\d,]+\.?\d*)\s*%?k?",
            r"([\d,]+\.?\d*)\s*%?\s*(?:or (?:higher|lower|more|less))",
        ]
        for pat in patterns:
            m = re.search(pat, question, re.IGNORECASE)
            if m:
                val_str = m.group(1).replace(",", "")
                try:
                    val = float(val_str)
                    # Handle "k" suffix (thousands)
                    if "k" in question[m.start():m.end()].lower():
                        val *= 1000
                    return val
                except ValueError:
                    continue
        return None

    def _compute_prob(
        self,
        value: float,
        threshold: Optional[float],
        market: Market,
    ) -> Optional[float]:
        """
        Estimate YES probability based on latest value vs threshold.
        Returns None when no threshold is extractable — callers treat this as
        "no signal" rather than the 0.5 that previously leaked into tradeable
        results and caused spurious trades on every economic market.
        """
        if threshold is None:
            logger.debug(
                "EconSource: _compute_prob → None (no threshold extracted) for %s",
                market.market_id,
            )
            return None

        logger.debug(
            "EconSource: _compute_prob — market=%s fetched_value=%.4f threshold=%.4f",
            market.market_id, value, threshold,
        )

        question_lower = market.question.lower()
        above_phrasing = any(
            w in question_lower
            for w in ("above", "over", "exceed", "higher", "greater", "more than", ">")
        )
        below_phrasing = any(
            w in question_lower
            for w in ("below", "under", "less than", "lower", "<")
        )

        if above_phrasing:
            prob = 1.0 if value > threshold else 0.0
        elif below_phrasing:
            prob = 1.0 if value < threshold else 0.0
        else:
            # Equal or near: treat as 1.0 if within 2% tolerance
            if abs(value - threshold) / max(abs(threshold), 1e-6) < 0.02:
                prob = 1.0
            else:
                prob = 0.0

        logger.debug(
            "EconSource: _compute_prob → prob=%.1f "
            "(value=%.4f %s threshold=%.4f) for %s",
            prob, value,
            ">" if above_phrasing else ("<" if below_phrasing else "~"),
            threshold, market.market_id,
        )
        return prob

    def compute_bracket_prob(self, raw_value: float, market: Market) -> Optional[float]:
        """Re-derive YES probability for a bracket market using a pre-fetched raw value.

        Called by GroundTruthRouter.recompute_bracket_prob() so the executor can
        compute probabilities for all markets in a bracket series (e.g.
        KXAAAGASW-26MAR02-2.888, -2.898, …) from a single cached underlying value
        without making repeated GT fetches.
        """
        indicator_key, _, _ = self._detect_indicator(market)
        threshold = self._extract_threshold(market.question, indicator_key)
        return self._compute_prob(raw_value, threshold, market)

    def _references_historical_period(self, question: str) -> bool:
        """
        True if the question is about a specific fixed historical data point
        (e.g. "Was GDP above 2% in Q3 2024?").  For such questions the latest
        FRED observation is the definitive answer regardless of how long ago it
        was published, so the normal staleness gate does not apply.
        """
        return bool(re.search(
            r"\b(?:Q[1-4]\s+20\d{2}|20\d{2}\s+Q[1-4]"
            r"|(?:january|february|march|april|may|june|july|august"
            r"|september|october|november|december)\s+20\d{2}"
            r"|(?:fiscal\s+)?year\s+20\d{2}"
            r"|(?:20\d{2})\s+annual)\b",
            question, re.IGNORECASE,
        ))

    def _compute_confidence(
        self,
        latest_date: Optional[str],
        market: Market,
        series_id: str = "",
    ) -> Optional[float]:
        """
        Return confidence based on how recently the data was released, or None
        if the data is too stale to trade on.

        Per-series windows (from _SERIES_STALENESS) override the defaults:
          < fresh_hours → 0.95  (data released very recently)
          < max_hours   → 0.80  (within the relevant release cycle)
          ≥ max_hours   → None  (market has already priced this in; skip)

        Exception: questions that reference a specific historical period
        ("in Q3 2024", "for fiscal year 2022") are about a fixed past value
        — staleness is irrelevant because the answer cannot change.
        """
        if latest_date is None:
            return 0.0

        # Historical-period questions: the data point is immutable, so staleness
        # doesn't affect signal quality.
        if self._references_historical_period(market.question):
            return 0.80

        try:
            release_dt = datetime.strptime(latest_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            now = datetime.now(timezone.utc)
            hours_since = (now - release_dt).total_seconds() / 3600

            # Use series-specific window if available, otherwise fall back to defaults
            fresh_hours, max_hours = _SERIES_STALENESS.get(series_id, (24, 168))

            if hours_since < fresh_hours:
                return 0.95
            if hours_since < max_hours:
                return 0.80
            return None
        except Exception:
            return None

    def _next_release_within_hours(self, series_id: str, hours: int = 24) -> bool:
        """
        Return True if the next scheduled FRED release for this series is within
        `hours` hours.  Requires a FRED API key (FRED_API_KEY env var).

        Without a key this always returns False (suppression is disabled).
        The FRED release calendar API:
          GET /fred/series/release/dates?series_id=X&api_key=KEY&sort_order=desc
        """
        if not _FRED_API_KEY:
            return False
        try:
            resp = requests.get(
                f"{_FRED_API_BASE}/series/release/dates",
                params={
                    "series_id": series_id,
                    "api_key": _FRED_API_KEY,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 5,
                    "include_release_dates_with_no_data": "false",
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            release_dates = resp.json().get("release_dates", [])
            now = datetime.now(timezone.utc)
            for rd in release_dates:
                date_str = rd.get("date", "")
                if not date_str:
                    continue
                try:
                    release_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    hours_until = (release_dt - now).total_seconds() / 3600
                    if 0 < hours_until < hours:
                        return True
                except ValueError:
                    continue
        except Exception as exc:
            logger.debug("EconSource: FRED release calendar check failed: %s", exc)
        return False
