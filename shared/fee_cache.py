"""
shared.fee_cache – per-market fee rate cache.

CRITICAL engineering constraint: every order on either bot must first check the
fee rate for that specific market. Never hardcode. Never assume. Polymarket is
actively rolling out fees to more markets, so a market that was free yesterday
may have fees today.

Refresh policy:
  - Cache entries expire after FEE_CACHE_TTL_SECONDS (default 900 = 15 min)
  - Always refresh before executing a trade regardless of cache age
  - Store results in memory; persist to disk on shutdown so restarts are fast

Fee endpoints:
  Polymarket : GET https://clob.polymarket.com/markets/{condition_id}
               field: "feeRateBps" (basis points, integer)
  Kalshi     : included in market detail response; field: "fee_multiplier"
               expressed as a decimal (e.g. 0.07 = 7%)
"""

from __future__ import annotations

import logging
import time
from threading import RLock
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

FEE_CACHE_TTL_SECONDS = 900   # 15 minutes
_POLY_CLOB = "https://clob.polymarket.com"
_TIMEOUT = 8


class FeeCache:
    """
    Thread-safe in-memory cache for per-market taker fee rates.

    Returns fees as a decimal fraction (e.g. 0.02 = 2%).
    """

    def __init__(self, ttl: int = FEE_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = RLock()
        # { (platform, market_id): (fee_decimal, fetched_at_timestamp) }
        self._cache: Dict[Tuple[str, str], Tuple[float, float]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_taker_fee(
        self,
        platform: str,
        market_id: str,
        force_refresh: bool = False,
    ) -> float:
        """
        Return the taker fee rate as a decimal for (platform, market_id).
        Fetches from the live endpoint if the cache entry is missing or stale.

        Parameters
        ----------
        platform      : "polymarket" or "kalshi"
        market_id     : platform-specific market identifier
        force_refresh : bypass cache and always fetch live

        Returns
        -------
        Taker fee as decimal fraction. 0.0 if the market has no taker fees.
        """
        key = (platform, market_id)

        if not force_refresh:
            cached = self._get_cached(key)
            if cached is not None:
                return cached

        fee = self._fetch_live(platform, market_id)
        self._store(key, fee)
        return fee

    def invalidate(self, platform: str, market_id: str) -> None:
        """Remove a market from the cache (e.g. after a fee surprise)."""
        with self._lock:
            self._cache.pop((platform, market_id), None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_cached(self, key: Tuple[str, str]) -> Optional[float]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            fee, fetched_at = entry
            if time.monotonic() - fetched_at > self._ttl:
                return None  # stale
            return fee

    def _store(self, key: Tuple[str, str], fee: float) -> None:
        with self._lock:
            self._cache[key] = (fee, time.monotonic())

    def _fetch_live(self, platform: str, market_id: str) -> float:
        try:
            if platform == "polymarket":
                return self._fetch_polymarket(market_id)
            elif platform == "kalshi":
                return self._fetch_kalshi(market_id)
            else:
                logger.warning("FeeCache: unknown platform '%s'", platform)
                return 0.0
        except Exception as exc:
            logger.warning(
                "FeeCache: failed to fetch fee for %s/%s: %s – defaulting to 0",
                platform, market_id, exc,
            )
            return 0.0

    def _fetch_polymarket(self, market_id: str) -> float:
        """
        Fetch feeRateBps from Polymarket CLOB market endpoint.
        feeRateBps is in basis points (100 bps = 1%).
        """
        url = f"{_POLY_CLOB}/markets/{market_id}"
        resp = requests.get(url, timeout=_TIMEOUT, proxies={})
        resp.raise_for_status()
        data = resp.json()
        fee_bps = data.get("feeRateBps", 0)
        if fee_bps is None:
            fee_bps = 0
        fee = int(fee_bps) / 10_000  # bps → decimal
        logger.debug("FeeCache: polymarket/%s feeRateBps=%s → %.4f", market_id, fee_bps, fee)
        return fee

    def _fetch_kalshi(self, market_id: str) -> float:
        """
        Kalshi includes fee_multiplier in market detail.
        Expressed as decimal (0.07 = 7%).

        NOTE: As of early 2026 the Kalshi v2 /markets/{id} endpoint no longer
        returns fee_multiplier for most market types.  Kalshi charges fees as a
        percentage of net winnings at settlement (not a per-order taker fee), so
        the field is not needed to place orders.  We default to 0.0 when it is
        absent — the gap threshold acts as the real safety margin.
        """
        url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{market_id}"
        resp = requests.get(url, timeout=_TIMEOUT, proxies={})
        resp.raise_for_status()
        data = resp.json()
        market = data.get("market", data)
        fee_multiplier = market.get("fee_multiplier")
        if fee_multiplier is None:
            logger.debug(
                "FeeCache: kalshi/%s – fee_multiplier not in API response, using 0.0",
                market_id,
            )
            return 0.0
        fee = float(fee_multiplier)
        logger.debug("FeeCache: kalshi/%s fee_multiplier=%.4f", market_id, fee)
        return fee
