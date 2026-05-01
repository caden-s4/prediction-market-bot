"""Tests for resolution.scanner weather-snipe dispatch hook."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from data.markets.base import Market
from resolution import scanner as scanner_mod
from resolution.scanner import (
    _dispatch_weather_snipe,
    _is_weather_snipe_candidate,
)
from strategies.weather_snipe import SnipeSignal


def _mk_market(market_id: str, hours_ahead: float = 0.5) -> Market:
    rd = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return Market(
        market_id=market_id,
        platform="kalshi",
        question="Will the high in PHX be > 80F?",
        category="weather",
        tags=[],
        resolution_date=rd,
        yes_price=0.5,
        no_price=0.5,
    )


def test_candidate_within_window_weather_prefix():
    m = _mk_market("KXHIGHTPHX-26APR30-T80", hours_ahead=0.5)
    assert _is_weather_snipe_candidate(m) is True


def test_candidate_empty_market_id():
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    m.market_id = ""
    assert _is_weather_snipe_candidate(m) is False


def test_candidate_already_closed():
    m = _mk_market("KXHIGHTPHX-26APR30-T80", hours_ahead=-0.1)
    assert _is_weather_snipe_candidate(m) is False


def test_candidate_too_far_out():
    m = _mk_market("KXHIGHTPHX-26APR30-T80", hours_ahead=2.0)
    assert _is_weather_snipe_candidate(m) is False


def test_candidate_non_weather_prefix():
    m = _mk_market("KXNBAGAME-26APR30LALDET-LAL", hours_ahead=0.5)
    assert _is_weather_snipe_candidate(m) is False


def test_candidate_naive_resolution_date_treated_as_utc():
    rd = datetime.utcnow() + timedelta(minutes=30)  # tzinfo=None
    m = Market(
        market_id="KXLOWTMIA-26APR30-T70",
        platform="kalshi",
        question="?",
        category="weather",
        tags=[],
        resolution_date=rd,
        yes_price=0.5,
        no_price=0.5,
    )
    assert _is_weather_snipe_candidate(m) is True


def test_dispatch_swallows_strategy_exception(monkeypatch, caplog):
    def boom(market, now_utc):
        raise RuntimeError("strategy crashed")
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", boom)
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.ERROR, logger="resolution.scanner"):
        _dispatch_weather_snipe(m)  # must not raise
    assert "WeatherSnipe dispatch failed" in caplog.text
    assert "strategy crashed" in caplog.text


def test_dispatch_logs_signal_on_success(monkeypatch, caplog):
    sig = SnipeSignal(
        market_id="KXHIGHTPHX-26APR30-T80",
        action="buy_yes",
        target_price=0.05,
        edge=0.94,
        confidence=0.99,
        rationale="max=85F, strike >80F, certain YES",
    )
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: sig)
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        _dispatch_weather_snipe(m)
    assert "WeatherSnipe candidate" in caplog.text
    assert "KXHIGHTPHX-26APR30-T80" in caplog.text


def test_dispatch_silent_when_strategy_returns_none(monkeypatch, caplog):
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: None)
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        _dispatch_weather_snipe(m)
    assert "WeatherSnipe candidate" not in caplog.text


def test_dispatch_skipped_for_non_candidate(monkeypatch):
    called = {"n": 0}
    def fake(market, now_utc):
        called["n"] += 1
        return None
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", fake)
    m = _mk_market("KXNBAGAME-26APR30LALDET-LAL")
    _dispatch_weather_snipe(m)
    assert called["n"] == 0


# ── callback path (Part 6) ──────────────────────────────────────────────────

def _ok_signal(market_id="KXHIGHTPHX-26APR30-T80"):
    return SnipeSignal(
        market_id=market_id,
        action="buy_yes",
        target_price=0.05,
        edge=0.94,
        confidence=0.99,
        rationale="max=85F, strike >80F, certain YES",
    )


def test_dispatch_invokes_callback_when_signal_emitted(monkeypatch, caplog):
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: _ok_signal())
    seen = {}
    def cb(market, sig):
        seen["mid"] = market.market_id
        seen["sig"] = sig
        return "ghost_KXHIGHTPHX_123"
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        _dispatch_weather_snipe(m, cb)
    assert seen["mid"] == "KXHIGHTPHX-26APR30-T80"
    assert isinstance(seen["sig"], SnipeSignal)
    assert "WeatherSnipe trade placed" in caplog.text
    assert "ghost_KXHIGHTPHX_123" in caplog.text


def test_dispatch_with_callback_silent_when_gate_blocks(monkeypatch, caplog):
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: _ok_signal())
    def cb(market, sig):
        return None  # placement blocked by a gate (already logged inside)
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        _dispatch_weather_snipe(m, cb)
    assert "WeatherSnipe trade placed" not in caplog.text
    # Also must NOT log the Part-5 candidate-only line when a callback is wired.
    assert "WeatherSnipe candidate" not in caplog.text


def test_dispatch_swallows_callback_exception(monkeypatch, caplog):
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: _ok_signal())
    def cb(market, sig):
        raise RuntimeError("placement crashed")
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.ERROR, logger="resolution.scanner"):
        _dispatch_weather_snipe(m, cb)
    assert "WeatherSnipe placement failed" in caplog.text
    assert "placement crashed" in caplog.text


def test_dispatch_logs_candidate_only_when_callback_is_none(monkeypatch, caplog):
    monkeypatch.setattr(scanner_mod, "evaluate_snipe", lambda m, now: _ok_signal())
    m = _mk_market("KXHIGHTPHX-26APR30-T80")
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        _dispatch_weather_snipe(m, None)
    assert "WeatherSnipe candidate" in caplog.text
