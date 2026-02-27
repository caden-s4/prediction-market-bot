"""
data.ground_truth.financial – real-time financial instrument prices.

Source: Yahoo Finance public API (no key required, stable since 2015).

Covers:
  - US stock indices : Nasdaq 100 (NQ=F futures), S&P 500 (ES=F futures), Dow (YM=F)
  - Forex pairs      : EUR/USD, USD/JPY, GBP/USD, USD/CAD, AUD/USD
  - Treasury yields  : 10-yr (^TNX), 5-yr (^FVX), 2-yr (^IRX), 30-yr (^TYX)
  - Commodities      : Gold (GC=F), WTI Crude (CL=F), Natural Gas (NG=F)

Index markets use E-mini futures (NQ=F, ES=F, YM=F) rather than spot index symbols
(^NDX, ^GSPC, ^DJI) because futures trade 24/5 and reflect the current market
expectation even on weekends/pre-market. Spot index symbols only update during
regular trading hours; on Sunday evening they still show Friday's close, which
is wrong for Kalshi markets that resolve Monday at 4pm.

Confidence is a function of how far the current price sits from the market
threshold. If current price is within 2% of the threshold we return None
(too close to call) and skip the trade. Beyond 5% we're at 0.90 confidence.

The module-level price cache caps Yahoo Finance at one HTTP request per symbol
per 60 seconds regardless of how many markets reference the same instrument.
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
_TIMEOUT = 8

# Optional API keys — set in .env to use more reliable primary sources.
# If absent, the source is skipped and Yahoo Finance is used as fallback.
_TWELVE_DATA_KEY: str = os.environ.get("TWELVE_DATA_KEY", "")
_ALPHA_VANTAGE_KEY: str = os.environ.get("ALPHA_VANTAGE_KEY", "")

# Twelve Data symbol translations from Yahoo Finance symbols.
# Twelve Data uses its own naming for futures and Forex pairs.
_TD_SYMBOL_MAP: Dict[str, str] = {
    # E-mini futures (continuous front-month contracts)
    "NQ=F": "NQ",       # Nasdaq 100 E-mini
    "ES=F": "ES",       # S&P 500 E-mini
    "YM=F": "YM",       # Dow Jones E-mini
    # Commodities
    "GC=F": "GC",       # Gold futures
    "CL=F": "CL",       # WTI Crude futures
    "NG=F": "NG",       # Natural Gas futures
    # Forex (Twelve Data uses standard ISO pairs)
    "EURUSD=X": "EUR/USD",
    "JPY=X":    "USD/JPY",
    "GBPUSD=X": "GBP/USD",
    "CAD=X":    "USD/CAD",
    "AUDUSD=X": "AUD/USD",
    "CHF=X":    "USD/CHF",
    # Treasury yields
    "^TNX": "US10Y",
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

# Module-level price cache: symbol → (fetched_at_monotonic, price)
_PRICE_CACHE: dict = {}
_CACHE_TTL = 60  # one request per symbol per minute max

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
# Spot/futures prices are highly predictive of settlement in the final hour
# but become unreliable over multi-hour horizons — NQ can move 3-5% in a day.
# We reduce source confidence linearly as hours-to-resolution grows, capping it
# at 0.55 beyond _MAX_SIGNAL_HOURS.  Because ConfidenceScorer requires both
# dimensions ≥ 0.80, no financial signal fires once time_confidence < 0.80.
#
# Calibration:
#   ≤ 1h  → 1.00  (spot ≈ settlement; full confidence)
#   2h    → 0.91  (fires for any spatial-confident signal)
#   3h    → 0.82  (fires only for ≥5%-margin signals)
#   4h    → 0.73  (BLOCKED — time_conf < 0.80 gate)
#   8h+   → 0.55  (BLOCKED — floor)
_MAX_SIGNAL_HOURS: float = 8.0
_FULL_SIGNAL_HOURS: float = 1.0
_TIME_CONF_FLOOR: float = 0.55

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


class FinancialDataSource(DataSource):
    """
    Fetches real-time prices/yields from Yahoo Finance for markets about
    index levels, forex rates, and Treasury yields.
    """

    def can_handle(self, market: Market) -> bool:
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
            symbol, instrument_name = self._detect_instrument(market)
            if not symbol:
                logger.debug(
                    "FinancialSource: no instrument detected for %s", market.market_id
                )
                return None

            current_price = self._fetch_price(symbol)
            if current_price is None:
                return None

            # Close-price questions need the official session price, not
            # extended-hours quotes.  Before 9:30am ET the "current price" is
            # a pre-market print that has nothing to do with where the stock
            # will actually close — skip rather than guess.
            if self._is_close_question(market.question) and not self._us_equity_session_open():
                logger.debug(
                    "FinancialSource: %s is a close-price question but market is "
                    "outside regular hours — pre-market price unreliable, skipping",
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

            ground_truth_prob, spatial_conf = self._compute_prob_and_confidence(
                current_price, threshold, is_above
            )
            if ground_truth_prob is None:
                # Price too close to threshold – skip rather than guess
                logger.debug(
                    "FinancialSource: price %.4f within 2%% of threshold %.4f for %s",
                    current_price, threshold, market.market_id,
                )
                return None

            # Time-decay: reduce confidence for markets far from resolution.
            # Spot/futures prices are not reliable predictors of settlement when
            # hours_to_resolution is large — so we cap source confidence using a
            # linear decay.  Signals with time_conf < 0.80 won't clear the
            # ConfidenceScorer threshold and will be silently skipped.
            time_conf = self._time_confidence(market.hours_to_resolution)
            confidence = min(spatial_conf, time_conf)
            if time_conf < 1.0:
                logger.debug(
                    "FinancialSource: time-adjusted confidence %.2f "
                    "(spatial=%.2f, time=%.2f, hours_left=%.1f) for %s",
                    confidence, spatial_conf, time_conf,
                    market.hours_to_resolution, market.market_id,
                )

            margin_pct = abs(current_price - threshold) / abs(threshold) * 100
            direction_str = "above" if is_above else "below"
            outcome_str = "YES" if ground_truth_prob == 1.0 else "NO"
            near_rollover = self._near_futures_rollover(symbol)
            if near_rollover:
                logger.warning(
                    "FinancialSource: %s (%s) is within the quarterly rollover window "
                    "— continuous-contract price may gap at contract changeover; "
                    "flagged in raw_data",
                    symbol, instrument_name,
                )
            return GroundTruthResult(
                ground_truth_prob=ground_truth_prob,
                confidence=confidence,
                source_type=SourceType.HARD,
                source_name=f"Yahoo Finance/{symbol}",
                source_url=(
                    f"https://finance.yahoo.com/quote/"
                    f"{symbol.replace('^', '%5E')}"
                ),
                raw_data={
                    "symbol": symbol,
                    "instrument": instrument_name,
                    "current_price": current_price,
                    "threshold": threshold,
                    "direction": direction_str,
                    "margin_pct": round(margin_pct, 2),
                    "near_futures_rollover": near_rollover,
                },
                reasoning=(
                    f"{instrument_name}: current={current_price:.4f}, "
                    f"threshold={threshold:.4f} ({direction_str}), "
                    f"margin={margin_pct:.1f}%, "
                    f"hours_left={market.hours_to_resolution:.1f}. "
                    f"→ {outcome_str} "
                    f"confidence={confidence:.2f} "
                    f"(spatial={spatial_conf:.2f} time={time_conf:.2f})"
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
    def _us_equity_session_open() -> bool:
        """
        True if US equity markets are currently in regular session (9:30–16:00 ET).

        Pre-market prices are NOT a reliable predictor of the official close price
        — the session hasn't happened yet.  Post-market prices (after 16:00 ET)
        are the settled close and ARE usable, so we only block pre-market.
        """
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:          # Saturday / Sunday
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
        We flag the result rather than blocking it — the gap size still needs to
        be ≥5% to trade, which filters most rollover-induced noise.
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

    def _fetch_price(self, symbol: str) -> Optional[float]:
        """
        Return current market price with a 60-second module-level cache.

        Source priority:
          1. Twelve Data  (if TWELVE_DATA_KEY is set and symbol is mapped)
          2. Alpha Vantage (if ALPHA_VANTAGE_KEY is set and symbol is mapped)
          3. Yahoo Finance  (always-available fallback, unofficial but proven)

        Using an unofficial source (Yahoo) as the last resort rather than the
        only resort means a Yahoo outage doesn't kill the entire financial
        pipeline — it degrades gracefully to whatever primary sources are
        configured.
        """
        now = time.monotonic()
        cached = _PRICE_CACHE.get(symbol)
        if cached:
            fetched_at, price = cached
            if now - fetched_at < _CACHE_TTL:
                return price

        price = (
            self._fetch_price_twelve_data(symbol)
            or self._fetch_price_alpha_vantage(symbol)
            or self._fetch_price_yahoo(symbol)
        )

        if price is not None:
            _PRICE_CACHE[symbol] = (time.monotonic(), price)
            logger.debug("FinancialSource: %s price=%.4f", symbol, price)
        return price

    def _fetch_price_twelve_data(self, yahoo_symbol: str) -> Optional[float]:
        """
        Fetch price from Twelve Data API (requires TWELVE_DATA_KEY env var).

        Free tier: 8 calls/minute, 800/day — well within our usage given the
        60-second symbol cache that limits us to one call per symbol per minute.
        """
        if not _TWELVE_DATA_KEY:
            return None
        td_symbol = _TD_SYMBOL_MAP.get(yahoo_symbol)
        if not td_symbol:
            return None
        try:
            resp = requests.get(
                f"{_TD_BASE}/price",
                params={"symbol": td_symbol, "apikey": _TWELVE_DATA_KEY},
                timeout=_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()
            data = resp.json()
            if "price" in data:
                return float(data["price"])
            # Twelve Data returns {"code": 4xx, "message": "..."} on error
            logger.warning(
                "FinancialSource: TwelveData unexpected response for %s: %s",
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
    def _time_confidence(hours_to_resolution: float) -> float:
        """
        Returns how much we trust the current spot price as a predictor of the
        settlement price given the time remaining.

        Full confidence (1.0) within the final hour; linearly decays to
        _TIME_CONF_FLOOR at _MAX_SIGNAL_HOURS.  The floor (0.55) sits below the
        ConfidenceScorer threshold (0.80), ensuring no financial signal fires
        beyond _MAX_SIGNAL_HOURS regardless of how large the spatial margin is.
        """
        if hours_to_resolution <= _FULL_SIGNAL_HOURS:
            return 1.0
        if hours_to_resolution >= _MAX_SIGNAL_HOURS:
            return _TIME_CONF_FLOOR
        frac = (
            (hours_to_resolution - _FULL_SIGNAL_HOURS)
            / (_MAX_SIGNAL_HOURS - _FULL_SIGNAL_HOURS)
        )
        return round(1.0 - frac * (1.0 - _TIME_CONF_FLOOR), 4)

    def _compute_prob_and_confidence(
        self, current: float, threshold: float, is_above: bool
    ) -> Tuple[Optional[float], float]:
        """
        Return (ground_truth_prob, confidence).

        Confidence scales with the margin between current price and threshold:
          ≥ 5% away  → 0.90  (strong signal)
          2–5% away  → 0.80–0.875 (tradeable)
          < 2% away  → None  (too close, skip)
        """
        if threshold == 0:
            return None, 0.0

        margin = (current - threshold) / abs(threshold)  # + means current is above

        # Clamp confidence between 0.80 and 0.90 in the tradeable range
        def _confidence(abs_margin: float) -> float:
            if abs_margin >= 0.05:
                return 0.90
            # Linear interpolation: 0.80 at 2%, 0.90 at 5%
            return 0.80 + (abs_margin - 0.02) / 0.03 * 0.10

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
