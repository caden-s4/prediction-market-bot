"""
shared/paper_log.py – Append-only ghost-trade journal.

PaperTradeLog records every ghost-trade entry and exit to a JSONL file
(ghost_trades.jsonl in the working directory).  Each line is a self-contained
JSON object with an "event" field: "entry" or "exit".

Entry fields:
  event, ts, market_id, platform, action, entry_price, size_usd,
  gt_prob, gap, confidence, source, tier, question

Exit fields:
  event, ts, market_id, exit_price, pnl, pnl_pct,
  exit_reason, hold_duration_minutes, exit_was_decisive_gt
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "ghost_trades.jsonl"


class PaperTradeLog:
    """Persistent, append-only log of ghost trades from entry to exit."""

    def __init__(self, path: str = _DEFAULT_PATH) -> None:
        self._path = Path(path)

    # ── Write API ─────────────────────────────────────────────────────────────

    def log_entry(
        self,
        market_id: str,
        platform: str,
        action: str,
        entry_price: float,
        size_usd: float,
        gt_prob: float,
        gap: float,
        confidence: float,
        source: str,
        tier: int,
        question: str,
        entry_time: Optional[float] = None,
    ) -> None:
        """Append an entry event to the log."""
        import time as _time
        ts = (
            datetime.fromtimestamp(entry_time, tz=timezone.utc).isoformat()
            if entry_time is not None
            else datetime.now(timezone.utc).isoformat()
        )
        record = {
            "event": "entry",
            "ts": ts,
            "market_id": market_id,
            "platform": platform,
            "action": action,
            "entry_price": round(entry_price, 5),
            "size_usd": round(size_usd, 2),
            "gt_prob": round(gt_prob, 5),
            "gap": round(gap, 5),
            "confidence": round(confidence, 4),
            "source": source,
            "tier": tier,
            "question": question,
        }
        self._append(record)

    def log_cap_blocked(
        self,
        market_id: str,
        action: str,
        entry_price: float,
        size_usd: float,
        gt_prob: float,
        gap: float,
        series_root: str,
        series_exposure: float,
        max_series_exposure: float,
    ) -> None:
        """Append a cap_blocked event — a trade that passed all checks but was
        stopped by the per-series exposure cap.  Logged so the data is available
        for post-session analysis without needing a live position entry."""
        record = {
            "event": "cap_blocked",
            "ts": datetime.now(timezone.utc).isoformat(),
            "market_id": market_id,
            "action": action,
            "entry_price": round(entry_price, 5),
            "size_usd": round(size_usd, 2),
            "gt_prob": round(gt_prob, 5),
            "gap": round(gap, 5),
            "series_root": series_root,
            "current_series_exposure": round(series_exposure, 2),
            "max_series_exposure": round(max_series_exposure, 2),
            "reason": (
                f"series exposure cap: ${series_exposure:.2f} + "
                f"${size_usd:.2f} > ${max_series_exposure:.2f}"
            ),
        }
        self._append(record)

    def log_exit(
        self,
        market_id: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        exit_reason: str,
        hold_duration_minutes: float,
        exit_time: Optional[float] = None,
        exit_was_decisive_gt: bool = False,
    ) -> None:
        """Append an exit event to the log."""
        ts = (
            datetime.fromtimestamp(exit_time, tz=timezone.utc).isoformat()
            if exit_time is not None
            else datetime.now(timezone.utc).isoformat()
        )
        record = {
            "event": "exit",
            "ts": ts,
            "market_id": market_id,
            "exit_price": round(exit_price, 5),
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 4),
            "exit_reason": exit_reason,
            "hold_duration_minutes": round(hold_duration_minutes, 1),
            "exit_was_decisive_gt": exit_was_decisive_gt,
        }
        self._append(record)

    def _append(self, record: dict) -> None:
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("PaperTradeLog: failed to write record: %s", exc)

    # ── Read API ──────────────────────────────────────────────────────────────

    def get_trades(self, since: Optional[datetime] = None) -> List[dict]:
        """
        Return all records from the log, optionally filtered to on/after `since`.

        Records are returned in file order (chronological).
        """
        records: List[dict] = []
        if not self._path.exists():
            return records
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if since is not None:
                        try:
                            rec_dt = datetime.fromisoformat(rec["ts"])
                            if rec_dt.tzinfo is None:
                                rec_dt = rec_dt.replace(tzinfo=timezone.utc)
                            if rec_dt < since:
                                continue
                        except (KeyError, ValueError):
                            continue
                    records.append(rec)
        except Exception as exc:
            logger.warning("PaperTradeLog: failed to read log: %s", exc)
        return records

    def get_daily_summary(
        self, date: Optional[datetime] = None
    ) -> dict:
        """
        Return a summary dict for a single calendar day (UTC).

        Parameters
        ----------
        date : specific date to summarise; defaults to today UTC.

        Summary keys
        ------------
        date_str, total_entries, exits, open_positions,
        wins, losses, win_rate, total_pnl, avg_pnl_per_trade,
        avg_gap_at_entry, avg_hold_minutes,
        best_trade, worst_trade, by_source
        """
        if date is None:
            date = datetime.now(timezone.utc)
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0,
                                  tzinfo=timezone.utc)
        day_end   = date.replace(hour=23, minute=59, second=59, microsecond=999999,
                                  tzinfo=timezone.utc)

        all_records = self.get_trades()

        # Separate entries, exits, and cap-blocked events for this day.
        entries:     List[dict] = []
        exits:       List[dict] = []
        cap_blocked: List[dict] = []
        for rec in all_records:
            try:
                ts = datetime.fromisoformat(rec["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if day_start <= ts <= day_end:
                if rec.get("event") == "entry":
                    entries.append(rec)
                elif rec.get("event") == "exit":
                    exits.append(rec)
                elif rec.get("event") == "cap_blocked":
                    cap_blocked.append(rec)

        # Match exits to entries by market_id (most recent entry per market).
        # Build an index: market_id → latest entry record.
        entry_by_mid: Dict[str, dict] = {}
        for e in entries:
            entry_by_mid[e["market_id"]] = e

        # Compute per-exit stats.
        wins = 0
        losses = 0
        drawdowns = 0
        total_pnl = 0.0
        pnl_list: List[float] = []
        gap_list:  List[float] = []
        hold_list: List[float] = []
        best_trade: Optional[dict]  = None
        worst_trade: Optional[dict] = None
        by_source: Dict[str, dict]  = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})

        for ex in exits:
            pnl   = ex.get("pnl", 0.0)
            hold  = ex.get("hold_duration_minutes", 0.0)
            mid   = ex.get("market_id", "?")
            entry = entry_by_mid.get(mid)

            total_pnl += pnl
            pnl_list.append(pnl)
            hold_list.append(hold)

            if pnl > 0:
                wins += 1
            elif pnl == 0:
                drawdowns += 1
            else:
                losses += 1

            gap = entry.get("gap", 0.0) if entry else 0.0
            gap_list.append(gap)
            src = entry.get("source", "unknown") if entry else "unknown"

            by_source[src]["trades"] += 1
            by_source[src]["pnl"]    += pnl
            if pnl > 0:
                by_source[src]["wins"] += 1

            trade_summary = {
                "market_id": mid,
                "pnl": round(pnl, 4),
                "source": src,
                "hold_minutes": round(hold, 1),
                "exit_reason": ex.get("exit_reason", "?"),
            }
            if best_trade is None or pnl > best_trade["pnl"]:
                best_trade = trade_summary
            if worst_trade is None or pnl < worst_trade["pnl"]:
                worst_trade = trade_summary

        n_exits = len(exits)
        win_rate = wins / n_exits if n_exits > 0 else 0.0

        # Open positions = entries this day with no matching exit (unfilled orders).
        exited_mids = {ex.get("market_id") for ex in exits}
        open_today  = sum(1 for e in entries if e["market_id"] not in exited_mids)

        return {
            "date_str":           date.strftime("%Y-%m-%d"),
            "total_entries":      len(entries),
            "exits":              n_exits,
            "open_positions":     open_today,
            "unfilled_orders":    open_today,
            "cap_blocked":        len(cap_blocked),
            "wins":               wins,
            "losses":             losses,
            "drawdowns":          drawdowns,
            "win_rate":           round(win_rate, 3),
            "total_pnl":          round(total_pnl, 4),
            "avg_pnl_per_trade":  round(total_pnl / n_exits, 4) if n_exits > 0 else 0.0,
            "avg_gap_at_entry":   round(sum(gap_list) / len(gap_list), 4) if gap_list else 0.0,
            "avg_hold_minutes":   round(sum(hold_list) / len(hold_list), 1) if hold_list else 0.0,
            "best_trade":         best_trade,
            "worst_trade":        worst_trade,
            "by_source":          dict(by_source),
        }
