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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

from data.markets.base import BaseMarketClient, Market
from data.markets.kalshi import _GAME_SERIES_PREFIXES, _WEATHER_SERIES_TICKERS
from data.markets.kalshi_ws import KalshiWebSocket
from monitoring import gate_names as gn
from monitoring.gate_events import log_gate_event
from resolution.priority import PriorityScorer
from shared.exclusion_list import ExclusionList
from strategies.weather_snipe import SnipeSignal, evaluate_snipe
from strategies.weather_peak_snipe import (
    WeatherPeakSnipeSignal,
    evaluate_event_signals as evaluate_peak_snipe_event_signals,
    group_markets_by_event as group_peak_snipe_markets,
    is_peak_snipe_candidate,
)

SnipeCallback = Callable[[Market, SnipeSignal], Optional[str]]

logger = logging.getLogger(__name__)


def _check_ws_rest_agreement(ws_mid: float, rest_mid: float, ticker: str) -> None:
    """Log a gate event if WS and REST mids disagree by more than 0.05."""
    if ws_mid is None or rest_mid is None:
        return
    if abs(ws_mid - rest_mid) > 0.05:
        log_gate_event(
            ticker=ticker,
            gate="invariant_violation",
            decision="ws_rest_mid_disagreement",
            extra={"ws_mid": ws_mid, "rest_mid": rest_mid, "delta": round(ws_mid - rest_mid, 4)},
        )

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

# Parallelism for refresh_markets(): one worker per market handles the
# get_market() + get_order_book() pair.  Kalshi's REST client has 429 retry
# with exponential backoff (see data/markets/kalshi.py), but at 8 workers the
# order-book endpoint produced ~16 429 warnings per cycle.  4 workers is the
# empirically safe value — still an order of magnitude faster than sequential,
# without triggering the rate limiter.
TIER_REFRESH_MAX_WORKERS = 4

# Coarse pre-filter window for the weather snipe dispatch hook.
# The strategy module re-validates the exact window — this is just a fast
# rejection so we don't construct a now/delta for every market.
_WEATHER_SNIPE_WINDOW = timedelta(minutes=60)

# Shadow window: markets 60-240 min from close are evaluated but never
# dispatched to the executor. Used to diagnose whether widening the live
# window would produce more actionable signals.
_WEATHER_SNIPE_SHADOW_WINDOW = timedelta(minutes=240)

# Process-lifetime counters for TUI state snapshot.
_SNIPES_ATTEMPTED: int = 0   # real-window candidates evaluated (0-60 min)
_SHADOW_SIGNALS: int = 0     # shadow-window evaluations that produced a signal

# ── Financial bracket disable switch ──────────────────────────────────────────
# Yahoo Finance quote_ts staleness blocks 100% of financial bracket signals at
# the executor freshness gate (gt_age > 60s threshold). Set False after routing
# to a real-time source (Twelve Data paid tier).
# DO NOT add KXBRENTD/KXBRENTW — wrong GT source regardless of freshness.
DISABLE_FINANCIAL_BRACKETS: bool = True
_FINANCIAL_BRACKET_PREFIXES: tuple = (
    "KXNASDAQ100U",  # before KXNASDAQ100 — longer prefix wins startswith
    "KXNASDAQ100",
    "KXGOLDD",
    "KXGOLDW",       # GC=F (Gold weekly) — Yahoo, TD free tier blocks GC/GC1!
    "KXTNOTED",
    "KXTNOTEW",      # ^TNX (10-yr Treasury weekly) — Yahoo, TD free tier blocks TNX
    "KXINX",         # ES=F (S&P 500) — Yahoo, TD free tier blocks SPX
    "KXWTI",         # CL=F (WTI Crude) — Yahoo, TD blocks CL1!. Matches daily KXWTI-* and weekly KXWTIW-*
)

# ── Legacy weather_snipe disable switch (Phase 15e) ───────────────────────────
# 27 trades over Phase 15b + 15b-bis settled at 1W-26L / -$1,521.19. Paired-bet
# logic fires near-max premium on both sides of a bracket, guaranteeing losses
# on most resolutions. No edge thesis intact. Disabled at scanner dispatch.
# Does NOT affect Phase 14b weather_peak_snipe (separate dispatch path).
DISABLE_LEGACY_WEATHER_SNIPE: bool = True


