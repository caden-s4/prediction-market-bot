"""
Per-class P&L diagnostic — to-do #13.

Buckets clean ghost trades by (source, signal_class) and reports P&L total,
WR, EV/$ risked, and N. Writes a markdown report and a CSV.

Clean trade set comes from `scripts/_ghost_loader.load_clean_trades` — see
that module's docstring for the exact filter set. Closed positions only;
open positions appear in a footer count.

Usage:
    python scripts/per_class_pnl.py [--last-7d-only]

Outputs:
    docs/diagnostics/per_class_pnl.md
    docs/diagnostics/per_class_pnl.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from scripts._ghost_loader import (
    Trade,
    ShrinkageReport,
    load_clean_trades,
    open_positions_by_source_class,
)

OUT_DIR = ROOT / "docs" / "diagnostics"
MD_OUT = OUT_DIR / "per_class_pnl.md"
CSV_OUT = OUT_DIR / "per_class_pnl.csv"

LOW_POWER_N = 10
LAST_7D_DAYS = 7


@dataclass
class Bucket:
    source: str
    signal_class: str
    n: int
    pnl_total_usd: float
    win_rate: float
    loss_rate: float
    flat_rate: float
    avg_pnl_usd: float
    ev_per_dollar_risked: float
    low_power: bool


def _bucket_trades(trades: list[Trade]) -> list[Bucket]:
    groups: dict[tuple[str, str], list[Trade]] = defaultdict(list)
    for t in trades:
        groups[(t.source, t.signal_class)].append(t)

    out: list[Bucket] = []
    for (src, sc), ts in groups.items():
        n = len(ts)
        pnl_total = sum(t.pnl for t in ts)
        n_win = sum(1 for t in ts if t.pnl > 0)
        n_loss = sum(1 for t in ts if t.pnl < 0)
        n_flat = sum(1 for t in ts if t.pnl == 0)
        size_total = sum(t.size_usd for t in ts)
        ev = pnl_total / size_total if size_total > 0 else 0.0
        out.append(Bucket(
            source=src,
            signal_class=sc,
            n=n,
            pnl_total_usd=pnl_total,
            win_rate=n_win / n,
            loss_rate=n_loss / n,
            flat_rate=n_flat / n,
            avg_pnl_usd=pnl_total / n,
            ev_per_dollar_risked=ev,
            low_power=n < LOW_POWER_N,
        ))
    out.sort(key=lambda b: b.pnl_total_usd)
    return out


def _format_bucket_table(buckets: list[Bucket]) -> str:
    if not buckets:
        return "_(no trades in this window)_\n"
    rows = [
        "| Source | Signal Class | N | Total P&L | WR | Loss% | Flat% | Avg | EV/$ | Note |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for b in buckets:
        note = "low-power" if b.low_power else ""
        rows.append(
            f"| {b.source} | {b.signal_class} | {b.n} | "
            f"${b.pnl_total_usd:+.2f} | {b.win_rate:.1%} | "
            f"{b.loss_rate:.1%} | {b.flat_rate:.1%} | "
            f"${b.avg_pnl_usd:+.2f} | {b.ev_per_dollar_risked:+.4f} | {note} |"
        )
    return "\n".join(rows) + "\n"


def _format_shrinkage(report: ShrinkageReport) -> str:
    paired_after_orphans = report.paired
    after_sign = paired_after_orphans - report.sign_inverted_excluded
    after_clamp = after_sign - report.clamped_excluded
    after_sports = after_clamp - report.pre_fix_sports_excluded
    lines = [
        "| Stage | Count | Running |",
        "| --- | ---: | ---: |",
        f"| Raw entries | {report.raw_entries} | |",
        f"| Raw exits | {report.raw_exits} | |",
        f"| Paired | {report.paired} | {report.paired} |",
        f"| Filter: sign-inverted | -{report.sign_inverted_excluded} | {after_sign} |",
        f"| Filter: clamped | -{report.clamped_excluded} | {after_clamp} |",
        f"| Filter: pre-2026-04-15 sports | -{report.pre_fix_sports_excluded} | {after_sports} |",
        f"| Filter: orphans (open) | -{report.orphans_open} | (already excluded by pairing) |",
        f"| Filter: orphans (log-missing-exit) | -{report.orphans_log_missing_exit} | (already excluded by pairing) |",
        f"| Clean | {report.clean_count} | {report.clean_count} |",
    ]
    assert after_sports == report.clean_count, (
        f"shrinkage math mismatch: after_sports={after_sports} clean={report.clean_count}"
    )
    return "\n".join(lines) + "\n"


def _format_open_positions(open_groups: dict[tuple[str, str], int]) -> str:
    if not open_groups:
        return "_(no open ghost positions)_\n"
    rows = [
        "| Source | Signal Class | Open Count |",
        "| --- | --- | ---: |",
    ]
    for (src, sc), n in sorted(open_groups.items(), key=lambda kv: -kv[1]):
        rows.append(f"| {src} | {sc} | {n} |")
    return "\n".join(rows) + "\n"


def _write_csv(
    all_time: list[Bucket],
    last_7d: list[Bucket],
    path: Path,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "window", "source", "signal_class", "n",
        "pnl_total_usd", "win_rate", "loss_rate", "flat_rate",
        "avg_pnl_usd", "ev_per_dollar_risked", "low_power",
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for window, buckets in (("all_time", all_time), ("last_7d", last_7d)):
            for b in buckets:
                writer.writerow({
                    "window": window,
                    "source": b.source,
                    "signal_class": b.signal_class,
                    "n": b.n,
                    "pnl_total_usd": f"{b.pnl_total_usd:.4f}",
                    "win_rate": f"{b.win_rate:.6f}",
                    "loss_rate": f"{b.loss_rate:.6f}",
                    "flat_rate": f"{b.flat_rate:.6f}",
                    "avg_pnl_usd": f"{b.avg_pnl_usd:.4f}",
                    "ev_per_dollar_risked": f"{b.ev_per_dollar_risked:.6f}",
                    "low_power": "true" if b.low_power else "false",
                })


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-class P&L diagnostic")
    parser.add_argument(
        "--last-7d-only",
        action="store_true",
        default=False,
        help="Suppress the all-time view; only render last-7d.",
    )
    args = parser.parse_args()

    trades, report = load_clean_trades()
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=LAST_7D_DAYS)

    trades_7d = [t for t in trades if t.event_exit_ts >= cutoff_7d]
    all_time_buckets = _bucket_trades(trades) if not args.last_7d_only else []
    last_7d_buckets = _bucket_trades(trades_7d)

    open_groups = open_positions_by_source_class()

    md = []
    md.append("# Per-class P&L diagnostic\n")
    md.append(f"Generated: {now.isoformat()}\n")
    md.append("\n## Shrinkage report\n\n")
    md.append(_format_shrinkage(report))
    if not args.last_7d_only:
        md.append("\n## All-time clean P&L by (source, signal_class)\n\n")
        md.append("Sorted by total P&L, most-bleeding first. "
                  "Rows marked `low-power` have N<10.\n\n")
        md.append(_format_bucket_table(all_time_buckets))
    md.append(f"\n## Last {LAST_7D_DAYS} days clean P&L by (source, signal_class)\n\n")
    md.append(f"Window: trades with exit_ts >= {cutoff_7d.isoformat()}\n\n")
    md.append(_format_bucket_table(last_7d_buckets))
    md.append("\n## Open positions by (source, signal_class)\n\n")
    md.append("Currently-open ghost positions (from `ghost_positions.json`). "
              "Excluded from the P&L tables above (closed-only).\n\n")
    md.append(_format_open_positions(open_groups))
    md.append("\n## Methodology\n\n")
    md.append("- Filter set documented in `scripts/_ghost_loader.py` "
              "(see module docstring).\n")
    md.append("- pnl=0 trades retained as valid flat exits "
              "(audit's \"unverifiable\" tag was for clamping detection, "
              "not analytical validity).\n")
    md.append("- Closed positions only; open positions in footer.\n")
    md.append("- Generated by `python scripts/per_class_pnl.py`.\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(MD_OUT, "w", encoding="utf-8") as fh:
        fh.write("".join(md))

    _write_csv(all_time_buckets, last_7d_buckets, CSV_OUT)

    print(f"Wrote {MD_OUT.relative_to(ROOT)}")
    print(f"Wrote {CSV_OUT.relative_to(ROOT)}")
    print(f"Clean trades: {report.clean_count}  "
          f"(orphans excluded: {report.orphans_excluded} "
          f"= {report.orphans_open} open + "
          f"{report.orphans_log_missing_exit} log-missing-exit)")
    if all_time_buckets:
        print("\nTop-3 bleeding buckets (all-time):")
        for b in all_time_buckets[:3]:
            print(f"  {b.source} / {b.signal_class}  "
                  f"N={b.n}  P&L=${b.pnl_total_usd:+.2f}  "
                  f"WR={b.win_rate:.1%}  EV/$={b.ev_per_dollar_risked:+.4f}")


if __name__ == "__main__":
    main()
