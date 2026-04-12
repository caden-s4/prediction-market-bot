"""
shared.fee_cache – per-market fee rate cache.

CRITICAL engineering constraint: every order on either bot must first check the
fee rate for that specific market. Never hardcode. Never assume. Polymarket is
actively rolling out fees to more markets, so a market that was free yesterday
may have fees today.

Refresh policy:
  - Polymarket entries cache for FEE_CACHE_TTL_SECONDS (default 900 = 15 min)
  - Kalshi fees are computed directly from the official formula — no API call.
    No caching needed; the formula is O(1) and deterministic for a given price.

Fee logic:
  Polymarket : GET https://clob.polymarket.com/markets/{condition_id}
               field: "feeRateBps" (basis points, integer)
  Kalshi     : Official formula: round_up(0.07 × C × P × (1-P)), min $0.01/contract
               The Kalshi v2 /markets/{id} endpoint stopped returning fee_multiplier
               in early 2026. We compute directly from price instead.
"""

from __future__ import annotations

import logging
import math
import time
from threading import RLock
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

FEE_CACHE_TTL_SECONDS = 900   # 15 minutes (Polymarket only)
_POLY_CLOB = "https://clob.polymarket.com"
_TIMEOUT = 8


def kalshi_fee_per_contract(price: float) -> float:
    """
    Official Kalshi fee: round_up(0.07 × C × P × (1-P)) to nearest cent,
    minimum $0.01 per contract.

    Parameters
    ----------
    price : YES price (0.0 – 1.0)

    Returns
    -------
    Fee in dollars for one $1 contract at the given price.

    Examples
    --------
    kalshi_fee_per_contract(0.50) → 0.02   # round_up(0.0175)
    kalshi_fee_per_contract(0.10) → 0.01   # round_up(0.0063)
    kalshi_fee_per_contract(0.90) → 0.01   # round_up(0.0063)
    kalshi_fee_per_contract(0.01) → 0.01   # minimum
    """
    if price <= 0.0 or price >= 1.0:
        return 0.01  # minimum — extreme prices still incur minimum fee
    raw = 0.07 * price * (1.0 - price)
    return max(math.ceil(raw * 100) / 100.0, 0.01)


class FeeCache:
    """
    Thread-safe in-memory cache for per-market taker fee rates.

    Returns fees as a per-contract dollar amount (same scale as probability
    gap values, since contract value = $1).

    Kalshi: computed directly via kalshi_fee_per_contract(price) — no API.
    Polymarket: fetched from CLOB API, cached for FEE_CACHE_TTL_SECONDS.
    """

    def __init__(self, ttl: int = FEE_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._lock = RLock()
        # { (platform, market_id): (fee_decimal, fetched_at_timestamp) }
        # Polymarket only — Kalshi is computed on the fly.
        self._cache: Dict[Tuple[str, str], Tuple[float, float]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_taker_fee(
        self,
        platform: str,
        market_id: str,
        price: float = 0.5,
        force_refresh: bool = False,
    ) -> float:
        """
        Return the taker fee as a per-contract dollar amount for (platform, market_id).

        Parameters
        ----------
        platform      : "polymarket" or "kalshi"
        market_id     : platform-specific market identifier
        price         : current YES price (required for Kalshi formula)
        force_refresh : bypass Polymarket cache and always fetch live

        Returns
        -------
        Fee per $1 contract as a decimal (e.g. 0.02 = $0.02/contract).
        """
        if platform == "kalshi":
            # Kalshi fee is deterministic from price — no API, no cache.
            return kalshi_fee_per_contract(price)

        # Polymarket: use cache
        key = (platform, market_id)
        if not force_refresh:
            cached = self._get_cached(key)
            if cached is not None:
                return cached

        fee = self._fetch_live(platform, market_id)
        self._store(key, fee)
        return fee

    def invalidate(self, platform: str, market_id: str) -> None:
        """Remove a Polymarket market from the cache (e.g. after a fee surprise)."""
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
        Returns fee as a per-contract dollar fraction (same scale as Kalshi).
        """
        url = f"{_POLY_CLOB}/markets/{market_id}"
        resp = requests.get(url, timeout=_TIMEOUT, proxies={})
        resp.raise_for_status()
        data = resp.json()
        fee_bps = data.get("feeRateBps", 0)
        if fee_bps is None:
            fee_bps = 0
        fee = int(fee_bps) / 10_000  # bps → decimal fraction
        logger.debug("FeeCache: polymarket/%s feeRateBps=%s → %.4f", market_id, fee_bps, fee)
        return fee
