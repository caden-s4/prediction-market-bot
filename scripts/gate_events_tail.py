"""scripts/gate_events_tail.py — CLI viewer for gate_events.jsonl.

Usage:
    python -m scripts.gate_events_tail [--n 20] [--gate NAME] [--reason NAME] [--ticker PREFIX]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GATE_EVENTS_PATH = Path("data/runtime/gate_events.jsonl")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tail gate_events.jsonl")
    p.add_argument("--n", type=int, default=20, help="Last N events to show")
    p.add_argument("--gate", default=None, help="Filter by gate name")
    p.add_argument("--reason", default=None, help="Filter by reason code")
    p.add_argument("--ticker", default=None, help="Filter by ticker prefix")
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if not GATE_EVENTS_PATH.exists():
        print("no gate_events.jsonl yet — run the bot to generate events")
        sys.exit(0)

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

    # Apply filters.
    if args.gate:
        events = [e for e in events if e.get("gate") == args.gate]
    if args.reason:
        events = [e for e in events if e.get("reason") == args.reason]
    if args.ticker:
        events = [e for e in events if str(e.get("ticker", "")).startswith(args.ticker)]

    # Take last N.
    events = events[-args.n:]

    if not events:
        print("(no matching events)")
    else:
        for e in events:
            ts       = e.get("ts", "?")
            gate     = e.get("gate", "?")
            ticker   = e.get("ticker", "?")
            decision = e.get("decision", "?")
            reason   = e.get("reason") or ""
            print(f"{ts}  {gate:<22}  {ticker:<40}  {decision:<6}  {reason}")

    if unparseable:
        print(f"\n{unparseable} unparseable line(s) skipped")


if __name__ == "__main__":
    main()
