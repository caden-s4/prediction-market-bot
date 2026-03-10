"""
data.sports.shock_detector – detects win-probability shocks during live games.

Runs after each LiveGameMonitor poll. For every in-progress game it computes
the current and previous win probability and flags large jumps as actionable
signals.

A shock is actionable when ALL of:
  1. shock_magnitude >= 0.12
  2. Game is in the final period/quarter (Q4 for NBA/NFL, H2 for NCAAB)
  3. prob_now is NOT between 0.45 and 0.55 (avoid genuine uncertainty)
  4. Data is not stale

Confidence scoring:
  0.99  Final game (completed) — not a shock signal but handled upstream
  0.92  shock >= 0.25, final period, seconds_remaining < 120
  0.85  shock >= 0.15, final period, seconds_remaining < 300
  0.78  shock >= 0.12, final period (below confidence gate — logs only)

All shocks are logged regardless of whether they become trades.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .live_game_monitor import (
    GameSnapshot,
    NFLState,
    NBAState,
    get_active_snapshots,
)
from .win_probability import compute_win_probability

logger = logging.getLogger(__name__)

# Shock thresholds
_SHOCK_MIN = 0.12         # minimum shock to process
_SHOCK_TRADE_MIN = 0.85   # minimum confidence to trade (0.78 = log only)
_UNCERTAIN_BAND = (0.45, 0.55)  # avoid trading genuine uncertainty

# seconds_remaining thresholds for confidence tiers
_TIER1_SECS = 120
_TIER2_SECS = 300

# Cache: game_id → latest SportSignal (one per game per cycle)
_shock_cache: Dict[str, "SportSignal"] = {}
_cache_timestamp: float = 0.0


@dataclass
class SportSignal:
    game_id: str
    sport: str
    home_team: str
    away_team: str
    prob_before: float
    prob_after: float
    shock_magnitude: float
    direction: str           # "home_winning" | "away_winning"
    confidence: float
    trigger_event: str       # last_event string that caused it
    seconds_remaining: float # seconds left in regulation at time of shock
    timestamp: float = field(default_factory=time.time)


def _seconds_remaining(sport: str, state: object) -> float:
    """Compute seconds remaining in regulation for any sport/state."""
    if sport == "nfl":
        s: NFLState = state  # type: ignore[assignment]
        elapsed = (s.quarter - 1) * 900 + (900 - s.clock)
        return max(0.0, 3600 - elapsed)
    if sport == "nba":
        s2: NBAState = state  # type: ignore[assignment]
        elapsed2 = (s2.quarter - 1) * 720 + (720 - s2.clock)
        return max(0.0, 2880 - elapsed2)
    if sport == "ncaab":
        s3: NBAState = state  # type: ignore[assignment]
        elapsed3 = (s3.quarter - 1) * 1200 + (1200 - s3.clock)
        return max(0.0, 2400 - elapsed3)
    return 0.0


def _in_final_period(sport: str, state: object) -> bool:
    """Return True if the game is in its final regulation period."""
    if sport == "nfl":
        return (state.quarter >= 4)  # type: ignore[attr-defined]
    if sport == "nba":
        return (state.quarter >= 4)  # type: ignore[attr-defined]
    if sport == "ncaab":
        return (state.quarter >= 2)  # 2 halves; H2 is final
    return False


def _score_confidence(shock: float, final_period: bool, secs_remaining: float) -> float:
    """Return confidence score for a shock signal."""
    if not final_period:
        return 0.0  # won't fire — only final-period shocks are actionable
    if shock >= 0.25 and secs_remaining < _TIER1_SECS:
        return 0.92
    if shock >= 0.15 and secs_remaining < _TIER2_SECS:
        return 0.85
    if shock >= _SHOCK_MIN:
        return 0.78   # below trade threshold — logged only
    return 0.0


def _get_last_event(state: object) -> str:
    return getattr(state, "last_event", "") or ""


def run_shock_detection() -> List[SportSignal]:
    """
    Run shock detection across all active in-progress games.

    Call this once per bot cycle, after refresh_if_stale() has been called.

    Returns a list of actionable SportSignal objects (confidence >= 0.85).
    Non-actionable shocks (confidence = 0.78) are logged but not returned.
    """
    global _shock_cache, _cache_timestamp

    snapshots = get_active_snapshots()
    actionable: List[SportSignal] = []
    now = time.time()

    for snap in snapshots:
        if snap.stale:
            logger.debug(
                "ShockDetector: skipping stale game %s (%s vs %s)",
                snap.game_id, snap.home_team, snap.away_team,
            )
            continue

        if snap.previous_state is None:
            # First time we've seen this game — no previous to compare
            continue

        try:
            prob_now = compute_win_probability(snap.sport, snap.current_state)
            prob_prev = compute_win_probability(snap.sport, snap.previous_state)
        except Exception as exc:
            logger.warning(
                "ShockDetector: probability error for %s: %s", snap.game_id, exc
            )
            continue

        shock = abs(prob_now - prob_prev)

        if shock < _SHOCK_MIN:
            continue

        final_period = _in_final_period(snap.sport, snap.current_state)
        secs_remaining = _seconds_remaining(snap.sport, snap.current_state)
        uncertain = _UNCERTAIN_BAND[0] <= prob_now <= _UNCERTAIN_BAND[1]
        trigger = _get_last_event(snap.current_state)

        confidence = _score_confidence(shock, final_period, secs_remaining)
        direction = "home_winning" if prob_now > 0.5 else "away_winning"

        signal = SportSignal(
            game_id=snap.game_id,
            sport=snap.sport,
            home_team=snap.home_team,
            away_team=snap.away_team,
            prob_before=prob_prev,
            prob_after=prob_now,
            shock_magnitude=shock,
            direction=direction,
            confidence=confidence,
            trigger_event=trigger,
            seconds_remaining=secs_remaining,
            timestamp=now,
        )

        logger.info(
            "ShockDetector: %s %s vs %s | prob %.2f→%.2f shock=%.2f "
            "trigger=%r conf=%.2f final_period=%s uncertain=%s",
            snap.sport.upper(),
            snap.home_team, snap.away_team,
            prob_prev, prob_now, shock,
            trigger, confidence, final_period, uncertain,
        )

        # Gate: final period + not uncertain + confidence tradeable
        if not final_period:
            logger.debug(
                "ShockDetector: %s shock %.2f ignored — not in final period",
                snap.game_id, shock,
            )
            continue

        if uncertain:
            logger.debug(
                "ShockDetector: %s shock %.2f ignored — prob %.2f in uncertain band",
                snap.game_id, shock, prob_now,
            )
            continue

        _shock_cache[snap.game_id] = signal

        if confidence >= _SHOCK_TRADE_MIN:
            actionable.append(signal)
        else:
            logger.info(
                "ShockDetector: %s conf=%.2f < %.2f — log only, no trade",
                snap.game_id, confidence, _SHOCK_TRADE_MIN,
            )

    _cache_timestamp = now
    return actionable


def get_cached_shock(game_id: str) -> Optional[SportSignal]:
    """Return the most recent shock signal for a game_id, or None."""
    return _shock_cache.get(game_id)


def get_all_cached_shocks() -> Dict[str, SportSignal]:
    """Return a copy of the full shock cache."""
    return dict(_shock_cache)


def clear_shock_cache() -> None:
    """Clear the shock cache (call between sessions or after resolution)."""
    global _shock_cache, _cache_timestamp
    _shock_cache = {}
    _cache_timestamp = 0.0
