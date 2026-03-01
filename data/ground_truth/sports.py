"""
data.ground_truth.sports – live sports scores via ESPN API (free, no key needed).

Supports:
  - NFL, NBA, MLB, NHL, MLS, NCAAF, NCAAB
  - Soccer (EPL, Champions League via ESPN)

Confidence:
  0.95  Game is FINAL — authoritative, full confidence.
  0.65  In-progress AND in final period AND lead is substantial (≥28% edge).
  None  In-progress but not final period or lead is too small — wait for final.
  0.0   Pre-game — no outcome yet.

In-progress probability uses a time-weighted formula:
  prob = clip(0.5 + lead × 0.03 × time_weight, 0.08, 0.92)
  time_weight scales from 0.5 at game start to 2.0 at game end.

"Substantial" is defined as prob ≥ 0.78 or ≤ 0.22 (28%+ away from even).

Rationale: a 10-point NBA lead in Q1 is very different from the same lead with
2 minutes left. The old linear formula gave both 0.80.  By multiplying by
time_weight (based on elapsed game time) the formula properly amplifies the
signal late in games and dampens it early.  The hard cap [0.08, 0.92] prevents
any in-progress game from scoring as near-certain — upsets happen.

ESPN's hidden public API endpoints are widely documented and have been stable
for years. No API key required.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

from data.markets.base import Market
from .base import DataSource, GroundTruthResult, SourceType

logger = logging.getLogger(__name__)

# Map of keyword → ESPN sport/league endpoint path (team sports)
_SPORT_MAP = {
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "mls": "soccer/usa.1",
    "ncaa football": "football/college-football",
    "college football": "football/college-football",
    "ncaaf": "football/college-football",
    "cfb": "football/college-football",
    "ncaa basketball": "basketball/mens-college-basketball",
    "college basketball": "basketball/mens-college-basketball",
    "ncaabb": "basketball/mens-college-basketball",
    "cbb": "basketball/mens-college-basketball",
    "epl": "soccer/eng.1",
    "premier league": "soccer/eng.1",
    "champions league": "soccer/uefa.champions",
    "la liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie a": "soccer/ita.1",
}

# Golf uses individual-player leaderboard logic instead of team scores.
# Keys are substrings matched against the full market text (question + tags + id).
_GOLF_MAP: dict = {
    # Generic identifiers
    "golf": "golf/pga",
    "pga": "golf/pga",
    "lpga": "golf/lpga",
    # Majors
    "masters": "golf/pga",
    "pga championship": "golf/pga",
    "ryder cup": "golf/pga",
    "solheim cup": "golf/lpga",
    "presidents cup": "golf/pga",
    # Regular PGA Tour events (add new names here as Kalshi series are created)
    "cognizant": "golf/pga",
    "valspar": "golf/pga",
    "arnold palmer": "golf/pga",
    "bay hill": "golf/pga",
    "players championship": "golf/pga",
    "wells fargo": "golf/pga",
    "genesis invitational": "golf/pga",
    "farmers insurance": "golf/pga",
    "waste management": "golf/pga",
    "honda classic": "golf/pga",
    "memorial tournament": "golf/pga",
    "travelers championship": "golf/pga",
    "fedex st. jude": "golf/pga",
    "bmw championship": "golf/pga",
    "tour championship": "golf/pga",
    "scottish open": "golf/pga",
    "british open": "golf/pga",
    "open championship": "golf/pga",
    "us open": "golf/pga",
}

_ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
_TIMEOUT = 8  # seconds

# Prop/mention markets: "will announcers say X during game Y" — ESPN scores can't
# answer these, so bail out early rather than returning a misleading result.
_PROP_BET_RE = re.compile(
    r"\b(announcer|commentator|broadcaster|host|analyst)s?\b"
    r"|\bwill\b.{0,80}\b(say|mention|utter|reference|call)\b.{0,60}"
    r"\b(during|in)\b.{0,40}\b(game|match|quarter|half|period|broadcast)\b",
    re.IGNORECASE | re.DOTALL,
)

# Module-level scoreboard cache: sport_path → (fetched_at, events_list)
# Shared across all markets in a cycle so ESPN is hit once per sport, not once per market.
_SCOREBOARD_CACHE: dict = {}
_CACHE_TTL = 90  # seconds — long enough to cover a full scan cycle


class SportsDataSource(DataSource):
    """
    Fetches live game scores/results from the ESPN public API.
    """

    def can_handle(self, market: Market) -> bool:
        # Prop/mention bets ("will announcers say X?") cannot be resolved from
        # game scores — reject early so we don't return misleading probabilities.
        if _PROP_BET_RE.search(market.question):
            return False
        # Include market_id so Kalshi tickers like "KXCOGNIZANTCLASSIC-..." match
        text = (
            market.question + " " + " ".join(market.tags) + " " + market.market_id
        ).lower()
        return (
            market.category.lower() in ("sports", "sport")
            or any(k in text for k in _SPORT_MAP)
            or any(k in text for k in _GOLF_MAP)
            or any(word in text for word in (
                "score", "win", "champion", "playoff", "super bowl",
                "world series", "stanley cup", "finals", "game",
                "tournament", "tennis", "ufc", "boxing", "mma", "nascar",
            ))
        )

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        try:
            sport_path = self._detect_sport(market)
            if not sport_path:
                logger.debug("SportsSource: no sport detected for %s", market.market_id)
                return None

            # Golf uses individual-player leaderboard logic, not team scores.
            if sport_path.startswith("golf/"):
                return self._fetch_golf_result(market, sport_path)

            teams = self._extract_teams(market.question)
            events = self._fetch_events(sport_path)
            if not events:
                return None

            match = self._match_event(events, teams, market)
            if not match:
                logger.debug("SportsSource: no matching event for %s", market.market_id)
                return None

            result = self._build_result(match, market, sport_path)
            # _build_result returns None for in-progress games that don't clear
            # the final-period + substantial-lead gate.
            return result

        except Exception as exc:
            logger.warning("SportsSource: error fetching for %s: %s", market.market_id, exc)
            return None

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _detect_sport(self, market: Market) -> Optional[str]:
        """Identify which sport/league this market is about.

        Includes the market_id in the search text so Kalshi-style IDs like
        'KXNCAABBGAME-...' correctly route to the NCAA basketball endpoint
        even when the question text doesn't contain the exact keyword phrase.
        Returns a path like "football/nfl" or "golf/pga".
        """
        text = (
            market.question + " " + " ".join(market.tags) + " " + market.market_id
        ).lower()
        for keyword, path in _SPORT_MAP.items():
            if keyword in text:
                return path
        for keyword, path in _GOLF_MAP.items():
            if keyword in text:
                return path
        # Generic sport category – do NOT fall back to NFL; that produces
        # false scoreboard lookups for non-football sports markets.
        return None

    def _extract_teams(self, question: str) -> list:
        """Extract team names from the market question.

        Returns a list whose first element is the team the question asks about
        (the potential YES side). Each extracted name is validated to be short
        enough to plausibly be a team name rather than a full sentence fragment.
        """
        # Explicit "beat/defeat/win against" pattern gives both teams cleanly
        patterns = [
            r"Will (?:the )?(.+?) (?:beat|defeat|win against) (?:the )?(.+?)[?]",
            r"Will (?:the )?(.+?) win\b",
        ]
        for pat in patterns:
            m = re.search(pat, question, re.IGNORECASE)
            if m:
                teams = [g.strip() for g in m.groups() if g and len(g.strip()) <= 40]
                if teams:
                    return teams

        # "X vs Y" — only accept if both sides look like short team/city names
        for sep in (" vs. ", " vs ", " v. ", " v "):
            idx = question.lower().find(sep)
            if idx != -1:
                left = question[:idx].strip()
                right = question[idx + len(sep):].strip().split("?")[0].strip()
                # Reject if either side is a long sentence fragment (> 35 chars)
                if 2 <= len(left) <= 35 and 2 <= len(right) <= 35:
                    return [left, right]

        return []

    def _fetch_events(self, sport_path: str) -> list:
        """Fetch scoreboard events from ESPN, using a short-lived module-level cache.

        All markets in a single scan cycle fall back to the same sport_path
        (usually football/nfl). Without caching this hits ESPN once per market
        — 90+ redundant identical requests. The cache ensures at most one real
        HTTP call per sport per 90-second window.
        """
        now = time.monotonic()
        cached = _SCOREBOARD_CACHE.get(sport_path)
        if cached:
            fetched_at, events = cached
            if now - fetched_at < _CACHE_TTL:
                return events

        url = f"{_ESPN_BASE}/{sport_path}/scoreboard"
        try:
            resp = requests.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            events = data.get("events", [])
            _SCOREBOARD_CACHE[sport_path] = (now, events)
            return events
        except Exception as exc:
            logger.debug("SportsSource: failed fetching %s: %s", url, exc)
            return []

    def _match_event(self, events: list, teams: list, market: Market) -> Optional[dict]:
        """Find the event matching the market's teams/title."""
        market_text = market.question.lower()
        for event in events:
            event_name = event.get("name", "").lower()
            short_name = event.get("shortName", "").lower()

            # Check if any team from the market question appears in the event
            if teams:
                matched = sum(
                    1 for t in teams
                    if t.lower() in event_name or t.lower() in short_name
                )
                if matched >= 1:
                    return event
            else:
                # Fuzzy: check significant words from the question
                words = [w for w in market_text.split() if len(w) > 4]
                if any(w in event_name for w in words):
                    return event

        return None

    # ── Sport configuration for time-progress calculation ─────────────────────

    # (total_periods, minutes_per_period) by sport path prefix
    _SPORT_TIMING = {
        "basketball": (4, 12),   # NBA: 4 quarters × 12 min
        "football": (4, 15),     # NFL/NCAAF: 4 quarters × 15 min
        "hockey": (3, 20),       # NHL: 3 periods × 20 min
        "soccer": (2, 45),       # EPL etc.: 2 halves × 45 min
        "baseball": (9, 20),     # MLB: 9 innings × ~20 min (approximate)
    }

    def _game_progress(self, sport_path: str, period: int, clock_str: str) -> float:
        """
        Return normalised game progress [0.0, 1.0] where 0.0 = start, 1.0 = end.

        Derived from the current period number and clock string ("MM:SS") from ESPN.
        Falls back to period-only estimate if the clock can't be parsed.
        """
        sport_key = sport_path.split("/")[0]  # "basketball", "football", etc.
        total_periods, period_mins = self._SPORT_TIMING.get(sport_key, (4, 15))
        total_mins = total_periods * period_mins

        # Minutes already completed from previous periods
        elapsed = (period - 1) * period_mins

        # Parse remaining time in the current period ("MM:SS" or "MM:SS.d")
        try:
            main = clock_str.split(".")[0]          # strip fractional seconds
            parts = main.split(":")
            remaining_mins = int(parts[0]) + int(parts[1]) / 60
        except (ValueError, IndexError, AttributeError):
            remaining_mins = period_mins / 2        # assume halfway if unparseable

        elapsed += period_mins - remaining_mins
        return min(elapsed / total_mins, 1.0)

    def _build_result(
        self, event: dict, market: Market, sport_path: str
    ) -> Optional[GroundTruthResult]:
        """
        Parse an ESPN event into a GroundTruthResult.

        Returns None for in-progress games that don't meet the trading gate
        (not in the final period, or lead too small) — the caller will wait
        for the final whistle instead.
        """
        status = event.get("status", {})
        status_type = status.get("type", {})
        state = status_type.get("state", "pre")  # pre | in | post
        completed = status_type.get("completed", False)
        description = status_type.get("description", state)
        period = status.get("period", 1)
        clock_str = status.get("displayClock", "")

        # Postponed, suspended, or cancelled games are NOT the same as pre-game.
        # Returning None tells the caller to skip this market entirely rather
        # than treating it as "scheduled" — the market resolution date may not
        # shift with the game, so we cannot use pre-game logic here.
        _suspended_keywords = ("postponed", "suspended", "cancelled", "canceled", "delayed")
        if any(kw in description.lower() for kw in _suspended_keywords):
            logger.info(
                "SportsSource: %s game status is '%s' — treating as None (not pre-game)",
                market.market_id, description,
            )
            return None

        # Overtime: ESPN uses period > regulation total for OT periods.
        # Log it explicitly so it's visible in debugging, but the formula
        # handles OT correctly — progress is capped at 1.0 → time_weight = 2.0,
        # maximally amplifying the lead signal.
        sport_key_ot = sport_path.split("/")[0]
        _total_periods_ot, _ = self._SPORT_TIMING.get(sport_key_ot, (4, 15))
        if state == "in" and period > _total_periods_ot:
            logger.debug(
                "SportsSource: %s game in OT (period %d, regulation=%d)",
                market.market_id, period, _total_periods_ot,
            )

        competitions = event.get("competitions", [{}])
        comp = competitions[0] if competitions else {}
        competitors = comp.get("competitors", [])

        # Determine scores
        scores: dict = {}
        for c in competitors:
            name = c.get("team", {}).get("displayName", "")
            score_str = c.get("score", "0")
            try:
                scores[name] = int(score_str)
            except ValueError:
                scores[name] = 0

        winner_name = None
        if completed and competitors:
            try:
                winner_name = max(scores, key=lambda k: scores[k])
            except Exception:
                pass

        # Derive ground truth probability
        question_lower = market.question.lower()
        ground_truth_prob: Optional[float] = None
        reasoning = ""

        if completed and winner_name:
            # Game is final – determine if YES or NO based on market question.
            winner_lower = winner_name.lower()
            teams = self._extract_teams(market.question)
            if teams:
                yes_team = teams[0].lower()
                yes_won = yes_team in winner_lower or winner_lower in yes_team
            else:
                yes_won = winner_lower in question_lower
            ground_truth_prob = 1.0 if yes_won else 0.0
            reasoning = (
                f"FINAL: {winner_name} won. "
                f"Market YES {'resolved' if yes_won else 'resolved against'}."
            )
            confidence = 0.95
            source_type = SourceType.HARD

        elif state == "in":
            # In-progress: time-weighted win probability.
            #
            # time_weight scales from 0.5 at game start to 2.0 at game end,
            # so the same point lead is worth much more in the final minutes.
            # Hard cap [0.08, 0.92] — no in-progress game is ever near-certain.
            #
            # Gate: only return a signal if we're in the FINAL period/quarter
            # AND the lead is substantial (prob ≥ 0.78 or ≤ 0.22).  Otherwise
            # return None and let the cycle wait for the final result.
            sport_key = sport_path.split("/")[0]
            total_periods, _ = self._SPORT_TIMING.get(sport_key, (4, 15))
            in_final_period = period >= total_periods

            if len(scores) >= 2:
                vals = sorted(scores.values(), reverse=True)
                lead = vals[0] - vals[1]

                progress = self._game_progress(sport_path, period, clock_str)
                time_weight = 0.5 + progress * 1.5   # 0.5 → 2.0

                raw_prob = 0.5 + lead * 0.03 * time_weight
                prob = min(max(raw_prob, 0.08), 0.92)  # hard cap
                substantial = prob >= 0.78 or prob <= 0.22

                reasoning = (
                    f"Live game in progress (period {period}/{total_periods}, "
                    f"clock='{clock_str}'). Score: {scores}. Lead={lead}. "
                    f"progress={progress:.2f} time_weight={time_weight:.2f} "
                    f"raw_prob={raw_prob:.2f} → capped_prob={prob:.2f}."
                )

                if not in_final_period or not substantial:
                    # Too early or too close — wait for the final result
                    logger.debug(
                        "SportsSource: skipping in-progress signal for %s "
                        "(final_period=%s substantial=%s prob=%.2f)",
                        market.market_id, in_final_period, substantial, prob,
                    )
                    return None

                ground_truth_prob = prob
            else:
                # No score data at all — can't compute anything useful
                return None

            confidence = 0.65   # In-progress is genuinely uncertain
            source_type = SourceType.HARD

        else:
            # Pre-game – no ground truth yet
            ground_truth_prob = None
            confidence = 0.0
            reasoning = "Game has not started."
            source_type = SourceType.AGGREGATED

        return GroundTruthResult(
            ground_truth_prob=ground_truth_prob,
            confidence=confidence,
            source_type=source_type,
            source_name=f"ESPN/{sport_path}",
            source_url=f"{_ESPN_BASE}/{sport_path}/scoreboard",
            raw_data={
                "event_name": event.get("name"),
                "state": state,
                "completed": completed,
                "description": description,
                "period": period,
                "clock": clock_str,
                "scores": scores,
                "winner": winner_name,
            },
            reasoning=reasoning,
        )

    # ── Golf (individual leaderboard) ─────────────────────────────────────────

    def _extract_golf_player(self, question: str) -> Optional[str]:
        """Extract the player name from 'Will [Player] win [Tournament]?'

        Handles both formats Kalshi uses:
          "Tournament Name: Will Player Name win?"
          "Will Player Name win the Tournament Name?"
        """
        # Strip the tournament prefix if the question uses "Tournament: Will …" format
        if ":" in question:
            question = question.split(":", 1)[1].strip()
        m = re.search(r"\bWill\s+(.+?)\s+win\b", question, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if 2 <= len(name) <= 45:
                return name
        return None

    def _fetch_golf_result(
        self, market: Market, sport_path: str
    ) -> Optional[GroundTruthResult]:
        """Resolve a player-wins-tournament market from the ESPN golf leaderboard.

        Returns:
          prob=1.0 / confidence=0.95  — tournament FINAL, player won
          prob=0.0 / confidence=0.95  — tournament FINAL, player did not win
          None                        — tournament in-progress or pre-tournament
                                        (too volatile; wait for the final result)
        """
        player_name = self._extract_golf_player(market.question)
        if not player_name:
            logger.debug(
                "SportsSource: could not extract player name from '%s'",
                market.question,
            )
            return None

        # Reuse the scoreboard cache (golf scoreboard has the same top-level
        # `events` structure as team sports on the ESPN public API).
        events = self._fetch_events(sport_path)
        if not events:
            return None

        player_lower = player_name.lower()
        player_parts = player_lower.split()

        for event in events:
            status = event.get("status", {})
            status_type = status.get("type", {})
            state = status_type.get("state", "pre")
            completed = status_type.get("completed", False)
            description = status_type.get("description", state)

            # Postponed / suspended events — skip rather than mislead
            if any(kw in description.lower() for kw in (
                "postponed", "suspended", "cancelled", "canceled",
            )):
                return None

            competitions = event.get("competitions", [{}])
            comp = competitions[0] if competitions else {}
            competitors = comp.get("competitors", [])

            for c in competitors:
                athlete = c.get("athlete", {})
                name = (
                    athlete.get("displayName", "")
                    or athlete.get("shortName", "")
                )
                if not name:
                    continue

                name_lower = name.lower()
                # Accept if one is a substring of the other (handles truncated
                # display names), OR if most name parts overlap.
                match = (
                    player_lower in name_lower
                    or name_lower in player_lower
                    or sum(1 for p in player_parts if p in name_lower)
                    >= max(1, len(player_parts) - 1)
                )
                if not match:
                    continue

                c_status = c.get("status", {})
                won = c_status.get("won", False)
                position_text = c_status.get("position", {}).get("displayText", "")

                if completed:
                    prob = 1.0 if (won or position_text == "1") else 0.0
                    return GroundTruthResult(
                        ground_truth_prob=prob,
                        confidence=0.95,
                        source_type=SourceType.HARD,
                        source_name=f"ESPN/{sport_path}",
                        source_url=f"{_ESPN_BASE}/{sport_path}/scoreboard",
                        raw_data={
                            "event": event.get("name", ""),
                            "player": name,
                            "won": won,
                            "position": position_text,
                            "state": state,
                        },
                        reasoning=(
                            f"Tournament FINAL. {name}: won={won}, "
                            f"position={position_text!r}. "
                            f"Market {'resolves YES' if prob == 1.0 else 'resolves NO'}."
                        ),
                    )
                else:
                    # In-progress golf — positions shift too much per hole;
                    # wait for completion rather than trading mid-round.
                    logger.debug(
                        "SportsSource: golf tournament in progress (state=%s) "
                        "for %s — waiting for final result",
                        state, market.market_id,
                    )
                    return None

        logger.debug(
            "SportsSource: player '%s' not found in ESPN golf leaderboard for %s",
            player_name, market.market_id,
        )
        return None

    # ── Golf leaderboard helpers ───────────────────────────────────────────────

    def _extract_golf_player(self, question: str) -> Optional[str]:
        """Extract player name from 'Will [Player] win [Tournament]?' style question.

        Handles both:
          "Tournament Name: Will Player Name win?"
          "Will Player Name win the Tournament Name?"
        """
        # Strip a leading "Tournament: " prefix if present
        if ":" in question:
            question = question.split(":", 1)[1].strip()
        m = re.search(r"\bWill\s+(.+?)\s+win\b", question, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            if 2 <= len(name) <= 45:
                return name
        return None

    def _fetch_golf_result(
        self, market: Market, sport_path: str
    ) -> Optional[GroundTruthResult]:
        """Resolve a player-wins-tournament market via the ESPN golf leaderboard.

        ESPN golf scoreboard uses the same /scoreboard endpoint but competitors
        are individual players with 'athlete.displayName' and 'status.position'.

        prob=1.0  Tournament FINAL and player won (position=="1" or won==True)
        prob=0.0  Tournament FINAL and player did NOT win
        None      Tournament in-progress or pre-tournament — wait for final result
        """
        player_name = self._extract_golf_player(market.question)
        if not player_name:
            logger.debug(
                "SportsSource(golf): could not extract player name from '%s'",
                market.question,
            )
            return None

        events = self._fetch_events(sport_path)
        if not events:
            return None

        player_lower = player_name.lower()

        for event in events:
            status = event.get("status", {})
            status_type = status.get("type", {})
            state = status_type.get("state", "pre")
            completed = status_type.get("completed", False)
            description = status_type.get("description", state)

            if any(kw in description.lower() for kw in (
                "postponed", "suspended", "cancelled", "canceled"
            )):
                return None

            competitions = event.get("competitions", [{}])
            comp = competitions[0] if competitions else {}
            competitors = comp.get("competitors", [])

            for c in competitors:
                athlete = c.get("athlete", {})
                name = (
                    athlete.get("displayName", "")
                    or athlete.get("shortName", "")
                )
                if not name:
                    continue

                name_lower = name.lower()
                # Accept if either name string is a substring of the other,
                # OR if all parts of the shorter name appear in the longer.
                if player_lower not in name_lower and name_lower not in player_lower:
                    player_parts = [p for p in player_lower.split() if len(p) > 1]
                    matched_parts = sum(1 for p in player_parts if p in name_lower)
                    if matched_parts < max(1, len(player_parts) - 1):
                        continue

                c_status = c.get("status", {})
                won = c_status.get("won", False)
                position = c_status.get("position", {}).get("displayText", "")

                if completed:
                    prob = 1.0 if (won or position == "1") else 0.0
                    return GroundTruthResult(
                        ground_truth_prob=prob,
                        confidence=0.95,
                        source_type=SourceType.HARD,
                        source_name=f"ESPN/{sport_path}",
                        source_url=f"{_ESPN_BASE}/{sport_path}/scoreboard",
                        raw_data={
                            "event": event.get("name", ""),
                            "player": name,
                            "won": won,
                            "position": position,
                            "state": state,
                        },
                        reasoning=(
                            f"Tournament FINAL. {name}: "
                            f"won={won}, position={position!r}. "
                            f"Market {'resolves YES' if prob == 1.0 else 'resolves NO'}."
                        ),
                    )

                # In-progress golf is too volatile to trade mid-round.
                logger.debug(
                    "SportsSource(golf): %s tournament in progress, waiting for final",
                    market.market_id,
                )
                return None

        logger.debug(
            "SportsSource(golf): player '%s' not found in ESPN leaderboard for %s",
            player_name, market.market_id,
        )
        return None