def get_snipe_stats() -> tuple:
    """Return (snipes_attempted, shadow_signals) cumulative since process start."""
    return _SNIPES_ATTEMPTED, _SHADOW_SIGNALS


def _is_weather_snipe_candidate(market: Market) -> bool:
    """Return True if `market` is a Kalshi weather market in its final 60 min.

    Defensive: handles missing/empty market_id and missing resolution_date,
    since the dispatch fires on every market in the kalshi fetch including
    rejected ones.
    """
    mid = getattr(market, "market_id", None)
    if not mid:
        return False
    rd = getattr(market, "resolution_date", None)
    if rd is None:
        return False
    if rd.tzinfo is None:
        rd = rd.replace(tzinfo=timezone.utc)
    delta = rd - datetime.now(timezone.utc)
    if delta <= timedelta(0):
        return False
    if delta > _WEATHER_SNIPE_WINDOW:
        return False
    return any(mid.startswith(p) for p in _WEATHER_SERIES_TICKERS)


def _is_weather_shadow_candidate(market: Market) -> bool:
    """Return True if `market` is a weather market 60-240 min from close.

    Mutually exclusive with _is_weather_snipe_candidate (which covers 0-60 min).
    Defensive: same missing-field handling as the real candidate check.
    """
    mid = getattr(market, "market_id", None)
    if not mid:
        return False
    rd = getattr(market, "resolution_date", None)
    if rd is None:
        return False
    if rd.tzinfo is None:
        rd = rd.replace(tzinfo=timezone.utc)
    delta = rd - datetime.now(timezone.utc)
    if delta <= _WEATHER_SNIPE_WINDOW:
        return False
    if delta > _WEATHER_SNIPE_SHADOW_WINDOW:
        return False
    return any(mid.startswith(p) for p in _WEATHER_SERIES_TICKERS)


def _dispatch_weather_peak_snipe_batch(
    candidates: List[Market],
    snipe_callback: Optional[SnipeCallback] = None,
) -> None:
    """Phase 14b weather peak-snipe batch dispatch (event-driven).

    Unlike the per-market `_dispatch_weather_snipe`, this strategy fires once
    per (series, event_date) per cycle: it groups candidate markets, evaluates
    the post-peak monotonic-trend trigger per group, and emits up to 5 signals
    per event (winner YES + ±2 adjacent NO) which the existing snipe placement
    callback handles.

    Hard-coded ``dry_run=True`` for Phase 14b v1 — the strategy is ghost-only
    until the per-signal-class PnL gate (pending) is in place. The executor
    has a defense-in-depth guard for the same.

    Any exception per event group is logged with traceback and swallowed; this
    hook must never crash the scan loop.
    """
    if not candidates:
        return
    groups = group_peak_snipe_markets(candidates)
    if not groups:
        return
    logger.info(
        "WeatherPeakSnipe: evaluating %d event group(s) (candidates=%d)",
        len(groups), len(candidates),
    )
    for event_id, event_markets in groups.items():
        try:
            signals = evaluate_peak_snipe_event_signals(
                event_markets,
                now_utc=datetime.now(timezone.utc),
                dry_run=True,
            )
        except Exception:
            logger.exception(
                "WeatherPeakSnipe batch eval failed for %s", event_id,
            )
            continue
        if not signals:
            continue
        if snipe_callback is None:
            for sig in signals:
                logger.info(
                    "WeatherPeakSnipe candidate (no callback): %s -> %s",
                    sig.market_id, sig,
                )
            continue
        # Build a fast lookup so we can pass the right Market into the
        # callback per signal — the strategy returns signals keyed by
        # market_id only.
        by_id = {m.market_id: m for m in event_markets}
        for sig in signals:
            market = by_id.get(sig.market_id)
            if market is None:
                logger.warning(
                    "WeatherPeakSnipe: signal market_id %s not in event group %s",
                    sig.market_id, event_id,
                )
                continue
            try:
                order_id = snipe_callback(market, sig)
            except Exception:
                logger.exception(
                    "WeatherPeakSnipe placement failed for %s", sig.market_id,
                )
                continue
            if order_id is not None:
                logger.info(
                    "WeatherPeakSnipe trade placed: %s order=%s",
                    sig.market_id, order_id,
                )


