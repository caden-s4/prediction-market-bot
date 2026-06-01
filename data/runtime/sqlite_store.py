"""SQLite-backed store for mutable bot runtime state.

Phase SQLite-1: infrastructure only. Not yet wired into the bot.
Mirrors data/runtime/ghost_positions.json and data/runtime/ghost_state.json
field-for-field, plus a new perm_skip_counters table for Bleed-Fix-1.

Concurrency model:
- One sqlite3.Connection per thread (thread-local).
- WAL journal mode for concurrent reader / single-writer durability.
- isolation_level=None: autocommit by default; multi-statement writes
  use explicit BEGIN / COMMIT.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Optional

_DB_PATH = Path("data/runtime/bot_state.db")
_thread_local = threading.local()


# ── Connection management ──────────────────────────────────────────────────

def set_db_path(path: Path | str) -> None:
    """Override the database path. Closes any existing thread-local connection
    so the next get_connection() call opens against the new path.

    Used by tests (which point at ':memory:') and the migration script
    (which points at a temp file for dry-run).
    """
    global _DB_PATH
    close_thread_connection()
    _DB_PATH = Path(path) if not isinstance(path, Path) else path


def get_db_path() -> Path:
    return _DB_PATH


def get_connection() -> sqlite3.Connection:
    """Get a thread-local connection. Opens one if needed."""
    if not hasattr(_thread_local, "conn"):
        conn = sqlite3.connect(
            str(_DB_PATH),
            isolation_level=None,
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        _thread_local.conn = conn
    return _thread_local.conn


def close_thread_connection() -> None:
    """Close the thread-local connection if one exists."""
    if hasattr(_thread_local, "conn"):
        try:
            _thread_local.conn.close()
        finally:
            del _thread_local.conn


# ── Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS ghost_positions (
    ticker              TEXT PRIMARY KEY NOT NULL,
    market_id           TEXT NOT NULL,
    platform            TEXT NOT NULL,
    action              TEXT NOT NULL CHECK(action IN ('buy_yes','buy_no')),
    entry_price         REAL NOT NULL,
    size_usd            REAL NOT NULL,
    ground_truth_prob   REAL NOT NULL,
    source_confidence   REAL NOT NULL,
    entry_time          REAL NOT NULL,
    order_id            TEXT,
    resolution_date_iso TEXT NOT NULL,
    question            TEXT NOT NULL,
    category            TEXT,
    tags_json           TEXT,
    fill_status         TEXT,
    limit_price_used    REAL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ghost_positions_resolution
    ON ghost_positions(resolution_date_iso);

CREATE TABLE IF NOT EXISTS ghost_state (
    id               INTEGER PRIMARY KEY CHECK(id = 1),
    total_usd        REAL NOT NULL,
    realized_pnl_usd REAL NOT NULL DEFAULT 0,
    last_updated     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS perm_skip_counters (
    ticker        TEXT NOT NULL,
    signal_source TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    last_updated  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, signal_source)
);
"""


def create_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Create tables and indexes if they don't already exist. Idempotent."""
    c = conn if conn is not None else get_connection()
    c.executescript(SCHEMA)


# ── Row → dict conversion ──────────────────────────────────────────────────

_POSITION_COLUMNS = (
    "ticker",
    "market_id",
    "platform",
    "action",
    "entry_price",
    "size_usd",
    "ground_truth_prob",
    "source_confidence",
    "entry_time",
    "order_id",
    "resolution_date_iso",
    "question",
    "category",
    "tags_json",
    "fill_status",
    "limit_price_used",
)


def _row_to_position(row: sqlite3.Row) -> dict:
    """Convert a ghost_positions row to a JSON-shaped dict.

    Decodes tags_json back into a Python list. Drops the database-internal
    `created_at` column. The result matches the on-disk JSON schema 1:1
    (modulo the ticker key, which is the dict key in the JSON file).
    """
    out = {col: row[col] for col in _POSITION_COLUMNS if col != "tags_json"}
    raw_tags = row["tags_json"]
    out["tags"] = json.loads(raw_tags) if raw_tags else []
    return out


# ── Read helpers ───────────────────────────────────────────────────────────

def get_position(ticker: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM ghost_positions WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_position(row)


def get_all_positions() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM ghost_positions").fetchall()
    return [_row_to_position(r) for r in rows]


def get_bankroll() -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT total_usd, realized_pnl_usd, last_updated "
        "FROM ghost_state WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return {
        "total_usd": row["total_usd"],
        "realized_pnl_usd": row["realized_pnl_usd"],
        "last_updated": row["last_updated"],
    }


def get_perm_skip_count(ticker: str, signal_source: str) -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT count FROM perm_skip_counters "
        "WHERE ticker = ? AND signal_source = ?",
        (ticker, signal_source),
    ).fetchone()
    return row["count"] if row else 0


# ── Write helpers ──────────────────────────────────────────────────────────

def upsert_position(position: dict) -> None:
    """Insert or replace a ghost position.

    `position` must have all NOT NULL fields. `tags` (list) is JSON-encoded
    into `tags_json`. Unknown fields are ignored.
    """
    tags = position.get("tags", [])
    tags_json = json.dumps(tags) if tags is not None else None

    row = (
        position["ticker"] if "ticker" in position else position["market_id"],
        position["market_id"],
        position["platform"],
        position["action"],
        float(position["entry_price"]),
        float(position["size_usd"]),
        float(position["ground_truth_prob"]),
        float(position["source_confidence"]),
        float(position["entry_time"]),
        position.get("order_id"),
        position["resolution_date_iso"],
        position["question"],
        position.get("category"),
        tags_json,
        position.get("fill_status"),
        position.get("limit_price_used"),
    )

    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ghost_positions ("
            "ticker, market_id, platform, action, entry_price, size_usd, "
            "ground_truth_prob, source_confidence, entry_time, order_id, "
            "resolution_date_iso, question, category, tags_json, "
            "fill_status, limit_price_used"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def delete_position(ticker: str) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM ghost_positions WHERE ticker = ?", (ticker,))


def set_bankroll(state: dict) -> None:
    """Upsert the single-row ghost_state. id is always 1."""
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ghost_state ("
            "id, total_usd, realized_pnl_usd, last_updated"
            ") VALUES (1, ?, ?, datetime('now'))",
            (float(state["total_usd"]), float(state.get("realized_pnl_usd", 0.0))),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def increment_perm_skip(ticker: str, signal_source: str) -> int:
    """Atomically increment the perm-skip counter. Returns the new count."""
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO perm_skip_counters (ticker, signal_source, count, last_updated) "
            "VALUES (?, ?, 1, datetime('now')) "
            "ON CONFLICT(ticker, signal_source) DO UPDATE SET "
            "count = count + 1, last_updated = datetime('now')",
            (ticker, signal_source),
        )
        row = conn.execute(
            "SELECT count FROM perm_skip_counters "
            "WHERE ticker = ? AND signal_source = ?",
            (ticker, signal_source),
        ).fetchone()
        conn.execute("COMMIT")
        return row["count"]
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reset_perm_skip(ticker: str, signal_source: str) -> None:
    """Set the counter to 0. Inserts the row if it doesn't exist."""
    conn = get_connection()
    conn.execute("BEGIN")
    try:
        conn.execute(
            "INSERT INTO perm_skip_counters (ticker, signal_source, count, last_updated) "
            "VALUES (?, ?, 0, datetime('now')) "
            "ON CONFLICT(ticker, signal_source) DO UPDATE SET "
            "count = 0, last_updated = datetime('now')",
            (ticker, signal_source),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
