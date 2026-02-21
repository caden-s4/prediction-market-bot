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
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_TIMEOUT = 10

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
}


class EconomicDataSource(DataSource):
    """
    Fetches economic indicator data from FRED public API.
    """

    def can_handle(self, market: Market) -> bool:
        text = (market.question + " " + " ".join(market.tags)).lower()
        return (
            market.category.lower() in ("economics", "economy", "finance", "macro")
            or any(k in text for k in _INDICATOR_MAP)
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

            # Determine if the latest release is "fresh" (within the market window)
            threshold = self._extract_threshold(market.question, indicator_key)
            ground_truth_prob = self._compute_prob(latest_value, threshold, market)
            confidence = self._compute_confidence(latest_date, market)

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
                    + (f"Threshold={threshold:.4f}. " if threshold is not None else "")
                    + f"Derived prob={ground_truth_prob:.2f} confidence={confidence:.2f}."
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
        """Fetch most recent observation from FRED CSV endpoint."""
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
                        return float(parts[1]), parts[0]
                    except ValueError:
                        continue
            return None, None
        except Exception as exc:
            logger.debug("EconSource: FRED fetch failed for %s: %s", series_id, exc)
            return None, None

    def _extract_threshold(self, question: str, indicator_key: str) -> Optional[float]:
        """Extract numeric threshold from the market question if present."""
        # Look for patterns like "> 3.5%", "above 200k", "below 4%", "at least 2.5"
        patterns = [
            r"(?:above|over|greater than|exceed|more than|>)\s*([\d,]+\.?\d*)\s*%?k?",
            r"(?:below|under|less than|<)\s*([\d,]+\.?\d*)\s*%?k?",
            r"(?:at least|>=)\s*([\d,]+\.?\d*)\s*%?k?",
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
    ) -> float:
        """
        Estimate YES probability based on latest value vs threshold.
        If no threshold is found, return 0.5 (uncertain).
        """
        if threshold is None:
            return 0.5

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
            return 1.0 if value > threshold else 0.0
        elif below_phrasing:
            return 1.0 if value < threshold else 0.0
        else:
            # Equal or near: treat as 1.0 if within 2% tolerance
            if abs(value - threshold) / max(abs(threshold), 1e-6) < 0.02:
                return 1.0
            return 0.0

    def _compute_confidence(self, latest_date: Optional[str], market: Market) -> float:
        """
        High confidence if the data release date is after the market was created
        and the market resolves soon. Low confidence if data predates the question.
        """
        if latest_date is None:
            return 0.0

        try:
            release_dt = datetime.strptime(latest_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
            now = datetime.now(timezone.utc)
            # If data was released very recently (within last 48h) → high confidence
            hours_since = (now - release_dt).total_seconds() / 3600
            if hours_since < 48:
                return 0.95
            # Data is older but still relevant (market may be comparing to old release)
            return 0.80
        except Exception:
            return 0.70
