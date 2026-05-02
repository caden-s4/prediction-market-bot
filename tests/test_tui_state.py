"""Tests for monitoring/tui_state.py — snapshot writer and helpers."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from monitoring.tui_state import (
    TUIStateSnapshot,
    _read_git_commit_short,
    write_snapshot,
)


def _make_snapshot(**overrides) -> TUIStateSnapshot:
    defaults = dict(
        mode="GHOST",
        paused=False,
        cycle_start_ts="2026-05-02T12:00:00+00:00",
        cycle_duration_s=4.2,
        uptime_start_ts="2026-05-02T11:00:00+00:00",
        wall_clock_utc="2026-05-02T12:00:04+00:00",
        disabled_features=["yahoo_brackets", "kxbrentd"],
        current_pipeline_stage="idle",
        signals_total=3,
        fills_total=1,
        snipes_attempted=0,
        snipes_placed=0,
        shadow_signals_total=0,
    )
    defaults.update(overrides)
    return TUIStateSnapshot(**defaults)


def test_write_snapshot_creates_file_atomically(tmp_path):
    path = str(tmp_path / "tui_state.json")
    snap = _make_snapshot()
    write_snapshot(snap, path)

    assert os.path.exists(path), "snapshot file must exist after write"
    data = json.loads(open(path, encoding="utf-8").read())
    for field in (
        "mode", "paused", "cycle_start_ts", "cycle_duration_s",
        "uptime_start_ts", "wall_clock_utc", "disabled_features",
        "current_pipeline_stage", "signals_total", "fills_total",
        "snipes_attempted", "snipes_placed", "shadow_signals_total",
        "schema_version",
    ):
        assert field in data, f"field '{field}' missing from snapshot JSON"


def test_write_snapshot_overwrites_atomically(tmp_path):
    path = str(tmp_path / "tui_state.json")
    write_snapshot(_make_snapshot(signals_total=1), path)
    write_snapshot(_make_snapshot(signals_total=99), path)

    data = json.loads(open(path, encoding="utf-8").read())
    assert data["signals_total"] == 99


def test_write_snapshot_does_not_leave_tmp_file(tmp_path):
    path = str(tmp_path / "tui_state.json")
    write_snapshot(_make_snapshot(), path)

    assert not os.path.exists(path + ".tmp"), ".tmp file must not remain after clean write"


def test_write_snapshot_failure_logged_not_raised(tmp_path, caplog):
    import logging
    bad_path = str(tmp_path / "no_such_dir" / "deep" / "tui_state.json")

    with patch("builtins.open", side_effect=OSError("disk full")):
        with caplog.at_level(logging.WARNING, logger="monitoring.tui_state"):
            write_snapshot(_make_snapshot(), bad_path)  # must not raise

    assert any("tui_state write failed" in r.message for r in caplog.records), (
        "expected WARNING 'tui_state write failed' in log"
    )


def test_schema_version_present(tmp_path):
    path = str(tmp_path / "tui_state.json")
    write_snapshot(_make_snapshot(), path)
    data = json.loads(open(path, encoding="utf-8").read())
    assert data["schema_version"] == 1


def test_git_commit_hash_format():
    commit = _read_git_commit_short()
    if commit is None:
        # acceptable when not in a git repo or git not installed
        return
    assert 7 <= len(commit) <= 12, f"expected 7-12 char hash, got {commit!r}"
    assert re.fullmatch(r"[0-9a-f]+", commit), f"expected hex hash, got {commit!r}"
