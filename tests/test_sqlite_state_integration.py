"""Integration tests for the SQLite-backed state code path (Phase SQLite-3a).

Covers:
- dispatcher routing in _load_positions / _save_positions / _load_ghost_state /
  _save_ghost_state — SQLite when (dry_run AND DB present), JSON otherwise
- Q3 atomicity: position open and close transactions write the position row
  and the ghost_state row in a single BEGIN/COMMIT
- perm-skip counter SQLite write-through on increment and reset
- in-memory _consecutive_stop_losses dict hydrated from SQLite at init
- Q1 crash-on-failure: a corrupt-DB read at load propagates rather than
  degrading to empty state

Stubs ResolutionBot and BotCoordinator via SimpleNamespace where possible to
avoid the full constructor chain (network clients, scanners, GT router).
Uses a real on-disk temp DB so Path.exists() dispatcher checks behave the
same as in production.
"""
from __future__ import annotations

import json
import sqlite3
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

from data.markets.base import Market
from data.runtime import sqlite_store
from resolution.executor import ResolutionBot, TradeRecord


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    """Provide a real on-disk SQLite DB; close the thread connection after."""
    db_path = tmp_path / "bot_state.db"
    sqlite_store.set_db_path(db_path)
    sqlite_store.create_schema()
    yield db_path
    sqlite_store.close_thread_connection()


@pytest.fixture
def no_sqlite_db(tmp_path: Path) -> Path:
    """Point sqlite_store at a path that does not exist on disk."""
    db_path = tmp_path / "absent.db"
    sqlite_store.set_db_path(db_path)
    yield db_path
    sqlite_store.close_thread_connection()


def _make_market(
    market_id: str = "KXTEST-26JAN01-T1.00",
    hours_ahead: float = 12.0,
) -> Market:
    return Market(
        market_id=market_id,
        platform="kalshi",
        question=f"Will {market_id} resolve YES?",
        category="economics",
        tags=["economics"],
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=hours_ahead),
        yes_price=0.42,
        no_price=0.58,
    )


def _make_record(market: Market, size_usd: float = 10.0) -> TradeRecord:
    return TradeRecord(
        market_id=market.market_id,
        platform=market.platform,
        market=market,
        signal=None,
        action="buy_yes",
        entry_price=0.42,
        size_usd=size_usd,
        ground_truth_prob=0.55,
        source_confidence=0.85,
        entry_time=1780000000.0,
        order_id=f"ghost_{market.market_id}_1780000000",
        fill_status="filled",
        limit_price_used=None,
    )


def _make_bankroll_stub(total: float, realized: float = 0.0) -> Any:
    return types.SimpleNamespace(
        summary=lambda: {
            "total_usd": total,
            "reserved_usd": 0.0,
            "available_usd": total,
            "realized_pnl_usd": realized,
        },
        reserve=lambda mid, amt: True,
    )


def _make_executor_stub(
    dry_run: bool,
    positions: Dict[str, TradeRecord] | None = None,
    total: float = 500.0,
    realized: float = 0.0,
) -> Any:
    """Build a SimpleNamespace bound to ResolutionBot's persistence methods."""
    bot = types.SimpleNamespace(
        _dry_run=dry_run,
        _positions=positions if positions is not None else {},
        _bankroll=_make_bankroll_stub(total, realized),
        _state=None,
        _GHOST_POSITIONS_FILE="data/runtime/ghost_positions.json",
        sqlite_calls=0,
        json_calls=0,
    )
    # Bind the four position methods to the stub.
    bot._save_positions = lambda: ResolutionBot._save_positions(bot)
    bot._save_positions_json = lambda: setattr(
        bot, "json_calls", bot.json_calls + 1
    )
    bot._save_positions_sqlite_real = (
        lambda: ResolutionBot._save_positions_sqlite(bot)
    )
    bot._save_positions_sqlite = lambda: setattr(
        bot, "sqlite_calls", bot.sqlite_calls + 1
    )
    bot._load_positions = lambda: ResolutionBot._load_positions(bot)
    bot._load_positions_json = lambda: setattr(
        bot, "json_calls", bot.json_calls + 1
    )
    bot._load_positions_sqlite_real = (
        lambda: ResolutionBot._load_positions_sqlite(bot)
    )
    bot._load_positions_sqlite = lambda: setattr(
        bot, "sqlite_calls", bot.sqlite_calls + 1
    )
    return bot


# ── Test 1: dispatcher routes to SQLite when (dry_run AND DB present) ──────

