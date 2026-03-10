"""
data.sports.win_probability – converts game state to home-team win probability.

All models are pure computation — no external calls, no I/O. Each model must
execute in under 5ms. Inputs and output are logged on every call for auditing
and future calibration.

Models
------
  nfl_win_probability(state)   → float [0.03, 0.97]
  nba_win_probability(state)   → float [0.03, 0.97]
  ncaab_win_probability(state) → float [0.03, 0.97]

All return the estimated probability that the HOME team wins.
"""

from __future__ import annotations

import logging

from .live_game_monitor import NFLState, NBAState

logger = logging.getLogger(__name__)

# Hard probability caps — even a 3-score lead in garbage time shouldn't be 1.0
_PROB_MIN = 0.03
_PROB_MAX = 0.97


def _clamp(p: float) -> float:
    return max(_PROB_MIN, min(_PROB_MAX, p))


# ── NFL ────────────────────────────────────────────────────────────────────────

# Total regulation seconds: 4 quarters × 900s = 3600
_NFL_TOTAL_SECONDS = 3600
_NFL_QUARTER_SECONDS = 900


def nfl_win_probability(state: NFLState) -> float:
    """
    Estimate home-team win probability from NFL game state.

    Formula (regulation only, capped at 0.03/0.97):
      seconds_elapsed  = (quarter-1)*900 + (900 - clock)
      seconds_remaining = 3600 - seconds_elapsed
      lead_value = score_diff * (seconds_remaining / 3600) * 0.06
      base_prob = 0.50 + lead_value

    Adjustments:
      +0.03 if leading team has possession
      -0.02 if trailing team has possession
      +0.02 if possession is inside opponent's 20 (field_position ≤ 20)
      ±0.01 per timeout advantage in final 2 minutes

    Hard overrides (late-game blowout):
      score_diff ≥ 8, < 120s left, trailing team not in possession → 0.96
      score_diff ≥ 16, < 60s left → 0.98
    """
    score_diff = state.home_score - state.away_score  # positive = home leading
    seconds_elapsed = (state.quarter - 1) * _NFL_QUARTER_SECONDS + (
        _NFL_QUARTER_SECONDS - state.clock
    )
    # OT: treat as a tiny positive remainder — full time_weight applies
    seconds_remaining = max(0, _NFL_TOTAL_SECONDS - seconds_elapsed)

    lead_value = score_diff * (seconds_remaining / _NFL_TOTAL_SECONDS) * 0.06
    base_prob = 0.50 + lead_value

    # Possession adjustment
    home_leading = score_diff > 0
    away_leading = score_diff < 0
    if state.possession == "home":
        if home_leading:
            base_prob += 0.03
        elif away_leading:
            base_prob -= 0.02  # trailing team has ball back
    elif state.possession == "away":
        if away_leading:
            base_prob -= 0.03   # away team leading AND has ball
        elif home_leading:
            base_prob += 0.02   # home still leading, away trying to catch up

    # Field position — inside opponent's red zone
    if state.possession in ("home", "away") and state.field_position <= 20:
        if state.possession == "home":
            base_prob += 0.02
        else:
            base_prob -= 0.02

    # Timeout value — only in final 2 minutes
    if seconds_remaining <= 120:
        to_diff = state.home_timeouts - state.away_timeouts
        base_prob += to_diff * 0.01  # each timeout advantage worth ~1%

    prob = _clamp(base_prob)

    # Hard blowout overrides
    abs_diff = abs(score_diff)
    losing_has_poss = (score_diff > 0 and state.possession == "away") or (
        score_diff < 0 and state.possession == "home"
    )
    if abs_diff >= 16 and seconds_remaining <= 60:
        prob = _PROB_MAX if score_diff > 0 else _PROB_MIN
    elif abs_diff >= 8 and seconds_remaining <= 120 and not losing_has_poss:
        prob = 0.96 if score_diff > 0 else 1.0 - 0.96

    logger.debug(
        "WinProb NFL | home=%s away=%s score=%d-%d q=%d clock=%ds | "
        "elapsed=%ds remain=%ds diff=%d pos=%s fp=%d yd=%d | prob=%.3f",
        state.home_team, state.away_team,
        state.home_score, state.away_score,
        state.quarter, state.clock,
        seconds_elapsed, seconds_remaining,
        score_diff, state.possession, state.field_position, state.yards_to_go,
        prob,
    )
    return prob


