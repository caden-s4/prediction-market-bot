"""
data.sports.pipeline – top-level coordinator for all Phase 2 sports signals.

Runs staleness, panic, and resolution lag detection each cycle and logs the
total sports signal pipeline timing:

  SportsSignalPipeline: staleness={X}ms panic={X}ms resolution={X}ms total={X}ms

Usage (once per bot cycle, after refresh_if_stale()):
  from data.sports.pipeline import run_sports_pipeline

  results = run_sports_pipeline(
      markets,            # List[Market] — Kalshi sports markets for this cycle
      game_snapshot_fn,  # callable(market_id) → Optional[GameSnapshot]
      model_prob_fn,     # callable(sport, state) → float
      order_book_fn,     # callable(market_id) → Optional[OrderBook]
  )

  results.staleness_signals   # List[StalenessSignal]
  results.panic_signals       # List[PanicSignal]
  results.resolution_signals  # List[ResolutionSignal]  ← highest priority

Callers are responsible for passing only matched, active-game markets.
The pipeline short-circuits on unmatched markets without fetching order books.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from data.markets.base import Market, OrderBook

from .live_game_monitor import GameSnapshot
from .market_matcher import match_market
from .win_probability import compute_win_probability
from .shock_detector import _in_final_period
from .staleness_detector import update_staleness, reset_cycle_timing as staleness_reset
from .panic_detector import check_for_panic, reset_cycle_timing as panic_reset
from .resolution_detector import (
    check_for_resolution_lags,
    reset_cycle_timing as resolution_reset,
    ResolutionSignal,
)
from .staleness_detector import StalenessSignal
from .panic_detector import PanicSignal

logger = logging.getLogger(__name__)

# Minimum model prob to attempt an order book fetch for panic detection
# (saves a round-trip when the game is clearly uncertain)
_PANIC_MIN_PROB_EXTREME = 0.40


@dataclass
class PipelineResult:
    staleness_signals: List[StalenessSignal] = field(default_factory=list)
    panic_signals: List[PanicSignal] = field(default_factory=list)
    resolution_signals: List[ResolutionSignal] = field(default_factory=list)
    staleness_ms: float = 0.0
    panic_ms: float = 0.0
    resolution_ms: float = 0.0
    total_ms: float = 0.0


def run_sports_pipeline(
    markets: List[Market],
    game_snapshot_fn: Callable[[str, str], Optional[GameSnapshot]],
    order_book_fn: Callable[[str], Optional[OrderBook]],
) -> PipelineResult:
    """
    Run all Phase 2 sports signal detectors for the current cycle.

    Parameters
    ----------
    markets
        Kalshi sports markets to evaluate. Can include non-sports markets —
        they will be skipped quickly via MarketMatcher.
    game_snapshot_fn
        Callable(home_team, away_team) → Optional[GameSnapshot].
        Use live_game_monitor.get_active_snapshots() to build a lookup dict
        and pass a dict.get wrapper.
    order_book_fn
        Callable(market_id) → Optional[OrderBook].
        Called only for markets that have a matched game AND a model prob
        outside the uncertain band. Return None to skip panic detection
        for a market (e.g. client not available, rate-limited).

    Returns
    -------
    PipelineResult with all signals and per-stage timing.
    """
    t_total = time.monotonic()

    # Reset per-cycle timing accumulators in each sub-module
    staleness_reset()
    panic_reset()
    resolution_reset()

    result = PipelineResult()

    # ── 1. Resolution lag (highest priority) — drain background thread queue ──
    t_res = time.monotonic()
    result.resolution_signals = check_for_resolution_lags()
    result.resolution_ms = (time.monotonic() - t_res) * 1000

    # ── 2. Per-market staleness and panic ─────────────────────────────────────
    t_stale = time.monotonic()
    t_panic = 0.0

    for market in markets:
        # Match market to a game — fast in-memory cache hit after first call
        sport_hint = _detect_sport_hint(market)
        match = match_market(market.market_id, market.question, sport_hint)
        if match is None:
            continue

        sport = match["sport"]
        snapshot = game_snapshot_fn(match["home_team"], match["away_team"])
        if snapshot is None or snapshot.stale:
            continue

        current_state = snapshot.current_state

        try:
            model_prob = compute_win_probability(sport, current_state)
        except Exception as exc:
            logger.debug("SportsSignalPipeline: prob error for %s: %s", market.market_id, exc)
            continue

        # ── Staleness ────────────────────────────────────────────────────────
        staleness_sig = update_staleness(
            game_id=snapshot.game_id,
            market_id=market.market_id,
            sport=sport,
            home_team=snapshot.home_team,
            away_team=snapshot.away_team,
            current_state=current_state,
            model_prob=model_prob,
            market_price=market.yes_price,
        )
        if staleness_sig is not None:
            result.staleness_signals.append(staleness_sig)

        # ── Panic (order book fetch — only for extreme model probs) ──────────
        model_extreme = (
            model_prob < (1.0 - _PANIC_MIN_PROB_EXTREME)
            or model_prob > _PANIC_MIN_PROB_EXTREME
        )
        if not model_extreme:
            continue

        t_panic_start = time.monotonic()
        try:
            order_book = order_book_fn(market.market_id)
        except Exception as exc:
            logger.debug(
                "SportsSignalPipeline: order book fetch error for %s: %s",
                market.market_id, exc,
            )
            order_book = None
        t_panic += (time.monotonic() - t_panic_start) * 1000

        if order_book is None:
            continue

        panic_sig = check_for_panic(
            market_id=market.market_id,
            order_book=order_book,
            model_prob=model_prob,
            game_id=snapshot.game_id,
            sport=sport,
            home_team=snapshot.home_team,
            away_team=snapshot.away_team,
        )
        if panic_sig is not None:
            result.panic_signals.append(panic_sig)

    result.staleness_ms = (time.monotonic() - t_stale) * 1000
    result.panic_ms = t_panic  # just the order book fetch time; check_for_panic time is in panic_reset()
    result.total_ms = (time.monotonic() - t_total) * 1000

    logger.info(
        "SportsSignalPipeline: staleness=%.0fms panic=%.0fms "
        "resolution=%.0fms total=%.0fms | "
        "stale=%d panic=%d resolution=%d",
        result.staleness_ms, result.panic_ms,
        result.resolution_ms, result.total_ms,
        len(result.staleness_signals),
        len(result.panic_signals),
        len(result.resolution_signals),
    )

    return result


def _detect_sport_hint(market: Market) -> Optional[str]:
    """Quick sport classification from market text."""
    text = (
        market.question + " " + " ".join(market.tags) + " " + market.market_id
    ).lower()
    if "nfl" in text or "americanfootball" in text:
        return "nfl"
    if "ncaab" in text or "college basketball" in text or "ncaa basketball" in text:
        return "ncaab"
    if "nba" in text or "basketball" in text:
        return "nba"
    return None
