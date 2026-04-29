"""
data.ground_truth.weather_kalshi — Kalshi weather ticker parser.

Parses Kalshi daily temperature market IDs (KXHIGHT*/KXLOWT*) into structured
WeatherMarket objects and maps Kalshi city abbreviations to NWS CLI station codes.

Not wired into the GT router — used by the validation script and eventually Phase 1C.

Bracket interpretation: B{N} means the band is [N - 0.5, N + 0.5] inclusive,
so B89.5 → [89.0, 90.0]. This matches Kalshi's rules_secondary language where
brackets span a 1°F window centered on the strike midpoint.

T-prefix interpretation: threshold_type is "above" or "below", determined from
question text. The ticker alone does not disambiguate direction.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# Kalshi city abbreviation → NWS CLI station code.
# Verified against rules_secondary text from Kalshi API on 2026-04-28.
# Note: some abbreviations differ between Kalshi tickers and NWS station codes.
_CITY_TO_CLI: dict[str, str] = {
    'PHX':  'PHX',
    'LAX':  'LAX',
    'HOU':  'HOU',
    'SATX': 'SAT',
    'NOLA': 'MSY',
    'ATL':  'ATL',
    'DAL':  'DFW',     # Kalshi uses DAL, but NWS station is DFW
    'DC':   'DCA',
    'SFO':  'SFO',
    'SEA':  'SEA',
    'OKC':  'OKC',
    'BOS':  'BOS',
    'MIN':  'MSP',     # Kalshi uses MIN (Minneapolis), but NWS station is MSP
    'MIA':  'MIA',
    'AUS':  'AUS',
    'CHI':  'MDW',     # Kalshi resolves Chicago against Midway, not O'Hare
    'DEN':  'DEN',
    'NYC':  'NYC',
    'PHIL': 'PHL',     # Kalshi uses PHIL, but NWS station is PHL
    'LV':   'LAS',     # Kalshi uses LV (Las Vegas), but NWS station is LAS
}

_MONTH_MAP: dict[str, int] = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

# Pattern: KX(HIGHT|LOWT){CITY}-{YYMMMDD}-{T|B}{STRIKE}
# CITY: 2–4 uppercase letters  |  YYMMMDD: 2-digit year + 3-letter month + 2-digit day
# Strike: integer or decimal float (e.g. 94, 89.5)
_TICKER_RE = re.compile(
    r"^KX(HIGHT|LOWT)([A-Z]{2,4})-(\d{2})([A-Z]{3})(\d{2})-([TB])(\d+(?:\.\d+)?)$"
)


@dataclass
class WeatherMarket:
    ticker: str
    city: str              # Kalshi city abbreviation, e.g. "PHX", "DAL"
    cli_station: str       # NWS station code for CLI lookup, e.g. "PHX", "DFW"
    target_date: date
    market_type: str       # "high" or "low"
    threshold_type: str    # "above", "below", or "bracket"
    threshold_value: float # the strike, e.g. 94.0 or 89.5
    bracket_low: Optional[float]   # for bracket markets, lower bound (inclusive)
    bracket_high: Optional[float]  # for bracket markets, upper bound (inclusive)


# Patterns for detecting direction in T-prefix market question text.
_ABOVE_RE = re.compile(r"(?:>|greater than|above)", re.IGNORECASE)
_BELOW_RE = re.compile(r"(?:<|less than|below)", re.IGNORECASE)


def parse_weather_ticker(
    ticker: str,
    question: Optional[str] = None,
) -> Optional[WeatherMarket]:
    """Parse a Kalshi weather market ID into structured form.

    Args:
        ticker:   Kalshi market ticker, e.g. "KXHIGHTPHX-26APR29-T94".
        question: Market question text. Required for T-prefix tickers so direction
                  ("above" vs "below") can be determined. Unused for B-prefix tickers.

    Returns None if the ticker is not a recognized weather market, silently when
    the regex doesn't match (not a weather ticker) or with a warning when the city
    is unrecognized, direction is ambiguous, or question is absent for T-prefix.
    """
    m = _TICKER_RE.match(ticker)
    if m is None:
        return None

    temp_type, city, yy, mon_str, dd, strike_prefix, strike_str = m.groups()

    month = _MONTH_MAP.get(mon_str.upper())
    if month is None:
        return None
    try:
        target_date = date(2000 + int(yy), month, int(dd))
    except ValueError:
        return None

    cli_station = _CITY_TO_CLI.get(city)
    if cli_station is None:
        logger.warning("Unknown Kalshi city abbreviation %r in ticker %s", city, ticker)
        return None

    market_type = "high" if temp_type == "HIGHT" else "low"
    threshold_value = float(strike_str)

    if strike_prefix == "T":
        if question is None:
            logger.warning("T-prefix market %s requires question text to determine direction", ticker)
            return None
        above_match = _ABOVE_RE.search(question)
        below_match = _BELOW_RE.search(question)
        if above_match and below_match:
            # Both patterns found — pick whichever appears first in the text.
            if above_match.start() < below_match.start():
                threshold_type = "above"
            else:
                threshold_type = "below"
        elif above_match:
            threshold_type = "above"
        elif below_match:
            threshold_type = "below"
        else:
            logger.warning("Could not determine direction from question: %r", question)
            return None
        bracket_low = None
        bracket_high = None
    else:  # "B"
        threshold_type = "bracket"
        bracket_low = threshold_value - 0.5
        bracket_high = threshold_value + 0.5

    return WeatherMarket(
        ticker=ticker,
        city=city,
        cli_station=cli_station,
        target_date=target_date,
        market_type=market_type,
        threshold_type=threshold_type,
        threshold_value=threshold_value,
        bracket_low=bracket_low,
        bracket_high=bracket_high,
    )
