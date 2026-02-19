"""
backtest.runner – replay recorded WebSocket book data through the signal
engine and risk manager, then analyse results with vectorbt.

Workflow
--------
1. Load one or more .jsonl / .jsonl.gz recording files produced by BookRecorder.
2. Feed each record through a reconstructed LiveOrderBook (replay mode).
3. Run SignalEngine.evaluate_all() on every book update.
4. RiskManager.check() on every fired signal.
5. Simulate fills at the book prices at signal time.
6. Collect a trade log (entry/exit price, size, PnL per trade).
7. Pass the trade log to vectorbt.Portfolio for Sharpe, drawdown, hit rate, etc.

Usage
-----
    runner = BacktestRunner(
        signal_engine=engine,
        risk_manager=risk_mgr,
        initial_bankroll=1000.0,
        fill_model="best_ask",  # or "mid"
    )
    report = runner.run(recording_files=BookRecorder.list_recordings())
    print(report.summary())

Output: BacktestReport with:
    .stats          dict of aggregate metrics
    .trades_df      pandas DataFrame of individual trades
    .vbt_portfolio  vectorbt Portfolio object (call .plot() for charts)
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

from adapters.base import LiveOrderBook
from signals.base import Signal, SignalDirection
from signals.engine import SignalEngine
from risk.manager import RiskManager

logger = logging.getLogger(__name__)


# ── Simulated fill model ───────────────────────────────────────────────────────

def _simulated_fill_price(book: LiveOrderBook, direction: SignalDirection) -> float:
    """
    Return the price we'd expect to fill at for a given direction.
    Uses the best ask (for BUY_YES) or best bid complement (for BUY_NO).
    Falls back to mid if the book is one-sided.
    """
    if direction == SignalDirection.BUY_YES:
        ask = book.get_best_ask()
        return ask if ask is not None else (book.get_mid() or 0.5)
    else:
        bid = book.get_best_bid()
        return (1.0 - bid) if bid is not None else (book.get_mid() or 0.5)


# ── Report dataclass ───────────────────────────────────────────────────────────

@dataclass
class BacktestReport:
    stats: dict = field(default_factory=dict)
    trades_df: Optional[pd.DataFrame] = None
    vbt_portfolio: Optional[object] = None    # vectorbt Portfolio

    def summary(self) -> str:
        lines = ["=== Backtest Report ==="]
        for k, v in self.stats.items():
            if isinstance(v, float):
                lines.append(f"  {k:<30} {v:.4f}")
            else:
                lines.append(f"  {k:<30} {v}")
        return "\n".join(lines)

    def plot(self) -> None:
        """Plot equity curve and drawdown via vectorbt (requires display)."""
        if self.vbt_portfolio is None:
            logger.warning("BacktestReport: no vectorbt portfolio to plot")
            return
        self.vbt_portfolio.plot().show()


# ── BacktestRunner ─────────────────────────────────────────────────────────────

class BacktestRunner:
    """
    Replays recorded order book data through the signal and risk stack
    and produces a BacktestReport.
    """

    def __init__(
        self,
        signal_engine: SignalEngine,
        risk_manager: RiskManager,
        initial_bankroll: float = 1000.0,
        hold_to_resolution: bool = False,   # True = hold until market closes
        max_hold_minutes: float = 60.0,     # max minutes before flat exit
        fee_polymarket: float = 0.02,
        fee_kalshi: float = 0.07,
    ) -> None:
        self._engine = signal_engine
        self._risk = risk_manager
        self._initial_bankroll = initial_bankroll
        self._hold_to_resolution = hold_to_resolution
        self._max_hold_minutes = max_hold_minutes
        self._fees = {"polymarket": fee_polymarket, "kalshi": fee_kalshi}

    # ── Main entry point ───────────────────────────────────────────────────────

    def run(self, recording_files: List[Path]) -> BacktestReport:
        """
        Replay all recording files and return a BacktestReport.

        Parameters
        ----------
        recording_files : list of .jsonl or .jsonl.gz paths to replay
        """
        if not recording_files:
            logger.warning("BacktestRunner: no recording files provided")
            return BacktestReport(stats={"n_trades": 0})

        # Reconstruct in-memory books for replay
        replay_books: dict[str, LiveOrderBook] = {}
        trades: list[dict] = []
        open_positions: dict[str, dict] = {}   # market_id → entry info

        all_records = self._load_records(recording_files)
        logger.info("BacktestRunner: replaying %d book snapshots", len(all_records))

        bankroll = self._initial_bankroll

        for record in all_records:
            ts_str = record.get("ts", "")
            platform = record.get("platform", "unknown")
            market_id = record.get("market_id", "")

            key = f"{platform}:{market_id}"
            if key not in replay_books:
                replay_books[key] = LiveOrderBook(market_id, platform)

            book = replay_books[key]
            book.apply_snapshot(
                bids=record.get("bids", []),
                asks=record.get("asks", []),
            )

            # ── Run signals on this book update ───────────────────────────
            signals: list[Signal] = self._engine.evaluate_all()

            for sig in signals:
                sig_key = f"{sig.platform}:{sig.market_id}"
                sig_book = replay_books.get(sig_key)
                if sig_book is None:
                    continue

                # ── Check if we should exit an open position ───────────────
                if sig.market_id in open_positions:
                    pos = open_positions[sig.market_id]
                    held_minutes = (
                        datetime.fromisoformat(ts_str) - pos["entry_ts"]
                    ).total_seconds() / 60.0
                    if held_minutes >= self._max_hold_minutes:
                        exit_price = _simulated_fill_price(sig_book, sig.direction)
                        pnl = self._compute_pnl(pos, exit_price, sig.platform)
                        bankroll += pnl
                        pos["exit_price"] = exit_price
                        pos["exit_ts"] = ts_str
                        pos["pnl_usd"] = pnl
                        pos["hold_minutes"] = held_minutes
                        trades.append(pos.copy())
                        del open_positions[sig.market_id]
                        self._risk.record_close(sig.market_id, pnl)
                    continue  # don't enter same market twice

                # ── Risk check ─────────────────────────────────────────────
                decision = self._risk.check(sig)
                if not decision.approved:
                    continue

                # ── Simulated entry ────────────────────────────────────────
                entry_price = _simulated_fill_price(sig_book, sig.direction)
                fee = self._fees.get(sig.platform, 0.03)

                open_positions[sig.market_id] = {
                    "market_id": sig.market_id,
                    "platform": sig.platform,
                    "direction": sig.direction.value,
                    "signal_type": sig.signal_type.value,
                    "entry_price": entry_price,
                    "size_usd": decision.position_size_usd,
                    "entry_ts": datetime.fromisoformat(ts_str),
                    "fee": fee,
                    "edge_at_entry": sig.edge_estimate,
                }
                self._risk.record_open(sig.market_id, decision.position_size_usd)
                bankroll -= decision.position_size_usd

        # Close any positions still open at end of replay (use last known price)
        for market_id, pos in open_positions.items():
            sig_key = f"{pos['platform']}:{market_id}"
            book = replay_books.get(sig_key)
            if book:
                exit_price = book.get_mid() or pos["entry_price"]
            else:
                exit_price = pos["entry_price"]
            pnl = self._compute_pnl(pos, exit_price, pos["platform"])
            bankroll += pnl
            pos["exit_price"] = exit_price
            pos["exit_ts"] = "end_of_replay"
            pos["pnl_usd"] = pnl
            pos["hold_minutes"] = None
            trades.append(pos.copy())

        return self._build_report(trades, self._initial_bankroll, bankroll)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_records(files: List[Path]) -> List[dict]:
        """Load and chronologically sort all records from recording files."""
        records = []
        for path in files:
            try:
                opener = gzip.open if path.suffix == ".gz" else open
                mode = "rt"
                with opener(str(path), mode, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except Exception as exc:
                logger.warning("BacktestRunner: could not load %s: %s", path, exc)
        records.sort(key=lambda r: r.get("ts", ""))
        return records

    @staticmethod
    def _compute_pnl(pos: dict, exit_price: float, platform: str) -> float:
        """Compute PnL for a closed position."""
        entry = pos["entry_price"]
        size = pos["size_usd"]
        fee = pos.get("fee", 0.03)
        direction = pos.get("direction", "BUY_YES")

        if direction == "BUY_YES":
            gross = size * (exit_price / entry - 1.0) if entry > 0 else 0.0
        else:
            gross = size * ((1.0 - exit_price) / (1.0 - entry) - 1.0) if entry < 1.0 else 0.0

        return gross - size * fee

    def _build_report(
        self,
        trades: List[dict],
        initial_bankroll: float,
        final_bankroll: float,
    ) -> BacktestReport:
        """Compute aggregate stats and build the BacktestReport."""
        if not trades:
            return BacktestReport(stats={
                "n_trades": 0,
                "total_pnl_usd": 0.0,
                "initial_bankroll": initial_bankroll,
                "final_bankroll": final_bankroll,
            })

        df = pd.DataFrame(trades)
        pnls = df["pnl_usd"].values.astype(float)

        n = len(pnls)
        wins = int(np.sum(pnls > 0))
        total_pnl = float(np.sum(pnls))
        mean_pnl = float(np.mean(pnls))
        std_pnl = float(np.std(pnls)) if n > 1 else 0.0
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0

        # Cumulative bankroll for drawdown calculation
        cumulative = initial_bankroll + np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        drawdowns = (peak - cumulative) / peak
        max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0

        stats = {
            "n_trades": n,
            "hit_rate": wins / n,
            "total_pnl_usd": total_pnl,
            "mean_pnl_per_trade": mean_pnl,
            "std_pnl": std_pnl,
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": max_drawdown,
            "initial_bankroll": initial_bankroll,
            "final_bankroll": final_bankroll,
            "total_return_pct": (final_bankroll - initial_bankroll) / initial_bankroll,
        }

        # Signal-type breakdown
        if "signal_type" in df.columns:
            for stype, grp in df.groupby("signal_type"):
                stats[f"n_trades_{stype}"] = len(grp)
                stats[f"mean_pnl_{stype}"] = float(grp["pnl_usd"].mean())

        # Vectorbt integration (optional)
        vbt_portfolio = None
        try:
            import vectorbt as vbt_lib
            # Build a simple returns series for vbt
            returns = pd.Series(pnls / initial_bankroll)
            vbt_portfolio = vbt_lib.Portfolio.from_returns(returns, freq="1T")
        except ImportError:
            logger.info("BacktestRunner: vectorbt not installed – skipping vbt portfolio")
        except Exception as exc:
            logger.warning("BacktestRunner: vectorbt error: %s", exc)

        return BacktestReport(
            stats=stats,
            trades_df=df,
            vbt_portfolio=vbt_portfolio,
        )