def test_load_positions_dispatches_to_sqlite_when_db_exists(sqlite_db):
    bot = _make_executor_stub(dry_run=True)
    bot._load_positions()
    assert bot.sqlite_calls == 1
    assert bot.json_calls == 0


def test_save_positions_dispatches_to_sqlite_when_db_exists(sqlite_db):
    bot = _make_executor_stub(dry_run=True)
    bot._save_positions()
    assert bot.sqlite_calls == 1
    assert bot.json_calls == 0


# ── Test 2: dispatcher routes to JSON when DB absent ───────────────────────

def test_load_positions_dispatches_to_json_when_db_absent(no_sqlite_db):
    bot = _make_executor_stub(dry_run=True)
    bot._load_positions()
    assert bot.sqlite_calls == 0
    assert bot.json_calls == 1


def test_save_positions_dispatches_to_json_when_live_mode(sqlite_db):
    """SQLite is dry_run-only at Phase 3a; live mode must stay on JSON/StateStore
    even when the DB file exists."""
    bot = _make_executor_stub(dry_run=False)
    bot._save_positions()
    assert bot.sqlite_calls == 0
    assert bot.json_calls == 1


# ── Test 3: position-open writes position row + ghost_state atomically ─────

def test_position_open_writes_position_and_bankroll_atomically(sqlite_db):
    market = _make_market("KXOPEN-26JUN01-T100.00")
    rec = _make_record(market, size_usd=12.5)
    bot = _make_executor_stub(
        dry_run=True,
        positions={market.market_id: rec},
        total=487.5,  # post-reserve bankroll snapshot
        realized=0.0,
    )
    bot._save_positions_sqlite_real()

    rows = sqlite_store.get_all_positions()
    assert len(rows) == 1
    assert rows[0]["market_id"] == market.market_id
    assert rows[0]["size_usd"] == pytest.approx(12.5)

    state = sqlite_store.get_bankroll()
    assert state is not None
    assert state["total_usd"] == pytest.approx(487.5)
    assert state["realized_pnl_usd"] == pytest.approx(0.0)


# ── Test 4: position-close writes deletion + ghost_state atomically ────────

def test_position_close_writes_deletion_and_bankroll_atomically(sqlite_db):
    # First write an "open" snapshot.
    market = _make_market("KXCLOSE-26JUN01-T100.00")
    rec = _make_record(market, size_usd=10.0)
    bot_open = _make_executor_stub(
        dry_run=True,
        positions={market.market_id: rec},
        total=490.0,
    )
    bot_open._save_positions_sqlite_real()
    assert len(sqlite_store.get_all_positions()) == 1

    # Now simulate the close: position popped from dict, bankroll reflects +pnl.
    bot_close = _make_executor_stub(
        dry_run=True,
        positions={},                 # closed → empty
        total=503.5,                  # 490.0 (reserve released) + 13.5 pnl
        realized=13.5,
    )
    bot_close._save_positions_sqlite_real()

    assert sqlite_store.get_all_positions() == []
    state = sqlite_store.get_bankroll()
    assert state["total_usd"] == pytest.approx(503.5)
    assert state["realized_pnl_usd"] == pytest.approx(13.5)


