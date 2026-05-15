"""Tests for scanner-level disable of KXAAAGASD / KXAAAGASW (Phase Gas-Disable).

EconomicDataSource routes these prefixes to FRED GASREGCOVW, which is
wrong-instrument GT (regular-conventional vs Kalshi's AAA all-formulations).
The scanner rejects them upstream so they never reach routing, gap detection,
or confidence scoring.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from data.markets.base import Market
from resolution.scanner import ResolutionScanner
from shared.exclusion_list import ExclusionList


def _mk_market(market_id: str, category: str = "economics") -> Market:
    rd = datetime.now(timezone.utc) + timedelta(hours=24)
    return Market(
        market_id=market_id,
        platform="kalshi",
        question="Will national avg gas price be > $3.50?",
        category=category,
        tags=[],
        resolution_date=rd,
        yes_price=0.5,
        no_price=0.5,
    )


def _scanner(tmp_path: Path) -> ResolutionScanner:
    return ResolutionScanner(
        kalshi_client=None,
        poly_client=None,
        exclusions=ExclusionList(path=tmp_path / "exclusions.json"),
    )


def test_kxaaagasd_rejected_with_economic_bracket_disabled(tmp_path):
    s = _scanner(tmp_path)
    m = _mk_market("KXAAAGASD-26MAY15-T3.499")
    assert s._reject_reason(m, window_hours=720) == "economic_bracket_disabled"


def test_kxaaagasw_rejected_with_economic_bracket_disabled(tmp_path):
    s = _scanner(tmp_path)
    m = _mk_market("KXAAAGASW-26MAY18-T3.499")
    assert s._reject_reason(m, window_hours=720) == "economic_bracket_disabled"


def test_non_aaa_economic_market_not_rejected_by_economic_bracket_branch(tmp_path):
    """Control: a non-AAA economic ticker must NOT trip the new reject branch.

    Picks a market_id outside the disabled-prefix list so the new branch is
    skipped. The market is otherwise a valid candidate.
    """
    s = _scanner(tmp_path)
    m = _mk_market("KXCPIYOY-26MAY13-T3.2", category="economics")
    assert s._reject_reason(m, window_hours=720) is None
