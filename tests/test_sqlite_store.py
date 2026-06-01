"""Unit tests for data.runtime.sqlite_store.

All tests use an in-memory SQLite database. The store's set_db_path() is
called in a fixture to point at ':memory:'; the thread-local connection is
closed afterwards so subsequent tests get a fresh DB.
"""

from __future__ import annotations

import sqlite3

import pytest

from data.runtime import sqlite_store


@pytest.fixture(autouse=True)
def _in_memory_db():
    sqlite_store.set_db_path(":memory:")
    sqlite_store.create_schema()
    yield
    sqlite_store.close_thread_connection()


# ── Schema ────────────────────────────────────────────────────────────────

def test_create_schema_is_idempotent():
    sqlite_store.create_schema()
    sqlite_store.create_schema()
    conn = sqlite_store.get_connection()
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = [r["name"] for r in rows]
    assert "ghost_positions" in names
    assert "ghost_state" in names
    assert "perm_skip_counters" in names


# ── Position CRUD ─────────────────────────────────────────────────────────

def _sample_position(**overrides) -> dict:
    base = {
        "ticker": "KXTEST-26JAN01-T1.00",
        "market_id": "KXTEST-26JAN01-T1.00",
        "platform": "kalshi",
        "action": "buy_yes",
        "entry_price": 0.42,
        "size_usd": 10.0,
        "ground_truth_prob": 0.55,
        "source_confidence": 0.85,
        "entry_time": 1780000000.123,
        "order_id": "ghost_KXTEST-26JAN01-T1.00_1780000000",
        "resolution_date_iso": "2026-01-01T23:59:00+00:00",
        "question": "Will X happen?",
        "category": "economics",
        "tags": ["economics"],
        "fill_status": "filled",
        "limit_price_used": None,
    }
    base.update(overrides)
    return base


def test_position_upsert_read_delete():
    pos = _sample_position()
    sqlite_store.upsert_position(pos)

    got = sqlite_store.get_position(pos["ticker"])
    assert got is not None
    assert got["market_id"] == pos["market_id"]
    assert got["action"] == "buy_yes"
    assert got["entry_price"] == pytest.approx(0.42)
    assert got["tags"] == ["economics"]

    # Upsert overwrites
    sqlite_store.upsert_position(_sample_position(entry_price=0.55))
    got2 = sqlite_store.get_position(pos["ticker"])
    assert got2["entry_price"] == pytest.approx(0.55)

    sqlite_store.delete_position(pos["ticker"])
    assert sqlite_store.get_position(pos["ticker"]) is None


def test_get_all_positions():
    sqlite_store.upsert_position(_sample_position(ticker="A", market_id="A"))
    sqlite_store.upsert_position(_sample_position(ticker="B", market_id="B"))
    rows = sqlite_store.get_all_positions()
    tickers = {r["market_id"] for r in rows}
    assert tickers == {"A", "B"}


# ── Round-trip parity (per user requirement) ──────────────────────────────

def _strip_ticker(d: dict) -> dict:
    """Tickers are the dict key in the JSON file, not a field on the value.
    Strip from the round-trip comparison to match the JSON shape."""
    return {k: v for k, v in d.items() if k != "ticker"}


@pytest.mark.parametrize(
    "case,overrides",
    [
        ("limit_price_none", {"limit_price_used": None}),
        ("limit_price_set", {"limit_price_used": 0.96}),
        ("tags_non_empty", {"tags": ["economics", "macro"]}),
        ("tags_empty", {"tags": []}),
        ("category_economics", {"category": "economics", "tags": ["economics"]}),
        ("category_general", {"category": "general", "tags": ["general"]}),
        ("action_buy_no", {"action": "buy_no"}),
    ],
)
def test_position_round_trip(case, overrides):
    pos = _sample_position(**overrides)
    sqlite_store.upsert_position(pos)
    got = sqlite_store.get_position(pos["ticker"])
    assert got is not None, case
    assert _strip_ticker(got) == _strip_ticker(pos), (
        f"{case}: round-trip diff. expected={pos} got={got}"
    )


# ── Bankroll ──────────────────────────────────────────────────────────────

def test_bankroll_set_and_read():
    assert sqlite_store.get_bankroll() is None
    sqlite_store.set_bankroll({"total_usd": 500.0, "realized_pnl_usd": -25.5})
    got = sqlite_store.get_bankroll()
    assert got is not None
    assert got["total_usd"] == pytest.approx(500.0)
    assert got["realized_pnl_usd"] == pytest.approx(-25.5)

    # Update overwrites the single row.
    sqlite_store.set_bankroll({"total_usd": 600.0, "realized_pnl_usd": 0.0})
    got2 = sqlite_store.get_bankroll()
    assert got2["total_usd"] == pytest.approx(600.0)
    assert got2["realized_pnl_usd"] == pytest.approx(0.0)

    # There should still be exactly one row.
    conn = sqlite_store.get_connection()
    n = conn.execute("SELECT COUNT(*) AS c FROM ghost_state").fetchone()["c"]
    assert n == 1


# ── Perm-skip counters ────────────────────────────────────────────────────

def test_perm_skip_increment_and_reset():
    assert sqlite_store.get_perm_skip_count("T", "src") == 0
    assert sqlite_store.increment_perm_skip("T", "src") == 1
    assert sqlite_store.increment_perm_skip("T", "src") == 2
    assert sqlite_store.get_perm_skip_count("T", "src") == 2
    sqlite_store.reset_perm_skip("T", "src")
    assert sqlite_store.get_perm_skip_count("T", "src") == 0

    # Reset on a never-incremented key creates a 0 row, doesn't error.
    sqlite_store.reset_perm_skip("U", "src2")
    assert sqlite_store.get_perm_skip_count("U", "src2") == 0


def test_perm_skip_distinct_keys_are_independent():
    sqlite_store.increment_perm_skip("T", "src1")
    sqlite_store.increment_perm_skip("T", "src1")
    sqlite_store.increment_perm_skip("T", "src2")
    assert sqlite_store.get_perm_skip_count("T", "src1") == 2
    assert sqlite_store.get_perm_skip_count("T", "src2") == 1


# ── CHECK constraints ─────────────────────────────────────────────────────

def test_action_check_constraint_rejects_invalid():
    with pytest.raises(sqlite3.IntegrityError):
        sqlite_store.upsert_position(_sample_position(action="sell_yes"))


def test_ghost_state_id_check_constraint_rejects_non_one():
    conn = sqlite_store.get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO ghost_state (id, total_usd) VALUES (?, ?)",
            (2, 500.0),
        )
