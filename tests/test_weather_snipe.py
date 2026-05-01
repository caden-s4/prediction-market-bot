"""Tests for strategies.weather_snipe.

Covers:
- _decide_outcome bracket logic (4 cases)
- evaluate_snipe end-to-end (window, observations, decisive YES/NO, undetermined,
  already-priced, bracket NO)
- CITY_TZ_MAP completeness vs _CITY_TO_CLI
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from data.ground_truth.weather_cli import ASOSDailyExtreme
from data.ground_truth.weather_kalshi import WeatherMarket, _CITY_TO_CLI
from data.ground_truth.weather_timezones import CITY_TZ_MAP
from data.markets.base import Market
from strategies import weather_snipe as ws_mod
from strategies.weather_snipe import (
    SnipeSignal,
    _decide_outcome,
    evaluate_snipe,
)


# ── _decide_outcome bracket logic ─────────────────────────────────────────────

def _bracket_wm(low: float, high: float) -> WeatherMarket:
    return WeatherMarket(
        ticker="KXHIGHTPHX-26APR29-B89.5",
        city="PHX",
        cli_station="PHX",
        target_date=date(2026, 4, 29),
        market_type="high",
        threshold_type="bracket",
        threshold_value=(low + high) / 2,
        bracket_low=low,
        bracket_high=high,
    )


def test_bracket_decisive_yes_within_bounds():
    wm = _bracket_wm(89.0, 90.0)
    assert _decide_outcome(wm, 89.0) == "yes"
    assert _decide_outcome(wm, 90.0) == "yes"


def test_bracket_decisive_no_far_above():
    wm = _bracket_wm(75.0, 76.0)
    assert _decide_outcome(wm, 82.0) == "no"


def test_bracket_decisive_no_just_outside_with_margin():
    wm = _bracket_wm(89.0, 90.0)
    assert _decide_outcome(wm, 88.4) == "no"
    assert _decide_outcome(wm, 90.6) == "no"


def test_bracket_undetermined_inside_no_margin():
    wm = _bracket_wm(89.0, 90.0)
    assert _decide_outcome(wm, 88.5) is None
    assert _decide_outcome(wm, 90.5) is None


# ── evaluate_snipe end-to-end ─────────────────────────────────────────────────

_NOW = datetime(2026, 4, 30, 6, 30, tzinfo=timezone.utc)
_CLOSE_30M = _NOW + timedelta(minutes=30)
_CLOSE_90M = _NOW + timedelta(minutes=90)


def _mk_market(
    ticker: str,
    question: str,
    resolution_date: datetime,
    yes_price: float = 0.50,
    yes_ask: Optional[float] = None,
    yes_bid: Optional[float] = None,
) -> Market:
    m = Market(
        market_id=ticker,
        platform="kalshi",
        question=question,
        category="weather",
        tags=[],
        resolution_date=resolution_date,
        yes_price=yes_price,
        no_price=1.0 - yes_price,
    )
    if yes_ask is not None:
        m.yes_ask = yes_ask
    if yes_bid is not None:
        m.yes_bid = yes_bid
    return m


def _patch_asos(monkeypatch, *, running_max=None, running_min=None, obs_count=12):
    def fake(station, tz_name, now_utc=None):
        return ASOSDailyExtreme(
            station=station,
            local_date=date(2026, 4, 29),
            running_max_f=running_max,
            running_min_f=running_min,
            last_observation_utc=_NOW,
            observation_count=obs_count,
        )
    monkeypatch.setattr(ws_mod, "fetch_asos_running_extreme", fake)


def _patch_asos_none(monkeypatch):
    monkeypatch.setattr(
        ws_mod, "fetch_asos_running_extreme",
        lambda station, tz_name, now_utc=None: None,
    )


def test_evaluate_snipe_outside_window_returns_none(monkeypatch):
    _patch_asos(monkeypatch, running_max=78.0)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_90M,
        yes_ask=0.50, yes_bid=0.49,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_insufficient_observations_returns_none(monkeypatch):
    _patch_asos(monkeypatch, running_max=78.0, obs_count=3)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_no_asos_data_returns_none(monkeypatch):
    _patch_asos_none(monkeypatch)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_decisive_yes_above(monkeypatch):
    _patch_asos(monkeypatch, running_max=78.0)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    sig = evaluate_snipe(market, _NOW)
    assert sig is not None
    assert sig.action == "buy_yes"
    assert sig.target_price == 0.50
    assert sig.confidence >= 0.95
    assert "YES" in sig.rationale


def test_evaluate_snipe_decisive_no_bracket(monkeypatch):
    # Bracket market, threshold_value=75.5 → bracket_low=75, bracket_high=76.
    # running_max=82 is decisively NO (82 > 76 + 0.5 margin).
    _patch_asos(monkeypatch, running_max=82.0)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-B75.5",
        "Will the high in Phoenix land in [75, 76]F?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    sig = evaluate_snipe(market, _NOW)
    assert sig is not None
    assert sig.action == "buy_no"
    # NO target = 1 - yes_bid = 0.51
    assert abs(sig.target_price - 0.51) < 1e-9
    assert sig.confidence >= 0.95
    assert "NO" in sig.rationale


def test_evaluate_snipe_undetermined_returns_none(monkeypatch):
    # running_max=75.5, strike=75, threshold "above". Margin only 0.5°F:
    # 75.5 > 76.0 (strike + 1.0 safety) is False;
    # 75.5 + 1.0 < 75.0 (strike) is False.  Neither side decisive.
    _patch_asos(monkeypatch, running_max=75.5)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_already_priced_yes_returns_none(monkeypatch):
    # Outcome decisive YES, but yes_ask is at 0.99 — no edge left.
    _patch_asos(monkeypatch, running_max=78.0)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.99, yes_bid=0.98,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_already_priced_no_returns_none(monkeypatch):
    # Outcome decisive NO, but yes_bid is at 0.02 — NO already at 0.98, no edge.
    _patch_asos(monkeypatch, running_max=70.0)
    market = _mk_market(
        "KXHIGHTPHX-26APR30-T75",
        "Will the high in Phoenix be > 75F?",
        _CLOSE_30M,
        yes_ask=0.03, yes_bid=0.02,
    )
    assert evaluate_snipe(market, _NOW) is None


def test_evaluate_snipe_unparseable_ticker_returns_none(monkeypatch):
    _patch_asos(monkeypatch, running_max=78.0)
    market = _mk_market(
        "NOT_A_WEATHER_TICKER",
        "Will something happen?",
        _CLOSE_30M,
        yes_ask=0.50, yes_bid=0.49,
    )
    assert evaluate_snipe(market, _NOW) is None


# ── CITY_TZ_MAP completeness ──────────────────────────────────────────────────

def test_city_tz_map_covers_all_cli_cities():
    missing = sorted(set(_CITY_TO_CLI.keys()) - set(CITY_TZ_MAP.keys()))
    assert missing == [], f"CITY_TZ_MAP missing cities: {missing}"


def test_city_tz_map_resolves_with_zoneinfo():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    bad = []
    for city, tz_name in CITY_TZ_MAP.items():
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            bad.append((city, tz_name))
    assert bad == [], f"unresolvable timezones: {bad}"
