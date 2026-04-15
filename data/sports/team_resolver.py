"""
data.sports.team_resolver — shared YES-team resolution for game markets.

Single source of truth for: given a Kalshi game market ID, which team's WIN
resolves the market YES?

Used at both entry time (SportsDataSource) and exit time (ResolutionDetector /
executor) so the home/away settlement logic is consistent.  The detector emits
the winning team's name; the executor calls get_yes_team() to compute correct_prob.
This means correct_prob is never derived from "winner == home" shortcuts that
assume the YES contract belongs to the home team.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def get_yes_team(market_id: str) -> Optional[str]:
    """
    Return the lowercase canonical team name that resolves YES for this market.

    For KXNBAGAME / KXNCAAMBGAME / KXNFLGAME / KXNCAAWBGAME markets the YES
    team is encoded in the market_id suffix (e.g. KXNBAGAME-26APR14MIACHA-MIA
    → "miami heat").  Returns None if the market_id cannot be parsed or the
    abbreviation is unknown.

    Pure in-memory lookup — no I/O, safe to call from background threads.
    """
    from data.sports.market_matcher import match_market  # noqa: PLC0415
    result = match_market(market_id, "", None)
    if result and result.get("market_team"):
        return result["market_team"].lower()
    return None
