"""
data.markets.kalshi – Kalshi REST API v2 client.

Kalshi is a regulated US prediction market exchange.
Docs: https://api.elections.kalshi.com/trade-api/v2

Authentication: RSA-SHA256 signed requests using API key + RSA private key.
Weather markets on Kalshi are well categorised (series ticker prefix "KXWEATHER").
"""

from __future__ import annotations

import base64
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from .base import (
    BaseMarketClient,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    PriceLevel,
    Side,
)

logger = logging.getLogger(__name__)

_WEATHER_SERIES = re.compile(
    r"^KXWEATHER|^KXPRECIP|^KXSNOW|^KXRAIN|^KXTEMP|^KXWIND|^KXHURR|^KXHIGHS|^KXLOWS",
    re.IGNORECASE,
)

# All known Kalshi weather series tickers — queried directly via series_ticker
# to avoid scanning thousands of unrelated markets.
_WEATHER_SERIES_TICKERS = [
    "KXHIGHS", "KXLOWS", "KXTEMP",
    "KXRAIN", "KXPRECIP", "KXSNOW",
    "KXWIND", "KXHURR", "KXWEATHER",
]

# Known Kalshi sports series tickers.  Same-day game markets are short-lived
# and can be buried deep in the paginated general fetch.  Querying these series
# directly ensures they surface before they expire.
_SPORTS_SERIES_TICKERS = [
    "KXNFL",       # NFL season/prop markets
    "KXNBA",       # NBA season/prop markets
    "KXMLB",       # MLB season/prop markets
    "KXNHL",       # NHL season/prop markets
    "KXNCAAF",     # College football
    "KXNCAAB",     # College basketball season/prop markets
    "KXMLS",       # MLS soccer
    "KXSOC",       # International soccer
    "KXUFC",       # UFC / MMA
    "KXGOLF",      # PGA / major golf events
    "KXTENNIS",    # Grand slam tennis
    "KXNASCAR",    # NASCAR race results
    # Game-result (moneyline) series — different series prefix from the prop/season markets
    "KXNBAGAME",     # NBA individual game results (e.g. KXNBAGAME-26MAR13MEMDET-DET)
    "KXNCAAMBGAME",  # NCAA Men's Basketball game results
    "KXNCAAWBGAME",  # NCAA Women's Basketball game results
]

# Sports game-result series whose close_time is a settlement window, not the game time.
_GAME_SERIES_PREFIXES = ("KXNBAGAME", "KXNCAAMBGAME", "KXNFLGAME", "KXNCAAWBGAME")

# DISABLED 2026-04-23: Yahoo quote_ts staleness blocks 100% of financial bracket signals at
# freshness gate. Re-enable individual prefixes only after routing to Twelve Data paid tier or
# equivalent fresh-timestamp source. See commit history for prior contents.
# KXBRENTD/KXBRENTW: DO NOT re-enable even after Twelve Data upgrade. CL=F (WTI) is the
# wrong GT source for Brent markets. Brent requires its own feed (BZ=F or equivalent).
# Phase 0b showed 44.4% accuracy on 54 trades — structurally broken routing.
_FINANCIAL_BRACKET_PREFIXES = ()

# Extra hours added to midnight UTC of the game date to estimate game end time.
# 30h covers the latest possible game end in UTC (e.g. 10:30pm ET tipoff + 2.5h
# = ~1am UTC next day = game_date + 25h) with buffer. The scanner uses a 48h
# window for game markets so this stays well within the scan window.
_GAME_END_OFFSET_HOURS: Dict[str, int] = {
    "KXNBAGAME": 30,
    "KXNCAAMBGAME": 30,
    "KXNFLGAME": 30,
    "KXNCAAWBGAME": 30,
}

_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _is_game_market(market_id: str) -> bool:
    return any(market_id.startswith(p) for p in _GAME_SERIES_PREFIXES)


def _is_financial_bracket_market(market_id: str) -> bool:
    return any(market_id.startswith(p) for p in _FINANCIAL_BRACKET_PREFIXES)


def _extract_game_date(market_id: str) -> Optional[datetime]:
    """Extract game date from market IDs like KXNBAGAME-26MAR13MEMDET-DET.

    The date segment (YYMMMDD) immediately follows the first '-'.
    Returns midnight UTC on that date, or None if the pattern is not found.
    """
    match = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", market_id)
    if not match:
        return None
    year = 2000 + int(match.group(1))
    month = _MONTH_MAP.get(match.group(2))
    if not month:
        return None
    day = int(match.group(3))
    return datetime(year, month, day, tzinfo=timezone.utc)


