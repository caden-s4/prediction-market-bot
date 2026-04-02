"""
data.sports.resolution_detector – fires high-priority signals when a game is
confirmed final on ESPN but the Kalshi market is still trading away from the
correct resolution price.

This is a latency-arbitrage play on the gap between ESPN publishing a final
score and Kalshi processing the resolution.  The window is typically seconds
to a few minutes.

Architecture
------------
The detector watches LiveGameMonitor for games that transition to
"confirmed final" (two consecutive ESPN 'post' readings, ~30s apart).
When a new confirmed final is detected, it spawns a background thread that:
  1. Immediately re-fetches the Kalshi market price (out-of-cycle HTTP request)
  2. Checks if the price is > 3¢ away from correct resolution (1.0 or 0.0)
  3. If yes, queues a ResolutionSignal with confidence=0.99
  4. Logs the resolution_lag_ms (time from ESPN final to now)
  5. Updates the per-sport latency histogram

The background thread puts completed signals into a thread-safe queue.
The main cycle drains this queue each cycle via check_for_resolution_lags().

Initialization
--------------
Call initialize() once at bot startup with two callables:
  market_id_resolver(home_team, away_team, sport) → Optional[str]
      Finds the Kalshi market ID for a given game.
  market_price_fetcher(market_id) → Optional[float]
      Fetches the current YES ask price for a Kalshi market.

These are provided by the main bot rather than imported directly so the
detector stays decoupled from any specific market client implementation.

Per-sport latency histogram
---------------------------
get_latency_histogram() returns a dict of sport → [lag_ms, ...].
Call log_latency_summary() to emit a human-readable histogram to the log.
This data drives the ghost-mode validation checklist item:
  "Resolution lag window has been measured for at least 10 completed games"

Per-cycle timing
----------------
_cycle_elapsed_ms tracks time spent on the main-thread work only (draining
the queue and launching background tasks).  Background HTTP time is excluded
because it happens off-thread.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .live_game_monitor import CompletedGame, get_newly_confirmed_finals

logger = logging.getLogger(__name__)

# Persistence file for the dispatched set — survives restarts
_DISPATCHED_FILE = "dispatched_finals.json"

# Gap threshold — do not fire if market is already within 3¢ of resolution
_RESOLUTION_THRESHOLD = 0.03

# Confidence is always 0.99 for a confirmed final
_CONFIDENCE = 0.99

# Background thread pool — max 2 concurrent Kalshi re-fetches
_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="resdet"
)

# Thread-safe queue of ready signals
_signal_queue: queue.Queue = queue.Queue()

# Thread-safe queue of (market_id, correct_prob) for ghost position exits.
# Populated synchronously in check_for_resolution_lags() whenever a confirmed
# final is matched to a Kalshi market — before the background thread fires.
# Drained each cycle by the executor via drain_ghost_exits().
_ghost_exit_queue: queue.Queue = queue.Queue()

# Set of game_ids we've already dispatched to avoid duplicates
_dispatched: set = set()
_dispatched_lock = threading.Lock()

# Per-sport latency histogram: sport → list of lag_ms values
_latency_histogram: Dict[str, List[float]] = defaultdict(list)
_histogram_lock = threading.Lock()

# Registered callbacks — set once by initialize()
_market_id_resolver: Optional[Callable] = None
_market_price_fetcher: Optional[Callable] = None

# Per-cycle main-thread timing accumulator
_cycle_elapsed_ms: float = 0.0


# ── Signal dataclass ──────────────────────────────────────────────────────────

@dataclass
class ResolutionSignal:
    game_id: str
    market_id: str
    sport: str
    home_team: str
    away_team: str
    winner: str              # "home" | "away" | "tie"
    correct_prob: float      # 1.0 or 0.0 (the resolution value)
    market_price: float      # current Kalshi YES ask price at time of detection
    gap: float               # abs(correct_prob - market_price)
    confidence: float = _CONFIDENCE
    resolution_lag_ms: float = 0.0   # ms from ESPN final to this check
    timestamp: float = field(default_factory=time.time)


# ── Dispatched-set persistence ────────────────────────────────────────────────

def _load_dispatched() -> None:
    """Load previously dispatched game IDs from disk into _dispatched."""
    path = Path(_DISPATCHED_FILE)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ids: list = data.get("dispatched", [])
        with _dispatched_lock:
            _dispatched.update(ids)
        logger.info(
            "ResolutionDetector: loaded %d previously dispatched game ID(s) from %s",
            len(ids), _DISPATCHED_FILE,
        )
    except Exception as exc:
        logger.warning(
            "ResolutionDetector: failed to load %s (starting fresh): %s",
            _DISPATCHED_FILE, exc,
        )


def _save_dispatched() -> None:
    """Persist the current dispatched set to disk."""
    try:
        with _dispatched_lock:
            ids = list(_dispatched)
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "dispatched": ids,
        }
        Path(_DISPATCHED_FILE).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning(
            "ResolutionDetector: failed to save %s: %s", _DISPATCHED_FILE, exc
        )


# ── Initialization ────────────────────────────────────────────────────────────

def initialize(
    market_id_resolver: Callable[[str, str, str], Optional[str]],
    market_price_fetcher: Callable[[str], Optional[float]],
) -> None:
    """
    Register the callables needed for resolution lag detection.

    Parameters
    ----------
    market_id_resolver
        Signature: (home_team: str, away_team: str, sport: str) → Optional[str]
        Returns the Kalshi market_id for a game, or None if not tracked.

    market_price_fetcher
        Signature: (market_id: str) → Optional[float]
        Returns the current YES ask price [0-1], or None if market is closed
        or the fetch fails.

    Call this once at bot startup before the first cycle.
    """
    global _market_id_resolver, _market_price_fetcher
    _market_id_resolver = market_id_resolver
    _market_price_fetcher = market_price_fetcher
    _load_dispatched()
    logger.info("ResolutionDetector: initialized")


# ── Main-cycle entry point ────────────────────────────────────────────────────

def check_for_resolution_lags() -> List[ResolutionSignal]:
    """
    Drain the signal queue and dispatch background checks for newly confirmed finals.

    Call once per bot cycle, after refresh_if_stale().

    Returns
    -------
    List of ResolutionSignals ready for immediate execution (highest priority).
    An empty list is returned when no games have resolved since the last cycle.
    """
    global _cycle_elapsed_ms
    t0 = time.monotonic()

    # 1. Drain any signals produced by background threads
    ready: List[ResolutionSignal] = []
    try:
        while True:
            ready.append(_signal_queue.get_nowait())
    except queue.Empty:
        pass

    # 2. Check for newly confirmed finals and dispatch background threads
    if _market_id_resolver is not None and _market_price_fetcher is not None:
        for completed in get_newly_confirmed_finals():
            with _dispatched_lock:
                if completed.game_id in _dispatched:
                    continue
                _dispatched.add(completed.game_id)
            # Persist outside the lock so file I/O doesn't block other threads.
            _save_dispatched()

            market_id = _market_id_resolver(
                completed.home_team, completed.away_team, completed.sport
            )
            if market_id is None:
                logger.debug(
                    "ResolutionDetector: no market ID for %s %s vs %s — skipping",
                    completed.sport, completed.home_team, completed.away_team,
                )
                continue

            logger.info(
                "ResolutionDetector: dispatching background check for %s %s vs %s "
                "(winner=%s market=%s)",
                completed.sport.upper(), completed.home_team, completed.away_team,
                completed.winner, market_id,
            )
            # Push immediately (on main thread) so the executor can exit any open
            # ghost positions this same cycle — before the background price check.
            _correct_prob = 1.0 if completed.winner == "home" else 0.0
            logger.info(
                "ResolutionDetector: queuing ghost exit for %s — home_score=%d away_score=%d "
                "winner=%s settlement_value=%.1f (YES=home)",
                market_id, completed.home_score, completed.away_score, completed.winner, _correct_prob,
            )
            _ghost_exit_queue.put((market_id, _correct_prob))

            _thread_pool.submit(
                _background_lag_check,
                completed,
                market_id,
                _market_price_fetcher,
            )
    else:
        logger.debug(
            "ResolutionDetector: not initialized — call initialize() at startup"
        )

    elapsed = (time.monotonic() - t0) * 1000
    _cycle_elapsed_ms += elapsed
    return ready


def drain_ghost_exits() -> List[tuple]:
    """
    Return and drain all pending (market_id, correct_prob) ghost exit entries.

    Call once per cycle, immediately after check_for_resolution_lags().
    Each entry represents a game that just went CONFIRMED FINAL; the executor
    should close any open ghost positions on that market at correct_prob.
    """
    exits = []
    try:
        while True:
            exits.append(_ghost_exit_queue.get_nowait())
    except queue.Empty:
        pass
    return exits


# ── Background thread ─────────────────────────────────────────────────────────

def _background_lag_check(
    completed: CompletedGame,
    market_id: str,
    price_fetcher: Callable[[str], Optional[float]],
) -> None:
    """
    Background thread: re-fetch the Kalshi market price and check for lag.

    Logs resolution_lag_ms regardless of whether a signal fires.
    Enqueues a ResolutionSignal if the market price is far from correct resolution.
    """
    fetch_start = time.monotonic()
    lag_wall = time.time() - completed.confirmed_at if completed.confirmed_at else 0.0

    try:
        current_price = price_fetcher(market_id)
    except Exception as exc:
        logger.warning(
            "ResolutionDetector: price fetch error for %s: %s", market_id, exc
        )
        return

    fetch_elapsed_ms = (time.monotonic() - fetch_start) * 1000
    resolution_lag_ms = lag_wall * 1000 + fetch_elapsed_ms

    correct_prob = 1.0 if completed.winner == "home" else 0.0
    # Ties resolve NO (prob=0.0) since "did home team win?" is False for a tie
    if completed.winner == "tie":
        correct_prob = 0.0

    if current_price is None:
        logger.info(
            "ResolutionDetector: %s %s vs %s | market=%s already closed "
            "(winner=%s lag=%.0fms)",
            completed.sport.upper(), completed.home_team, completed.away_team,
            market_id, completed.winner, resolution_lag_ms,
        )
        _record_latency(completed.sport, resolution_lag_ms)
        return

    gap = abs(correct_prob - current_price)

    logger.info(
        "ResolutionDetector: %s %s vs %s CONFIRMED FINAL | winner=%s "
        "correct=%.2f market=%.2f gap=%.3f lag=%.0fms",
        completed.sport.upper(), completed.home_team, completed.away_team,
        completed.winner, correct_prob, current_price, gap, resolution_lag_ms,
    )

    _record_latency(completed.sport, resolution_lag_ms)

    if gap <= _RESOLUTION_THRESHOLD:
        logger.info(
            "ResolutionDetector: %s gap=%.3f ≤ %.2f — market already resolved",
            market_id, gap, _RESOLUTION_THRESHOLD,
        )
        return

    # Market is still mispriced — fire a resolution lag signal
    signal = ResolutionSignal(
        game_id=completed.game_id,
        market_id=market_id,
        sport=completed.sport,
        home_team=completed.home_team,
        away_team=completed.away_team,
        winner=completed.winner,
        correct_prob=correct_prob,
        market_price=current_price,
        gap=gap,
        confidence=_CONFIDENCE,
        resolution_lag_ms=resolution_lag_ms,
    )
    _signal_queue.put(signal)
    logger.info(
        "ResolutionDetector: SIGNAL queued for %s — gap=%.3f lag=%.0fms",
        market_id, gap, resolution_lag_ms,
    )


# ── Latency histogram ─────────────────────────────────────────────────────────

def _record_latency(sport: str, lag_ms: float) -> None:
    with _histogram_lock:
        _latency_histogram[sport].append(lag_ms)


def get_latency_histogram() -> Dict[str, List[float]]:
    """Return a copy of the per-sport latency histogram (in milliseconds)."""
    with _histogram_lock:
        return {sport: list(lags) for sport, lags in _latency_histogram.items()}


def log_latency_summary() -> None:
    """
    Emit a human-readable latency histogram to the log.

    Useful for the ghost-mode validation checklist:
      "Resolution lag window has been measured for at least 10 completed games"
    """
    with _histogram_lock:
        histogram = dict(_latency_histogram)

    if not histogram:
        logger.info("ResolutionDetector: no latency data yet")
        return

    for sport, lags in sorted(histogram.items()):
        if not lags:
            continue
        n = len(lags)
        sorted_lags = sorted(lags)
        p50 = sorted_lags[int(n * 0.50)]
        p90 = sorted_lags[int(n * 0.90)]
        p99 = sorted_lags[min(int(n * 0.99), n - 1)]
        logger.info(
            "ResolutionDetector latency [%s]: n=%d "
            "p50=%.0fms p90=%.0fms p99=%.0fms min=%.0fms max=%.0fms",
            sport.upper(), n, p50, p90, p99, sorted_lags[0], sorted_lags[-1],
        )


# ── Cleanup ───────────────────────────────────────────────────────────────────

def reset_session() -> None:
    """
    Clear all session state (dispatched set, signal queue, histogram).

    Call between trading sessions or in tests.
    """
    global _cycle_elapsed_ms
    with _dispatched_lock:
        _dispatched.clear()
    _save_dispatched()
    try:
        while True:
            _signal_queue.get_nowait()
    except queue.Empty:
        pass
    try:
        while True:
            _ghost_exit_queue.get_nowait()
    except queue.Empty:
        pass
    with _histogram_lock:
        _latency_histogram.clear()
    _cycle_elapsed_ms = 0.0
    logger.info("ResolutionDetector: session reset")


def reset_cycle_timing() -> float:
    """Return and reset the accumulated main-thread resolution check time."""
    global _cycle_elapsed_ms
    elapsed = _cycle_elapsed_ms
    _cycle_elapsed_ms = 0.0
    return elapsed
