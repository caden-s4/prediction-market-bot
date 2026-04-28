"""
data.markets.scanner – pluggable market scanner configuration.

Defines WHICH platforms and WHICH categories to scan.
To add a new platform or change focus (e.g. add politics markets):
  1. Add the platform client to ENABLED_PLATFORMS
  2. Add the category to SCAN_CONFIG

The pipeline stages consume a flat list of Market objects produced here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .base import BaseMarketClient, Market

logger = logging.getLogger(__name__)


@dataclass
class PlatformScanConfig:
    """
    Configuration for scanning one platform.

    name          : human-readable platform name
    client        : the instantiated market client
    categories    : list of category names to fetch (in priority order)
    max_markets   : maximum markets to pull per category
    enabled       : toggle off a platform without removing config
    """
    name: str
    client: BaseMarketClient
    categories: List[str] = field(default_factory=lambda: ["weather"])
    max_markets: int = 200
    enabled: bool = True

    # Optional post-filter: called on each Market; return True to keep it
    market_filter: Optional[Callable[[Market], bool]] = None


class MarketScanner:
    """
    Aggregates markets from all enabled platforms according to config.

    Usage
    -----
    scanner = MarketScanner(platform_configs)
    markets = scanner.scan()   # returns List[Market], weather markets first
    """

    def __init__(self, platform_configs: List[PlatformScanConfig]) -> None:
        self._configs = platform_configs

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self, prioritise_weather: bool = True) -> List[Market]:
        """
        Scan all enabled platforms and return a unified list of Market objects.

        Parameters
        ----------
        prioritise_weather : if True, weather markets bubble to the top.
        """
        all_markets: List[Market] = []

        for cfg in self._configs:
            if not cfg.enabled:
                logger.debug("Platform %s is disabled – skipping", cfg.name)
                continue

            platform_markets: List[Market] = []
            for category in cfg.categories:
                try:
                    if category == "weather":
                        # Prefer the dedicated weather-market endpoint if available
                        if hasattr(cfg.client, "get_weather_markets"):
                            markets = cfg.client.get_weather_markets(limit=cfg.max_markets)
                        else:
                            markets = cfg.client.get_markets(
                                category=category,
                                tags=cfg.client.WEATHER_CATEGORY_TAGS,
                                limit=cfg.max_markets,
                            )
                    else:
                        markets = cfg.client.get_markets(
                            category=category, limit=cfg.max_markets
                        )

                    if cfg.market_filter:
                        markets = [m for m in markets if cfg.market_filter(m)]

                    logger.info(
                        "Platform=%s category=%s → %d markets found",
                        cfg.name, category, len(markets),
                    )
                    platform_markets.extend(markets)
                except Exception as exc:
                    logger.error(
                        "Scan failed for platform=%s category=%s: %s",
                        cfg.name, category, exc,
                    )

            all_markets.extend(platform_markets)

        if prioritise_weather:
            all_markets.sort(key=lambda m: (0 if m.is_weather_market() else 1, m.market_id))

        # Deduplicate by (platform, market_id)
        seen: set = set()
        unique: List[Market] = []
        for m in all_markets:
            key = (m.platform, m.market_id)
            if key not in seen:
                seen.add(key)
                unique.append(m)

        logger.info("MarketScanner: %d unique markets after dedup", len(unique))
        return unique

    def scan_arbitrage_candidates(self) -> List[Dict]:
        """
        Look for the same event priced differently across platforms.
        Returns a list of dicts with 'poly_market' and 'kalshi_market' keys
        where both describe the same underlying event.
        """
        markets = self.scan()
        poly = [m for m in markets if m.platform == "polymarket"]
        kalshi = [m for m in markets if m.platform == "kalshi"]
        candidates = []
        for pm in poly:
            for km in kalshi:
                if self._same_event(pm, km):
                    spread = abs(pm.yes_price - km.yes_price)
                    candidates.append({
                        "poly_market": pm,
                        "kalshi_market": km,
                        "price_spread": spread,
                    })
        return sorted(candidates, key=lambda x: -x["price_spread"])

    @staticmethod
    def _same_event(m1: Market, m2: Market) -> bool:
        """Heuristic: same location + resolution date within 24 h."""
        if not (m1.location and m2.location):
            return False
        lat_close = abs(m1.location.get("lat", 0) - m2.location.get("lat", 0)) < 1.0
        lon_close = abs(m1.location.get("lon", 0) - m2.location.get("lon", 0)) < 1.0
        time_close = abs(
            (m1.resolution_date - m2.resolution_date).total_seconds()
        ) < 86400
        return lat_close and lon_close and time_close


# ── Default scanner factory ───────────────────────────────────────────────────

def build_default_scanner(
    polymarket_client: Optional[BaseMarketClient] = None,
    kalshi_client: Optional[BaseMarketClient] = None,
) -> MarketScanner:
    """
    Build a MarketScanner with the default configuration:
    - Weather markets on both Polymarket and Kalshi (priority order)
    - Easy to extend: just append to `configs`

    Pass None for a client to skip that platform.
    """
    configs: List[PlatformScanConfig] = []

    if kalshi_client is not None:
        configs.append(PlatformScanConfig(
            name="Kalshi",
            client=kalshi_client,
            categories=["weather"],   # ← change to add more categories
            max_markets=200,
            enabled=True,
        ))

    if polymarket_client is not None:
        configs.append(PlatformScanConfig(
            name="Polymarket",
            client=polymarket_client,
            categories=["weather"],   # ← change to add more categories
            max_markets=200,
            enabled=True,
        ))

    return MarketScanner(configs)
