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
import time
from datetime import datetime, timezone
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
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        # Cache the loaded RSA private key so we only parse it once.
        self._private_key = self._load_private_key()

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
        headers = self._sign("POST", self._path_prefix + path)
        url = self._base_url + path
        resp = self._session.post(url, data=body_str, headers=headers, timeout=15)
        if not resp.ok:
            logger.error(
                "Kalshi POST %s HTTP %d – key=%s… body: %s",
                path, resp.status_code,
                self._api_key[:8],
                resp.text[:500],
            )
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> Any:
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

            if page > 0:
                time.sleep(0.6)   # ~1 req/s – matches Kalshi's documented rate limit
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
            if i > 0:
                time.sleep(0.6)  # stay under Kalshi rate limit (~1 req/s)
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

    def _log_available_series(self) -> None:
        """Query /series and log all tickers so we can see what's on this API."""
        try:
            time.sleep(0.6)
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

            # Kalshi prices are in cents [0–100]; convert to [0–1]
            yes_bid = float(item.get("yes_bid", 50)) / 100.0
            yes_ask = float(item.get("yes_ask", 50)) / 100.0
            no_bid = float(item.get("no_bid", 50)) / 100.0
            no_ask = float(item.get("no_ask", 50)) / 100.0

            yes_price = (yes_bid + yes_ask) / 2.0
            no_price = (no_bid + no_ask) / 2.0

            # Infer category from series ticker or question keywords
            category = self._infer_category(ticker, question, is_weather)
            tags = [category]
            if is_weather:
                tags.append("weather")

            return Market(
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

    def get_order_book(self, market_id: str) -> OrderBook:
        try:
            data = self._get(f"/markets/{market_id}/orderbook")
            book = data.get("orderbook", {})
            # Kalshi order book: "yes" = YES bid levels, "no" = NO bid levels.
            # NO bids at price p cents are equivalent to YES asks at (100 - p) cents,
            # because a NO buyer willing to pay p for NO is offering 100-p for YES.
            yes_bids = [
                PriceLevel(price=float(b[0]) / 100.0, size=float(b[1]))
                for b in sorted(book.get("yes") or [], key=lambda x: -x[0])
            ]
            yes_asks = [
                PriceLevel(price=(100.0 - float(a[0])) / 100.0, size=float(a[1]))
                for a in sorted(book.get("no") or [], key=lambda x: x[0])
            ]
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
            # Kalshi expects price in cents
            body = {
                "ticker": order.market_id,
                "action": "buy",
                "side": order.side.value.lower(),
                "type": "limit",
                "yes_price": int(round(order.price * 100)),
                "count": int(order.size_usd),   # Kalshi: count = number of contracts ($1 each)
            }
            resp = self._post("/portfolio/orders", body)
            order.order_id = resp.get("order", {}).get("order_id")
            order.status = OrderStatus.OPEN
            logger.info("Kalshi order placed: %s", order.order_id)
        except Exception as exc:
            logger.error("Kalshi place_order failed: %s", exc)
        return order

    def cancel_order(self, order_id: str, market_id: str) -> bool:
        try:
            self._delete(f"/portfolio/orders/{order_id}")
            return True
        except Exception as exc:
            logger.error("Kalshi cancel_order failed for %s: %s", order_id, exc)
            return False

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
