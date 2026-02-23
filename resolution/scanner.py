"""
resolution.scanner – market scanner for Bot 2 (Resolution Drift Arbitrage).

Every scan cycle (every 5 minutes):
  1. Pull all active non-crypto markets from Polymarket + Kalshi
  2. Filter to markets expiring within 24 hours
  3. Exclude any market on the shared exclusion list
  4. Return with current YES probability from each platform

Non-crypto means: category is NOT "crypto" / "cryptocurrency", and the market
question does not appear to be a short-duration price prediction.

Only markets with a binary, unambiguous resolution criteria should be traded.
Vague or subjective markets are excluded by the confidence scorer downstream.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from data.markets.base import BaseMarketClient, Market
from shared.exclusion_list import ExclusionList

logger = logging.getLogger(__name__)

# Scan all these categories (weather removed, crypto excluded in filter)
SCAN_CATEGORIES = [
    "politics",
    "sports",
    "economics",
    "legal",
    "science",
    "entertainment",
    "culture",
    "world",
    "financials",
]

# Hard exclusion: never touch these category strings
EXCLUDED_CATEGORIES = {"crypto", "cryptocurrency", "weather", "esports"}

# Default scan window. Overridden by config (RESOLUTION_WINDOW_HOURS env var).
# Strategy spec: 24h for live prod. Kalshi demo markets are 14-30 days out — use 720.
RESOLUTION_WINDOW_HOURS = 720

# Minimum order book depth (total $ on best bid + ask) to consider a market
MIN_DEPTH_USD = 50.0


class ResolutionScanner:
    """
    Scans both platforms for non-crypto markets expiring within 24 hours.

    Parameters
    ----------
    kalshi_client    : Kalshi REST client (or None if disabled)
    poly_client      : Polymarket REST client (or None if disabled)
    exclusions       : shared exclusion list
    window_hours     : resolution window to scan (default 24h)
    max_per_platform : market fetch limit per platform per scan
    """

    def __init__(
        self,
        kalshi_client: Optional[BaseMarketClient],
        poly_client: Optional[BaseMarketClient],
        exclusions: ExclusionList,
        window_hours: float = RESOLUTION_WINDOW_HOURS,
        kalshi_window_hours: Optional[float] = None,
        poly_window_hours: Optional[float] = None,
        max_per_platform: int = 500,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._exclusions = exclusions
        self._kalshi_window = kalshi_window_hours if kalshi_window_hours is not None else window_hours
        self._poly_window = poly_window_hours if poly_window_hours is not None else window_hours
        self._max = max_per_platform

    # ── Public API ────────────────────────────────────────────────────────────

    def scan(self) -> List[Market]:
        """
        Return all candidate markets expiring within the window, from both platforms.
        Filtered, deduplicated, sorted by time to resolution (soonest first).
        """
        markets: List[Market] = []

        if self._kalshi:
            markets.extend(self._scan_platform(self._kalshi, "kalshi", self._kalshi_window))

        if self._poly:
            markets.extend(self._scan_platform(self._poly, "polymarket", self._poly_window))

        # Sort soonest-expiring first (most urgent to evaluate)
        markets.sort(key=lambda m: m.resolution_date)

        logger.info(
            "ResolutionScanner: %d candidate markets (kalshi_window=%gh poly_window=%gh)",
            len(markets), self._kalshi_window, self._poly_window,
        )
        return markets

    def scan_cross_platform_pairs(
        self, markets: List[Market]
    ) -> List[Tuple[Market, Market]]:
        """
        From a flat market list, return pairs (poly_market, kalshi_market)
        that appear to describe the same real-world event.

        Used by the gap detector for cross-platform signal.
        """
        poly = [m for m in markets if m.platform == "polymarket"]
        kalshi = [m for m in markets if m.platform == "kalshi"]
        pairs = []
        for pm in poly:
            for km in kalshi:
                if self._same_event(pm, km):
                    pairs.append((pm, km))
        logger.info("ResolutionScanner: %d cross-platform pairs found", len(pairs))
        return pairs

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scan_platform(
        self, client: BaseMarketClient, platform_name: str, window_hours: float
    ) -> List[Market]:
        results: List[Market] = []
        seen: set = set()
        rejected_reasons: dict = {"excluded": 0, "category": 0, "hours": 0, "price": 0}

        # Kalshi has no server-side category filter – one paginated call covers all
        # open markets and we filter client-side. Polymarket supports per-category
        # params so we query each category separately for complete coverage.
        if platform_name == "kalshi":
            try:
                # Pass max_close_ts so Kalshi filters server-side – avoids fetching
                # the full 76k+ market universe just to discard 99.9% of it.
                max_close_ts = int(time.time() + window_hours * 3600)
                all_markets = client.get_markets(max_close_ts=max_close_ts)
                logger.debug(
                    "ResolutionScanner: kalshi raw fetch → %d markets", len(all_markets)
                )
                if all_markets:
                    sample = all_markets[0]
                    logger.debug(
                        "ResolutionScanner: kalshi sample market id=%s cat=%s "
                        "hours=%.1f yes_price=%.3f question=%s",
                        sample.market_id, sample.category,
                        sample.hours_to_resolution, sample.yes_price,
                        sample.question[:60],
                    )
                for m in all_markets:
                    reason = self._reject_reason(m, window_hours)
                    if reason:
                        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    elif m.market_id not in seen:
                        seen.add(m.market_id)
                        results.append(m)
            except Exception as exc:
                logger.warning(
                    "ResolutionScanner: failed fetching kalshi markets: %s", exc
                )
        else:
            for category in SCAN_CATEGORIES:
                try:
                    all_markets = client.get_markets(category=category, limit=self._max)
                    for m in all_markets:
                        reason = self._reject_reason(m, window_hours)
                        if not reason and m.market_id not in seen:
                            seen.add(m.market_id)
                            results.append(m)
                        elif reason:
                            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                except Exception as exc:
                    logger.warning(
                        "ResolutionScanner: failed fetching %s/%s: %s",
                        platform_name, category, exc,
                    )

        if rejected_reasons:
            logger.info(
                "ResolutionScanner: %s rejection breakdown: %s",
                platform_name, rejected_reasons,
            )
        logger.info(
            "ResolutionScanner: %s → %d candidates", platform_name, len(results)
        )
        return results

    def _reject_reason(self, market: Market, window_hours: float) -> Optional[str]:
        """Return a rejection reason string, or None if the market is a valid candidate."""
        if self._exclusions.is_excluded(market.platform, market.market_id):
            return "excluded"
        if market.category.lower() in EXCLUDED_CATEGORIES or market.is_weather_market():
            return "category"
        hours_left = market.hours_to_resolution   # uses fixed timezone-aware property
        if not (0 < hours_left <= window_hours):
            return "hours"
        if not (0.05 < market.yes_price < 0.95):
            return "price"
        return None

    def _is_candidate(self, market: Market, window_hours: float) -> bool:
        return self._reject_reason(market, window_hours) is None

    @staticmethod
    def _same_event(poly: Market, kalshi: Market) -> bool:
        """
        Heuristic to detect if two markets (different platforms) describe the
        same real-world event.

        Uses question text similarity: if 3+ significant words overlap AND
        resolution dates are within 6 hours of each other.
        """
        # Time window check
        try:
            dt = abs((poly.resolution_date - kalshi.resolution_date).total_seconds())
            if dt > 6 * 3600:
                return False
        except Exception:
            return False

        # Word overlap heuristic
        stop = {
            "will", "the", "a", "an", "be", "is", "are", "was",
            "by", "in", "on", "at", "to", "of", "for", "and", "or",
            "yes", "no", "this", "that", "before", "after",
        }

        def sig_words(text: str) -> set:
            return {
                w.lower() for w in text.split()
                if len(w) > 3 and w.lower() not in stop
            }

        p_words = sig_words(poly.question)
        k_words = sig_words(kalshi.question)
        overlap = len(p_words & k_words)
        return overlap >= 3
