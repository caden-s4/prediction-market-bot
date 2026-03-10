"""
data.sports.live_game_monitor – polls ESPN for in-progress game state.

Fetches NBA, NFL, and NCAAB scoreboards every 15 seconds (matching the bot
cycle). Caches the full ESPN response per sport; only makes a new HTTP request
when 15 seconds have elapsed since the last fetch for that sport.

Never fetches mid-cycle — callers invoke refresh_if_stale() at the start of
each cycle, then read from the cache for all market evaluations within that
cycle.

If ESPN returns an error or timeout the last known state is preserved and
marked stale=True. Stale data is never traded.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_FETCH_TTL = 15       # seconds between real ESPN requests
_REQUEST_TIMEOUT = 3  # seconds — hard cap per HTTP call

# ESPN sport paths for the three supported leagues
SPORT_PATHS: Dict[str, str] = {
    "nba":   "basketball/nba",
    "nfl":   "americanfootball/nfl",
    "ncaab": "basketball/mens-college-basketball",
}


# ── Game state dataclasses ────────────────────────────────────────────────────

@dataclass
class NFLState:
    game_id: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    quarter: int          # 1-4, 5+ = OT
    clock: int            # seconds remaining in current quarter
    possession: str       # "home" | "away" | "none"
    field_position: int   # yards from opponent's end zone (0-100)
    down: int             # 1-4
    yards_to_go: int
    home_timeouts: int
    away_timeouts: int
    last_event: str       # "touchdown", "turnover", "field_goal", "punt", etc.


@dataclass
class NBAState:
    game_id: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    quarter: int          # 1-4, 5+ = OT
    clock: int            # seconds remaining in current quarter
    possession: str       # "home" | "away"
    home_foul_trouble: int   # players with 4+ fouls
    away_foul_trouble: int
    last_event: str       # "three_pointer", "foul_out", "timeout", etc.


# NCAAB uses the same structure as NBA but with 2 periods (halves) of 20 min
NCABState = NBAState


@dataclass
class GameSnapshot:
    """Holds current and previous state for shock detection."""
    sport: str                  # "nba" | "nfl" | "ncaab"
    game_id: str
    home_team: str
    away_team: str
    current_state: object       # NFLState | NBAState
    previous_state: Optional[object] = None
    stale: bool = False
    fetched_at: float = field(default_factory=time.monotonic)


# ── Cache ──────────────────────────────────────────────────────────────────────

@dataclass
class _SportCache:
    fetched_at: float
    raw_events: list
    stale: bool = False


_sport_cache: Dict[str, _SportCache] = {}

# game_id → GameSnapshot (maintained across cycles)
_game_snapshots: Dict[str, GameSnapshot] = {}


# ── ESPN fetch ─────────────────────────────────────────────────────────────────

def _fetch_sport(sport: str) -> List[dict]:
    """Return the ESPN events list for a sport, using the 15-second cache.

    Sets stale=True on the cache entry if the fetch fails.
    """
    path = SPORT_PATHS[sport]
    now = time.monotonic()
    cached = _sport_cache.get(sport)

    if cached and (now - cached.fetched_at) < _FETCH_TTL:
        return cached.raw_events

    url = f"{_ESPN_BASE}/{path}/scoreboard"
    try:
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        events = resp.json().get("events", [])
        _sport_cache[sport] = _SportCache(fetched_at=now, raw_events=events, stale=False)
        logger.debug("LiveGameMonitor: fetched %d events for %s", len(events), sport)
        return events
    except requests.exceptions.Timeout:
        logger.warning("LiveGameMonitor: ESPN timeout for %s — keeping last known state", sport)
    except requests.exceptions.RequestException as exc:
        logger.warning("LiveGameMonitor: ESPN error for %s: %s", sport, exc)
    except Exception as exc:
        logger.warning("LiveGameMonitor: unexpected error for %s: %s", sport, exc)

    # On failure, preserve last data but mark stale
    if cached:
        _sport_cache[sport] = _SportCache(
            fetched_at=now,
            raw_events=cached.raw_events,
            stale=True,
        )
        return cached.raw_events

    _sport_cache[sport] = _SportCache(fetched_at=now, raw_events=[], stale=True)
    return []


# ── Parsing helpers ────────────────────────────────────────────────────────────

def _clock_to_seconds(clock_str: str) -> int:
    """Convert 'MM:SS' or 'MM:SS.d' ESPN clock string to integer seconds."""
    try:
        main = clock_str.split(".")[0]
        parts = main.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError, AttributeError):
        return 0


def _parse_nfl_event(event: dict) -> Optional[NFLState]:
    """Parse one ESPN NFL event into an NFLState. Returns None on parse error."""
    try:
        comp = (event.get("competitions") or [{}])[0]
        status = event.get("status", {})
        status_type = status.get("type", {})
        state = status_type.get("state", "pre")

        if state != "in":
            return None

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        situation = comp.get("situation", {})

        def team_name(c: dict) -> str:
            return c.get("team", {}).get("displayName", "") or c.get("team", {}).get("name", "")

        def score(c: dict) -> int:
            try:
                return int(c.get("score", "0"))
            except ValueError:
                return 0

        possession_team = situation.get("possession", {})
        poss_id = possession_team if isinstance(possession_team, str) else possession_team.get("id", "")
        home_id = home.get("id", "")
        away_id = away.get("id", "")
        if poss_id == home_id:
            possession = "home"
        elif poss_id == away_id:
            possession = "away"
        else:
            possession = "none"

        try:
            field_pos = int(situation.get("yardLine", 50))
        except (TypeError, ValueError):
            field_pos = 50

        try:
            down = int(situation.get("down", 1))
        except (TypeError, ValueError):
            down = 1

        try:
            ytg = int(situation.get("distance", 10))
        except (TypeError, ValueError):
            ytg = 10

        try:
            home_to = int(situation.get("homeTimeouts", 3))
        except (TypeError, ValueError):
            home_to = 3

        try:
            away_to = int(situation.get("awayTimeouts", 3))
        except (TypeError, ValueError):
            away_to = 3

        # Last play description as the event string
        last_play = situation.get("lastPlay", {})
        last_event = (
            last_play.get("type", {}).get("text", "")
            or last_play.get("text", "")
            or ""
        ).lower()

        return NFLState(
            game_id=event.get("id", ""),
            home_team=team_name(home),
            away_team=team_name(away),
            home_score=score(home),
            away_score=score(away),
            quarter=status.get("period", 1),
            clock=_clock_to_seconds(status.get("displayClock", "15:00")),
            possession=possession,
            field_position=field_pos,
            down=down,
            yards_to_go=ytg,
            home_timeouts=home_to,
            away_timeouts=away_to,
            last_event=last_event,
        )
    except Exception as exc:
        logger.debug("LiveGameMonitor: NFL parse error for event %s: %s", event.get("id"), exc)
        return None


def _foul_trouble_count(competitors_list: list, home_away: str) -> int:
    """Count players with 4+ fouls for the given team side."""
    team = next((c for c in competitors_list if c.get("homeAway") == home_away), {})
    # ESPN sometimes includes player-level foul data in the statistics array
    stats = team.get("statistics", [])
    count = 0
    for stat in stats:
        if stat.get("name", "").lower() in ("fouls", "personal fouls"):
            try:
                if int(stat.get("value", 0)) >= 4:
                    count += 1
            except (TypeError, ValueError):
                pass
    return count


def _parse_nba_event(event: dict, sport: str = "nba") -> Optional[NBAState]:
    """Parse one ESPN NBA/NCAAB event into an NBAState. Returns None on parse error."""
    try:
        comp = (event.get("competitions") or [{}])[0]
        status = event.get("status", {})
        status_type = status.get("type", {})
        state = status_type.get("state", "pre")

        if state != "in":
            return None

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away = next((c for c in competitors if c.get("homeAway") == "away"), {})

        situation = comp.get("situation", {})

        def team_name(c: dict) -> str:
            return c.get("team", {}).get("displayName", "") or c.get("team", {}).get("name", "")

        def score(c: dict) -> int:
            try:
                return int(c.get("score", "0"))
            except ValueError:
                return 0

        possession_team = situation.get("possession", {})
        poss_id = possession_team if isinstance(possession_team, str) else possession_team.get("id", "")
        home_id = home.get("id", "")
        away_id = away.get("id", "")
        if poss_id == home_id:
            possession = "home"
        elif poss_id == away_id:
            possession = "away"
        else:
            possession = "home"  # default; neutral for probability purposes

        last_play = situation.get("lastPlay", {})
        last_event = (
            last_play.get("type", {}).get("text", "")
            or last_play.get("text", "")
            or ""
        ).lower()

        return NBAState(
            game_id=event.get("id", ""),
            home_team=team_name(home),
            away_team=team_name(away),
            home_score=score(home),
            away_score=score(away),
            quarter=status.get("period", 1),
            clock=_clock_to_seconds(status.get("displayClock", "12:00")),
            possession=possession,
            home_foul_trouble=_foul_trouble_count(competitors, "home"),
            away_foul_trouble=_foul_trouble_count(competitors, "away"),
            last_event=last_event,
        )
    except Exception as exc:
        logger.debug("LiveGameMonitor: NBA parse error for event %s: %s", event.get("id"), exc)
        return None


# ── Public API ─────────────────────────────────────────────────────────────────

def refresh_if_stale() -> None:
    """
    Fetch fresh ESPN data for all three sports if the 15-second TTL has elapsed.

    Call this exactly once at the start of each bot cycle before reading any
    game state. Never call it mid-cycle — all evaluations within a cycle share
    the same snapshot.
    """
    for sport in SPORT_PATHS:
        events = _fetch_sport(sport)
        _update_snapshots(sport, events)


def _update_snapshots(sport: str, events: list) -> None:
    """Parse events and update the game snapshot registry."""
    stale = _sport_cache.get(sport, _SportCache(0, [], True)).stale
    now = time.monotonic()

    active_ids = set()
    for event in events:
        status = event.get("status", {})
        status_type = status.get("type", {})
        state = status_type.get("state", "pre")
        if state != "in":
            continue

        game_id = event.get("id", "")
        if not game_id:
            continue
        active_ids.add(game_id)

        if sport == "nfl":
            new_state = _parse_nfl_event(event)
        else:
            new_state = _parse_nba_event(event, sport)

        if new_state is None:
            continue

        existing = _game_snapshots.get(game_id)
        if existing is None:
            # First time we've seen this game
            _game_snapshots[game_id] = GameSnapshot(
                sport=sport,
                game_id=game_id,
                home_team=new_state.home_team,
                away_team=new_state.away_team,
                current_state=new_state,
                previous_state=None,
                stale=stale,
                fetched_at=now,
            )
        else:
            _game_snapshots[game_id] = GameSnapshot(
                sport=sport,
                game_id=game_id,
                home_team=new_state.home_team,
                away_team=new_state.away_team,
                current_state=new_state,
                previous_state=existing.current_state,
                stale=stale,
                fetched_at=now,
            )

    # Remove games that are no longer in-progress (finished or haven't started)
    finished = [gid for gid, snap in _game_snapshots.items()
                if snap.sport == sport and gid not in active_ids]
    for gid in finished:
        logger.debug("LiveGameMonitor: removing completed/pre-game %s from snapshots", gid)
        del _game_snapshots[gid]


def get_active_snapshots() -> List[GameSnapshot]:
    """Return all currently in-progress game snapshots."""
    return list(_game_snapshots.values())


def get_snapshot(game_id: str) -> Optional[GameSnapshot]:
    """Return the snapshot for a specific game, or None if not tracked."""
    return _game_snapshots.get(game_id)


def is_sport_stale(sport: str) -> bool:
    """Return True if the last ESPN fetch for this sport failed."""
    cached = _sport_cache.get(sport)
    return cached is None or cached.stale
