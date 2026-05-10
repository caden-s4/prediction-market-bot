"""Tests for strategies.weather_peak_snipe.

Covers:
- Series prefix matcher: KXHIGH<CITY> + KXHIGHT<CITY> + KXLOWT<CITY>
- Trigger window gating (local hour vs peak_hour + 1)
- Monotonic-trend trigger: positive fire, bounce-tolerance, bounce-violation,
  insufficient duration, not past peak
- Bracket parsing from yes_sub_title (range / above / below)
- Bracket-distance signal generation: winner + adjacents, price gates
- Per-event 6-contract cap
- Per-day dedup
- Ghost-only refusal when dry_run=False
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import List, Tuple

from data.markets.base import Market
from strategies import weather_peak_snipe as wps


# ── helpers ───────────────────────────────────────────────────────────────────

def _mk_market(
    ticker: str,
    subtitle: str,
    yes_ask: float = 0.50,
) -> Market:
    m = Market(
        market_id=ticker,
        platform="kalshi",
        question=f"Test {ticker} {subtitle}",
        category="weather",
        tags=["weather"],
        resolution_date=datetime(2026, 5, 10, 5, 0, tzinfo=timezone.utc),
        yes_price=yes_ask,
        no_price=1.0 - yes_ask,
        raw={"subtitle": subtitle},
    )
    m.yes_ask = yes_ask
    m.yes_bid = max(0.0, yes_ask - 0.01)
    return m


def _series_brackets(
    series_prefix: str,
    event_date: str,
    bands: List[Tuple[str, float]],
) -> List[Market]:
    """Build a list of brackets for a single (series, event_date)."""
    out: List[Market] = []
    for i, (subtitle, yes_ask) in enumerate(bands):
        ticker = f"{series_prefix}-{event_date}-B{i:02d}"
        out.append(_mk_market(ticker, subtitle, yes_ask=yes_ask))
    return out


def _hour_obs(start_utc: datetime, temps: List[float]) -> List[Tuple[datetime, float]]:
    """Return one observation per hour starting at start_utc."""
    return [(start_utc + timedelta(hours=i), t) for i, t in enumerate(temps)]


def setup_function(_fn) -> None:
    wps._clear_dedup_for_test()


# ── series matcher ────────────────────────────────────────────────────────────

def test_match_series_old_high_form():
    out = wps._match_series("KXHIGHNY-26MAY09-B75.5")
    assert out is not None
    direction, cfg, event_date = out
    assert direction == "high"
    assert cfg.city_code == "NYC"
    assert cfg.asos_station == "NYC"
    assert event_date == "26MAY09"


def test_match_series_new_high_form_unsupported_city_returns_none():
    # Phase 14b cities are NY/CHI/MIA/DEN — KXHIGHTBOS exists in the universe
    # but is not in our 4-city set, so it should return None.
    assert wps._match_series("KXHIGHTBOS-26MAY09-B40.5") is None


def test_match_series_low_form():
    out = wps._match_series("KXLOWTNYC-26MAY09-B25")
    assert out is not None
    direction, cfg, event_date = out
    assert direction == "low"
    assert cfg.city_code == "NYC"


def test_match_series_unparseable():
    assert wps._match_series("KXNFLGAME-26MAY09-XYZ") is None
    assert wps._match_series("not a ticker") is None


def test_is_peak_snipe_candidate():
    assert wps.is_peak_snipe_candidate("KXHIGHNY-26MAY09-B75")
    assert wps.is_peak_snipe_candidate("KXHIGHCHI-26MAY09-B40")
    assert wps.is_peak_snipe_candidate("KXHIGHMIA-26MAY09-B85")
    assert wps.is_peak_snipe_candidate("KXHIGHDEN-26MAY09-B65")
    assert wps.is_peak_snipe_candidate("KXLOWTNYC-26MAY09-B30")
    assert not wps.is_peak_snipe_candidate("KXHIGHTBOS-26MAY09-B40")
    assert not wps.is_peak_snipe_candidate("KXNBAGAME-26MAY09-NYK")


# ── bracket parsing ───────────────────────────────────────────────────────────

def test_parse_bracket_range():
    m = _mk_market("KXHIGHNY-26MAY09-B75.5", "75° to 76°", yes_ask=0.30)
    b = wps._parse_bracket(m)
    assert b is not None
    assert b.low == 75 and b.high == 76
    assert b.contains(75) and b.contains(76) and not b.contains(77)


def test_parse_bracket_above():
    m = _mk_market("KXHIGHNY-26MAY09-T80", "80° or above", yes_ask=0.10)
    b = wps._parse_bracket(m)
    assert b is not None
    assert b.low == 80 and b.high is None
    assert b.contains(80) and b.contains(95) and not b.contains(79)


def test_parse_bracket_below():
    m = _mk_market("KXLOWTNYC-26MAY09-T25", "25° or below", yes_ask=0.10)
    b = wps._parse_bracket(m)
    assert b is not None
    assert b.low is None and b.high == 25
    assert b.contains(25) and b.contains(0) and not b.contains(26)


def test_parse_bracket_unparseable_returns_none():
    m = _mk_market("KXHIGHNY-26MAY09-B70.5", "weather report", yes_ask=0.50)
    assert wps._parse_bracket(m) is None


# ── trigger evaluation ────────────────────────────────────────────────────────

def test_evaluate_trigger_high_fires_on_clear_decline():
    """Peak at index 0 (12:00 UTC, 80°F), then 4h of decline 79→78→77→76°F.
    Latest obs is 4°F below peak with 4h elapsed → fires.
    """
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.0])
    out = wps._evaluate_trigger(obs, "high", obs[-1][0])
    assert out.fired is True
    assert out.observed_temp_f == 76.0


def test_evaluate_trigger_high_blocked_by_rebound():
    # Peak 80, declines to 76, but then bounces to 78 (>1°F above post-peak min 76).
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 76.0, 78.0])
    out = wps._evaluate_trigger(obs, "high", obs[-1][0])
    assert out.fired is False
    assert "rebound" in out.reason


def test_evaluate_trigger_high_tolerates_small_bounce():
    # Peak 80, declines to 76, bounces to 76.5 (≤1°F tolerance) — should fire.
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 76.0, 76.5])
    out = wps._evaluate_trigger(obs, "high", obs[-1][0])
    assert out.fired is True


def test_evaluate_trigger_high_blocked_too_recent():
    # Peak set at the latest observation — elapsed=0 → not past peak.
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [76.0, 77.0, 78.0, 79.0, 80.0])
    out = wps._evaluate_trigger(obs, "high", obs[-1][0])
    assert out.fired is False
    assert "not_past_peak" in out.reason or "ext_too_recent" in out.reason


def test_evaluate_trigger_high_blocked_not_past_peak():
    # Peak 80 at index 0, latest obs 79.5 — only 0.5°F decline (< 1°F).
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.5, 79.5, 79.5])
    out = wps._evaluate_trigger(obs, "high", obs[-1][0])
    assert out.fired is False
    assert "not_past_peak" in out.reason


def test_evaluate_trigger_low_fires_on_clear_incline():
    """Trough at index 0 (06:00 UTC, 30°F), then 4h of incline 31→32→33→34°F."""
    base = datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)
    obs = _hour_obs(base, [30.0, 31.0, 32.0, 33.0, 34.0])
    out = wps._evaluate_trigger(obs, "low", obs[-1][0])
    assert out.fired is True
    assert out.observed_temp_f == 34.0


def test_evaluate_trigger_low_blocked_by_downward_bounce():
    base = datetime(2026, 5, 9, 6, 0, tzinfo=timezone.utc)
    # Trough 30, climbs to 34, then drops to 32 (>1°F below post-trough max 34).
    obs = _hour_obs(base, [30.0, 31.0, 32.0, 34.0, 32.0])
    out = wps._evaluate_trigger(obs, "low", obs[-1][0])
    assert out.fired is False
    assert "rebound" in out.reason


# ── end-to-end evaluate_event_signals ─────────────────────────────────────────

def _stub_fetcher(obs: List[Tuple[datetime, float]]):
    def fake(station: str, lookback_hours: int):
        return list(obs)
    return fake


def test_evaluate_event_signals_winner_yes_and_adjacent_no():
    """Trigger fires; observed temp 76°F lands in [76,76]; ±1 / ±2 brackets are
    cheap NO and one is rich YES adjacent. Verify selection."""
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)  # 15:30 ET (post-peak)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    fetcher = _stub_fetcher(obs)

    bands = [
        ("73° to 73°", 0.05),  # adj-3 (out of band)
        ("74° to 74°", 0.05),  # adj-2 (NO target, gate ≤ 0.15)
        ("75° to 75°", 0.10),  # adj-1 (NO target)
        ("76° to 76°", 0.92),  # WINNER (YES target, gate ≥ 0.85)
        ("77° to 77°", 0.05),  # adj+1 (NO target)
        ("78° to 78°", 0.05),  # adj+2 (NO target)
        ("79° to 79°", 0.05),  # adj+3 (out of band)
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)

    sigs = wps.evaluate_event_signals(
        markets,
        now_utc=now_utc,
        asos_fetcher=fetcher,
        dry_run=True,
    )
    actions = {s.market_id.split("-")[-1]: s.action for s in sigs}
    # Winner B03, adjacents B01/B02/B04/B05 — all should produce signals.
    assert actions.get("B03") == "buy_yes"
    assert actions.get("B01") == "buy_no"
    assert actions.get("B02") == "buy_no"
    assert actions.get("B04") == "buy_no"
    assert actions.get("B05") == "buy_no"
    # B00 / B06 are out of ±2 band → no signal.
    assert "B00" not in actions
    assert "B06" not in actions
    # Cap: 6 contracts max → ≤6 signals
    assert len(sigs) <= 6
    # Every signal carries the signal_class + max_risk_usd fields the
    # executor will read via getattr.
    assert all(s.signal_class == wps.SIGNAL_CLASS for s in sigs)
    assert all(s.max_risk_usd == wps.PER_BRACKET_MAX_RISK_USD for s in sigs)


def test_evaluate_event_signals_winner_priced_too_low_skipped():
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    fetcher = _stub_fetcher(obs)
    bands = [
        ("75° to 75°", 0.05),
        ("76° to 76°", 0.50),  # winner but yes_ask=0.50 fails 0.85 gate
        ("77° to 77°", 0.05),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)
    sigs = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=True,
    )
    # No winner signal; only adjacents.
    actions = {s.market_id.split("-")[-1]: s.action for s in sigs}
    assert "B01" not in actions  # winner skipped
    assert actions.get("B00") == "buy_no"
    assert actions.get("B02") == "buy_no"


def test_evaluate_event_signals_outside_window_returns_empty():
    # 13:30 ET = before 15:00 trigger window for highs.
    now_utc = datetime(2026, 5, 9, 17, 30, tzinfo=timezone.utc)
    fetcher = _stub_fetcher([])
    markets = _series_brackets(
        "KXHIGHNY", "26MAY09", [("76° to 76°", 0.92)],
    )
    sigs = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=True,
    )
    assert sigs == []


def test_evaluate_event_signals_dry_run_false_returns_empty():
    # Even with all conditions met, dry_run=False must return [].
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.0])
    fetcher = _stub_fetcher(obs)
    markets = _series_brackets(
        "KXHIGHNY", "26MAY09", [("76° to 76°", 0.92)],
    )
    sigs = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=False,
    )
    assert sigs == []


def test_evaluate_event_signals_dedup_within_day():
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.0])
    fetcher = _stub_fetcher(obs)
    markets = _series_brackets("KXHIGHNY", "26MAY09", [
        ("75° to 75°", 0.05),
        ("76° to 76°", 0.92),
        ("77° to 77°", 0.05),
    ])
    sigs1 = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=True,
    )
    assert len(sigs1) > 0
    # Second call same day → dedup blocks all the markets we just signaled.
    sigs2 = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=True,
    )
    assert sigs2 == []


def test_evaluate_event_signals_no_asos_returns_empty():
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)

    def fetcher(station: str, lookback_hours: int):
        return None

    markets = _series_brackets(
        "KXHIGHNY", "26MAY09", [("76° to 76°", 0.92)],
    )
    sigs = wps.evaluate_event_signals(
        markets, now_utc=now_utc, asos_fetcher=fetcher, dry_run=True,
    )
    assert sigs == []


# ── group_markets_by_event ────────────────────────────────────────────────────

def test_group_markets_by_event():
    bands = [("75° to 75°", 0.05), ("76° to 76°", 0.92)]
    ny_a = _series_brackets("KXHIGHNY", "26MAY09", bands)
    ny_b = _series_brackets("KXHIGHNY", "26MAY10", bands)
    chi = _series_brackets("KXHIGHCHI", "26MAY09", bands)
    other = [_mk_market("KXNBAGAME-26MAY09-NYK", "irrelevant", yes_ask=0.50)]
    groups = wps.group_markets_by_event(ny_a + ny_b + chi + other)
    assert "KXHIGHNY-26MAY09" in groups
    assert "KXHIGHNY-26MAY10" in groups
    assert "KXHIGHCHI-26MAY09" in groups
    assert len(groups) == 3
    assert all(len(v) == 2 for v in groups.values())


# ── observability: gate_events ────────────────────────────────────────────────

def _capture_gate_events(monkeypatch):
    """Patch log_gate_event in strategies.weather_peak_snipe and return a list
    that accumulates each call's kwargs."""
    captured: List[dict] = []

    def fake(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(wps, "log_gate_event", fake)
    return captured


def test_evaluated_event_emitted_when_trigger_fires(monkeypatch):
    captured = _capture_gate_events(monkeypatch)
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    bands = [
        ("75° to 75°", 0.05),
        ("76° to 76°", 0.92),
        ("77° to 77°", 0.05),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)
    wps.evaluate_event_signals(
        markets, now_utc=now_utc,
        asos_fetcher=_stub_fetcher(obs), dry_run=True,
    )
    evaluated = [e for e in captured if e.get("decision") == "evaluated"]
    assert len(evaluated) == 1
    e = evaluated[0]
    assert e["gate"] == "snipe"
    assert e["reason"] is None
    assert e["extra"]["signal_class"] == "weather_peak_snipe"
    assert e["extra"]["winner_idx"] == 1
    assert e["extra"]["obs_temp_f"] == 76.0
    assert e["extra"]["ticker"].endswith("-B01")
    assert "trigger_time_utc" in e["extra"]


def test_price_gate_skip_event_records_bracket_prices(monkeypatch):
    captured = _capture_gate_events(monkeypatch)
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    # All brackets fail price gates: winner=0.50 (<0.85), adjacents=0.50 (>0.15).
    bands = [
        ("74° to 74°", 0.50),
        ("75° to 75°", 0.50),
        ("76° to 76°", 0.50),  # winner — too cheap
        ("77° to 77°", 0.50),
        ("78° to 78°", 0.50),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)
    sigs = wps.evaluate_event_signals(
        markets, now_utc=now_utc,
        asos_fetcher=_stub_fetcher(obs), dry_run=True,
    )
    assert sigs == []
    skips = [e for e in captured if e.get("reason") == "price_gate"]
    assert len(skips) == 1
    e = skips[0]
    assert e["gate"] == "snipe"
    assert e["decision"] == "skip"
    assert e["extra"]["signal_class"] == "weather_peak_snipe"
    assert e["extra"]["winner_yes_ask"] == 0.50
    # ADJACENT_OFFSETS = (-2, -1, +1, +2) → indices 0, 1, 3, 4.
    assert e["extra"]["adjacent_yes_asks"] == [0.50, 0.50, 0.50, 0.50]
    assert e["extra"]["winner_idx"] == 2
    assert e["extra"]["obs_temp_f"] == 76.0


def test_dedup_suppresses_repeat_evaluated_and_price_gate_events(monkeypatch):
    """Phase 14c: repeat cycles against the same ASOS obs must not re-emit
    `evaluated` or `skip/price_gate` events.  Triggered by re-running the
    full evaluation against the same fetcher (which returns the same obs)
    and the same now_utc."""
    captured = _capture_gate_events(monkeypatch)
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    bands = [
        ("75° to 75°", 0.50),
        ("76° to 76°", 0.50),  # winner — too cheap, will hit price_gate
        ("77° to 77°", 0.50),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)

    # Three consecutive evaluations against the same obs (same trigger_obs_ts).
    for _ in range(3):
        wps.evaluate_event_signals(
            markets, now_utc=now_utc,
            asos_fetcher=_stub_fetcher(obs), dry_run=True,
        )

    evaluated = [e for e in captured if e.get("decision") == "evaluated"]
    skipped = [e for e in captured if e.get("reason") == "price_gate"]
    assert len(evaluated) == 1, f"expected 1 evaluated event, got {len(evaluated)}"
    assert len(skipped) == 1, f"expected 1 price_gate skip event, got {len(skipped)}"


def test_dedup_re_emits_on_new_obs_timestamp(monkeypatch):
    """A fresh ASOS observation (different cur_ts) is a new trigger and must
    log again, even within the same process."""
    captured = _capture_gate_events(monkeypatch)
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs_first = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    # Same shape but extra hourly obs appended → cur_ts changes.
    obs_second = obs_first + [(base + timedelta(hours=8), 75.5)]
    bands = [
        ("75° to 75°", 0.50),
        ("76° to 76°", 0.50),
        ("77° to 77°", 0.50),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)

    wps.evaluate_event_signals(
        markets, now_utc=now_utc,
        asos_fetcher=_stub_fetcher(obs_first), dry_run=True,
    )
    wps.evaluate_event_signals(
        markets, now_utc=now_utc,
        asos_fetcher=_stub_fetcher(obs_second), dry_run=True,
    )

    evaluated = [e for e in captured if e.get("decision") == "evaluated"]
    skipped = [e for e in captured if e.get("reason") == "price_gate"]
    assert len(evaluated) == 2
    assert len(skipped) == 2


def test_price_gate_skip_event_handles_missing_adjacent(monkeypatch):
    """If the winner is at an end of the bracket grid, off-array adjacents
    are recorded as None, not omitted."""
    captured = _capture_gate_events(monkeypatch)
    base = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    now_utc = datetime(2026, 5, 9, 19, 30, tzinfo=timezone.utc)
    obs = _hour_obs(base, [80.0, 79.0, 78.0, 77.0, 76.5, 76.0, 76.0, 76.0])
    bands = [
        ("76° to 76°", 0.50),  # winner at idx 0; idx -2 and -1 are off-array
        ("77° to 77°", 0.50),
        ("78° to 78°", 0.50),
    ]
    markets = _series_brackets("KXHIGHNY", "26MAY09", bands)
    wps.evaluate_event_signals(
        markets, now_utc=now_utc,
        asos_fetcher=_stub_fetcher(obs), dry_run=True,
    )
    skips = [e for e in captured if e.get("reason") == "price_gate"]
    assert len(skips) == 1
    # winner_idx = 0; offsets (-2, -1, +1, +2) → indices (-2, -1, 1, 2).
    # Only +1 and +2 are valid; -2 and -1 are out of bounds → None.
    assert skips[0]["extra"]["adjacent_yes_asks"] == [None, None, 0.50, 0.50]


# ── contract cap ──────────────────────────────────────────────────────────────

def test_enforce_contract_cap_truncates_to_max():
    # Construct 8 dummy signals; cap should truncate to 6.
    base_kwargs = dict(
        action="buy_yes", target_price=0.90,
        confidence=wps.SNIPE_CONFIDENCE, rationale="test",
    )
    sigs = [
        wps.WeatherPeakSnipeSignal(
            market_id=f"KXHIGHNY-26MAY09-B{i:02d}",
            edge=0.10 - 0.001 * i, **base_kwargs,
        )
        for i in range(8)
    ]
    capped = wps._enforce_contract_cap(sigs)
    assert len(capped) == wps.PER_EVENT_MAX_CONTRACTS
    # Highest-edge signals retained.
    assert capped[0].market_id == "KXHIGHNY-26MAY09-B00"