# ── NBA ────────────────────────────────────────────────────────────────────────

# Total regulation seconds: 4 quarters × 720s = 2880
_NBA_TOTAL_SECONDS = 2880
_NBA_QUARTER_SECONDS = 720


def nba_win_probability(state: NBAState) -> float:
    """
    Estimate home-team win probability from NBA game state.

    Formula:
      seconds_elapsed  = (quarter-1)*720 + (720 - clock)
      seconds_remaining = 2880 - seconds_elapsed
      lead_value = score_diff * (seconds_remaining / 2880) * 0.08
      base_prob = 0.50 + lead_value

    Adjustments:
      Away team 2+ players in foul trouble in Q4 → +0.04
      Home team 2+ players in foul trouble in Q4 → -0.04

    Hard overrides:
      score_diff ≥ 10, < 120s remaining → 0.95
      score_diff ≥ 15, < 60s remaining  → 0.97
    """
    return _nba_model(
        state=state,
        total_seconds=_NBA_TOTAL_SECONDS,
        quarter_seconds=_NBA_QUARTER_SECONDS,
        sport_label="NBA",
    )


# ── NCAAB ─────────────────────────────────────────────────────────────────────

# Total seconds: 2 halves × 1200s = 2400
_NCAAB_TOTAL_SECONDS = 2400
_NCAAB_HALF_SECONDS = 1200


def ncaab_win_probability(state: NBAState) -> float:
    """
    Estimate home-team win probability from NCAAB game state.

    Uses the NBA model with 20-minute halves (1200s each) and 2 periods total.
    """
    return _nba_model(
        state=state,
        total_seconds=_NCAAB_TOTAL_SECONDS,
        quarter_seconds=_NCAAB_HALF_SECONDS,
        sport_label="NCAAB",
    )


def _nba_model(
    state: NBAState,
    total_seconds: int,
    quarter_seconds: int,
    sport_label: str,
) -> float:
    """Shared computation for NBA and NCAAB."""
    score_diff = state.home_score - state.away_score
    seconds_elapsed = (state.quarter - 1) * quarter_seconds + (
        quarter_seconds - state.clock
    )
    seconds_remaining = max(0, total_seconds - seconds_elapsed)

    lead_value = score_diff * (seconds_remaining / total_seconds) * 0.08
    base_prob = 0.50 + lead_value

    # Foul trouble in the final period only (Q4 for NBA, H2 for NCAAB)
    final_period = 4 if total_seconds == _NBA_TOTAL_SECONDS else 2
    if state.quarter >= final_period:
        if state.away_foul_trouble >= 2:
            base_prob += 0.04
        if state.home_foul_trouble >= 2:
            base_prob -= 0.04

    prob = _clamp(base_prob)

    # Hard blowout overrides
    abs_diff = abs(score_diff)
    if abs_diff >= 15 and seconds_remaining <= 60:
        prob = _PROB_MAX if score_diff > 0 else _PROB_MIN
    elif abs_diff >= 10 and seconds_remaining <= 120:
        prob = 0.95 if score_diff > 0 else 1.0 - 0.95

    logger.debug(
        "WinProb %s | home=%s away=%s score=%d-%d q=%d clock=%ds | "
        "elapsed=%ds remain=%ds diff=%d pos=%s foul_h=%d foul_a=%d | prob=%.3f",
        sport_label,
        state.home_team, state.away_team,
        state.home_score, state.away_score,
        state.quarter, state.clock,
        seconds_elapsed, seconds_remaining,
        score_diff, state.possession,
        state.home_foul_trouble, state.away_foul_trouble,
        prob,
    )
    return prob


# ── Dispatch ───────────────────────────────────────────────────────────────────

def compute_win_probability(sport: str, state: object) -> float:
    """
    Dispatch to the correct model based on sport.

    Parameters
    ----------
    sport : "nfl" | "nba" | "ncaab"
    state : NFLState | NBAState (NCABState is an alias for NBAState)

    Returns
    -------
    float – estimated home-team win probability in [0.03, 0.97]
    """
    if sport == "nfl":
        return nfl_win_probability(state)  # type: ignore[arg-type]
    if sport == "nba":
        return nba_win_probability(state)  # type: ignore[arg-type]
    if sport == "ncaab":
        return ncaab_win_probability(state)  # type: ignore[arg-type]
    raise ValueError(f"Unknown sport: {sport!r}")
