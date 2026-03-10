"""
data.sports.staleness_detector – detects stale markets where game probability
has drifted but the Kalshi market price has not followed.

A stale market is one where the ESPN model has shifted meaningfully over
several cycles (gradual drift) but the order book has barely moved.  This is
different from a shock signal — the edge is in slow markets, not sudden events.

Detection logic
---------------
For each (game_id, market_id) pair we maintain a rolling 5-cycle buffer of
(prob, price) readings.  Each cycle, the buffer is updated via update_staleness()
and checked against the following gates (all must be true):

  1. prob_delta_cumulative  > 0.10  — model drifted ≥10pp in net direction
  2. |price_delta_cumulative| < 0.02 — market price barely moved (<2pp)
  3. game is in its final period (Q4 / H2)
  4. current model prob is not in [0.45, 0.55] — avoid genuine uncertainty

Confidence tiers
----------------
  0.88  prob_delta_cumulative > 0.20 and price unchanged
  0.80  prob_delta_cumulative > 0.10 and price unchanged

Logging (every evaluation):
  StalenessDetector: {sport} {home} vs {away} | prob_drift={X:.2f}
      price_drift={X:.2f} | stale={True/False}

Reset the buffer for a market when the position is exited (call clear_market()).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .shock_detector import _in_final_period, _seconds_remaining

logger = logging.getLogger(__name__)

# Buffer depth — 5 cycles of history per market
_BUFFER_DEPTH = 5

# Thresholds
_PROB_DRIFT_MIN = 0.10        # minimum net model drift to consider stale
_PROB_DRIFT_HIGH = 0.20       # high-confidence tier
_PRICE_STATIC_MAX = 0.02      # maximum market movement to classify as stale
_UNCERTAIN_BAND = (0.45, 0.55)

# Confidence levels
_CONF_HIGH = 0.88
_CONF_LOW = 0.80
_CONF_TRADE_MIN = 0.80        # minimum to produce a tradeable signal

# Per-cycle timing accumulator (reset by pipeline.py each cycle)
_cycle_elapsed_ms: float = 0.0


@dataclass
class _BufferEntry:
    prob: float    # model win probability (home team)
    price: float   # Kalshi YES ask price at time of reading
    ts: float = field(default_factory=time.time)


@dataclass
class StalenessSignal:
    game_id: str
    market_id: str
    sport: str
    home_team: str
    away_team: str
    prob_delta_cumulative: float    # net drift: prob_now - prob_5_cycles_ago
    price_delta_cumulative: float   # net drift: price_now - price_5_cycles_ago
    prob_now: float
    market_price: float
    direction: str                  # "home_winning" | "away_winning"
    confidence: float
    seconds_remaining: float
    timestamp: float = field(default_factory=time.time)


# ── State ────────────────────────────────────────────────────────────────────

# (game_id, market_id) → rolling buffer
_buffers: Dict[Tuple[str, str], Deque[_BufferEntry]] = {}


# ── Public API ────────────────────────────────────────────────────────────────

def update_staleness(
    game_id: str,
    market_id: str,
    sport: str,
    home_team: str,
    away_team: str,
    current_state: object,
    model_prob: float,
    market_price: float,
) -> Optional[StalenessSignal]:
    """
    Update the rolling buffer for this (game, market) pair and check for
    a staleness signal.

    Parameters
    ----------
    game_id       : ESPN game ID (from LiveGameMonitor snapshot)
    market_id     : Kalshi market ID
    sport         : "nfl" | "nba" | "ncaab"
    home_team     : home team name (for logging)
    away_team     : away team name (for logging)
    current_state : current game state (NFLState or NBAState)
    model_prob    : current WinProbabilityModel output for the home team
    market_price  : current Kalshi YES ask price [0-1]

    Returns
    -------
    StalenessSignal if the market is stale and confidence >= 0.80, else None.
    Always logs the evaluation.
    """
    global _cycle_elapsed_ms
    t0 = time.monotonic()

    key = (game_id, market_id)
    buf = _buffers.setdefault(key, deque(maxlen=_BUFFER_DEPTH))
    buf.append(_BufferEntry(prob=model_prob, price=market_price))

    signal = None

    if len(buf) >= 2:
        prob_delta = model_prob - buf[0].prob          # net signed drift
        price_delta = market_price - buf[0].price      # net signed drift

        final_period = _in_final_period(sport, current_state)
        uncertain = _UNCERTAIN_BAND[0] <= model_prob <= _UNCERTAIN_BAND[1]
        secs = _seconds_remaining(sport, current_state)

        is_stale = (
            prob_delta > _PROB_DRIFT_MIN
            and abs(price_delta) < _PRICE_STATIC_MAX
            and final_period
            and not uncertain
        )

        # Confidence
        if is_stale:
            if prob_delta > _PROB_DRIFT_HIGH:
                confidence = _CONF_HIGH
            else:
                confidence = _CONF_LOW
        else:
            confidence = 0.0

        logger.info(
            "StalenessDetector: %s %s vs %s | prob_drift=%.2f price_drift=%.2f "
            "| stale=%s final_period=%s uncertain=%s conf=%.2f cycles=%d",
            sport.upper(), home_team, away_team,
            prob_delta, price_delta,
            is_stale, final_period, uncertain, confidence, len(buf),
        )

        if is_stale and confidence >= _CONF_TRADE_MIN:
            direction = "home_winning" if model_prob > 0.5 else "away_winning"
            signal = StalenessSignal(
                game_id=game_id,
                market_id=market_id,
                sport=sport,
                home_team=home_team,
                away_team=away_team,
                prob_delta_cumulative=prob_delta,
                price_delta_cumulative=price_delta,
                prob_now=model_prob,
                market_price=market_price,
                direction=direction,
                confidence=confidence,
                seconds_remaining=secs,
            )
    else:
        logger.debug(
            "StalenessDetector: %s %s vs %s | buffer filling (%d/%d) — no check yet",
            sport.upper(), home_team, away_team, len(buf), _BUFFER_DEPTH,
        )

    elapsed = (time.monotonic() - t0) * 1000
    _cycle_elapsed_ms += elapsed
    return signal


def clear_market(game_id: str, market_id: str) -> None:
    """
    Reset the rolling buffer for a (game, market) pair.

    Call this when a position on the market is exited or the market resolves.
    """
    key = (game_id, market_id)
    _buffers.pop(key, None)
    logger.debug("StalenessDetector: cleared buffer for game=%s market=%s", game_id, market_id)


def clear_game(game_id: str) -> None:
    """Reset all buffers for a game (call when a game completes)."""
    keys = [k for k in _buffers if k[0] == game_id]
    for k in keys:
        del _buffers[k]
    if keys:
        logger.debug("StalenessDetector: cleared %d buffers for game=%s", len(keys), game_id)


def reset_cycle_timing() -> float:
    """Return and reset the accumulated staleness check time for this cycle."""
    global _cycle_elapsed_ms
    elapsed = _cycle_elapsed_ms
    _cycle_elapsed_ms = 0.0
    return elapsed
