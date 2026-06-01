"""Tests for the per-(ticker, signal_source) consecutive-stop_loss perm-skip
guard introduced in Phase Bleed-Fix-1.

Uses lightweight SimpleNamespace stubs bound to ResolutionBot helper methods,
avoiding the full ResolutionBot.__init__ chain (Kalshi client, scanner, GT
router, etc.).  All network calls, market data, and the gate_events writer
are stubbed.
"""
from __future__ import annotations

import types
from typing import Dict, List, Tuple

import pytest

from resolution import executor as executor_mod
from resolution.executor import ResolutionBot, _STOP_LOSS_PERM_SKIP_THRESHOLD


# ── Builders ──────────────────────────────────────────────────────────────────

def _make_rec(source_name: str = "FRED/GASREGCOVW", signal_type: str = "information"):
    """Build a minimal TradeRecord-like object covering only the fields the
    perm-skip helpers read: rec.signal.ground_truth_result.source_name and
    rec.signal.signal_type."""
    gt = types.SimpleNamespace(source_name=source_name)
    sig = types.SimpleNamespace(ground_truth_result=gt, signal_type=signal_type)
    return types.SimpleNamespace(signal=sig)


def _make_bot() -> types.SimpleNamespace:
    """Build a stub bot with the perm-skip dict and helper methods bound."""
    bot = types.SimpleNamespace(_consecutive_stop_losses={})
    bot._signal_source_name = lambda rec: ResolutionBot._signal_source_name(rec)
    bot._update_stop_loss_counter = lambda mid, rec, reason: (
        ResolutionBot._update_stop_loss_counter(bot, mid, rec, reason)
    )
    bot._check_stop_loss_perm_skip = lambda mid, src, plat: (
        ResolutionBot._check_stop_loss_perm_skip(bot, mid, src, plat)
    )
    return bot


@pytest.fixture
def gate_events(monkeypatch) -> List[Dict]:
    """Capture log_gate_event calls instead of writing to disk."""
    captured: List[Dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(executor_mod, "log_gate_event", _capture)
    return captured


# ── Tests ─────────────────────────────────────────────────────────────────────

MID = "KXTRUFGAS-26JUN01-T4.34"
SRC = "FRED/GASREGCOVW"
KEY: Tuple[str, str] = (MID, SRC)


def test_counter_increments_on_stop_loss_exit():
    bot = _make_bot()
    rec = _make_rec(source_name=SRC)
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    assert bot._consecutive_stop_losses[KEY] == 1


def test_counter_increments_on_stop_loss_partial_exit():
    """_partial_exit calls _update_stop_loss_counter with the unsuffixed
    exit_reason ("stop_loss"), not "stop_loss_partial".  Both full and
    partial stop_loss exits feed the same counter."""
    bot = _make_bot()
    rec = _make_rec(source_name=SRC)
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    assert bot._consecutive_stop_losses[KEY] == 2


def test_counter_resets_on_non_stop_loss_exit():
    bot = _make_bot()
    rec = _make_rec(source_name=SRC)
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    assert bot._consecutive_stop_losses[KEY] == 1
    bot._update_stop_loss_counter(MID, rec, "early_exit")
    assert (MID, SRC) not in bot._consecutive_stop_losses


def test_counter_resets_on_successful_entry_without_immediate_stop_loss():
    """A successful entry whose subsequent exit is NOT a stop_loss resets
    the counter — avoiding permanent lockout on a legitimate signal that
    had one bad fill."""
    bot = _make_bot()
    rec = _make_rec(source_name=SRC)
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    # Re-entry happens; subsequent exit is resolution (non-stop_loss)
    bot._update_stop_loss_counter(MID, rec, "resolution")
    assert (MID, SRC) not in bot._consecutive_stop_losses


def test_entry_rejected_at_threshold(gate_events):
    bot = _make_bot()
    bot._consecutive_stop_losses[KEY] = _STOP_LOSS_PERM_SKIP_THRESHOLD
    skipped = bot._check_stop_loss_perm_skip(MID, SRC, "kalshi")
    assert skipped is True
    assert len(gate_events) == 1
    ev = gate_events[0]
    assert ev["ticker"] == MID
    assert ev["gate"] == "executor_pretrade"
    assert ev["reason"] == "perm_skip_stop_losses"
    assert ev["decision"] == "skip"
    assert ev["extra"]["signal_source"] == SRC
    assert ev["extra"]["consecutive_stop_losses"] == _STOP_LOSS_PERM_SKIP_THRESHOLD


def test_entry_allowed_below_threshold(gate_events):
    bot = _make_bot()
    bot._consecutive_stop_losses[KEY] = _STOP_LOSS_PERM_SKIP_THRESHOLD - 1
    skipped = bot._check_stop_loss_perm_skip(MID, SRC, "kalshi")
    assert skipped is False
    assert gate_events == []


def test_counters_are_independent_across_keys(gate_events):
    """A bad signal from FRED on KXTRUFGAS does NOT lock out:
      (a) a different source on the same ticker, or
      (b) the same source on a different ticker."""
    bot = _make_bot()
    bot._consecutive_stop_losses[KEY] = _STOP_LOSS_PERM_SKIP_THRESHOLD

    # Same ticker, different source — should pass through.
    assert bot._check_stop_loss_perm_skip(
        MID, "Yahoo Finance/CL=F", "kalshi",
    ) is False
    # Different ticker, same source — should pass through.
    assert bot._check_stop_loss_perm_skip(
        "KXNATGASD-26JUN01-T3", SRC, "kalshi",
    ) is False
    # Original key still blocked.
    assert bot._check_stop_loss_perm_skip(MID, SRC, "kalshi") is True

    # Only the blocking call emitted a gate event.
    assert len(gate_events) == 1
    assert gate_events[0]["ticker"] == MID
    assert gate_events[0]["extra"]["signal_source"] == SRC


def test_profit_target_exit_resets_avoiding_permanent_lockout():
    """Same (ticker, source) pair: 1 stop_loss then a profit_target exit
    resets the counter so the bot can re-engage the signal next session."""
    bot = _make_bot()
    rec = _make_rec(source_name=SRC)
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    assert bot._consecutive_stop_losses[KEY] == 1
    bot._update_stop_loss_counter(MID, rec, "approach_exit")
    assert (MID, SRC) not in bot._consecutive_stop_losses
    # And a fresh stop_loss starts counting from 0 again.
    bot._update_stop_loss_counter(MID, rec, "stop_loss")
    assert bot._consecutive_stop_losses[KEY] == 1
