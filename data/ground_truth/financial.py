"""
data.ground_truth.financial – real-time financial instrument prices.

Primary source: Twelve Data API (set TWELVEDATA_API_KEY in .env).
  Free tier: 800 calls/day, 8/minute — sufficient with the 60-second symbol cache.
  Confidence: 0.85 (commercial API, reliable but not an exchange feed).
  Register at https://twelvedata.com/apikey (instant, no credit card).

Fallback: Yahoo Finance (unofficial scraping endpoint, no key required).
  Confidence: capped at 0.55 for markets beyond 8 h — not trustworthy enough
  to fire signals well before resolution.  Use Twelve Data for live trading.

Covers:
  - US stock indices : Nasdaq 100 (NDX/NQ=F), S&P 500 (SPX/ES=F), Dow (YM=F)
  - Forex pairs      : EUR/USD, USD/JPY, GBP/USD, USD/CAD, AUD/USD
  - Treasury yields  : 10-yr (TNX), 5-yr (US5Y), 2-yr (US3M), 30-yr (US30Y)
  - Commodities      : Gold (GC), WTI Crude (CL/USD), Natural Gas (NG)

The module-level price cache limits each symbol to one HTTP request per 60 seconds
regardless of how many markets reference the same instrument.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, datetime
from typing import Dict, Optional, Tuple

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:          # Python < 3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

_YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
_TD_BASE = "https://api.twelvedata.com"
_AV_BASE = "https://www.alphavantage.co/query"
_TIMEOUT = 3

# Optional API keys — set in .env to use more reliable primary sources.
# Accepted env var names for Twelve Data (checked in order, first non-empty wins):
#   TWELVEDATA_API_KEY  — canonical name used in .env.example
#   TWELVE_API_KEY      — common short alias users may set instead
# ALPHA_VANTAGE_KEY: free tier (25/day) is too low; paid tier not worth it over TD.
# When no key is set, Yahoo Finance is used as an unofficial fallback.
_TWELVE_DATA_KEY: str = (
    os.environ.get("TWELVEDATA_API_KEY", "")
    or os.environ.get("TWELVE_API_KEY", "")
)
_ALPHA_VANTAGE_KEY: str = os.environ.get("ALPHA_VANTAGE_KEY", "")

# Simulation mode: use Yahoo Finance prices but report pro-level confidence.
# Set SIMULATE_PRO_DATA=true in .env to evaluate strategy profitability without
# a Twelve Data subscription.  Prices still come from Yahoo Finance; only the
# time-confidence floor is lifted to 0.85 so signals pass the 0.80 gate.
# Every fetch under this mode logs a clearly marked [SIMULATED PRO] warning so
# ghost trades are distinguishable from live signals.  Never use in production.
_SIMULATE_PRO_DATA: bool = os.environ.get("SIMULATE_PRO_DATA", "").lower() in (
    "1", "true", "yes"
)

# Twelve Data symbols known to require a paid plan (Grow tier or above).
# On the free tier these return {"code": 404, "message": "... Grow plan ..."}
# every single cycle, flooding logs with WARNING noise.  These are silently
# skipped and fall through to Yahoo Finance instead.  Update this set after
# upgrading to a paid Twelve Data plan.
_TD_FREE_TIER_BLOCKED: frozenset = frozenset({
    "NDX",    # Nasdaq 100 index (NQ=F) — requires paid plan
    "SPX",    # S&P 500 index (ES=F / ^GSPC) — requires paid plan
    "RUT",    # Russell 2000 (^RUT) — requires paid plan
    "VIX",    # CBOE Volatility Index (^VIX) — requires paid plan
    "TNX",    # 10-yr Treasury yield (^TNX)
    "US5Y",   # 5-yr Treasury yield (^FVX)
    "US3M",   # 2-yr Treasury yield (^IRX)
    "US30Y",  # 30-yr Treasury yield (^TYX)
    "GC",     # Gold spot — invalid/unavailable on free tier
    "GC1!",   # Gold front-month futures — requires paid plan
    "CL1!",   # WTI Crude front-month futures — requires paid plan
    "SI",     # Silver spot — requires paid plan
    "SI1!",   # Silver front-month futures — requires paid plan
})

# Twelve Data symbol translations from Yahoo Finance symbols.
# Twelve Data uses spot-index symbols for NDX/SPX and its own commodity format.
_TD_SYMBOL_MAP: Dict[str, str] = {
    # Indices — Twelve Data spot symbols (NDX, SPX) match Kalshi resolution values
    "NQ=F": "NDX",      # Nasdaq 100 (spot index; Kalshi resolves against NDX, not NQ futures)
    "ES=F": "SPX",      # S&P 500 (spot index)
    "YM=F": "YM",       # Dow Jones E-mini (no spot equivalent available free-tier)
    # Commodities — continuous front-month futures (standard Twelve Data format)
    "GC=F": "GC1!",     # Gold front-month futures (not "GC" which is spot/expired)
    "CL=F": "CL1!",     # WTI Crude front-month futures ("CL/USD" was invalid on free tier)
    "NG=F": "NG",       # Natural Gas futures
    "SI=F": "SI1!",     # Silver front-month futures
    # Indices — additional Yahoo Finance tickers for the same underlyings
    "^GSPC": "SPX",     # S&P 500 via Yahoo ^GSPC → Twelve Data SPX
    "^RUT":  "RUT",     # Russell 2000
    "^VIX":  "VIX",     # CBOE Volatility Index
    # Forex (Twelve Data uses standard ISO pairs)
    "EURUSD=X": "EUR/USD",
    "JPY=X":    "USD/JPY",
    "GBPUSD=X": "GBP/USD",
    "CAD=X":    "USD/CAD",
    "AUDUSD=X": "AUD/USD",
    "CHF=X":    "USD/CHF",
    # Treasury yields
    "^TNX": "TNX",
    "^FVX": "US5Y",
    "^IRX": "US3M",
    "^TYX": "US30Y",
}

# Alpha Vantage symbol translations.
# AV supports standard equity/ETF tickers and some forex; futures require a
# paid subscription so we only map what's available on the free tier.
_AV_SYMBOL_MAP: Dict[str, str] = {
    # Forex — use CURRENCY_EXCHANGE_RATE function (free tier)
    "EURUSD=X": "EUR:USD",
    "JPY=X":    "USD:JPY",
    "GBPUSD=X": "GBP:USD",
    "CAD=X":    "USD:CAD",
    "AUDUSD=X": "AUD:USD",
    "CHF=X":    "USD:CHF",
    # Treasury yields — available via GLOBAL_QUOTE
    "^TNX": "^TNX",
    "^FVX": "^FVX",
    "^IRX": "^IRX",
    "^TYX": "^TYX",
}

# Map text keyword → (Yahoo Finance symbol, human-readable name).
# Sorted longest-first when searching so "nasdaq 100" matches before "nasdaq".
_INSTRUMENT_MAP: Dict[str, Tuple[str, str]] = {
    # ── Indices (E-mini futures — trade 24/5, correct pre/post-market prices) ──
    "nasdaq 100":       ("NQ=F",  "Nasdaq 100"),
    "nasdaq-100":       ("NQ=F",  "Nasdaq 100"),
    "nasdaq100":        ("NQ=F",  "Nasdaq 100"),
    "nasdaq":           ("NQ=F",  "Nasdaq 100"),
    "ndx":              ("NQ=F",  "Nasdaq 100"),
    "s&p 500":          ("ES=F",  "S&P 500"),
    "sp 500":           ("ES=F",  "S&P 500"),
    "sp500":            ("ES=F",  "S&P 500"),
    "s&p500":           ("ES=F",  "S&P 500"),
    "s&p":              ("ES=F",  "S&P 500"),
    "spx":              ("ES=F",  "S&P 500"),
    "dow jones":        ("YM=F",  "Dow Jones"),
    "dow":              ("YM=F",  "Dow Jones"),
    # ── Forex ────────────────────────────────────────────────────────────────
    "eur/usd":          ("EURUSD=X", "EUR/USD"),
    "euro/dollar":      ("EURUSD=X", "EUR/USD"),
    "eurusd":           ("EURUSD=X", "EUR/USD"),
    "usd/jpy":          ("JPY=X",    "USD/JPY"),
    "dollar/yen":       ("JPY=X",    "USD/JPY"),
    "usdjpy":           ("JPY=X",    "USD/JPY"),
    "gbp/usd":          ("GBPUSD=X", "GBP/USD"),
    "cable":            ("GBPUSD=X", "GBP/USD"),
    "gbpusd":           ("GBPUSD=X", "GBP/USD"),
    "usd/cad":          ("CAD=X",    "USD/CAD"),
    "usdcad":           ("CAD=X",    "USD/CAD"),
    "aud/usd":          ("AUDUSD=X", "AUD/USD"),
    "audusd":           ("AUDUSD=X", "AUD/USD"),
    "usd/chf":          ("CHF=X",    "USD/CHF"),
    "usdchf":           ("CHF=X",    "USD/CHF"),
    # ── Treasury yields ──────────────────────────────────────────────────────
    "10-year treasury": ("^TNX",  "10-Year Treasury Yield"),
    "10-year yield":    ("^TNX",  "10-Year Treasury Yield"),
    "10yr treasury":    ("^TNX",  "10-Year Treasury Yield"),
    "10yr yield":       ("^TNX",  "10-Year Treasury Yield"),
    "10 year treasury": ("^TNX",  "10-Year Treasury Yield"),
    "t-note":           ("^TNX",  "10-Year Treasury Yield"),
    "tnote":            ("^TNX",  "10-Year Treasury Yield"),
    "treasury note":    ("^TNX",  "10-Year Treasury Yield"),
    "2-year treasury":  ("^IRX",  "2-Year Treasury Yield"),
    "2yr treasury":     ("^IRX",  "2-Year Treasury Yield"),
    "30-year treasury": ("^TYX",  "30-Year Treasury Yield"),
    "30yr treasury":    ("^TYX",  "30-Year Treasury Yield"),
    # ── Commodities ──────────────────────────────────────────────────────────
    "gold price":       ("GC=F",  "Gold Futures"),
    "gold":             ("GC=F",  "Gold Futures"),
    "crude oil":        ("CL=F",  "WTI Crude Oil"),
    "wti":              ("CL=F",  "WTI Crude Oil"),
    "natural gas":      ("NG=F",  "Natural Gas Futures"),
}

# Keywords that reliably indicate a financial-level market (used in can_handle).
_DETECT_KEYWORDS = tuple(_INSTRUMENT_MAP.keys()) + (
    "close above", "close below", "close at", "settle above", "settle below",
)

# ── Exclusion gate 1: question-text keywords ──────────────────────────────────
# If any of these phrases appear in the market question (case-insensitive), the
# market is NOT about a financial instrument — reject before any ticker matching.
#
# Root-cause example: "Will Donald Trump's approval rating be above 42.3%?"
# The market_id KXVOTEHUBTRUMPUPDOWN contains the substring "down" which
# includes "dow", triggering the "dow" → YM=F entry in _INSTRUMENT_MAP and
# producing a nonsensical 46,857 vs 42.3 comparison with prob=1.00.
FINANCIAL_EXCLUSION_KEYWORDS: tuple = (
    "approval rating",
    "favorability",
    "poll",
    "polling",
    "disapproval",
    "job approval",
    "vote",
    "election",
    "ballot",
    "nominee",
    "primary",
    "caucus",
)

# ── Exclusion gate 2: known non-financial Kalshi series prefixes ──────────────
# Series whose market_ids may contain financial-sounding substrings but are
# definitively about politics/polling, not financial instruments.
FINANCIAL_EXCLUDED_SERIES: frozenset = frozenset({
    "KXVOTEHUB",
    "KXPRESMENTION",
    "KXMENTION",
    "KXAPPROVAL",
})

# Regex that identifies the date segment in a Kalshi market_id
# (e.g. "-26MAR12" in "KXVOTEHUBTRUMPUPDOWN-26MAR12").
# Everything before the first match is the series prefix.
_SERIES_PREFIX_RE = re.compile(r"-\d{2}[A-Z]{3}\d{2}")


def _extract_series_prefix(market_id: str) -> str:
    """Return the series root of a Kalshi market ID (before the date segment).

    Example: "KXVOTEHUBTRUMPUPDOWN-26MAR12-T42.3" → "KXVOTEHUBTRUMPUPDOWN"
    """
    m = _SERIES_PREFIX_RE.search(market_id)
    if m:
        return market_id[: m.start()]
    return market_id.split("-")[0]


# Module-level price cache: symbol → (fetched_at_monotonic, price, source_key)
# Caches both successes AND failures to prevent the same broken symbol from
# being retried on every market that references it within the same cycle.
#
# TTL policy:
#   Success → 60 s (_CACHE_TTL): re-fetch at most once per minute per symbol.
#   Failure → 30 s (_FAILURE_CACHE_TTL): skip retries within the same cycle
#             (typical cycle is 15–20 s) so unresponsive APIs don't burn
#             N×timeout when N markets share the same underlying instrument.
_PRICE_CACHE: dict = {}
_CACHE_TTL = 60          # seconds — success cache
_FAILURE_CACHE_TTL = 30  # seconds — failure cache (within-cycle dedup)

# Futures symbols that roll quarterly (March/June/September/December).
# Around the rollover window (2nd–3rd week of the expiry month) the
# continuous-contract price can gap at the changeover.
_FUTURES_SYMBOLS = frozenset({"NQ=F", "ES=F", "YM=F", "GC=F", "CL=F", "NG=F"})
_ROLLOVER_MONTHS = frozenset({3, 6, 9, 12})

# Regex that identifies questions specifically about a closing/settlement price.
# These should only trade when the official session is open; pre-market prices
# don't predict what the close will be.
_CLOSE_QUESTION_RE = re.compile(
    r"\b(close|closing price|settlement price|end of day|eod|4[:\s]?00\s*(?:pm|et))\b",
    re.IGNORECASE,
)

# Time-decay for financial information signals.
#
# Spot prices become unreliable predictors of settlement over long horizons
# (NQ can move 3-5% in a day).  We decay source confidence linearly from 1.0
# to a source-dependent floor beyond _MAX_SIGNAL_HOURS.
#
# Floor by data source:
#   Twelve Data (commercial API)  → 0.85  fires at any horizon with ≥5% margin
#   Yahoo Finance (unofficial)    → 0.55  blocked beyond ~3h (floor < 0.80 gate)
#
# Calibration (Yahoo Finance / floor=0.55):
#   ≤ 1h  → 1.00  (spot ≈ settlement; full confidence)
#   2h    → 0.91  (fires for any spatial-confident signal)
#   3h    → 0.82  (fires only for ≥5%-margin signals)
#   4h    → 0.73  (BLOCKED — time_conf < 0.80 gate)
#   8h+   → 0.55  (BLOCKED — floor)
#
# Calibration (Twelve Data / floor=0.85):
#   ≤ 1h  → 1.00
#   any   → 0.85–1.00  (always above 0.80 gate; spatial margin still applies)
_MAX_SIGNAL_HOURS: float = 8.0
_FULL_SIGNAL_HOURS: float = 1.0
_TIME_CONF_FLOOR: float = 0.55          # Yahoo Finance fallback floor
_TD_TIME_CONF_FLOOR: float = 0.85       # Twelve Data primary floor
_TD_MAX_SPATIAL_CONF: float = 0.85      # Twelve Data spatial confidence cap

# Regex to extract threshold and direction from a market question.
_ABOVE_RE = re.compile(
    r"(?:above|over|exceed|higher than|greater than|close above|settle above|at least)\s*"
    r"\$?\s*([\d,]+\.?\d*)",
    re.IGNORECASE,
)
_BELOW_RE = re.compile(
    r"(?:below|under|less than|lower than|close below|settle below|at most)\s*"
    r"\$?\s*([\d,]+\.?\d*)",
    re.IGNORECASE,
)
# Detects price-range questions like "Will WTI be $63-63.99?" or "$63 to $64".
# These are bucket/range markets that cannot be reduced to a single above/below
# threshold — the -B{val} Kalshi suffix means "bucket at val", NOT "below val".
_RANGE_RE = re.compile(
    r"\$\s*([\d,]+\.?\d*)\s*[-–]\s*\$?\s*([\d,]+\.?\d*)",
    re.IGNORECASE,
)
# Detects "between X and Y" / "from X to Y" phrasing WITHOUT a leading dollar sign
# (e.g. "Will the Nasdaq-100 be between 25500 and 25599.99 at 4pm?").
# These are range/bucket markets that cannot be handled with a single threshold.
_BETWEEN_RE = re.compile(
    r"\b(?:between|from)\s+\$?\s*[\d,]+\.?\d*\s+(?:and|to)\s+\$?\s*[\d,]+\.?\d*",
    re.IGNORECASE,
)
# Reverse-direction patterns: the threshold number appears BEFORE the direction
# word.  Standard regexes above require "below $60" (keyword then number).
# These catch the common Kalshi phrasing "$59.99 or below", "60.00 or more", etc.
# Must be checked AFTER the forward patterns so "$60 or below $61" isn't mis-parsed.
_BELOW_SUFFIX_RE = re.compile(
    r"\$?\s*([\d,]+\.?\d*)\s*%?\s+or\s+(?:below|less|under)\b",
    re.IGNORECASE,
)
_ABOVE_SUFFIX_RE = re.compile(
    r"\$?\s*([\d,]+\.?\d*)\s*%?\s+or\s+(?:above|more|higher|over)\b",
    re.IGNORECASE,
)


class FinancialDataSource(DataSource):
    """
    Fetches real-time prices/yields from Yahoo Finance for markets about
    index levels, forex rates, and Treasury yields.
    """

    def __init__(self) -> None:
        # Diagnostic: always log exactly which env vars are present and which
        # provider was selected.  Run once at startup so operators can immediately
        # spot "key present=False" without having to grep through cycle logs.
        td_canonical = bool(os.environ.get("TWELVEDATA_API_KEY"))
        td_alias     = bool(os.environ.get("TWELVE_API_KEY"))
        logger.info(
            "FinancialDataSource: TWELVE_API_KEY present=%s  "
            "TWELVEDATA_API_KEY present=%s",
            td_alias, td_canonical,
        )
        if _TWELVE_DATA_KEY:
            provider = "twelve_data"
        elif _ALPHA_VANTAGE_KEY:
            provider = "alpha_vantage"
        else:
            provider = "yahoo (fallback — set TWELVEDATA_API_KEY or TWELVE_API_KEY for higher confidence)"
        logger.info(
            "FinancialDataSource: active provider=%s  key_active=%s",
            provider, bool(_TWELVE_DATA_KEY),
        )
        if _SIMULATE_PRO_DATA and not _TWELVE_DATA_KEY:
            logger.warning(
                "FinancialDataSource: SIMULATE_PRO_DATA=true — Yahoo Finance prices "
                "will be reported with confidence=0.85 (pro-level floor).  "
                "Ghost trades are for strategy evaluation ONLY; "
                "do not use in production without a real Twelve Data key."
            )
        # Log the full symbol translation table once at startup so operators can
        # verify every Yahoo→Twelve Data mapping without grepping source code.
        logger.info(
            "FinancialDataSource: ticker translations loaded — %s",
            list(_TD_SYMBOL_MAP.keys()),
        )

    def can_handle(self, market: Market) -> bool:
        # ── Exclusion gate 1: question-text keywords ──────────────────────────
        # Reject non-financial markets before any ticker matching runs.
        question_lower = market.question.lower()
        if any(kw in question_lower for kw in FINANCIAL_EXCLUSION_KEYWORDS):
            return False

        # ── Exclusion gate 2: known non-financial Kalshi series ───────────────
        if _extract_series_prefix(market.market_id) in FINANCIAL_EXCLUDED_SERIES:
            return False

        # Include market_id so Kalshi tickers like KXUSDJPY, KXEURUSD, KXNASDAQ100
        # are caught even when the question text uses different phrasing.
        text = (
            market.question + " " + " ".join(market.tags) + " " + market.market_id
        ).lower()
        return (
            market.category.lower() in ("financials", "finance")
            or any(kw in text for kw in _DETECT_KEYWORDS)
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            # ── Exclusion gate 1: question-text keywords ──────────────────────
            question_lower = market.question.lower()
            if any(kw in question_lower for kw in FINANCIAL_EXCLUSION_KEYWORDS):
                logger.warning(
                    "FinancialSource: rejected %s — exclusion keyword match "
                    "(question snippet: '%.80s')",
                    market.market_id, market.question,
                )
                return None

            # ── Exclusion gate 2: known non-financial Kalshi series ───────────
            series = _extract_series_prefix(market.market_id)
            if series in FINANCIAL_EXCLUDED_SERIES:
                logger.warning(
                    "FinancialSource: rejected %s — excluded series prefix '%s' "
                    "(question snippet: '%.80s')",
                    market.market_id, series, market.question,
                )
                return None

            symbol, instrument_name = self._detect_instrument(market)
            if not symbol:
                logger.debug(
                    "FinancialSource: no instrument detected for %s", market.market_id
                )
                return None

            current_price, price_source = self._fetch_price(symbol)
            if current_price is None:
                return None

            # Close-price questions: block only during pre-market (before 9:30 AM ET).
            # Pre-market prices are speculative — the session hasn't started yet and
            # there's no signal about where the index will close.
            # Post-market (after 16:00 ET) the official close is already settled:
            # Yahoo Finance returns the authoritative close price and we SHOULD fire.
            if self._is_close_question(market.question) and self._us_equity_premarket():
                logger.debug(
                    "FinancialSource: %s is a close-price question and market is "
                    "pre-market — current price unreliable for close prediction, skipping",
                    market.market_id,
                )
                return None

            threshold, is_above = self._extract_threshold_and_direction(
                market.question, market.market_id
            )
            if threshold is None:
                logger.debug(
                    "FinancialSource: no threshold in question for %s", market.market_id
                )
                return None

            # ── Exclusion gate 3: magnitude sanity check ──────────────────────
            # If the fetched price and the parsed threshold are more than 100×
            # apart, we almost certainly routed to the wrong instrument
            # (e.g. futures at 46,857 vs an approval-rating threshold of 42.3).
            # No legitimate financial comparison should ever span two orders of
            # magnitude — the threshold would be 100× above or below the price.
            _magnitude_ratio = (
                max(current_price, threshold)
                / max(min(current_price, threshold), 0.001)
            )
            if _magnitude_ratio > 100:
                logger.warning(
                    "FinancialSource: rejected %s — magnitude mismatch "
                    "(price=%.4f threshold=%.4f ratio=%.0fx); likely misroute. "
                    "(question snippet: '%.80s')",
                    market.market_id, current_price, threshold, _magnitude_ratio,
                    market.question,
                )
                return None

            # Log detected direction on every fetch so operators can verify all
            # markets are parsed correctly before enabling live trades.
            logger.info(
                "FinancialSource: %s detected_direction=%s threshold=%.4f "
                "question='%.60s'",
                market.market_id,
                "BELOW" if not is_above else "ABOVE",
                threshold,
                market.question,
            )

            # Always log the raw fetched value so operators can verify correctness
            # before trusting any signal (especially important for WTI, Nasdaq, etc.
            # where a stale or wrong price produces a misleading ground_truth_prob).
            logger.info(
                "FinancialSource: %s → %.4f via %s (threshold %.4f for %s)",
                symbol, current_price, price_source, threshold or 0.0,
                market.market_id,
            )

            # Per-source confidence parameters:
            #   Twelve Data       — commercial API, reliable prices; spatial cap 0.85,
            #                       time floor 0.85 (signals fire at any horizon).
            #   SIMULATE_PRO_DATA — Yahoo Finance prices, but pro-level floor 0.85.
            #                       Logs a [SIMULATED PRO] warning on every fetch.
            #                       For strategy back-evaluation only, not production.
            #   Yahoo Finance     — unofficial scraping; spatial cap 0.90, time floor
            #                       0.55 (signals blocked beyond ~3h).
            if price_source == "twelve_data":
                max_spatial = _TD_MAX_SPATIAL_CONF      # 0.85
                time_floor  = _TD_TIME_CONF_FLOOR       # 0.85
            elif _SIMULATE_PRO_DATA:
                max_spatial = _TD_MAX_SPATIAL_CONF      # 0.85 — simulated
                time_floor  = _TD_TIME_CONF_FLOOR       # 0.85 — simulated
                logger.warning(
                    "[SIMULATED PRO] %s → %.4f  confidence=0.85 "
                    "(would require Twelve Data in production)",
                    symbol, current_price,
                )
            else:
                max_spatial = 0.90
                time_floor  = _TIME_CONF_FLOOR          # 0.55

            ground_truth_prob, spatial_conf = self._compute_prob_and_confidence(
                current_price, threshold, is_above, max_conf=max_spatial
            )
            if ground_truth_prob is None:
                # Price too close to threshold – skip rather than guess
                logger.debug(
                    "FinancialSource: price %.4f within 2%% of threshold %.4f for %s",
                    current_price, threshold, market.market_id,
                )
                return None

            # Time-decay: reduce confidence for markets far from resolution.
            # Twelve Data floor (0.85) stays above the 0.80 gate at any horizon.
            # Yahoo Finance floor (0.55) blocks signals beyond ~3h.
            time_conf = self._time_confidence(market.hours_to_resolution, floor=time_floor)
            confidence = min(spatial_conf, time_conf)
            if time_conf < 1.0:
                logger.debug(
                    "FinancialSource: time-adjusted confidence %.2f "
                    "(spatial=%.2f, time=%.2f floor=%.2f, hours_left=%.1f) for %s",
                    confidence, spatial_conf, time_conf, time_floor,
                    market.hours_to_resolution, market.market_id,
                )

            margin_pct = abs(current_price - threshold) / abs(threshold) * 100
            direction_str = "above" if is_above else "below"
            outcome_str = "YES" if ground_truth_prob == 1.0 else "NO"
            near_rollover = self._near_futures_rollover(symbol)
            if near_rollover:
                if symbol == "CL=F":
                    # KXWTIW- markets resolve against the official daily settlement
                    # price (a single-day snapshot), not the continuous contract.
                    # Rollover gaps don't affect settlement prices, so these are safe
                    # to trade.  Apply a small confidence haircut for the wider
                    # spreads and unusual price action typical of rollover week.
                    # KXWTI- (daily bracket) markets ARE affected — block them.
                    is_weekly_settlement = market.market_id.startswith("KXWTIW")
                    if is_weekly_settlement:
                        logger.info(
                            "FinancialSource: CL=F in rollover window but %s is weekly "
                            "settlement — allowing (settlement price not affected by "
                            "continuous contract rollover)",
                            market.market_id,
                        )
                        confidence *= 0.90  # 10% haircut for rollover-week uncertainty
                    else:
                        # Crude oil continuous-contract gaps on rollover are too wide
                        # and unpredictable to trade safely.  Block entirely.
                        logger.warning(
                            "FinancialSource: CL=F skipped — active rollover window, "
                            "too much gap risk",
                        )
                        return None
                else:
                    # Other futures (NQ=F, ES=F, …) rollover risk is lower — still
                    # trade but executor will reduce position size to 25%.
                    logger.warning(
                        "FinancialSource: %s (%s) is within the quarterly rollover window "
                        "— continuous-contract price may gap at contract changeover; "
                        "flagged in raw_data",
                        symbol, instrument_name,
                    )

            # Build source metadata based on which API actually returned the price.
            if price_source == "twelve_data":
                td_sym = _TD_SYMBOL_MAP.get(symbol, symbol)
                src_name = f"Twelve Data/{td_sym}"
                src_url  = f"{_TD_BASE}/quote?symbol={td_sym}"
            elif price_source == "alpha_vantage":
                src_name = f"Alpha Vantage/{symbol}"
                src_url  = _AV_BASE
            else:
                src_name = f"Yahoo Finance/{symbol}"
                src_url  = (
                    f"https://finance.yahoo.com/quote/"
                    f"{symbol.replace('^', '%5E')}"
                )

            return GroundTruthResult(
                ground_truth_prob=ground_truth_prob,
                confidence=confidence,
                source_type=SourceType.HARD,
                source_name=src_name,
                source_url=src_url,
                raw_data={
                    "symbol": symbol,
                    "price_source": price_source,
                    "instrument": instrument_name,
                    "current_price": current_price,
                    "threshold": threshold,
                    "direction": direction_str,
                    "margin_pct": round(margin_pct, 2),
                    "near_futures_rollover": near_rollover,
                    "rollover_risk": near_rollover,
                },
                reasoning=(
                    f"{instrument_name}: current={current_price:.4f} (via {price_source}), "
                    f"threshold={threshold:.4f} ({direction_str}), "
                    f"margin={margin_pct:.1f}%, "
                    f"hours_left={market.hours_to_resolution:.1f}. "
                    f"→ {outcome_str} "
                    f"confidence={confidence:.2f} "
                    f"(spatial={spatial_conf:.2f} time={time_conf:.2f} floor={time_floor:.2f})"
                    + (" [ROLLOVER_RISK: size will be reduced to 25%]" if near_rollover else "")
                ),
            )

        except Exception as exc:
            logger.warning(
                "FinancialSource: error for %s: %s", market.market_id, exc
            )
            return None

    # ── Guard helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_close_question(question: str) -> bool:
        """True if the question is about a closing or settlement price."""
        return bool(_CLOSE_QUESTION_RE.search(question))

    @staticmethod
    def _us_equity_premarket() -> bool:
        """
        True if currently in the pre-market window where the session-close
        price has NOT yet been set (weekdays before 9:30 AM ET).

        Pre-market prices are speculative — they tell us nothing about where
        the market will actually close. Post-market (after 16:00 ET) the
        official close is already printed, so the current Yahoo Finance price
        IS the authoritative settlement. We must NOT block post-market.

        Returns False on weekends: equity markets don't open, so the last
        close (Friday) is already the settled value and is usable.
        """
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:           # Saturday / Sunday – no open session
            return False
        return now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30)

    @staticmethod
    def _us_equity_session_open() -> bool:
        """True if US equity markets are currently in regular session (9:30–16:00 ET)."""
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:
            return False
        t = (now_et.hour, now_et.minute)
        return (9, 30) <= t < (16, 0)

    @staticmethod
    def _near_futures_rollover(symbol: str) -> bool:
        """
        True if within the typical quarterly rollover window for E-mini futures.

        Futures roll during the 2nd–3rd week of March/June/September/December.
        Around this window Yahoo Finance's continuous-contract price (NQ=F etc.)
        can gap as the front month changes, which could look like a massive price
        move and flip our ground_truth_prob from 1.0 to 0.0 spuriously.

        Callers differentiate by symbol:
          CL=F  – crude oil gaps are too wide; caller returns None (no trade).
          Others – lower gap risk; caller sets rollover_risk=True so the executor
                   applies a 25% size reduction.
        """
        if symbol not in _FUTURES_SYMBOLS:
            return False
        today = date.today()
        if today.month not in _ROLLOVER_MONTHS:
            return False
        week_of_month = (today.day - 1) // 7 + 1   # 1-indexed
        return week_of_month in (2, 3)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _detect_instrument(self, market: Market) -> Tuple[str, str]:
        """Return (yahoo_symbol, human_name) or ('', '') if not found."""
        # Include market_id so KXUSDJPY / KXEURUSD / KXNASDAQ100 are detected
        # even when the question text uses a different phrasing.
        text = (
            market.question + " " + " ".join(market.tags) + " " + market.market_id
        ).lower()
        # Longest keyword first so "nasdaq 100" wins over "nasdaq"
        for kw in sorted(_INSTRUMENT_MAP, key=len, reverse=True):
            if kw in text:
                return _INSTRUMENT_MAP[kw]
        return "", ""

    def _fetch_price(self, symbol: str) -> Tuple[Optional[float], str]:
        """
        Return (current market price, source_key) with a 60-second module-level cache.

        source_key is one of: "twelve_data" | "alpha_vantage" | "yahoo".

        Source priority:
          1. Twelve Data  (if TWELVEDATA_API_KEY is set and symbol is mapped)
          2. Alpha Vantage (if ALPHA_VANTAGE_KEY is set and symbol is mapped)
          3. Yahoo Finance  (always-available fallback, unofficial but proven)

        Tracking which source succeeded matters for confidence scoring: Twelve
        Data results carry a higher time-confidence floor (0.85) than Yahoo
        Finance results (0.55), allowing signals to fire at longer horizons.
        """
        now = time.monotonic()
        cached = _PRICE_CACHE.get(symbol)
        if cached:
            fetched_at, price, source_key = cached
            ttl = _CACHE_TTL if price is not None else _FAILURE_CACHE_TTL
            if now - fetched_at < ttl:
                if price is None:
                    logger.debug(
                        "FinancialSource: %s — skipping all APIs (cached failure, "
                        "%.0fs ago, retry in %.0fs)",
                        symbol, now - fetched_at, ttl - (now - fetched_at),
                    )
                return price, source_key

        td_price = self._fetch_price_twelve_data(symbol)
        if td_price is not None:
            price, source_key = td_price, "twelve_data"
        else:
            av_price = self._fetch_price_alpha_vantage(symbol)
            if av_price is not None:
                price, source_key = av_price, "alpha_vantage"
            else:
                price, source_key = self._fetch_price_yahoo(symbol), "yahoo"

        if price is not None:
            _PRICE_CACHE[symbol] = (time.monotonic(), price, source_key)
            logger.debug(
                "FinancialSource: %s price=%.4f (source=%s)", symbol, price, source_key
            )
        else:
            # Cache the failure so subsequent markets referencing the same instrument
            # this cycle skip all API calls immediately instead of re-hitting timeouts.
            _PRICE_CACHE[symbol] = (time.monotonic(), None, "")
            logger.debug(
                "FinancialSource: %s — all APIs failed; caching miss for %ds",
                symbol, _FAILURE_CACHE_TTL,
            )
        return price, source_key

    def _fetch_price_twelve_data(self, yahoo_symbol: str) -> Optional[float]:
        """
        Fetch price from Twelve Data /quote endpoint (requires TWELVEDATA_API_KEY).

        /quote returns the latest close price plus open/high/low/volume so we
        can confirm the symbol resolved correctly.  The `close` field is used
        because it reflects the most recent completed bar (real-time during
        market hours; last close outside hours).

        Free tier: 8 calls/minute, 800/day — well within our usage given the
        60-second symbol cache that limits us to one call per symbol per minute.
        """
        if not _TWELVE_DATA_KEY:
            return None
        td_symbol = _TD_SYMBOL_MAP.get(yahoo_symbol)
        if not td_symbol:
            return None
        # Skip symbols known to be blocked on the free tier — avoids WARNING spam
        # every cycle.  Falls through to Yahoo Finance silently.
        if td_symbol in _TD_FREE_TIER_BLOCKED:
            logger.debug(
                "FinancialSource: %s (%s) skipped — Twelve Data free tier blocked, "
                "falling back to Yahoo",
                yahoo_symbol, td_symbol,
            )
            return None
        try:
            resp = requests.get(
                f"{_TD_BASE}/quote",
                params={"symbol": td_symbol, "apikey": _TWELVE_DATA_KEY},
                timeout=_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            # /quote returns {"close": "19823.99", "symbol": "NDX", ...}
            close = data.get("close")
            if close is not None:
                return float(close)
            # Twelve Data returns {"code": 4xx, "message": "..."} on auth/limit errors
            logger.warning(
                "FinancialSource: TwelveData unexpected /quote response for %s: %s",
                td_symbol, data,
            )
        except Exception as exc:
            logger.warning(
                "FinancialSource: TwelveData fetch failed for %s (%s): %s",
                yahoo_symbol, td_symbol, exc,
            )
        return None

    def _fetch_price_alpha_vantage(self, yahoo_symbol: str) -> Optional[float]:
        """
        Fetch price from Alpha Vantage API (requires ALPHA_VANTAGE_KEY env var).

        Free tier: 5 calls/minute, 500/day.  Only covers forex and some indices
        on the free plan — futures require a paid subscription so they fall
        through to Yahoo Finance automatically.
        """
        if not _ALPHA_VANTAGE_KEY:
            return None
        av_symbol = _AV_SYMBOL_MAP.get(yahoo_symbol)
        if not av_symbol:
            return None

        try:
            # Forex symbols are encoded as "FROM:TO" in our map
            if ":" in av_symbol:
                from_ccy, to_ccy = av_symbol.split(":", 1)
                resp = requests.get(
                    _AV_BASE,
                    params={
                        "function": "CURRENCY_EXCHANGE_RATE",
                        "from_currency": from_ccy,
                        "to_currency": to_ccy,
                        "apikey": _ALPHA_VANTAGE_KEY,
                    },
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                rate = (
                    resp.json()
                    .get("Realtime Currency Exchange Rate", {})
                    .get("5. Exchange Rate")
                )
                if rate:
                    return float(rate)
            else:
                # Equity indices / yields via GLOBAL_QUOTE
                resp = requests.get(
                    _AV_BASE,
                    params={
                        "function": "GLOBAL_QUOTE",
                        "symbol": av_symbol,
                        "apikey": _ALPHA_VANTAGE_KEY,
                    },
                    timeout=_TIMEOUT,
                )
                resp.raise_for_status()
                price = resp.json().get("Global Quote", {}).get("05. price")
                if price:
                    return float(price)
        except Exception as exc:
            logger.warning(
                "FinancialSource: AlphaVantage fetch failed for %s (%s): %s",
                yahoo_symbol, av_symbol, exc,
            )
        return None

    def _fetch_price_yahoo(self, symbol: str) -> Optional[float]:
        """
        Fetch price from Yahoo Finance (unofficial but broadly reliable fallback).

        Retries once with a 1-second backoff — Yahoo occasionally rate-limits
        burst requests, and a single retry recovers most transient failures.
        """
        url = f"{_YAHOO_BASE}/{symbol}"
        for attempt in range(2):
            try:
                if attempt > 0:
                    time.sleep(1.0)
                resp = requests.get(
                    url,
                    params={"interval": "1d", "range": "5d"},
                    timeout=_TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.raise_for_status()
                meta = resp.json()["chart"]["result"][0]["meta"]
                return float(meta["regularMarketPrice"])
            except Exception as exc:
                logger.warning(
                    "FinancialSource: Yahoo fetch failed for %s (attempt %d/2): %s",
                    symbol, attempt + 1, exc,
                )
        return None

    def _extract_threshold_and_direction(
        self, question: str, market_id: str
    ) -> Tuple[Optional[float], bool]:
        """
        Return (threshold, is_above) where is_above=True means YES if price > threshold.

        Tries the question text first, then falls back to Kalshi market ID
        conventions (-T{val} = above, -B{val} = below).

        Returns (None, True) for price-range questions ("$63-63.99") so that the
        caller skips the market.  The -B{val} Kalshi suffix means "bucket at val"
        for range contracts, NOT "below val" — misreading it as "below" caused
        WTI bucket markets to receive a wrong ground-truth direction.
        """
        # Bail out early on range/bucket markets: "$X-$Y" or "between X and Y"
        # cannot be reduced to a single above/below threshold.  The Kalshi -B{val}
        # suffix means "bucket at val" not "below val" — misreading it produces
        # wildly wrong signals (e.g. BUY YES on a $100-range band 20h from close).
        if _RANGE_RE.search(question) or _BETWEEN_RE.search(question):
            logger.debug(
                "FinancialSource: price-range question detected, skipping %s", market_id
            )
            return None, True

        m = _ABOVE_RE.search(question)
        if m:
            return _parse_float(m.group(1)), True

        m = _BELOW_RE.search(question)
        if m:
            return _parse_float(m.group(1)), False

        # Reverse-direction patterns: "$59.99 or below", "$60.00 or more"
        # These are checked AFTER the standard patterns so "above $60" still wins
        # when both could match.
        m = _BELOW_SUFFIX_RE.search(question)
        if m:
            return _parse_float(m.group(1)), False

        m = _ABOVE_SUFFIX_RE.search(question)
        if m:
            return _parse_float(m.group(1)), True

        # Kalshi market ID suffix convention: -T{threshold} or -B{threshold}
        # NOTE: only use the -B suffix when the question text is absent — and only
        # when no range pattern was found above (guarded by the early return).
        m = re.search(r"-T([\d.]+)$", market_id)
        if m:
            return _parse_float(m.group(1)), True  # T = "target above"

        m = re.search(r"-B([\d.]+)$", market_id)
        if m:
            return _parse_float(m.group(1)), False  # B = "below bucket"

        return None, True

    @staticmethod
    def _time_confidence(
        hours_to_resolution: float, floor: float = _TIME_CONF_FLOOR
    ) -> float:
        """
        Returns how much we trust the current spot price as a predictor of the
        settlement price given the time remaining.

        Full confidence (1.0) within the final hour; linearly decays to `floor`
        at _MAX_SIGNAL_HOURS.

        floor=_TIME_CONF_FLOOR (0.55) for Yahoo Finance — sits below the
        ConfidenceScorer threshold (0.80), blocking signals beyond ~3h.

        floor=_TD_TIME_CONF_FLOOR (0.85) for Twelve Data — stays above the
        gate at any horizon; signals fire whenever the spatial margin is large
        enough (price is ≥2% from threshold).
        """
        if hours_to_resolution <= _FULL_SIGNAL_HOURS:
            return 1.0
        if hours_to_resolution >= _MAX_SIGNAL_HOURS:
            return floor
        frac = (
            (hours_to_resolution - _FULL_SIGNAL_HOURS)
            / (_MAX_SIGNAL_HOURS - _FULL_SIGNAL_HOURS)
        )
        return round(1.0 - frac * (1.0 - floor), 4)

    def _compute_prob_and_confidence(
        self, current: float, threshold: float, is_above: bool,
        max_conf: float = 0.90,
    ) -> Tuple[Optional[float], float]:
        """
        Return (ground_truth_prob, confidence).

        Confidence scales with the margin between current price and threshold:
          ≥ 5% away  → max_conf  (strong signal; 0.90 Yahoo, 0.85 Twelve Data)
          2–5% away  → 0.80 – max_conf  (tradeable range, linear interpolation)
          < 2% away  → None  (too close to threshold, skip)

        max_conf is lowered to 0.85 for Twelve Data to reflect that it is a
        commercial API but not an exchange-native feed.
        """
        if threshold == 0:
            return None, 0.0

        margin = (current - threshold) / abs(threshold)  # + means current is above

        # Confidence scales from 0.80 (at ±2% margin) to max_conf (at ±5%+).
        def _confidence(abs_margin: float) -> float:
            if abs_margin >= 0.05:
                return max_conf
            # Linear interpolation: 0.80 at 2%, max_conf at 5%
            return round(0.80 + (abs_margin - 0.02) / 0.03 * (max_conf - 0.80), 4)

        if is_above:
            if margin > 0.02:
                return 1.0, _confidence(margin)
            if margin < -0.02:
                return 0.0, _confidence(-margin)
        else:
            if margin < -0.02:
                return 1.0, _confidence(-margin)
            if margin > 0.02:
                return 0.0, _confidence(margin)

        return None, 0.0  # within ±2% — too uncertain


def _parse_float(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None
