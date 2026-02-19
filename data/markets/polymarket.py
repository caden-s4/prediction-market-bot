"""
data.markets.polymarket – Polymarket CLOB API client.

Polymarket runs on Polygon (MATIC) and uses a Central Limit Order Book.
Docs: https://docs.polymarket.com/

Authentication: L1 auth uses a private key + API key/secret/passphrase.
We use the official py-clob-client library where available, falling back
to raw HTTP when the library is not installed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

# Known weather-related keyword patterns for Polymarket market titles
_WEATHER_PATTERNS = re.compile(
    r"\b(rain|snow|precip|storm|hurricane|tornado|flood|drought|temperature|"
    r"celsius|fahrenheit|wind|weather|blizzard|heatwave|frost|ice)\b",
    re.IGNORECASE,
)

# City → approximate coordinates (extend as needed)
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
    """Best-effort city extraction from market question text."""
    lower = text.lower()
    for city, coords in _CITY_COORDS.items():
        if city in lower:
            return {**coords, "city": city.title()}
    return None


class PolymarketClient(BaseMarketClient):
    PLATFORM = "polymarket"
    WEATHER_CATEGORY_TAGS = ["weather", "precipitation", "temperature"]

    _GAMMA_BASE = "https://gamma-api.polymarket.com"
    _CLOB_BASE = "https://clob.polymarket.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        private_key: str,
        funder_address: str,
        chain_id: int = 137,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._private_key = private_key
        self._funder_address = funder_address
        self._chain_id = chain_id
        self._session = requests.Session()
        # Try to import official py-clob-client
        self._clob_client = self._init_clob_client()

    def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            )
            client = ClobClient(
                host=self._CLOB_BASE,
                chain_id=self._chain_id,
                key=self._private_key,
                creds=creds,
                funder=self._funder_address,
            )
            logger.info("Polymarket: using py-clob-client library")
            return client
        except ImportError:
            logger.warning(
                "py-clob-client not installed; falling back to raw HTTP. "
                "Run: pip install py-clob-client"
            )
            return None

    # ── Market scanning ───────────────────────────────────────────────────────

    def get_markets(
        self,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 100,
    ) -> List[Market]:
        """
        Fetch open Polymarket markets via the Gamma (REST) API.
        Filters to weather-related markets by keyword matching.
        """
        params: Dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": limit,
        }
        if tags:
            params["tag_slug"] = ",".join(tags)

        try:
            resp = self._session.get(
                f"{self._GAMMA_BASE}/markets", params=params, timeout=15
            )
            resp.raise_for_status()
            raw_markets = resp.json()
        except Exception as exc:
            logger.error("Polymarket get_markets failed: %s", exc)
            return []

        markets: List[Market] = []
        for item in raw_markets:
            market = self._parse_market(item)
            if market:
                markets.append(market)
        return markets

    def get_weather_markets(self, limit: int = 200) -> List[Market]:
        """Convenience method: fetch and return only weather-related markets."""
        all_markets = self.get_markets(limit=limit)
        return [m for m in all_markets if m.is_weather_market()]

    def _parse_market(self, item: dict) -> Optional[Market]:
        try:
            question = item.get("question", item.get("title", ""))
            is_weather = bool(_WEATHER_PATTERNS.search(question))
            tags_raw = item.get("tags", [])
            tag_names = [t.get("slug", t) if isinstance(t, dict) else str(t)
                         for t in tags_raw]
            if not is_weather and "weather" not in " ".join(tag_names).lower():
                # Skip non-weather markets
                return None

            end_date = item.get("endDate") or item.get("end_date_iso")
            resolution_date = (
                datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if end_date
                else datetime.now(timezone.utc)
            )

            # Prices: Polymarket stores outcome prices as 0–1 floats
            outcomes = item.get("outcomes", ["Yes", "No"])
            prices = item.get("outcomePrices", ["0.5", "0.5"])
            yes_price = float(prices[0]) if prices else 0.5
            no_price = float(prices[1]) if len(prices) > 1 else 1.0 - yes_price

            return Market(
                market_id=item.get("conditionId") or item.get("id", ""),
                platform=self.PLATFORM,
                question=question,
                category="weather" if is_weather else item.get("category", ""),
                tags=tag_names,
                resolution_date=resolution_date,
                yes_price=yes_price,
                no_price=no_price,
                volume_usd=float(item.get("volume", 0) or 0),
                open_interest=float(item.get("liquidity", 0) or 0),
                location=_extract_location(question),
                raw=item,
            )
        except Exception as exc:
            logger.warning("Polymarket _parse_market error: %s | item=%s", exc, item)
            return None

    # ── Order book ────────────────────────────────────────────────────────────

    def get_order_book(self, market_id: str) -> OrderBook:
        if self._clob_client:
            return self._get_order_book_clob(market_id)
        return self._get_order_book_http(market_id)

    def _get_order_book_clob(self, market_id: str) -> OrderBook:
        try:
            book = self._clob_client.get_order_book(market_id)
            bids = [PriceLevel(price=float(b.price), size=float(b.size))
                    for b in sorted(book.bids, key=lambda x: -float(x.price))]
            asks = [PriceLevel(price=float(a.price), size=float(a.size))
                    for a in sorted(book.asks, key=lambda x: float(x.price))]
            return OrderBook(
                market_id=market_id,
                platform=self.PLATFORM,
                yes_bids=bids,
                yes_asks=asks,
            )
        except Exception as exc:
            logger.warning("CLOB order book failed for %s: %s", market_id, exc)
            return self._get_order_book_http(market_id)

    def _get_order_book_http(self, market_id: str) -> OrderBook:
        try:
            resp = self._session.get(
                f"{self._CLOB_BASE}/book",
                params={"token_id": market_id},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            bids = [
                PriceLevel(price=float(b["price"]), size=float(b["size"]))
                for b in sorted(data.get("bids", []), key=lambda x: -float(x["price"]))
            ]
            asks = [
                PriceLevel(price=float(a["price"]), size=float(a["size"]))
                for a in sorted(data.get("asks", []), key=lambda x: float(x["price"]))
            ]
            return OrderBook(
                market_id=market_id,
                platform=self.PLATFORM,
                yes_bids=bids,
                yes_asks=asks,
            )
        except Exception as exc:
            logger.error("HTTP order book failed for %s: %s", market_id, exc)
            return OrderBook(
                market_id=market_id, platform=self.PLATFORM, yes_bids=[], yes_asks=[]
            )

    # ── Order management ──────────────────────────────────────────────────────

    def place_order(self, order: Order) -> Order:
        if order.dry_run:
            logger.info("[DRY RUN] Would place %s order on Polymarket: %s", order.side, order)
            order.status = OrderStatus.FILLED
            order.filled_price = order.price
            order.filled_size = order.size_usd
            return order

        if self._clob_client:
            return self._place_order_clob(order)
        raise NotImplementedError(
            "Live order placement requires py-clob-client. pip install py-clob-client"
        )

    def _place_order_clob(self, order: Order) -> Order:
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            args = OrderArgs(
                token_id=order.market_id,
                price=order.price,
                size=order.size_usd,
                side=order.side.value,
            )
            resp = self._clob_client.create_and_post_order(args)
            order.order_id = resp.get("orderID")
            order.status = OrderStatus.OPEN
            logger.info("Polymarket order placed: %s", order.order_id)
        except Exception as exc:
            logger.error("Polymarket place_order failed: %s", exc)
        return order

    def cancel_order(self, order_id: str, market_id: str) -> bool:
        if not self._clob_client:
            logger.warning("Cannot cancel: py-clob-client not available")
            return False
        try:
            self._clob_client.cancel(order_id=order_id)
            return True
        except Exception as exc:
            logger.error("Polymarket cancel_order failed: %s", exc)
            return False

    def get_positions(self) -> List[Order]:
        # Positions are tracked internally by the portfolio module;
        # Polymarket doesn't have a direct "positions" endpoint like Kalshi.
        return []
