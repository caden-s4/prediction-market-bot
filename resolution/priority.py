"""
resolution.priority – Priority scoring for discovered markets.

Scores each Market on four criteria and attaches a priority_score attribute
(float 0–1) so the scan loop within each tier evaluates the most actionable
markets first.

Criteria
--------
1. Freshness (weight 0.20)
   Markets created < 2 h ago get a boost because pricing is least efficient
   at creation.  Field read from market.created_time (dynamic attr set by
   the Kalshi client) or market.raw["created_time"] / ["open_time"].

2. Low volume (weight 0.25)
   Markets with low lifetime USD volume are more likely to have stale or
   inefficient pricing.

3. Bracket proximity (weight 0.30)
   On multi-outcome bracket markets (e.g. Nasdaq 100 range markets), strikes
   closest to the current underlying price have the most mispricing potential.
   Requires a known series→symbol mapping and non-stale price data.

4. Staleness (weight 0.25)
   Markets whose pricing has not been updated recently (per updated_time),
   or that show a wide bid/ask spread and low 24h volume, are more likely to
   carry mispricing.  Higher score = more stale = more opportunity.

Combined score
--------------
    priority_score = (freshness * 0.20) + (low_volume * 0.25)
                   + (bracket * 0.30)   + (staleness * 0.25)

Run after market discovery, before tier registry ingest.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from data.markets.base import Market

logger = logging.getLogger(__name__)

# Score weights
_W_FRESHNESS  = 0.20
_W_VOLUME     = 0.25
_W_BRACKET    = 0.30
_W_STALENESS  = 0.25

# Map Kalshi series prefix → Yahoo Finance symbol used by FinancialDataSource.
# Only include series where the financial source can actually fetch a live price.
# KXAAAGASW (AAA gas) has no Yahoo Finance mapping and is intentionally omitted.
_SERIES_TO_SYMBOL: Dict[str, str] = {
    "KXNASDAQ100U": "NQ=F",   # Nasdaq 100 (up-bracket variant)
    "KXNASDAQ100":  "NQ=F",   # Nasdaq 100
    "KXINX":        "ES=F",   # S&P 500 (^INX index → ES=F futures price)
    "KXWTI":        "CL=F",   # WTI crude oil (daily brackets)
    "KXWTIW":       "CL=F",   # WTI crude oil (weekly brackets)
    "KXBRENTD":     "CL=F",   # Brent crude daily (proxy: CL=F WTI, ~$5-10 spread)
    "KXTNOTEW":     "^TNX",   # 10-year Treasury yield
    "KXGOLDD":      "GC=F",   # Gold daily brackets
    "KXGOLDW":      "GC=F",   # Gold futures
    "KXSILVERD":    "SI=F",   # Silver daily brackets
    "KXSILVERW":    "SI=F",   # Silver futures
}

# Price staleness threshold: skip bracket scoring if price data is older than this
_PRICE_STALENESS_S = 300  # 5 minutes


# ── Module-level helpers ───────────────────────────────────────────────────────

def _bracket_prefix(market_id: str) -> Optional[str]:
    """Return the series prefix for bracket markets, or None for singletons.

    Mirrors the logic in executor.py's ``_bracket_prefix()`` exactly so the
    two components agree on which markets are bracket markets.

    Examples
    --------
    KXNASDAQ100U-26MAR02H1600-T22099.99  → "KXNASDAQ100U-26MAR02H1600"
    KXAAAGASW-26MAR02-2.888              → "KXAAAGASW-26MAR02"
    KXSOME-MARKET                        → None
    """
    parts = market_id.split("-")
    if len(parts) >= 2 and re.match(r"^(?:[\d.]+|T[\d.]+)$", parts[-1]):
        return "-".join(parts[:-1])
    return None


def _parse_strike(market_id: str) -> Optional[float]:
    """Extract the numeric strike from a bracket market ID."""
    parts = market_id.split("-")
    if not parts:
        return None
    suffix = parts[-1]
    try:
        return float(suffix.lstrip("T"))
    except (ValueError, AttributeError):
        return None


def _proximity_score(strike: float, current_price: float) -> float:
    """Score bracket proximity.

    Returns
    -------
    1.0  if |strike - price| / price ≤ 1.2 %
    linear decay from 1.0 → 0.0 between 1.2 % and 3.0 %
    0.0  if beyond 3.0 % or current_price is zero
    """
    if current_price <= 0:
        return 0.0
    dist = abs(strike - current_price) / current_price
    if dist <= 0.012:
        return 1.0
    if dist >= 0.030:
        return 0.0
    return 1.0 - (dist - 0.012) / (0.030 - 0.012)


def _parse_datetime(value: object) -> Optional[datetime]:
    """Parse a datetime from a string, int/float (unix timestamp), or datetime.

    Handles all formats returned by the Kalshi v2 API:
      - datetime object (returned as-is)
      - int or float epoch seconds → UTC datetime
      - numeric string (e.g. "1741779000") → epoch → UTC datetime
      - ISO 8601 with Z suffix: "2026-03-12T10:30:00Z"
      - ISO 8601 with offset:   "2026-03-12T10:30:00+00:00"
      - ISO 8601 without tz:    "2026-03-12T10:30:00"  (assumed UTC)
      - Empty string or None    → None
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Numeric string → treat as epoch seconds
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except ValueError:
            pass
        # ISO 8601: replace Z suffix so fromisoformat works on Python < 3.11
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            # Attach UTC if no timezone was present
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        # Last-resort: common strptime formats (naive → UTC)
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


