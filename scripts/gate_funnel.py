"""scripts/gate_funnel.py — Gate funnel analysis for pipeline diagnostics.

Usage:
    python -m scripts.gate_funnel
        [--since <duration>]    # e.g. "1h", "30m", "24h" — default 1h
        [--ticker <prefix>]     # filter to tickers starting with prefix
        [--gate <name>]         # filter to a single gate
        [--reason <name>]       # filter to a single reason
        [--top <n>]             # top N reasons per gate — default 10
        [--detail]              # show ticker-level detail per reason
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

GATE_EVENTS_PATH = Path("data/runtime/gate_events.jsonl")

PIPELINE_ORDER = [
    "scanner_reject",
    "gt_routing",
    "confidence",
    "executor_pretrade",
    "snipe",
]


def _parse_since(value: str) -> timedelta:
    if value.endswith("h"):
        try:
            return timedelta(hours=float(value[:-1]))
        except ValueError:
            pass
    elif value.endswith("m"):
        try:
            return timedelta(minutes=float(value[:-1]))
        except ValueError:
            pass
    elif value.endswith("s"):
        try:
            return timedelta(seconds=float(value[:-1]))
        except ValueError:
            pass
    print(f"error: --since '{value}' is not a valid duration (use e.g. 1h, 30m, 90s)", file=sys.stderr)
    sys.exit(1)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Gate funnel analysis for gate_events.jsonl")
    p.add_argument("--since", default="1h", help="Time window (e.g. 1h, 30m, 24h) — default 1h")
    p.add_argument("--ticker", default=None, help="Filter to tickers starting with prefix (case-insensitive)")
    p.add_argument("--gate", default=None, help="Filter to a single gate name")
    p.add_argument("--reason", default=None, help="Filter to a single reason code")
    p.add_argument("--top", type=int, default=10, help="Top N reasons per gate — default 10")
    p.add_argument("--detail", action="store_true", help="Show ticker-level detail per reason")
    return p.parse_args()


def _parse_ts(ts_str: str) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def main() -> None:
    args = _parse_args()
    window = _parse_since(args.since)

    if not GATE_EVENTS_PATH.exists():
        print("error: data/runtime/gate_events.jsonl not found — run the bot to generate events", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(timezone.utc)
    cutoff = now - window

    events: list[dict] = []
    unparseable = 0

    with GATE_EVENTS_PATH.open("r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                events.append(json.loads(raw))
            except json.JSONDecodeError:
                unparseable += 1

    # Time filter.
    filtered: list[dict] = []
    for e in events:
        ts = _parse_ts(e.get("ts", ""))
        if ts is None or ts < cutoff:
            continue
        filtered.append(e)

    # Ticker prefix filter (case-insensitive).
    if args.ticker:
        prefix = args.ticker.upper()
        filtered = [e for e in filtered if str(e.get("ticker", "")).upper().startswith(prefix)]

    # Gate filter.
    if args.gate:
        filtered = [e for e in filtered if e.get("gate") == args.gate]

    # Reason filter.
    if args.reason:
        filtered = [e for e in filtered if e.get("reason") == args.reason]

    if unparseable:
        print(f"{unparseable} unparseable line(s) skipped\n")

    if not filtered:
        print("no events match filter criteria")
        sys.exit(0)

    # Window header.
    since_label = args.since
    window_start = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"Gate funnel — last {since_label} ({window_start} to {window_end})\n")

    # Aggregate per gate.
    gate_counts: Counter[str] = Counter()
    gate_reason_counts: dict[str, Counter[str]] = defaultdict(Counter)
    # For detail: (gate, reason) -> ticker counts + most recent extra.
    gate_reason_tickers: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    gate_reason_latest_extra: dict[tuple[str, str], dict | None] = {}

    for e in filtered:
        gate = e.get("gate") or "unknown"
        reason = e.get("reason") or "(none)"
        ticker = e.get("ticker") or ""
        extra = e.get("extra")

        gate_counts[gate] += 1
        gate_reason_counts[gate][reason] += 1
        gate_reason_tickers[(gate, reason)][ticker] += 1
        # last-write wins — JSONL is chronological so last = most recent
        gate_reason_latest_extra[(gate, reason)] = extra

    # Determine which gates to display and in what order.
    if args.gate:
        gates_to_show = [args.gate] if args.gate in gate_counts else list(gate_counts.keys())
    else:
        # Pipeline order first, then any extra gates not in the canonical list.
        seen = set()
        gates_to_show = []
        for g in PIPELINE_ORDER:
            if g in gate_counts:
                gates_to_show.append(g)
                seen.add(g)
        for g in gate_counts:
            if g not in seen:
                gates_to_show.append(g)

    for gate in gates_to_show:
        total = gate_counts[gate]
        print(f"{gate:<28}  {total:,} events")

        reason_counts = gate_reason_counts[gate]
        sorted_reasons = reason_counts.most_common()
        shown = sorted_reasons[: args.top]
        remaining = sorted_reasons[args.top :]

        for reason, count in shown:
            pct = count / total * 100
            print(f"  {reason:<34}  {count:>6,} ({pct:.1f}%)")

            if args.detail:
                # Ticker breakdown.
                ticker_counts = gate_reason_tickers[(gate, reason)]
                top_tickers = ticker_counts.most_common(5)
                rest = len(ticker_counts) - len(top_tickers)
                for ticker, tc in top_tickers:
                    print(f"    {ticker:<50}  {tc}x")
                if rest > 0:
                    print(f"    ... and {rest} more ticker(s)")

                # Sample extra payload.
                extra = gate_reason_latest_extra.get((gate, reason))
                if extra and isinstance(extra, dict):
                    pairs = ", ".join(f"{k}={v}" for k, v in extra.items())
                    # Wrap long lines at ~100 chars.
                    if len(pairs) > 90:
                        lines = []
                        current = "    sample extra: "
                        for part in pairs.split(", "):
                            if len(current) + len(part) + 2 > 100 and len(current) > 18:
                                lines.append(current.rstrip(", "))
                                current = "                  " + part + ", "
                            else:
                                current += part + ", "
                        lines.append(current.rstrip(", "))
                        print("\n".join(lines))
                    else:
                        print(f"    sample extra: {pairs}")

        if remaining:
            leftover_count = sum(c for _, c in remaining)
            print(f"  ... and {len(remaining)} more reason(s) ({leftover_count:,} events)")

        print()


if __name__ == "__main__":
    main()