def test_save_positions_sqlite_rolls_back_on_failure(sqlite_db, monkeypatch):
    """If the ghost_state UPSERT fails after the positions INSERT succeeds,
    the BEGIN/COMMIT block must roll back so neither row mutates.

    Wraps the real connection in a proxy that raises sqlite3.OperationalError
    on the ghost_state UPSERT.  sqlite3.Connection.execute itself is a C-level
    attribute and can't be monkeypatched, so we monkeypatch the module-level
    get_connection() helper instead — _save_positions_sqlite resolves it via
    `from data.runtime.sqlite_store import get_connection` at call time, so
    the proxy is picked up.
    """
    market = _make_market("KXATOMIC-26JUN01-T100.00")
    rec = _make_record(market, size_usd=7.5)
    bot = _make_executor_stub(
        dry_run=True,
        positions={market.market_id: rec},
        total=492.5,
    )

    # Pre-populate ghost_state with a known sentinel so we can verify rollback.
    sqlite_store.set_bankroll({"total_usd": 100.0, "realized_pnl_usd": -50.0})

    real_conn = sqlite_store.get_connection()

    class _FailingConnProxy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("INSERT OR REPLACE INTO ghost_state"):
                raise sqlite3.OperationalError("simulated mid-transaction failure")
            return self._conn.execute(sql, *args, **kwargs)

        def executemany(self, sql, *args, **kwargs):
            return self._conn.executemany(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    proxy = _FailingConnProxy(real_conn)
    monkeypatch.setattr(sqlite_store, "get_connection", lambda: proxy)

    with pytest.raises(sqlite3.OperationalError):
        bot._save_positions_sqlite_real()

    # Restore the real get_connection for verification reads.
    monkeypatch.undo()

    # Rollback should leave both tables in pre-transaction state.
    assert sqlite_store.get_all_positions() == []
    state = sqlite_store.get_bankroll()
    assert state["total_usd"] == pytest.approx(100.0)
    assert state["realized_pnl_usd"] == pytest.approx(-50.0)


# ── Test 5/6: perm-skip increments / resets persist when DB present ────────

def _make_permskip_bot(dry_run: bool) -> Any:
    bot = types.SimpleNamespace(
        _dry_run=dry_run,
        _consecutive_stop_losses={},
    )
    bot._signal_source_name = lambda rec: ResolutionBot._signal_source_name(rec)
    bot._update_stop_loss_counter = lambda mid, rec, reason: (
        ResolutionBot._update_stop_loss_counter(bot, mid, rec, reason)
    )
    return bot


def _make_rec_with_source(source_name: str = "FRED/CPIAUCSL") -> Any:
    gt = types.SimpleNamespace(source_name=source_name)
    sig = types.SimpleNamespace(ground_truth_result=gt, signal_type="information")
    return types.SimpleNamespace(signal=sig)


def test_permskip_increment_persists_to_sqlite(sqlite_db):
    bot = _make_permskip_bot(dry_run=True)
    rec = _make_rec_with_source("FRED/CPIAUCSL")
    mid = "KXCPIYY-26JUN01-T3.99"

    bot._update_stop_loss_counter(mid, rec, "stop_loss")
    bot._update_stop_loss_counter(mid, rec, "stop_loss")

    assert bot._consecutive_stop_losses[(mid, "FRED/CPIAUCSL")] == 2
    assert sqlite_store.get_perm_skip_count(mid, "FRED/CPIAUCSL") == 2


def test_permskip_reset_persists_to_sqlite(sqlite_db):
    bot = _make_permskip_bot(dry_run=True)
    rec = _make_rec_with_source("FRED/CPIAUCSL")
    mid = "KXCPIYY-26JUN01-T3.99"

    bot._update_stop_loss_counter(mid, rec, "stop_loss")
    assert sqlite_store.get_perm_skip_count(mid, "FRED/CPIAUCSL") == 1

    bot._update_stop_loss_counter(mid, rec, "early_exit")
    assert (mid, "FRED/CPIAUCSL") not in bot._consecutive_stop_losses
    assert sqlite_store.get_perm_skip_count(mid, "FRED/CPIAUCSL") == 0


def test_permskip_does_not_touch_sqlite_in_live_mode(sqlite_db):
    bot = _make_permskip_bot(dry_run=False)
    rec = _make_rec_with_source("FRED/CPIAUCSL")
    mid = "KXCPIYY-26JUN01-T3.99"

    bot._update_stop_loss_counter(mid, rec, "stop_loss")

    # In-memory dict mutated as before, but SQLite is left untouched.
    assert bot._consecutive_stop_losses[(mid, "FRED/CPIAUCSL")] == 1
    assert sqlite_store.get_perm_skip_count(mid, "FRED/CPIAUCSL") == 0


def test_permskip_does_not_touch_sqlite_when_db_absent(no_sqlite_db):
    bot = _make_permskip_bot(dry_run=True)
    rec = _make_rec_with_source("FRED/CPIAUCSL")
    mid = "KXCPIYY-26JUN01-T3.99"

    # No DB file exists, so write-through is skipped entirely.
    bot._update_stop_loss_counter(mid, rec, "stop_loss")
    assert bot._consecutive_stop_losses[(mid, "FRED/CPIAUCSL")] == 1


# ── Test 7: load failure propagates (no fallback to JSON or empty) ─────────

def test_load_positions_sqlite_failure_propagates(tmp_path: Path):
    """Q1: if the SQLite DB is corrupt at startup, the load raises instead of
    silently returning an empty state.  Simulated by writing garbage bytes to
    the DB path and verifying that get_all_positions() (and therefore
    _load_positions_sqlite) surfaces the sqlite3 error."""
    corrupt_path = tmp_path / "corrupt.db"
    corrupt_path.write_bytes(b"this is not a valid sqlite database file at all")

    sqlite_store.set_db_path(corrupt_path)
    try:
        bot = _make_executor_stub(dry_run=True)
        with pytest.raises(sqlite3.DatabaseError):
            bot._load_positions_sqlite_real()
    finally:
        sqlite_store.close_thread_connection()


# ── Test 8: in-memory perm-skip dict hydrated from SQLite at init ──────────

def test_permskip_dict_hydrated_from_sqlite(sqlite_db):
    """Mirrors the hydration block in ResolutionBot.__init__: when dry_run AND
    DB present, every row of perm_skip_counters lands in _consecutive_stop_losses."""
    sqlite_store.increment_perm_skip("KX1-T1.00", "FRED/CPIAUCSL")
    sqlite_store.increment_perm_skip("KX1-T1.00", "FRED/CPIAUCSL")
    sqlite_store.increment_perm_skip("KX2-T2.00", "FRED/PAYEMS")

    # Run the same hydration logic the executor __init__ runs.
    dry_run = True
    consecutive_stop_losses: dict = {}
    if dry_run:
        from data.runtime.sqlite_store import (
            get_all_perm_skip_counts,
            get_db_path,
        )
        if get_db_path().exists():
            for ticker, source, count in get_all_perm_skip_counts():
                consecutive_stop_losses[(ticker, source)] = count

    assert consecutive_stop_losses == {
        ("KX1-T1.00", "FRED/CPIAUCSL"): 2,
        ("KX2-T2.00", "FRED/PAYEMS"): 1,
    }


# ── Bonus: ghost_state dispatcher round-trip on bot.py path ────────────────

def test_ghost_state_dispatcher_routes_to_sqlite(sqlite_db):
    """BotCoordinator._save_ghost_state / _load_ghost_state should route to
    the SQLite variants when (dry_run AND DB present)."""
    from bot import BotCoordinator

    cfg_bot = types.SimpleNamespace(dry_run=True)
    cfg = types.SimpleNamespace(bot=cfg_bot)
    bot = types.SimpleNamespace(
        _cfg=cfg,
        _bankroll=_make_bankroll_stub(525.0, realized=25.0),
        sqlite_calls=0,
        json_calls=0,
    )
    bot._save_ghost_state = lambda: BotCoordinator._save_ghost_state(bot)
    bot._load_ghost_state = lambda: BotCoordinator._load_ghost_state(bot)
    bot._save_ghost_state_json = lambda: setattr(bot, "json_calls", bot.json_calls + 1)
    bot._save_ghost_state_sqlite = lambda: setattr(bot, "sqlite_calls", bot.sqlite_calls + 1)
    bot._load_ghost_state_json = lambda: (
        setattr(bot, "json_calls", bot.json_calls + 1) or None
    )
    bot._load_ghost_state_sqlite = lambda: (
        setattr(bot, "sqlite_calls", bot.sqlite_calls + 1) or None
    )

    bot._save_ghost_state()
    bot._load_ghost_state()
    assert bot.sqlite_calls == 2
    assert bot.json_calls == 0


def test_ghost_state_dispatcher_routes_to_json_when_live(sqlite_db):
    from bot import BotCoordinator

    cfg_bot = types.SimpleNamespace(dry_run=False)
    cfg = types.SimpleNamespace(bot=cfg_bot)
    bot = types.SimpleNamespace(
        _cfg=cfg,
        _bankroll=_make_bankroll_stub(525.0),
        sqlite_calls=0,
        json_calls=0,
    )
    bot._save_ghost_state = lambda: BotCoordinator._save_ghost_state(bot)
    bot._load_ghost_state = lambda: BotCoordinator._load_ghost_state(bot)
    bot._save_ghost_state_json = lambda: setattr(bot, "json_calls", bot.json_calls + 1)
    bot._save_ghost_state_sqlite = lambda: setattr(bot, "sqlite_calls", bot.sqlite_calls + 1)
    bot._load_ghost_state_json = lambda: (
        setattr(bot, "json_calls", bot.json_calls + 1) or None
    )
    bot._load_ghost_state_sqlite = lambda: (
        setattr(bot, "sqlite_calls", bot.sqlite_calls + 1) or None
    )

    bot._save_ghost_state()
    bot._load_ghost_state()
    assert bot.sqlite_calls == 0
    assert bot.json_calls == 2
