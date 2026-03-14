"""
data.sports.live_source – DataSource adapter that feeds live sports shocks
into the GroundTruthRouter pipeline.

SportsLiveSource is registered in the router BEFORE SportsDataSource so that
shock signals (high confidence, fast) shadow the slower final-only ESPN results
for in-progress markets.

Activation criteria (all must be true):
  1. Market category is "sports" (or tagged as sports)
  2. Market matches at least one in-progress game (via MarketMatcher)
  3. ShockDetector has a fresh signal for that game OR game is in the final
     period with a large lead (prob > 0.85) — non-shock late-game signal

The signal flows into the existing gap detector, confidence gate, and executor
unchanged. Sports signals are just another ground truth source.

Returns tradeable=False for:
  - Scheduled (pre-game) markets
  - Early-period in-progress markets with no shock and prob <= 0.85
  - Markets that fail team resolution (MarketMatcher returns None)
  - Stale ESPN data

Timing budget: this entire path must execute in < 200ms. ESPN data is pre-cached
by LiveGameMonitor.refresh_if_stale() called at cycle start, so this module
performs only in-memory lookups and probability computations.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, TYPE_CHECKING

from data.markets.base import Market
from data.ground_truth.base import DataSource, GroundTruthResult, SourceType
from .live_game_monitor import get_active_snapshots, is_sport_stale
from .market_matcher import match_market
from .shock_detector import get_cached_shock
from .win_probability import compute_win_probability

if TYPE_CHECKING:
    from config import SignalTestSettings

logger = logging.getLogger(__name__)

# Non-shock late-game signal threshold — trade if prob is this lopsided
_LATE_GAME_PROB_THRESHOLD = 0.85

# Categories and tags that identify sports markets
_SPORTS_CATEGORIES = {"sports", "sport"}
_SPORTS_TAGS = {
    "sports", "nfl", "nba", "ncaab", "ncaa", "basketball", "football",
    "baseball", "hockey", "mls", "soccer",
}


class SportsLiveSource(DataSource):
    """
    Ground-truth source powered by real-time ESPN polling + shock detection.

    Designed to be prepended to the GroundTruthRouter source list so it runs
    before the slower SportsDataSource for in-progress game markets.

    In signal test mode, individual sports sub-signals (shock / staleness /
    panic / resolution) can be activated or suppressed via signal_test config.
    The router serves shock (and late-game) signals; staleness / panic /
    resolution are co-ordinated by pipeline.py but respect the same config.
    """

    def __init__(self, signal_test: Optional["SignalTestSettings"] = None) -> None:
        self._signal_test = signal_test

    def _sub_signal_active(self, sub: str) -> bool:
        """Return True if the given sports sub-signal should run."""
        st = self._signal_test
        if st is None or not st.enabled:
            return True
        return st.is_signal_active(sub)

    def can_handle(self, market: Market) -> bool:
        """Fast in-memory check — no I/O."""
        if market.category.lower() in _SPORTS_CATEGORIES:
            return True
        tags_lower = {t.lower() for t in market.tags}
        if tags_lower & _SPORTS_TAGS:
            return True
        # Kalshi sports ticker prefixes
        mid = market.market_id.upper()
        return any(mid.startswith(p) for p in (
            "KXNBA", "KXNFL", "KXNCAAMBGAME",
        ))

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        """
        Attempt to match this market to an in-progress game and return a
        shock-driven or late-game ground-truth result.

        Returns None if:
          - Market can't be matched to an active game
          - Data is stale
          - Game is not in final period and no shock signal exists
        """
        t0 = time.monotonic()

        # Detect sport hint from market text
        sport_hint = self._detect_sport_hint(market)

        # Match the market title to a game
        match = match_market(market.market_id, market.question, sport_hint)
        if match is None:
            logger.debug(
                "SportsLiveSource: no team match for %s — skipping",
                market.market_id,
            )
            return None

        sport = match["sport"]

        # Check if the sport's data is stale
        if is_sport_stale(sport):
            logger.debug(
                "SportsLiveSource: stale ESPN data for %s — not trading", sport
            )
            return None

        # Find the matching in-progress game snapshot
        snapshot = self._find_game_snapshot(match, sport)
        if snapshot is None:
            logger.debug(
                "SportsLiveSource: no active game snapshot for %s — market may be pre-game",
                market.market_id,
            )
            return None

        # Check for a cached shock signal first (suppressed in test mode if
        # sports_shock is not in active_signals)
        if self._sub_signal_active("sports_shock"):
            shock = get_cached_shock(snapshot.game_id)
            if shock is not None and shock.confidence >= 0.85:
                prob = self._directional_prob(shock.prob_after, match, snapshot)
                elapsed_ms = (time.monotonic() - t0) * 1000
                logger.info(
                    "SportsLiveSource: shock signal for %s | prob=%.3f conf=%.2f "
                    "shock=%.2f trigger=%r | elapsed=%.1fms",
                    market.market_id, prob, shock.confidence,
                    shock.shock_magnitude, shock.trigger_event, elapsed_ms,
                )
                return GroundTruthResult(
                    ground_truth_prob=prob,
                    confidence=shock.confidence,
                    source_type=SourceType.HARD,
                    source_name="SportsLiveSource/Shock",
                    source_url="https://site.api.espn.com/apis/site/v2/sports",
                    raw_data={
                        "game_id": snapshot.game_id,
                        "sport": sport,
                        "home_team": snapshot.home_team,
                        "away_team": snapshot.away_team,
                        "prob_before": shock.prob_before,
                        "prob_after": shock.prob_after,
                        "shock_magnitude": shock.shock_magnitude,
                        "trigger_event": shock.trigger_event,
                        "seconds_remaining": shock.seconds_remaining,
                        "signal_type": "sports_shock",
                        "sub_signal": "sports_shock",
                    },
                    reasoning=(
                        f"SHOCK signal: {sport.upper()} {snapshot.home_team} vs "
                        f"{snapshot.away_team} | prob {shock.prob_before:.2f}→"
                        f"{shock.prob_after:.2f} (Δ{shock.shock_magnitude:.2f}) | "
                        f"trigger={shock.trigger_event!r} | "
                        f"{shock.seconds_remaining:.0f}s remaining"
                    ),
                )
        else:
            logger.debug(
                "SportsLiveSource: sports_shock suppressed in test mode for %s",
                market.market_id,
            )

        # No shock — try non-shock late-game signal (also under sports_shock gate)
        if not self._sub_signal_active("sports_shock"):
            return None
        result = self._late_game_signal(market, snapshot, match, sport, t0)
        return result

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _detect_sport_hint(self, market: Market) -> Optional[str]:
        text = (
            market.question + " " + " ".join(market.tags) + " " + market.market_id
        ).lower()
        if "nfl" in text or "football" in text:
            return "nfl"
        if "nba" in text or "basketball" in text:
            return "nba"
        if "ncaab" in text or "college basketball" in text or "ncaa basketball" in text or "kxncaambgame" in text:
            return "ncaab"
        return None

    def _find_game_snapshot(self, match: dict, sport: str):
        """Find the active snapshot whose teams best match the market's teams."""
        home_canonical = match["home_team"].lower()
        away_canonical = match["away_team"].lower()

        best = None
        best_score = 0
        for snap in get_active_snapshots():
            if snap.sport != sport:
                continue
            snap_home = snap.home_team.lower()
            snap_away = snap.away_team.lower()
            score = 0
            # Substring matching is sufficient — canonical names are long and unique
            if home_canonical in snap_home or snap_home in home_canonical:
                score += 2
            if away_canonical in snap_away or snap_away in away_canonical:
                score += 2
            # Partial team name match (city or nickname)
            for part in home_canonical.split():
                if len(part) > 3 and part in snap_home:
                    score += 1
            for part in away_canonical.split():
                if len(part) > 3 and part in snap_away:
                    score += 1
            if score > best_score:
                best_score = score
                best = snap

        if best and best_score >= 2:
            return best
        return None

    def _directional_prob(self, home_prob: float, match: dict, snapshot) -> float:
        """
        Convert home-team probability to market-facing YES probability.

        The market may be asking about the home OR away team winning.
        """
        market_team = match["market_team"].lower()
        snap_home = snapshot.home_team.lower()

        # Is the market asking about the home team?
        market_is_home = (
            market_team in snap_home
            or snap_home in market_team
            or any(p in snap_home for p in market_team.split() if len(p) > 3)
        )

        direction = match.get("direction", "win")
        if direction == "lose":
            home_prob = 1.0 - home_prob

        return home_prob if market_is_home else (1.0 - home_prob)

    def _late_game_signal(
        self, market: Market, snapshot, match: dict, sport: str, t0: float
    ) -> Optional[GroundTruthResult]:
        """
        Non-shock late-game signal: final period with a large lead (prob > 0.85).
        """
        current = snapshot.current_state
        try:
            prob_home = compute_win_probability(sport, current)
        except Exception as exc:
            logger.debug("SportsLiveSource: probability error: %s", exc)
            return None

        # Only fire if clearly lopsided
        if prob_home <= _LATE_GAME_PROB_THRESHOLD and prob_home >= (1.0 - _LATE_GAME_PROB_THRESHOLD):
            return None

        # Must be in final period
        from .shock_detector import _in_final_period, _seconds_remaining  # local import to avoid circularity
        if not _in_final_period(sport, current):
            return None

        prob = self._directional_prob(prob_home, match, snapshot)
        secs = _seconds_remaining(sport, current)

        # Confidence: same scale as shock detector's late-game tiers
        confidence = 0.85 if secs < 300 else 0.78

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "SportsLiveSource: late-game signal for %s | home_prob=%.3f "
            "market_prob=%.3f conf=%.2f secs=%.0f | elapsed=%.1fms",
            market.market_id, prob_home, prob, confidence, secs, elapsed_ms,
        )

        if confidence < 0.85:
            return None  # below trade gate — don't return to router

        return GroundTruthResult(
            ground_truth_prob=prob,
            confidence=confidence,
            source_type=SourceType.HARD,
            source_name="SportsLiveSource/LateGame",
            source_url="https://site.api.espn.com/apis/site/v2/sports",
            raw_data={
                "game_id": snapshot.game_id,
                "sport": sport,
                "home_team": snapshot.home_team,
                "away_team": snapshot.away_team,
                "home_prob": prob_home,
                "seconds_remaining": secs,
                "signal_type": "late_game",
            },
            reasoning=(
                f"Late-game signal: {sport.upper()} {snapshot.home_team} vs "
                f"{snapshot.away_team} | home_prob={prob_home:.2f} | "
                f"{secs:.0f}s remaining in final period"
            ),
        )