_CITY_COORDS: Dict[str, Dict[str, float]] = {
    # Major US metros
    "new york": {"lat": 40.71, "lon": -74.01},
    "new york city": {"lat": 40.71, "lon": -74.01},
    "nyc": {"lat": 40.71, "lon": -74.01},
    "los angeles": {"lat": 34.05, "lon": -118.24},
    "la": {"lat": 34.05, "lon": -118.24},
    "chicago": {"lat": 41.88, "lon": -87.63},
    "seattle": {"lat": 47.61, "lon": -122.33},
    "miami": {"lat": 25.77, "lon": -80.19},
    "boston": {"lat": 42.36, "lon": -71.06},
    "denver": {"lat": 39.74, "lon": -104.98},
    "dallas": {"lat": 32.78, "lon": -96.80},
    "atlanta": {"lat": 33.75, "lon": -84.39},
    "san francisco": {"lat": 37.77, "lon": -122.42},
    "sf": {"lat": 37.77, "lon": -122.42},
    # Additional US cities
    "houston": {"lat": 29.76, "lon": -95.37},
    "phoenix": {"lat": 33.45, "lon": -112.07},
    "philadelphia": {"lat": 39.95, "lon": -75.17},
    "san antonio": {"lat": 29.42, "lon": -98.49},
    "san diego": {"lat": 32.72, "lon": -117.16},
    "portland": {"lat": 45.52, "lon": -122.68},
    "las vegas": {"lat": 36.17, "lon": -115.14},
    "minneapolis": {"lat": 44.98, "lon": -93.27},
    "kansas city": {"lat": 39.10, "lon": -94.58},
    "nashville": {"lat": 36.17, "lon": -86.78},
    "oklahoma city": {"lat": 35.47, "lon": -97.52},
    "charlotte": {"lat": 35.23, "lon": -80.84},
    "raleigh": {"lat": 35.78, "lon": -78.64},
    "richmond": {"lat": 37.54, "lon": -77.43},
    "salt lake city": {"lat": 40.76, "lon": -111.89},
    "memphis": {"lat": 35.15, "lon": -90.05},
    "new orleans": {"lat": 29.95, "lon": -90.07},
    "detroit": {"lat": 42.33, "lon": -83.05},
    "indianapolis": {"lat": 39.77, "lon": -86.16},
    "columbus": {"lat": 39.96, "lon": -82.99},
    "cleveland": {"lat": 41.50, "lon": -81.69},
    "pittsburgh": {"lat": 40.44, "lon": -79.99},
    "buffalo": {"lat": 42.89, "lon": -78.87},
    "sacramento": {"lat": 38.58, "lon": -121.49},
    "st. louis": {"lat": 38.63, "lon": -90.20},
    "st louis": {"lat": 38.63, "lon": -90.20},
    "tampa": {"lat": 27.95, "lon": -82.46},
    "orlando": {"lat": 28.54, "lon": -81.38},
    "jacksonville": {"lat": 30.33, "lon": -81.66},
    "tucson": {"lat": 32.22, "lon": -110.93},
    "albuquerque": {"lat": 35.08, "lon": -106.65},
    "boise": {"lat": 43.62, "lon": -116.20},
    "anchorage": {"lat": 61.22, "lon": -149.90},
    "honolulu": {"lat": 21.31, "lon": -157.82},
    # International cities common on prediction markets
    "london": {"lat": 51.51, "lon": -0.13},
    "paris": {"lat": 48.86, "lon": 2.35},
    "tokyo": {"lat": 35.69, "lon": 139.69},
    "berlin": {"lat": 52.52, "lon": 13.40},
    "toronto": {"lat": 43.65, "lon": -79.38},
    "sydney": {"lat": -33.87, "lon": 151.21},
    "miami beach": {"lat": 25.79, "lon": -80.13},
}


# Kalshi documents ~1 req/s but tolerates bursts. 5/s sustained with burst=3
# eliminates 429s while keeping cycles under 60s.
_KALSHI_RATE_LIMIT = 8.0   # requests per second (sustained)
_KALSHI_BURST_LIMIT = 3    # max burst tokens


class _TokenBucket:
    """Thread-safe token bucket rate limiter."""

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            time.sleep(0.05)


