"""
meta.calibration – Meta-reasoning layer: calibration tracking and self-correction.

Tracks:
  1. Overall calibration: are our 60% predictions resolving ~60% of the time?
  2. Per-market-type calibration (precipitation vs temperature vs wind)
  3. Per-model calibration (is ECMWF systematically over/underpredicting?)
  4. Performance metrics: PnL, win rate, Sharpe ratio per trade type

The output is fed back into:
  - WeatherAggregator.BiasStore (model-level corrections)
  - Stage3Edge (type-level edge threshold adjustments)
  - Stage5Timing (timing parameter adjustments)

Persistence: SQLite via sqlite-utils (lightweight, no server needed).
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "calibration.db"


@dataclass
class TradeRecord:
    """
    Stores the full lifecycle of a single trade for calibration purposes.

    Fields populated at entry:
        market_id, platform, market_type, our_prob, market_prob, edge,
        position_size_usd, direction, entered_at

    Fields populated at resolution:
        resolved_at, outcome (0 or 1), pnl_usd
    """
    market_id: str
    platform: str
    market_type: str        # e.g. "precipitation", "temperature", "wind"
    our_prob: float
    market_prob: float
    edge: float
    position_size_usd: float
    direction: int          # +1 = YES, -1 = NO

    entered_at: str = ""    # ISO format UTC
    resolved_at: str = ""
    outcome: Optional[float] = None   # 0.0 or 1.0 after resolution
    pnl_usd: Optional[float] = None
    model_weights: str = ""  # JSON string of model weights used


class CalibrationTracker:
    """
    Tracks trade outcomes and computes calibration / performance statistics.

    The tracker adjusts edge thresholds and model bias corrections based on
    observed performance history.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._db_path = db_path or _DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Database setup ────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    market_id TEXT NOT NULL,
                    platform TEXT,
                    market_type TEXT,
                    our_prob REAL,
                    market_prob REAL,
                    edge REAL,
                    position_size_usd REAL,
                    direction INTEGER,
                    entered_at TEXT,
                    resolved_at TEXT,
                    outcome REAL,
                    pnl_usd REAL,
                    model_weights TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS calibration_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_at TEXT,
                    market_type TEXT,
                    n_trades INTEGER,
                    mean_pred REAL,
                    mean_outcome REAL,
                    brier_score REAL,
                    win_rate REAL,
                    total_pnl REAL,
                    sharpe REAL
                )
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._db_path))

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def record_entry(self, record: TradeRecord) -> int:
        """Save a new trade entry.  Returns the row ID."""
        record.entered_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (market_id, platform, market_type, our_prob, market_prob,
                    edge, position_size_usd, direction, entered_at, model_weights)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (record.market_id, record.platform, record.market_type,
                 record.our_prob, record.market_prob, record.edge,
                 record.position_size_usd, record.direction,
                 record.entered_at, record.model_weights),
            )
            return cur.lastrowid

    def record_resolution(
        self,
        trade_id: int,
        outcome: float,
        pnl_usd: float,
    ) -> None:
        """Update a trade with its final outcome and PnL."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE trades SET resolved_at=?, outcome=?, pnl_usd=?
                   WHERE id=?""",
                (datetime.now(timezone.utc).isoformat(), outcome, pnl_usd, trade_id),
            )
        logger.info("CalibrationTracker: trade %d resolved → outcome=%.0f pnl=$%.2f",
                    trade_id, outcome, pnl_usd)

    # ── Calibration metrics ───────────────────────────────────────────────────

    def compute_calibration(
        self,
        market_type: Optional[str] = None,
        window: int = 100,
    ) -> Dict:
        """
        Compute calibration statistics over the last `window` resolved trades.

        Returns a dict with:
          - n_trades, mean_pred, mean_outcome, brier_score,
          - win_rate, total_pnl, sharpe, edge_correction
        """
        with self._conn() as conn:
            query = """
                SELECT our_prob, market_prob, outcome, pnl_usd, edge
                FROM trades
                WHERE outcome IS NOT NULL
                {}
                ORDER BY id DESC LIMIT ?
            """.format("AND market_type=?" if market_type else "")
            params = ([market_type, window] if market_type else [window])
            rows = conn.execute(query, params).fetchall()

        if not rows:
            return {"n_trades": 0}

        preds = np.array([r[0] for r in rows])
        outcomes = np.array([r[2] for r in rows])
        pnls = np.array([r[3] for r in rows if r[3] is not None])

        n = len(preds)
        mean_pred = float(np.mean(preds))
        mean_outcome = float(np.mean(outcomes))
        brier = float(np.mean((preds - outcomes) ** 2))
        win_rate = float(np.mean(outcomes))
        total_pnl = float(np.sum(pnls)) if len(pnls) else 0.0
        sharpe = (float(np.mean(pnls)) / (float(np.std(pnls)) + 1e-9)
                  if len(pnls) > 1 else 0.0)

        # Bias: if mean_pred > mean_outcome → we're overconfident
        # Recommend regressing predictions by this fraction toward 0.5
        bias = mean_pred - mean_outcome
        edge_correction = -bias   # subtract bias from future estimates

        stats = {
            "n_trades": n,
            "mean_pred": mean_pred,
            "mean_outcome": mean_outcome,
            "brier_score": brier,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "sharpe": sharpe,
            "bias": bias,
            "edge_correction": edge_correction,
            "market_type": market_type or "all",
        }
        logger.info("Calibration(%s): %s", market_type or "all", stats)
        return stats

    def get_edge_threshold_adjustment(
        self,
        market_type: str,
        base_threshold: float = 0.05,
        window: int = 50,
    ) -> float:
        """
        Return an adjusted edge threshold for a given market type.
        If we've been underperforming on this type, raise the threshold.
        """
        stats = self.compute_calibration(market_type=market_type, window=window)
        if stats.get("n_trades", 0) < 10:
            return base_threshold  # not enough data to adjust

        sharpe = stats.get("sharpe", 0.0)
        # If Sharpe < 0.5, we're not performing well here → raise threshold by up to 3%
        if sharpe < 0.5:
            adjustment = 0.03 * (1.0 - max(sharpe, 0.0) / 0.5)
            return base_threshold + adjustment
        # If Sharpe > 1.5, we're doing well → can lower threshold slightly
        if sharpe > 1.5:
            return max(base_threshold - 0.01, 0.03)
        return base_threshold

    def get_model_bias_corrections(self, window: int = 50) -> Dict[str, float]:
        """
        Placeholder: returns per-model bias corrections.
        In practice, model errors would be stored per-model in a separate table.
        Returns zeroes until enough data is collected.
        """
        # TODO: extend schema to store per-model probability contribution
        return {"GFS": 0.0, "ECMWF": 0.0, "NAM": 0.0, "HRRR": 0.0}

    def snapshot(self) -> None:
        """Write a calibration snapshot to the DB for historical tracking."""
        for mtype in [None, "precipitation", "temperature", "wind"]:
            stats = self.compute_calibration(market_type=mtype)
            if stats.get("n_trades", 0) == 0:
                continue
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO calibration_snapshots
                       (snapshot_at, market_type, n_trades, mean_pred,
                        mean_outcome, brier_score, win_rate, total_pnl, sharpe)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        datetime.now(timezone.utc).isoformat(),
                        mtype or "all",
                        stats["n_trades"],
                        stats.get("mean_pred"),
                        stats.get("mean_outcome"),
                        stats.get("brier_score"),
                        stats.get("win_rate"),
                        stats.get("total_pnl"),
                        stats.get("sharpe"),
                    ),
                )