def _dispatch_weather_snipe(
    market: Market,
    snipe_callback: Optional[SnipeCallback] = None,
) -> None:
    """Per-market snipe dispatch hook.

    Fires AFTER standard market processing. Snipes are additive — they do
    not replace standard signals. Any exception from the strategy or the
    placement callback is logged with traceback and swallowed; this hook
    must never crash the scan loop.

    Real path (0-60 min): evaluates and dispatches to the executor callback.
    If ``snipe_callback`` is None, logs the SnipeSignal without dispatching
    (useful for tests and dry inspection).

    Shadow path (60-240 min): evaluates but never dispatches — logs only,
    with SHADOW_ prefixed lines. Used for window-tuning diagnostics.
    """
    if _is_weather_snipe_candidate(market):
        global _SNIPES_ATTEMPTED
        _SNIPES_ATTEMPTED += 1
        try:
            signal = evaluate_snipe(market, datetime.now(timezone.utc))
        except Exception:
            logger.exception(
                "WeatherSnipe dispatch failed for %s", market.market_id,
            )
            return
        if signal is None:
            return
        if snipe_callback is None:
            logger.info(
                "WeatherSnipe candidate: %s -> %s", market.market_id, signal,
            )
            return
        try:
            order_id = snipe_callback(market, signal)
        except Exception:
            logger.exception(
                "WeatherSnipe placement failed for %s", market.market_id,
            )
            return
        if order_id is not None:
            logger.info(
                "WeatherSnipe trade placed: %s order=%s",
                market.market_id, order_id,
            )
    elif _is_weather_shadow_candidate(market):
        now_utc = datetime.now(timezone.utc)
        rd = market.resolution_date
        if rd.tzinfo is None:
            rd = rd.replace(tzinfo=timezone.utc)
        minutes_to_close = int((rd - now_utc).total_seconds() / 60)
        mid = market.market_id
        logger.info(
            "ResolutionBot: SHADOW_CANDIDATE %s — minutes_to_close=%d",
            mid, minutes_to_close,
        )
        try:
            signal = evaluate_snipe(market, now_utc, shadow_mode=True)
        except Exception:
            logger.exception(
                "WeatherSnipe shadow dispatch failed for %s", mid,
            )
            return
        if signal is None:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="reject",
                reason=gn.REASON_NO_SIGNAL,
                platform="kalshi",
                extra={"shadow": True, "minutes_to_close": minutes_to_close},
            )
            logger.info(
                "ResolutionBot: SHADOW_REJECT %s reject_reason=no_signal minutes_to_close=%d",
                mid, minutes_to_close,
            )
            return
        global _SHADOW_SIGNALS
        _SHADOW_SIGNALS += 1
        _br = (
            f"[{signal.bracket_low:.2f},{signal.bracket_high:.2f}]"
            if signal.bracket_low is not None
            else "N/A"
        )
        _tmp = f"{signal.asos_temp_f:.1f}" if signal.asos_temp_f is not None else "N/A"
        _mm = f"{signal.market_mid:.4f}" if signal.market_mid is not None else "N/A"
        logger.info(
            "ResolutionBot: SHADOW_SIGNAL %s action=%s target_price=%.4f "
            "edge=%.4f gt_prob=%.4f bracket=%s asos_temp_f=%s "
            "market_mid=%s minutes_to_close=%d",
            mid, signal.action, signal.target_price, signal.edge,
            signal.gt_prob, _br, _tmp, _mm, minutes_to_close,
        )


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
    kalshi_ws        : KalshiWebSocket instance for orderbook fast path (optional)
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
        priority_scorer: Optional[PriorityScorer] = None,
        snipe_callback: Optional[SnipeCallback] = None,
        kalshi_ws: Optional[KalshiWebSocket] = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._exclusions = exclusions
        self._kalshi_window = kalshi_window_hours if kalshi_window_hours is not None else window_hours
        self._poly_window = poly_window_hours if poly_window_hours is not None else window_hours
        self._max = max_per_platform
        self._priority_scorer = priority_scorer
        self._snipe_callback = snipe_callback
        self._kalshi_ws: Optional[KalshiWebSocket] = kalshi_ws

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

        # Priority scoring: attach priority_score to each market before tier ingest.
        # score_batch() returns markets sorted by priority_score (highest first).
        # Tier assignment remains purely time-based; priority only controls scan
        # ORDER within each tier.
        if self._priority_scorer is not None:
            markets = self._priority_scorer.score_batch(markets)

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

    def refresh_markets(self, markets: List[Market]) -> List[Market]:
        """
        Re-fetch current data for a set of already-known markets.

        Uses the per-market ``get_market(market_id)`` endpoint where the
        platform client supports it, then overlays the live order book mid_price
        to correct stale yes_price fields on illiquid markets.

        The Kalshi bulk /markets endpoint often returns cached yes_bid/yes_ask
        (e.g. 49/50 by default) that don't reflect true market liquidity.
        get_order_book() provides the true bid/ask, so we update yes_price to
        its mid_price before returning.

        If the client returns ``None`` (endpoint not implemented or market not
        found) the last-known ``Market`` object is kept unchanged so callers
        always get a full list back.

        Fails silently per market: a single failed refresh only loses freshness
        for that market, not the whole batch.  The executor's ``_try_execute``
        always re-validates prices from the live order book before placement, so
        slightly stale prices here only affect the initial gap-detection pass.

        Returns a list the same length as ``markets``.

        Per-market refreshes run in parallel via a ThreadPoolExecutor so a
        large tier batch (hundreds of markets × 2 API calls each) doesn't
        dominate cycle time.  Input ordering is preserved in the output.
        """
        if not markets:
            return []

        _ws_hits = [0]
        _rest_fallbacks = [0]
        _counter_lock = threading.Lock()

        def _refresh_one(market: Market) -> Market:
            client = self._kalshi if market.platform == "kalshi" else self._poly
            if client is None:
                return market
            try:
                fresh = client.get_market(market.market_id)
                if fresh is None:
                    return market

                # Orderbook resolution: WS cache first (Kalshi only), REST fallback.
                ws_book = None
                if self._kalshi_ws is not None and market.platform == "kalshi":
                    ws_age = self._kalshi_ws.get_book_age(market.market_id)
                    if ws_age is not None and ws_age < 30.0:
                        ws_book = self._kalshi_ws.get_book(market.market_id)

                if ws_book is not None and ws_book.mid_price is not None:
                    _check_ws_rest_agreement(ws_book.mid_price, fresh.yes_price, market.market_id)
                    fresh.yes_price = ws_book.mid_price
                    with _counter_lock:
                        _ws_hits[0] += 1
                else:
                    try:
                        ob = client.get_order_book(market.market_id)
                        if ob is not None and ob.mid_price is not None:
                            fresh.yes_price = ob.mid_price
                    except Exception:
                        # Order book fetch failed; keep fresh.yes_price from get_market().
                        pass
                    if market.platform == "kalshi":
                        with _counter_lock:
                            _rest_fallbacks[0] += 1

                return fresh
            except Exception as exc:
                logger.debug(
                    "ResolutionScanner: refresh failed for %s/%s: %s",
                    market.platform, market.market_id, exc,
                )
                return market

        # Fast path: single market or single-worker config → sequential with
        # the historical stagger (keeps behaviour identical when parallelism
        # is disabled for debugging).
        if TIER_REFRESH_MAX_WORKERS <= 1 or len(markets) == 1:
            result = [_refresh_one(m) for m in markets]
            logger.info(
                "ResolutionScanner: refresh book sources: ws=%d rest=%d total=%d",
                _ws_hits[0], _rest_fallbacks[0], _ws_hits[0] + _rest_fallbacks[0],
            )
            return result

        # Parallel path: preserve input ordering by indexing futures.
        refreshed: List[Market] = [markets[0]] * len(markets)  # placeholder
        workers = min(TIER_REFRESH_MAX_WORKERS, len(markets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_idx = {
                pool.submit(_refresh_one, m): i for i, m in enumerate(markets)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    refreshed[idx] = future.result()
                except Exception as exc:
                    # Belt-and-suspenders: _refresh_one catches its own errors,
                    # but if the worker itself raises, fall back to the original
                    # market object so the batch length and ordering stay intact.
                    logger.debug(
                        "ResolutionScanner: refresh worker crashed for %s/%s: %s",
                        markets[idx].platform, markets[idx].market_id, exc,
                    )
                    refreshed[idx] = markets[idx]
        logger.info(
            "ResolutionScanner: refresh book sources: ws=%d rest=%d total=%d",
            _ws_hits[0], _rest_fallbacks[0], _ws_hits[0] + _rest_fallbacks[0],
        )
        return refreshed

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scan_platform(
        self, client: BaseMarketClient, platform_name: str, window_hours: float
    ) -> List[Market]:
        results: List[Market] = []
        seen: set = set()
        rejected_reasons: dict = {"excluded": 0, "category": 0, "hours": 0, "price": 0}
        # Phase 14b weather peak-snipe candidate accumulator. Filled during
        # the kalshi loop; flushed via batch dispatcher at the end.
        peak_snipe_candidates: List[Market] = []

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
                        log_gate_event(
                            ticker=m.market_id,
                            gate=gn.GATE_SCANNER_REJECT,
                            decision="reject",
                            reason=reason,
                            platform=m.platform,
                        )
                        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    elif m.market_id not in seen:
                        seen.add(m.market_id)
                        results.append(m)
                    # Weather snipe dispatch fires AFTER standard processing.
                    # Weather markets are filtered out by EXCLUDED_CATEGORIES,
                    # so they appear here as reason="category" — the snipe
                    # strategy intentionally bypasses that exclusion.
                    # Phase 15e: legacy weather_snipe disabled. Emit a
                    # scanner_reject event only for markets that would have
                    # actually entered the dispatch (real or shadow window),
                    # to keep the funnel signal-bearing.
                    if DISABLE_LEGACY_WEATHER_SNIPE:
                        if (
                            _is_weather_snipe_candidate(m)
                            or _is_weather_shadow_candidate(m)
                        ):
                            log_gate_event(
                                ticker=m.market_id,
                                gate=gn.GATE_SCANNER_REJECT,
                                decision="reject",
                                reason=gn.REASON_LEGACY_WEATHER_SNIPE_DISABLED,
                                platform=m.platform,
                            )
                    else:
                        _dispatch_weather_snipe(m, self._snipe_callback)
                    if is_peak_snipe_candidate(m.market_id):
                        peak_snipe_candidates.append(m)
            except Exception as exc:
                logger.warning(
                    "ResolutionScanner: failed fetching kalshi markets: %s", exc
                )

            # Sports supplement: query sports series tickers directly.
            # Same-day game markets (NBA tonight, NFL Sunday) are short-lived
            # and may be buried behind long-dated markets in the general fetch.
            # SportsDataSource needs these to generate GT signals.
            if hasattr(client, "get_sports_markets"):
                try:
                    sports_markets = client.get_sports_markets()
                    added = 0
                    for m in sports_markets:
                        if m.market_id not in seen:
                            reason = self._reject_reason(m, window_hours)
                            if reason:
                                log_gate_event(
                                    ticker=m.market_id,
                                    gate=gn.GATE_SCANNER_REJECT,
                                    decision="reject",
                                    reason=reason,
                                    platform=m.platform,
                                )
                                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                            else:
                                seen.add(m.market_id)
                                results.append(m)
                                added += 1
                    if added:
                        logger.info(
                            "ResolutionScanner: kalshi sports supplement → %d additional markets",
                            added,
                        )
                except Exception as exc:
                    logger.debug(
                        "ResolutionScanner: kalshi sports supplement failed: %s", exc
                    )

            # Financial bracket supplement: query bracket series tickers directly.
            # Financial bracket markets (KXNASDAQ100, KXWTI, KXGOLD, etc.) are not
            # returned by the default paginated fetch — they must be queried via
            # series_ticker parameter. This ensures bracket markets for FRED arbitrage
            # are discovered fresh each cycle.
            if hasattr(client, "get_financial_bracket_markets"):
                try:
                    bracket_markets = client.get_financial_bracket_markets()
                    added = 0
                    for m in bracket_markets:
                        if m.market_id not in seen:
                            reason = self._reject_reason(m, window_hours)
                            if reason:
                                log_gate_event(
                                    ticker=m.market_id,
                                    gate=gn.GATE_SCANNER_REJECT,
                                    decision="reject",
                                    reason=reason,
                                    platform=m.platform,
                                )
                                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                            else:
                                seen.add(m.market_id)
                                results.append(m)
                                added += 1
                    if added:
                        logger.info(
                            "ResolutionScanner: kalshi financial bracket supplement → %d additional markets",
                            added,
                        )
                except Exception as exc:
                    logger.debug(
                        "ResolutionScanner: kalshi financial bracket supplement failed: %s", exc
                    )

            # Phase 14b weather peak-snipe batch dispatch. Runs once per cycle
            # after all kalshi markets are discovered. Ghost-only by spec.
            try:
                _dispatch_weather_peak_snipe_batch(
                    peak_snipe_candidates, self._snipe_callback,
                )
            except Exception:
                logger.exception(
                    "WeatherPeakSnipe batch dispatch failed (top-level guard)",
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
                            log_gate_event(
                                ticker=m.market_id,
                                gate=gn.GATE_SCANNER_REJECT,
                                decision="reject",
                                reason=reason,
                                platform=m.platform,
                            )
                            rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                except Exception as exc:
                    logger.warning(
                        "ResolutionScanner: failed fetching %s/%s: %s",
                        platform_name, category, exc,
                    )

            # Near-term sweep: fetch same-day Polymarket markets (expiring in ≤48h)
            # for every category.  The Gamma API default sort buries short-dated markets
            # behind high-volume long-dated ones, so a targeted end_date_max query is
            # the only reliable way to surface today's NBA games, economic releases, etc.
            if hasattr(client, "get_markets"):
                now = datetime.now(timezone.utc)
                end_max = (now + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
                added = 0
                for category in SCAN_CATEGORIES:
                    try:
                        near_markets = client.get_markets(
                            category=category,
                            limit=100,
                            end_date_max=end_max,
                        )
                        for m in near_markets:
                            if m.market_id not in seen:
                                reason = self._reject_reason(m, window_hours)
                                if not reason:
                                    seen.add(m.market_id)
                                    results.append(m)
                                    added += 1
                                elif reason:
                                    log_gate_event(
                                        ticker=m.market_id,
                                        gate=gn.GATE_SCANNER_REJECT,
                                        decision="reject",
                                        reason=reason,
                                        platform=m.platform,
                                    )
                                    rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    except Exception as exc:
                        logger.debug(
                            "ResolutionScanner: near-term sweep failed %s/%s: %s",
                            platform_name, category, exc,
                        )
                if added:
                    logger.info(
                        "ResolutionScanner: polymarket near-term sweep (≤48h) → %d additional markets",
                        added,
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
        if DISABLE_FINANCIAL_BRACKETS and any(
            market.market_id.startswith(p) for p in _FINANCIAL_BRACKET_PREFIXES
        ):
            return "financial_bracket_disabled"
        if market.category.lower() in EXCLUDED_CATEGORIES or market.is_weather_market():
            return "category"
        hours_left = market.hours_to_resolution   # uses fixed timezone-aware property

        # Extend scan window for markets with external resolution sources:
        # - Game markets (48h): same-day, quick turnaround
        # - Financial brackets (72h): daily/weekly, need longer scan window for weekend discovery
        #   (e.g. Monday 4pm brackets discovered Saturday afternoon are ~48h away)
        # When DISABLE_FINANCIAL_BRACKETS is True, bracket markets are caught by the early
        # return above and never reach this branch.
        # KXBRENTD/KXBRENTW: DO NOT add to _FINANCIAL_BRACKET_PREFIXES — wrong GT source.
        _GAME_SERIES_PREFIXES = ("KXNBAGAME", "KXNCAAMBGAME", "KXNFLGAME", "KXNCAAWBGAME")

        if any(market.market_id.startswith(p) for p in _GAME_SERIES_PREFIXES):
            effective_window = 48.0
        elif any(market.market_id.startswith(p) for p in _FINANCIAL_BRACKET_PREFIXES):
            effective_window = 72.0
        else:
            effective_window = window_hours

        if not (0 < hours_left <= effective_window):
            return "hours"
        # Only exclude markets that are literally fully resolved (price at 0 or 1).
        # Near-certain prices (e.g. YES=0.93) are the core of the resolution-drift
        # strategy: the real-world outcome is known but the market hasn't caught up.
        # The gap detector's MIN_GAP_THRESHOLD enforces the minimum tradeable edge.
        if not (0.0 < market.yes_price < 1.0):
            return "price"
        return None

    def _is_candidate(self, market: Market, window_hours: float) -> bool:
        return self._reject_reason(market, window_hours) is None

    # ── Word-overlap helpers ──────────────────────────────────────────────────

    _STOP_WORDS: frozenset = frozenset({
        # Articles, prepositions, linking verbs (original)
        "will", "the", "a", "an", "be", "is", "are", "was",
        "by", "in", "on", "at", "to", "of", "for", "and", "or",
        "yes", "no", "this", "that", "before", "after",
        # Auxiliary and question verbs
        "would", "does", "did", "has", "have", "been", "any",
        # Month names — present in almost every market title, zero entity signal
        "january", "february", "march", "april", "june", "july",
        "august", "september", "october", "november", "december",
        # Calendar years — same reason
        "2024", "2025", "2026", "2027",
        # Generic temporal qualifiers
        "until", "during", "start", "beginning",
    })

    # Words that must appear in the overlap set for a pair to be considered
    # the same event.  Pure date / temporal matches with no entity word score 0.
    _ENTITY_WORDS: frozenset = frozenset({
        # ── Named people (politics) ───────────────────────────────────────────
        "trump", "biden", "harris", "obama", "clinton", "pence",
        "putin", "zelensky", "netanyahu", "modi", "macron", "scholz",
        "musk", "elon",
        # ── US political institutions / roles ─────────────────────────────────
        "congress", "senate", "house", "supreme", "court", "president",
        "democrat", "democratic", "republican", "electoral", "inauguration",
        "cabinet",
        # ── Countries and regions ─────────────────────────────────────────────
        "china", "russia", "ukraine", "iran", "israel", "india", "france",
        "germany", "britain", "japan", "korea", "taiwan", "mexico", "canada",
        "europe", "nato", "european", "union", "saudi", "arabia", "pakistan",
        "turkey", "brazil", "australia",
        # ── Economic indicators and institutions ──────────────────────────────
        "federal", "reserve", "fomc", "inflation", "recession",
        "unemployment", "payrolls", "nonfarm",
        # ── Financial instruments / markets ───────────────────────────────────
        "nasdaq", "bitcoin", "ethereum", "gold", "silver", "crude",
        "treasury", "dollar", "euro", "pound",
        # ── Major companies / brands ──────────────────────────────────────────
        "apple", "microsoft", "nvidia", "tesla", "amazon", "google",
        "meta", "openai", "anthropic", "spacex",
        # ── Sports events and teams ───────────────────────────────────────────
        "superbowl", "championship", "playoffs", "worldcup", "wimbledon",
        "lakers", "celtics", "cowboys", "patriots", "chiefs", "eagles",
        "yankees", "dodgers", "warriors", "knicks", "lebron", "mahomes",
        # ── Legal / regulatory ────────────────────────────────────────────────
        "antitrust", "verdict", "conviction", "indictment",
    })

    @staticmethod
    def _sig_words(text: str) -> set:
        """Return the set of significant (non-stop, length > 3) words from text."""
        stop = ResolutionScanner._STOP_WORDS
        return {
            w.lower() for w in text.split()
            if len(w) > 3 and w.lower() not in stop
        }

    @staticmethod
    def _has_entity_overlap(common_words: set) -> bool:
        """Return True if at least one word in common_words is a recognised entity."""
        return bool(common_words & ResolutionScanner._ENTITY_WORDS)

    @staticmethod
    def _same_event(poly: Market, kalshi: Market) -> bool:
        """
        Heuristic to detect if two markets (different platforms) describe the
        same real-world event.  Three criteria, evaluated in order:

        1. Time delta <= 6h    (hard gate, checked first — cheap)
        2. 3+ significant word overlap
        3. At least one overlapping word is a recognised entity (named person,
           organisation, country, team, or economic indicator).  Pure
           date/temporal overlap with no entity word scores 0.
        """
        # ── 1. Time gate (fast, no string work) ───────────────────────────────
        try:
            dt = abs((poly.resolution_date - kalshi.resolution_date).total_seconds())
            if dt > 6 * 3600:
                return False
        except Exception:
            return False

        # ── 2. Word overlap ───────────────────────────────────────────────────
        p_words = ResolutionScanner._sig_words(poly.question)
        k_words = ResolutionScanner._sig_words(kalshi.question)
        common  = p_words & k_words
        if len(common) < 3:
            return False

        # ── 3. Entity requirement ─────────────────────────────────────────────
        return ResolutionScanner._has_entity_overlap(common)

    def score_near_miss_pairs(
        self, markets: List[Market], top_n: int = 10
    ) -> Tuple[List[dict], dict]:
        """
        Score all (poly × kalshi) combinations within the 6h time window and
        return ``(results[:top_n], stats)``.

        The time delta is checked first (hard gate, same order as _same_event)
        so pairs 700h apart are never even text-compared.  A "near miss" is a
        within-window pair with at least 1 overlapping significant word that
        did NOT satisfy both the word-count (>=3) AND entity requirements.
        Fully-matched pairs are excluded.

        Each result dict contains:
          overlap_count          – number of shared significant words
          overlap_words          – sorted list of those words
          entity_words           – subset of overlap_words that are entities
          has_entity_overlap     – True if at least one entity word matched
          time_delta_hours       – absolute difference in resolution times
          would_match_on_words   – True if overlap >= 3
          would_match_on_entity  – True if has_entity_overlap
          poly_question / poly_market_id / poly_hours_left / poly_yes_price
          kalshi_question / kalshi_market_id / kalshi_hours_left / kalshi_yes_price
          price_gap              – |poly_yes - kalshi_yes|

        The stats dict contains:
          poly_count        – number of Polymarket markets in the registry
          kalshi_count      – number of Kalshi markets in the registry
          within_window     – pairs that passed the Δt ≤ 6h gate
          with_word_overlap – within-window pairs with ≥1 overlapping word

        Sorted by overlap_count desc, entity overlap (present > absent), then
        time_delta_hours asc.
        """
        poly_markets   = [m for m in markets if m.platform == "polymarket"]
        kalshi_markets = [m for m in markets if m.platform == "kalshi"]

        within_window    = 0
        with_word_overlap = 0
        results: List[dict] = []

        for pm in poly_markets:
            pw = self._sig_words(pm.question)
            for km in kalshi_markets:
                # ── 1. Time gate (pre-filter — no text work for distant pairs) ─
                try:
                    dt_hours = abs(
                        (pm.resolution_date - km.resolution_date).total_seconds()
                    ) / 3600
                except Exception:
                    continue  # unparseable dates — skip
                if dt_hours > 6.0:
                    continue
                within_window += 1

                # ── 2. Word overlap ────────────────────────────────────────────
                kw     = self._sig_words(km.question)
                common = pw & kw
                if not common:
                    continue
                with_word_overlap += 1

                # ── 3. Entity overlap ──────────────────────────────────────────
                entity_words = sorted(common & self._ENTITY_WORDS)
                has_entity   = bool(entity_words)
                words_ok     = len(common) >= 3

                if words_ok and has_entity:
                    continue  # full match — not a near-miss

                results.append({
                    "overlap_count":         len(common),
                    "overlap_words":         sorted(common),
                    "entity_words":          entity_words,
                    "has_entity_overlap":    has_entity,
                    "time_delta_hours":      round(dt_hours, 1),
                    "would_match_on_words":  words_ok,
                    "would_match_on_entity": has_entity,
                    "poly_question":         pm.question,
                    "poly_market_id":        pm.market_id,
                    "poly_hours_left":       round(pm.hours_to_resolution, 1),
                    "poly_yes_price":        pm.yes_price,
                    "kalshi_question":       km.question,
                    "kalshi_market_id":      km.market_id,
                    "kalshi_hours_left":     round(km.hours_to_resolution, 1),
                    "kalshi_yes_price":      km.yes_price,
                    "price_gap":             round(abs(pm.yes_price - km.yes_price), 4),
                })

        # Best near-misses first: most overlapping words; entity overlap floats
        # above non-entity; ties broken by smallest time delta.
        results.sort(key=lambda r: (
            -r["overlap_count"],
            -int(r["has_entity_overlap"]),
            r["time_delta_hours"],
        ))

        stats = {
            "poly_count":       len(poly_markets),
            "kalshi_count":     len(kalshi_markets),
            "within_window":    within_window,
            "with_word_overlap": with_word_overlap,
        }
        logger.info(
            "ResolutionScanner: near-miss analysis: %d poly × %d kalshi, "
            "%d within-window, %d with word overlap, %d near-misses (top %d returned)",
            len(poly_markets), len(kalshi_markets),
            within_window, with_word_overlap, len(results), top_n,
        )
        return results[:top_n], stats