def _safe_float(val) -> Optional[float]:
    """Convert val to float, returning None on failure or if val is None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _extract_location(text: str) -> Optional[Dict[str, Any]]:
    lower = text.lower()
    for city, coords in _CITY_COORDS.items():
        if city in lower:
            return {**coords, "city": city.title()}
    return None


class KalshiClient(BaseMarketClient):
    PLATFORM = "kalshi"
    WEATHER_CATEGORY_TAGS = ["weather"]

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.elections.kalshi.com/trade-api/v2",
    ) -> None:
        from urllib.parse import urlparse
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        # Path prefix for signing (e.g. "/trade-api/v2")
        self._path_prefix = urlparse(self._base_url).path.rstrip("/")
        self._session = requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        # Cache the loaded RSA private key so we only parse it once.
        self._private_key = self._load_private_key()
        self._rate_limiter = _TokenBucket(rate=_KALSHI_RATE_LIMIT, burst=_KALSHI_BURST_LIMIT)

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _load_private_key(self):
        """
        Load the RSA private key from self._api_secret.

        KALSHI_API_SECRET in .env should be the full PEM private key with
        literal \\n escapes (dotenv expands these to real newlines), e.g.:

            KALSHI_API_SECRET="-----BEGIN PRIVATE KEY-----\\nMIIE...\\n-----END PRIVATE KEY-----"

        Alternatively, bare base64 (no PEM headers) is also accepted – the
        code will add the standard PKCS#8 header/footer automatically.
        """
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        secret = self._api_secret
        # python-dotenv >=1.0 converts \\n inside quoted values to real newlines,
        # but handle the literal-backslash-n case as a safety net.
        secret = secret.replace("\\n", "\n")

        if "BEGIN" in secret:
            pem_bytes = secret.encode("utf-8")
        else:
            # Bare base64 body – wrap it in PKCS#8 PEM headers.
            body = "\n".join(secret[i:i + 64] for i in range(0, len(secret), 64))
            pem_bytes = (
                "-----BEGIN PRIVATE KEY-----\n"
                + body
                + "\n-----END PRIVATE KEY-----\n"
            ).encode("utf-8")

        return load_pem_private_key(pem_bytes, password=None)

    def _sign(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """
        Generate Kalshi RSA-SHA256 signature headers.
        Timestamp is in milliseconds.
        path must be the bare path (no query string).
        Message = timestamp + METHOD + path + body, signed with RSA-PSS/SHA-256.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        ts_ms = str(int(time.time() * 1000))
        message = (ts_ms + method.upper() + path + body).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
        }

    def _get(self, path: str, params: Optional[Dict] = None, _quiet: bool = False) -> Any:
        full_path = self._path_prefix + path
        url = self._base_url + path
        backoff = 5.0
        for attempt in range(4):
            self._rate_limiter.acquire()
            headers = self._sign("GET", full_path)
            resp = self._session.get(url, params=params, headers=headers, timeout=15)
            if not _quiet:
                logger.debug("Kalshi GET %s → HTTP %d", url, resp.status_code)
            if resp.status_code == 429:
                logger.warning(
                    "Kalshi 429 rate limit on %s – waiting %.0fs (attempt %d/4)",
                    path, backoff, attempt + 1,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            if not resp.ok:
                logger.warning(
                    "Kalshi HTTP %d on %s → %s",
                    resp.status_code, path, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()
        # All retries exhausted
        resp.raise_for_status()
        return resp.json()  # unreachable but satisfies type checker

    def _post(self, path: str, body: Dict) -> Any:
        import json
        body_str = json.dumps(body)
        url = self._base_url + path
        full_path = self._path_prefix + path
        backoff = 2.0
        last_resp = None
        for attempt in range(4):
            self._rate_limiter.acquire()
            headers = self._sign("POST", full_path)
            headers["Content-Type"] = "application/json"
            resp = self._session.post(url, data=body_str, headers=headers, timeout=15)
            last_resp = resp
            if resp.status_code == 429:
                logger.warning(
                    "Kalshi 429 rate limit on POST %s – waiting %.0fs (attempt %d/4)",
                    path, backoff, attempt + 1,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            # Retry on transient "service unavailable" errors that Kalshi surfaces
            # as 401 authentication_error (backend routing failure, not a bad key).
            if resp.status_code == 401:
                try:
                    detail = resp.json().get("error", {}).get("details", "")
                except Exception:
                    detail = ""
                if "service unavailable" in detail and attempt < 3:
                    logger.warning(
                        "Kalshi POST %s – transient 401 (service unavailable) "
                        "waiting %.0fs (attempt %d/4)",
                        path, backoff, attempt + 1,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
            if not resp.ok:
                logger.error(
                    "Kalshi POST %s HTTP %d – key=%s… body: %s",
                    path, resp.status_code,
                    self._api_key[:8],
                    resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()
        # All retries exhausted
        last_resp.raise_for_status()
        return last_resp.json()  # unreachable

    def _delete(self, path: str) -> Any:
        self._rate_limiter.acquire()
        headers = self._sign("DELETE", self._path_prefix + path)
        url = self._base_url + path
        resp = self._session.delete(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Market scanning ───────────────────────────────────────────────────────

    def get_markets(
        self,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: Optional[int] = None,
        max_close_ts: Optional[int] = None,
    ) -> List[Market]:
        """
        Fetch open markets from Kalshi with cursor-based pagination.

        limit=None (default) fetches ALL pages — recommended for full coverage.
        Pass an integer to cap at that many markets (useful for testing).

        max_close_ts: Unix timestamp (seconds). When set, Kalshi filters server-side
        to only return markets closing before that time. This dramatically reduces
        page count when used with a short resolution window (e.g. 24h).

        category is matched client-side (Kalshi API has no category filter).
        """
        weather_only = (category == "weather") if category else False
        page_size = 200
        fetched: List[Market] = []
        cursor: Optional[str] = None
        page = 0

        while True:
            if limit is not None and len(fetched) >= limit:
                break

            page += 1

            params: Dict[str, Any] = {"status": "open", "limit": page_size}
            if max_close_ts is not None:
                params["max_close_ts"] = max_close_ts
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get("/markets", params=params, _quiet=(page > 1))
            except Exception as exc:
                logger.error("Kalshi get_markets failed: %s", exc)
                break

            raw = data.get("markets", [])
            if not raw:
                break

            for item in raw:
                market = self._parse_market(item, weather_only=weather_only)
                if not market:
                    continue
                # Client-side category filter (skip only if a specific non-weather
                # category was requested and market doesn't match)
                if category and not weather_only and market.category != category:
                    continue
                fetched.append(market)

            # Log progress every 5 pages so the user can see pagination is still running
            if page % 5 == 0:
                logger.info(
                    "Kalshi get_markets: fetched %d markets so far (page %d)…",
                    len(fetched), page,
                )

            cursor = data.get("cursor")
            if not cursor or len(raw) < page_size:
                break  # exhausted all pages

        result = fetched if limit is None else fetched[:limit]
        logger.debug(
            "Kalshi get_markets: %d pages, %d markets fetched", page, len(result)
        )
        return result

    def get_weather_markets(self, limit: int = 200) -> List[Market]:
        """Fetch weather-series markets from Kalshi.

        Queries each known weather series ticker directly via the series_ticker
        filter instead of scanning all markets.  If a series returns nothing it
        is silently skipped.  Falls back to an empty list when no series yield
        results (e.g. off-season or series not offered on this API instance).
        """
        results: List[Market] = []
        seen: set = set()

        for i, series in enumerate(_WEATHER_SERIES_TICKERS):
            try:
                params: Dict[str, Any] = {
                    "status": "open",
                    "series_ticker": series,
                    "limit": 200,
                }
                data = self._get("/markets", params=params)
                raw = data.get("markets", [])
                logger.debug("Kalshi series=%s → %d markets", series, len(raw))
                for item in raw:
                    ticker = item.get("ticker", "")
                    if ticker in seen:
                        continue
                    seen.add(ticker)
                    m = self._parse_market(item, weather_only=True)
                    if m:
                        results.append(m)
            except Exception as exc:
                logger.warning("Kalshi series=%s fetch failed: %s", series, exc)

        logger.debug("Kalshi weather scan complete: %d markets found", len(results))
        if not results:
            self._log_available_series()
        return results

    def get_sports_markets(self, limit: int = 100) -> List[Market]:
        """Fetch sports-series markets directly via series_ticker.

        Mirrors get_weather_markets() — queries each known sports series
        ticker rather than relying on the general paginated fetch.  Same-day
        game markets are short-lived and often buried behind long-dated markets
        when fetching by close timestamp alone.

        Silently skips series that return 0 results (off-season / not offered).
        """
        results: List[Market] = []
        seen: set = set()

        for i, series in enumerate(_SPORTS_SERIES_TICKERS):
            try:
                params: Dict[str, Any] = {
                    "status": "open",
                    "series_ticker": series,
                    "limit": limit,
                }
                data = self._get("/markets", params=params)
                raw = data.get("markets", [])
                logger.debug("Kalshi sports series=%s → %d markets", series, len(raw))
                for item in raw:
                    ticker = item.get("ticker", "")
                    if ticker in seen:
                        continue
                    seen.add(ticker)
                    m = self._parse_market(item)
                    if m:
                        results.append(m)
            except Exception as exc:
                logger.debug("Kalshi sports series=%s fetch failed: %s", series, exc)

        logger.info(
            "Kalshi sports scan: %d markets across %d series",
            len(results), len(_SPORTS_SERIES_TICKERS),
        )
        _game_prefixes = ("KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME")
        game_markets = [m for m in results if any(m.market_id.startswith(p) for p in _game_prefixes)]
        nba_count = sum(1 for m in game_markets if m.market_id.startswith("KXNBAGAME"))
        ncaab_count = sum(1 for m in game_markets if m.market_id.startswith("KXNCAAMBGAME"))
        ncaaw_count = sum(1 for m in game_markets if m.market_id.startswith("KXNCAAWBGAME"))
        logger.info(
            "Kalshi sports scan: %d game-result markets found (NBA=%d, NCAAB=%d, NCAAW=%d)",
            len(game_markets), nba_count, ncaab_count, ncaaw_count,
        )
        return results

    def get_financial_bracket_markets(self, limit: int = 100) -> List[Market]:
        """Fetch financial bracket markets (KXNASDAQ100, KXWTI, etc.) directly via series_ticker.

        Financial bracket markets are not returned by the default get_markets() paginated fetch.
        This method queries each known financial series ticker directly to surface bracket
        markets for resolution drift arbitrage.

        Silently skips series that return 0 results (not offered / off-season).
        """
        results: List[Market] = []
        seen: set = set()

        for i, series in enumerate(_FINANCIAL_BRACKET_PREFIXES):
            try:
                params: Dict[str, Any] = {
                    "status": "open",
                    "series_ticker": series,
                    "limit": limit,
                }
                data = self._get("/markets", params=params)
                raw = data.get("markets", [])
                logger.debug("Kalshi financial series=%s → %d markets", series, len(raw))
                for item in raw:
                    ticker = item.get("ticker", "")
                    if ticker in seen:
                        continue
                    seen.add(ticker)
                    m = self._parse_market(item)
                    if m:
                        results.append(m)
            except Exception as exc:
                logger.debug("Kalshi financial series=%s fetch failed: %s", series, exc)

        logger.info(
            "Kalshi financial bracket scan: %d markets across %d series",
            len(results), len(_FINANCIAL_BRACKET_PREFIXES),
        )
        return results

    def _log_available_series(self) -> None:
        """Query /series and log all tickers so we can see what's on this API."""
        try:
            data = self._get("/series", params={"limit": 200})
            series_list = data.get("series", [])
            tickers = [s.get("ticker", "") for s in series_list]
            logger.info(
                "Kalshi available series (%d total): %s",
                len(tickers),
                sorted(tickers),
            )
        except Exception as exc:
            logger.warning("Kalshi /series discovery failed: %s", exc)

    def _parse_market(self, item: dict, weather_only: bool = False) -> Optional[Market]:
        try:
            ticker = item.get("ticker", "")
            title = item.get("title", "")
            subtitle = item.get("subtitle", "")
            question = f"{title} {subtitle}".strip()

            is_weather = bool(
                _WEATHER_SERIES.match(ticker)
                or any(kw in question.lower() for kw in
                       ("rain", "snow", "precip", "storm", "temperature", "wind", "weather"))
            )
            if weather_only and not is_weather:
                return None

            close_time = item.get("close_time") or item.get("expiration_time")
            resolution_date = (
                datetime.fromisoformat(close_time.replace("Z", "+00:00"))
                if close_time
                else datetime.now(timezone.utc)
            )

            # For sports game markets, Kalshi sets close_time to the settlement
            # window deadline (~2 weeks after the game), not the actual game time.
            # Override resolution_date with an estimated game end time derived
            # from the date encoded in the market ID (e.g. KXNBAGAME-26MAR13…).
            settlement_deadline = None
            if _is_game_market(ticker):
                game_date = _extract_game_date(ticker)
                if game_date:
                    offset_hours = next(
                        (h for p, h in _GAME_END_OFFSET_HOURS.items() if ticker.startswith(p)),
                        30,
                    )
                    estimated_end = game_date + timedelta(hours=offset_hours)
                    settlement_deadline = resolution_date
                    resolution_date = estimated_end
                    logger.debug(
                        "Sports market %s: overriding resolution_date from %s to %s "
                        "(game date extraction)",
                        ticker, settlement_deadline, estimated_end,
                    )

            # Kalshi prices: try cent-based integers first [0–100], fall back to
            # dollar fractions [0–1] if the cent fields are absent from the response.
            _yes_bid_c = item.get("yes_bid")
            _yes_ask_c = item.get("yes_ask")
            _no_bid_c  = item.get("no_bid")
            _no_ask_c  = item.get("no_ask")

            if _yes_bid_c is not None and _yes_ask_c is not None:
                yes_bid = float(_yes_bid_c) / 100.0
                yes_ask = float(_yes_ask_c) / 100.0
                no_bid  = float(_no_bid_c)  / 100.0 if _no_bid_c is not None else 0.50
                no_ask  = float(_no_ask_c)  / 100.0 if _no_ask_c is not None else 0.50
            else:
                # Dollar-denominated fallback (field names end in _dollars)
                yes_bid = _safe_float(item.get("yes_bid_dollars")) or 0.50
                yes_ask = _safe_float(item.get("yes_ask_dollars")) or 0.50
                no_bid  = 1.0 - yes_ask
                no_ask  = 1.0 - yes_bid

            yes_price = (yes_bid + yes_ask) / 2.0
            no_price  = (no_bid + no_ask)  / 2.0

            # Infer category from series ticker or question keywords
            category = self._infer_category(ticker, question, is_weather)
            tags = [category]
            if is_weather:
                tags.append("weather")

            market = Market(
                market_id=ticker,
                platform=self.PLATFORM,
                question=question,
                category=category,
                tags=tags,
                resolution_date=resolution_date,
                yes_price=yes_price,
                no_price=no_price,
                volume_usd=float(item.get("volume", 0) or 0),
                open_interest=float(item.get("open_interest", 0) or 0),
                location=_extract_location(question),
                raw=item,
            )

            # Attach additional Kalshi-specific fields as dynamic attributes.
            # All downstream code must use getattr(market, 'field', None) since
            # these are absent on Polymarket markets and may change with API updates.
            market.settlement_deadline = settlement_deadline  # original Kalshi close_time for game markets
            market.last_price    = _safe_float(item.get("last_price_dollars"))
            market.volume_24h    = _safe_float(item.get("volume_24h_fp"))
            market.liquidity     = _safe_float(item.get("liquidity_dollars"))
            market.yes_ask       = _safe_float(item.get("yes_ask_dollars"))
            market.yes_bid       = _safe_float(item.get("yes_bid_dollars"))
            market.yes_ask_size  = _safe_float(item.get("yes_ask_size_fp"))
            market.yes_bid_size  = _safe_float(item.get("yes_bid_size_fp"))
            market.updated_time  = item.get("updated_time")   # raw string; parse downstream
            market.created_time  = item.get("created_time")   # raw string; parse downstream
            market.open_time     = item.get("open_time")       # raw string; parse downstream

            return market
        except Exception as exc:
            logger.warning("Kalshi _parse_market error: %s | item=%s", exc, item)
            return None

    @staticmethod
    def _infer_category(ticker: str, question: str, is_weather: bool) -> str:
        if is_weather:
            return "weather"
        t = ticker.upper()
        q = question.lower()
        # Crypto series: price/level markets for BTC, ETH, SOL, and altcoins.
        _CRYPTO_PREFIXES = (
            "KXBTC", "KXETH", "KXSOL", "KXLTC", "KXDOGE", "KXBNB",
            "KXADA", "KXXRP", "KXMATIC", "KXAVAX", "KXLINK", "KXDOT",
            "KXCRYPTO", "KXSHIBA", "KXPEPE", "KXFLOKI", "KXTRX", "KXATOM",
            "KXNEAR", "KXFIL", "KXALGO", "KXICP", "KXAPT", "KXARB",
            "KXWIF", "KXBONK", "KXINJ", "KXOP", "KXSUIPER",
        )
        if any(t.startswith(p) for p in _CRYPTO_PREFIXES):
            return "crypto"
        # Catch-all: Kalshi embeds price thresholds in crypto tickers as -T<decimal>
        # e.g. KXSHIBAD-26FEB2217-T0.000006999 — no non-crypto market uses this format
        if re.search(r"-T\d*\.\d{3,}", t):
            return "crypto"
        if any(x in t for x in ("KXMVESPORTS", "KXESPORTS")):
            return "esports"
        if any(x in t for x in ("KXELECT", "KXPRES", "KXSEN", "KXGOV", "KXHOUS", "KXCONG")):
            return "politics"
        if any(x in t for x in ("KXNFL", "KXNBA", "KXMLB", "KXNHL", "KXSOC", "KXCFB", "KXNCAR")):
            return "sports"
        if any(x in t for x in ("KXFED", "KXFOMC", "KXCPI", "KXGDP", "KXECON", "KXJOBS")):
            return "economics"
        if any(w in q for w in ("election", "vote", "senate", "congress", "president", "governor")):
            return "politics"
        if any(w in q for w in ("win", "championship", "super bowl", "world series", "nfl", "nba", "mlb")):
            return "sports"
        if any(w in q for w in ("fed", "rate", "inflation", "gdp", "cpi", "unemployment", "jobs")):
            return "economics"
        if any(w in q for w in ("court", "judge", "ruling", "lawsuit", "sec", "ftc", "fda")):
            return "legal"
        return "general"

    # ── Order book ────────────────────────────────────────────────────────────

    # Temporary: log raw orderbook response once per process run to diagnose API shape.
    _raw_book_logged: bool = False

    def get_order_book(self, market_id: str) -> OrderBook:
        try:
            data = self._get(f"/markets/{market_id}/orderbook")

            # Temporary one-shot raw response log to diagnose API shape.
            if not KalshiClient._raw_book_logged:
                KalshiClient._raw_book_logged = True
                import json as _json
                raw_snippet = _json.dumps(data)[:500]
                logger.info("RAW orderbook API response for %s: %s", market_id, raw_snippet)

            # API returns "orderbook_fp" (fingerprint), not "orderbook"
            book = data.get("orderbook_fp", {})
            # Kalshi order book: "yes_dollars" = YES bid levels, "no_dollars" = NO bid levels.
            # NO bids at price p cents are equivalent to YES asks at (100 - p) cents,
            # because a NO buyer willing to pay p for NO is offering 100-p for YES.

            def _parse_level(level, side_label: str):
                """Parse [price, size] level, skipping non-numeric entries."""
                try:
                    price_raw, size_raw = level[0], level[1]
                    price = float(price_raw)
                    size = float(size_raw)
                    return price, size
                except (TypeError, ValueError, IndexError) as e:
                    logger.warning(
                        "Skipping malformed orderbook level for %s %s: %r (%s)",
                        market_id, side_label, level, e,
                    )
                    return None

            yes_bid_levels = []
            for b in book.get("yes_dollars") or []:
                parsed = _parse_level(b, "yes_bid")
                if parsed is not None:
                    yes_bid_levels.append(parsed)
            yes_bid_levels.sort(key=lambda x: -x[0])
            yes_bids = [PriceLevel(price=p / 100.0, size=s) for p, s in yes_bid_levels]

            yes_ask_levels = []
            for a in book.get("no_dollars") or []:
                parsed = _parse_level(a, "no_bid")
                if parsed is not None:
                    yes_ask_levels.append(parsed)
            yes_ask_levels.sort(key=lambda x: -x[0])
            yes_asks = [PriceLevel(price=(100.0 - p) / 100.0, size=s) for p, s in yes_ask_levels]

            return OrderBook(
                market_id=market_id,
                platform=self.PLATFORM,
                yes_bids=yes_bids,
                yes_asks=yes_asks,
            )
        except Exception as exc:
            logger.error("Kalshi get_order_book failed for %s: %s", market_id, exc)
            return OrderBook(
                market_id=market_id, platform=self.PLATFORM, yes_bids=[], yes_asks=[]
            )

    # ── Order management ──────────────────────────────────────────────────────

    def place_order(self, order: Order) -> Order:
        if order.dry_run:
            logger.info("[DRY RUN] Would place %s order on Kalshi: %s", order.side, order)
            order.status = OrderStatus.FILLED
            order.filled_price = order.price
            order.filled_size = order.size_usd
            return order

        try:
            # Kalshi expects price in cents (integer) for the API payload.
            # Each contract costs order.price (YES) or 1-order.price (NO) in USD.
            # Use the raw float for count to avoid rounding the cost before division.
            yes_price_cents = int(round(order.price * 100))
            if order.side == Side.YES:
                contract_cost_usd = order.price
            else:
                contract_cost_usd = 1.0 - order.price
            count = int(order.size_usd / contract_cost_usd) if contract_cost_usd > 0 else 0
            body = {
                "ticker": order.market_id,
                "action": "buy",
                "side": order.side.value.lower(),
                "type": "limit",
                "yes_price": yes_price_cents,
                "count": count,
            }
            logger.info(
                "Kalshi placing order: ticker=%s side=%s yes_price=%d count=%d (spend≈$%.2f)",
                body["ticker"], body["side"], body["yes_price"], body["count"],
                count * contract_cost_usd,
            )
            resp = self._post("/portfolio/orders", body)
            raw_order = resp.get("order", {})
            order.order_id = raw_order.get("order_id")
            order.status = OrderStatus.OPEN
            logger.info(
                "Kalshi order response: order_id=%s status=%s remaining=%s "
                "yes_price=%s side=%s",
                raw_order.get("order_id"),
                raw_order.get("status"),
                raw_order.get("remaining_count"),
                raw_order.get("yes_price"),
                raw_order.get("side"),
            )
        except Exception as exc:
            logger.error("Kalshi place_order failed: %s", exc)
            raise   # propagate so callers (e.g. executor circuit breaker) can handle it
        return order

    def cancel_order(self, order_id: str, market_id: str) -> bool:
        try:
            self._delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as exc:
            logger.error("Kalshi cancel_order failed for %s: %s", order_id, exc)
            return False

    def close_position(self, market_id: str) -> None:
        """
        Cancel any resting (unfilled) orders for this market.
        Filled contracts are held to resolution — Kalshi has no direct
        'close position' endpoint; exiting a filled position would require
        a counter-order which is not implemented here.
        """
        try:
            open_orders = self.get_open_orders()
            relevant = [o for o in open_orders if o.market_id == market_id]
            if not relevant:
                logger.info(
                    "Kalshi close_position %s: no resting orders to cancel "
                    "(position already filled – will resolve naturally)", market_id
                )
                return
            for o in relevant:
                if o.order_id:
                    cancelled = self.cancel_order(o.order_id, market_id)
                    logger.info(
                        "Kalshi close_position %s: cancel order %s → %s",
                        market_id, o.order_id, "OK" if cancelled else "FAILED",
                    )
        except Exception as exc:
            logger.warning("Kalshi close_position failed for %s: %s", market_id, exc)

    def get_market(self, market_id: str) -> Optional[Market]:
        """Fetch a single market by ID."""
        try:
            data = self._get(f"/markets/{market_id}")
            return self._parse_market(data.get("market", {}))
        except Exception as exc:
            logger.warning("Kalshi get_market failed for %s: %s", market_id, exc)
            return None

    def get_balance(self) -> Optional[float]:
        """Fetch available cash balance from Kalshi portfolio (in USD).

        Kalshi v2 API returns balance in cents under the key "balance".
        We also check "available_balance" as a fallback in case the schema
        changes between API versions.
        """
        try:
            data = self._get("/portfolio/balance")
            # Try the documented field names (Kalshi returns cents).
            # Log the raw payload once at DEBUG so it's visible in the log file.
            logger.debug("Kalshi /portfolio/balance raw: %s", data)
            for field in ("balance", "available_balance"):
                raw = data.get(field)
                if raw is not None:
                    return round(float(raw) / 100.0, 2)
            # Field not found – log the entire response so we can see what changed.
            logger.warning(
                "Kalshi get_balance: unexpected response format "
                "(no 'balance' field). Raw: %s", data
            )
            return None
        except Exception as exc:
            logger.warning("Kalshi get_balance failed: %s", exc)
            return None

    def get_positions(self) -> List[Order]:
        try:
            data = self._get("/portfolio/positions")
            positions = []
            for p in data.get("market_positions", []):
                if p.get("position", 0) == 0:
                    continue
                side = Side.YES if p["position"] > 0 else Side.NO
                positions.append(Order(
                    market_id=p["ticker"],
                    platform=self.PLATFORM,
                    side=side,
                    price=float(p.get("market_exposure", 0)),
                    size_usd=abs(float(p.get("position", 0))),
                    status=OrderStatus.OPEN,
                ))
            return positions
        except Exception as exc:
            logger.error("Kalshi get_positions failed: %s", exc)
            return []

    def get_open_orders(self) -> List[Order]:
        """Fetch all resting (unfilled) orders from Kalshi."""
        try:
            data = self._get("/portfolio/orders", params={"status": "open"})
            orders = []
            for o in data.get("orders", []):
                side = Side.YES if o.get("side") == "yes" else Side.NO
                yes_price_frac = float(o.get("yes_price", 50)) / 100.0
                remaining = float(o.get("remaining_count", 0))
                # remaining_count is contracts; convert to USD using per-contract cost.
                contract_cost = yes_price_frac if side == Side.YES else (1.0 - yes_price_frac)
                size_usd = remaining * contract_cost
                orders.append(Order(
                    market_id=o["ticker"],
                    platform=self.PLATFORM,
                    side=side,
                    price=yes_price_frac,
                    size_usd=size_usd,
                    status=OrderStatus.OPEN,
                    order_id=o.get("order_id"),
                ))
            return orders
        except Exception as exc:
            logger.error("Kalshi get_open_orders failed: %s", exc)
            return []