# ── Main class ─────────────────────────────────────────────────────────────────

class PriorityScorer:
    """
    Scores a batch of markets for scan-order priority within each tier.

    Parameters
    ----------
    financial_source : FinancialDataSource instance (or None)
        Used to fetch current underlying prices for bracket proximity scoring.
        If None, bracket proximity scores default to 0.0.
    """

    def __init__(self, financial_source) -> None:
        self._financial_source = financial_source

    # ── Public API ────────────────────────────────────────────────────────────

    def score_batch(self, markets: List[Market]) -> List[Market]:
        """
        Score all markets, attach ``priority_score`` attribute to each, and
        return the list sorted descending by score (highest priority first).

        This method is idempotent — calling it twice on the same list updates
        the ``priority_score`` attribute in place.
        """
        if not markets:
            return markets

        # Log a raw-value sample once per batch so operators can verify the
        # freshness parser is picking up the right field format.
        _sample = markets[0]
        logger.debug(
            "[PRIORITY] freshness raw sample: created_time=%r open_time=%r",
            _sample.raw.get("created_time") if _sample.raw else None,
            _sample.raw.get("open_time") if _sample.raw else None,
        )

        # Build price cache: one fetch per underlying per batch
        price_cache: Dict[str, Optional[Tuple[float, float]]] = {}

        # Gather bracket groups: prefix → [(market, strike)]
        bracket_groups: Dict[str, List[Tuple[Market, float]]] = {}
        for m in markets:
            prefix = _bracket_prefix(m.market_id)
            if prefix is None:
                continue
            strike = _parse_strike(m.market_id)
            if strike is None:
                continue
            bracket_groups.setdefault(prefix, []).append((m, strike))

        # Score bracket proximity (with staleness guard and floor rule)
        bracket_scores: Dict[str, float] = {}
        self._score_all_brackets(bracket_groups, price_cache, bracket_scores)

        # Compute combined scores and collect summary stats
        fresh_count      = 0
        low_vol_count    = 0
        near_money_count = 0
        stale_count      = 0

        for m in markets:
            f = self._score_freshness(m)
            v = self._score_volume(m)
            b = bracket_scores.get(m.market_id, 0.0)
            s = self._score_staleness(m)

            if f > 0:
                fresh_count += 1
            if v >= 0.7:
                low_vol_count += 1
            if b > 0:
                near_money_count += 1
            if s > 0.3:
                stale_count += 1

            m.priority_score = (
                f * _W_FRESHNESS
                + v * _W_VOLUME
                + b * _W_BRACKET
                + s * _W_STALENESS
            )

        markets_sorted = sorted(markets, key=lambda m: m.priority_score, reverse=True)

        top5 = ", ".join(
            f"{m.market_id} ({m.priority_score:.3f})"
            for m in markets_sorted[:5]
        )
        logger.info(
            "[PRIORITY] Scored %d markets: %d fresh, %d low-volume, "
            "%d near-money brackets, %d stale",
            len(markets), fresh_count, low_vol_count, near_money_count, stale_count,
        )
        logger.info("[PRIORITY] Top 5: %s", top5 if top5 else "none")

        return markets_sorted

    # ── Private helpers ───────────────────────────────────────────────────────

    def _score_freshness(self, market: Market) -> float:
        """Score market freshness from created_time / open_time.

        Checks the dynamic attribute set by the Kalshi client first, then falls
        back to market.raw so the scorer works even without the attribute.
        """
        raw_value = (
            getattr(market, "created_time", None)
            or getattr(market, "open_time", None)
            or (market.raw.get("created_time") if market.raw else None)
            or (market.raw.get("open_time") if market.raw else None)
        )
        if raw_value is None:
            return 0.0
        try:
            created_dt = _parse_datetime(raw_value)
            if created_dt is None:
                return 0.0
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - created_dt).total_seconds() / 3600.0
            if age_h < 2.0:
                return 1.0
            if age_h < 6.0:
                return 0.5
            return 0.0
        except Exception:
            return 0.0

    def _score_volume(self, market: Market) -> float:
        """Return low-volume score from market.volume_usd."""
        v = market.volume_usd
        if v < 500:
            return 1.0
        if v < 2_000:
            return 0.7
        if v < 10_000:
            return 0.3
        return 0.0

    def _score_staleness(self, market: Market) -> float:
        """Score pricing staleness.  Higher = more stale = more mispricing potential.

        Combines three signals:
          time_score    (0.50 weight) — how long since updated_time
          spread_score  (0.30 weight) — yes_ask − yes_bid width
          vol_24h_score (0.20 weight) — low 24h volume

        Returns 0.0 if updated_time is missing AND spread data is unavailable.
        """
        # ── Time since last price update ──────────────────────────────────────
        updated_raw = (
            getattr(market, "updated_time", None)
            or (market.raw.get("updated_time") if market.raw else None)
        )
        time_score = 0.0
        if updated_raw is not None:
            try:
                updated_dt = _parse_datetime(updated_raw)
                if updated_dt is not None:
                    if updated_dt.tzinfo is None:
                        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
                    stale_minutes = (
                        datetime.now(timezone.utc) - updated_dt
                    ).total_seconds() / 60.0
                    if stale_minutes > 120:
                        time_score = 1.0
                    elif stale_minutes > 60:
                        time_score = 0.8
                    elif stale_minutes > 30:
                        time_score = 0.5
                    elif stale_minutes > 10:
                        time_score = 0.2
            except Exception:
                pass

        # ── Bid/ask spread ────────────────────────────────────────────────────
        ask = getattr(market, "yes_ask", None)
        bid = getattr(market, "yes_bid", None)
        spread_score = 0.0
        if ask is not None and bid is not None and ask > 0 and bid > 0:
            spread = ask - bid
            if spread > 0.15:
                spread_score = 1.0
            elif spread > 0.08:
                spread_score = 0.6
            elif spread > 0.03:
                spread_score = 0.3

        # ── 24h volume ───────────────────────────────────────────────────────
        vol_24h = getattr(market, "volume_24h", None)
        vol_24h_score = 0.0
        if vol_24h is not None:
            if vol_24h < 100:
                vol_24h_score = 1.0
            elif vol_24h < 500:
                vol_24h_score = 0.5

        # Skip entirely if the two primary signals are both absent
        if time_score == 0.0 and spread_score == 0.0:
            return 0.0

        return time_score * 0.50 + spread_score * 0.30 + vol_24h_score * 0.20

    def _score_bracket_proximity(self, market: Market, price_cache: dict) -> float:
        """Score bracket proximity for a single market using the shared price cache."""
        prefix = _bracket_prefix(market.market_id)
        if prefix is None:
            return 0.0
        strike = _parse_strike(market.market_id)
        if strike is None:
            return 0.0
        series_root = prefix.split("-")[0]
        symbol = _SERIES_TO_SYMBOL.get(series_root)
        if symbol is None:
            return 0.0
        entry = price_cache.get(symbol)
        if entry is None:
            return 0.0
        current_price, fetched_at = entry
        if time.monotonic() - fetched_at > _PRICE_STALENESS_S:
            return 0.0
        return _proximity_score(strike, current_price)

    def _score_all_brackets(
        self,
        bracket_groups: Dict[str, List[Tuple[Market, float]]],
        price_cache: Dict[str, Optional[Tuple[float, float]]],
        bracket_scores: Dict[str, float],
    ) -> None:
        """
        Populate ``bracket_scores`` for all bracket markets in ``bracket_groups``.

        For each series:
          1. Determine the underlying symbol from _SERIES_TO_SYMBOL.
          2. Fetch the current price (once per symbol per batch via price_cache).
          3. Apply the staleness guard — skip whole series on stale data.
          4. Apply the floor rule: if fewer than 3 strikes score ≥ 0.5,
             take the 5 closest strikes and set their score to at least 0.5.
        """
        for prefix, group in bracket_groups.items():
            series_root = prefix.split("-")[0]
            symbol = _SERIES_TO_SYMBOL.get(series_root)

            if symbol is None:
                # Unknown series — skip bracket scoring for all markets in group
                for m, _ in group:
                    bracket_scores[m.market_id] = 0.0
                continue

            # Fetch price exactly once per symbol per batch
            if symbol not in price_cache:
                price_cache[symbol] = self._fetch_price_cached(symbol)

            entry = price_cache[symbol]
            if entry is None:
                for m, _ in group:
                    bracket_scores[m.market_id] = 0.0
                continue

            current_price, fetched_at = entry
            age_s = time.monotonic() - fetched_at

            if age_s > _PRICE_STALENESS_S:
                logger.warning(
                    "[PRIORITY] Skipping bracket proximity for %s: "
                    "price data stale (%.0fs old)",
                    series_root, age_s,
                )
                for m, _ in group:
                    bracket_scores[m.market_id] = 0.0
                continue

            # Score each strike by proximity to current price
            raw_scores: List[Tuple[Market, float, float]] = []  # (market, strike, score)
            for m, strike in group:
                score = _proximity_score(strike, current_price)
                raw_scores.append((m, strike, score))
                bracket_scores[m.market_id] = score

            # Floor rule: ensure at least 3 markets in the series score ≥ 0.5
            above_half = sum(1 for _, _, s in raw_scores if s >= 0.5)
            if above_half < 3:
                # Take up to 5 closest strikes and boost to at least 0.5
                closest = sorted(raw_scores, key=lambda t: abs(t[1] - current_price))
                for m, _, _ in closest[:5]:
                    bracket_scores[m.market_id] = max(bracket_scores[m.market_id], 0.5)

    def _fetch_price_cached(self, symbol: str) -> Optional[Tuple[float, float]]:
        """
        Fetch the current price for ``symbol`` via the financial source.

        Returns
        -------
        (price, fetched_at_monotonic) if successful, else None.

        The fetched_at timestamp is read back from the module-level
        ``_PRICE_CACHE`` after the fetch so the staleness guard uses the same
        clock reference as the cache TTL logic.
        """
        if self._financial_source is None:
            return None
        try:
            price, _ = self._financial_source._fetch_price(symbol)
            if price is None:
                return None
            # Read the timestamp stored by _fetch_price so staleness calculations
            # are consistent with the cache's own TTL tracking.
            from data.ground_truth.financial import _PRICE_CACHE  # noqa: PLC0415
            cached = _PRICE_CACHE.get(symbol)
            if cached:
                fetched_at, _, _ = cached
                return (price, fetched_at)
            # Fallback: use current monotonic time (price was just fetched)
            return (price, time.monotonic())
        except Exception as exc:
            logger.debug("[PRIORITY] _fetch_price_cached(%s) failed: %s", symbol, exc)
            return None
