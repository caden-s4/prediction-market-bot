"""
monitoring.event_db – SQLite event logger.

Logs every signal seen (fired or not), every order placed, realised PnL,
and slippage (expected vs actual fill price).  All tables live in a single
SQLite file at data/events.db.

Tables
------
signals
    id, ts, signal_type, direction, platform, market_id,
    edge_estimate, strength, fired, metadata_json

orders
    id, ts, platform, market_id, side, expected_price, size_usd,
    order_id, status

fills
    id, order_id, ts, fill_price, fill_size, slippage

pnl
    id, ts, market_id, platform, entry_price, exit_price,
    size_usd, pnl_usd, holding_seconds

summary_snapshots
    id, ts, bankroll, total_exposure, open_positions,
    daily_pnl, total_pnl

All writes are synchronous and use WAL mode for safe concurrent access.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from signals.base import Signal

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "events.db"


class EventDB:
    """
    Append-only SQLite event store for the live trading loop.

    All methods are synchronous and safe to call from any thread.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ─────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS signals (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts            TEXT    NOT NULL,
                    signal_type   TEXT,
                    direction     TEXT,
                    platform      TEXT,
                    market_id     TEXT,
                    edge_estimate REAL,
                    strength      REAL,
                    fired         INTEGER,
                    metadata_json TEXT
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts             TEXT    NOT NULL,
                    platform       TEXT,
                    market_id      TEXT,
                    side           TEXT,
                    expected_price REAL,
                    size_usd       REAL,
                    order_id       TEXT,
                    status         TEXT
                );

                CREATE TABLE IF NOT EXISTS fills (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id   INTEGER REFERENCES orders(id),
                    ts         TEXT    NOT NULL,
                    fill_price REAL,
                    fill_size  REAL,
                    slippage   REAL
                );

                CREATE TABLE IF NOT EXISTS pnl (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               TEXT    NOT NULL,
                    market_id        TEXT,
                    platform         TEXT,
                    entry_price      REAL,
                    exit_price       REAL,
                    size_usd         REAL,
                    pnl_usd          REAL,
                    holding_seconds  REAL
                );

                CREATE TABLE IF NOT EXISTS summary_snapshots (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts               TEXT    NOT NULL,
                    bankroll         REAL,
                    total_exposure   REAL,
                    open_positions   INTEGER,
                    daily_pnl        REAL,
                    total_pnl        REAL
                );

                CREATE INDEX IF NOT EXISTS idx_signals_ts       ON signals(ts);
                CREATE INDEX IF NOT EXISTS idx_signals_market   ON signals(market_id);
                CREATE INDEX IF NOT EXISTS idx_orders_ts        ON orders(ts);
                CREATE INDEX IF NOT EXISTS idx_pnl_ts           ON pnl(ts);
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path), timeout=10)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── Signal logging ─────────────────────────────────────────────────────────

    def log_signal(self, signal: Signal, fired: bool = False) -> int:
        """Log a signal evaluation (fired or not).  Returns row ID."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals
                   (ts, signal_type, direction, platform, market_id,
                    edge_estimate, strength, fired, metadata_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    self._now(),
                    signal.signal_type.value,
                    signal.direction.value,
                    signal.platform,
                    signal.market_id,
                    signal.edge_estimate,
                    signal.strength,
                    int(fired),
                    json.dumps(signal.metadata, default=str),
                ),
            )
            return cur.lastrowid

    # ── Order logging ──────────────────────────────────────────────────────────

    def log_order(
        self,
        platform: str,
        market_id: str,
        side: str,
        expected_price: float,
        size_usd: float,
        order_id: Optional[str] = None,
        status: str = "SUBMITTED",
    ) -> int:
        """Log an outbound order.  Returns row ID."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO orders
                   (ts, platform, market_id, side, expected_price,
                    size_usd, order_id, status)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self._now(), platform, market_id, side,
                    expected_price, size_usd, order_id, status,
                ),
            )
            return cur.lastrowid

    def update_order_status(self, order_row_id: int, status: str, order_id: Optional[str] = None) -> None:
        """Update the status (and optionally the exchange order_id) of a logged order."""
        with self._conn() as conn:
            if order_id:
                conn.execute(
                    "UPDATE orders SET status=?, order_id=? WHERE id=?",
                    (status, order_id, order_row_id),
                )
            else:
                conn.execute(
                    "UPDATE orders SET status=? WHERE id=?",
                    (status, order_row_id),
                )

    # ── Fill / slippage logging ────────────────────────────────────────────────

    def log_fill(
        self,
        order_row_id: int,
        fill_price: float,
        fill_size: float,
        expected_price: float,
    ) -> None:
        """
        Log a fill and compute slippage.
        slippage = fill_price − expected_price  (positive = paid more than expected)
        """
        slippage = fill_price - expected_price
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO fills (order_id, ts, fill_price, fill_size, slippage)
                   VALUES (?,?,?,?,?)""",
                (order_row_id, self._now(), fill_price, fill_size, slippage),
            )
        if abs(slippage) > 0.005:
            logger.warning(
                "EventDB: significant slippage on order %d: expected=%.4f fill=%.4f slip=%.4f",
                order_row_id, expected_price, fill_price, slippage,
            )

    # ── PnL logging ────────────────────────────────────────────────────────────

    def log_pnl(
        self,
        market_id: str,
        platform: str,
        entry_price: float,
        exit_price: float,
        size_usd: float,
        pnl_usd: float,
        holding_seconds: float,
    ) -> None:
        """Log a closed trade's P&L."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO pnl
                   (ts, market_id, platform, entry_price, exit_price,
                    size_usd, pnl_usd, holding_seconds)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    self._now(), market_id, platform,
                    entry_price, exit_price,
                    size_usd, pnl_usd, holding_seconds,
                ),
            )
        logger.info(
            "EventDB: PnL closed %s %s pnl=$%.2f hold=%.0fs",
            platform, market_id, pnl_usd, holding_seconds,
        )

    # ── Portfolio snapshots ────────────────────────────────────────────────────

    def snapshot(
        self,
        bankroll: float,
        total_exposure: float,
        open_positions: int,
        daily_pnl: float,
        total_pnl: float,
    ) -> None:
        """Write a portfolio summary snapshot for time-series analysis."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO summary_snapshots
                   (ts, bankroll, total_exposure, open_positions, daily_pnl, total_pnl)
                   VALUES (?,?,?,?,?,?)""",
                (
                    self._now(), bankroll, total_exposure,
                    open_positions, daily_pnl, total_pnl,
                ),
            )

    # ── Analytics queries ──────────────────────────────────────────────────────

    def get_daily_pnl(self, date_str: Optional[str] = None) -> float:
        """Return total PnL for a UTC date (YYYY-MM-DD).  Defaults to today."""
        if date_str is None:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) FROM pnl WHERE ts LIKE ?",
                (f"{date_str}%",),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def get_avg_slippage(self) -> float:
        """Return the mean slippage across all fills."""
        with self._conn() as conn:
            row = conn.execute("SELECT AVG(slippage) FROM fills").fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def get_signal_hit_rate(self, signal_type: Optional[str] = None) -> Dict[str, Any]:
        """Return signal count and fired/unfired breakdown."""
        where = "WHERE signal_type=?" if signal_type else ""
        params = (signal_type,) if signal_type else ()
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT fired, COUNT(*) FROM signals {where} GROUP BY fired",
                params,
            ).fetchall()
        result = {"fired": 0, "not_fired": 0}
        for fired, cnt in rows:
            key = "fired" if fired else "not_fired"
            result[key] = cnt
        total = result["fired"] + result["not_fired"]
        result["hit_rate"] = result["fired"] / total if total > 0 else 0.0
        return result
