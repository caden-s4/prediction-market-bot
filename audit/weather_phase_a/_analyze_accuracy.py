"""Phase 14a Steps 4 + 5: trigger accuracy + Kalshi bracket cross-reference.

Inputs:
  triggers_summary.csv  — 1 row per (station, date, kind) with trigger sim outcome
  brackets.csv          — 1 row per Kalshi market with parsed [low, high] bounds + result

Per fired trigger, compute:
  - delta_F = settled_ext - observed_ext_at_trigger    (signed; positive = trigger underestimated peak)
  - trigger_pick_bracket = which bracket contains observed_ext_at_trigger
  - actual_winner_bracket = bracket where result='yes' for that event
  - bracket_match = (trigger_pick == actual_winner)

Also classify accuracy by ±1°F proxy:
  - exact: settled_ext == observed_ext_at_trigger
  - within_1: |delta| ≤ 1
  - off: |delta| > 1

Outputs:
  accuracy_by_trigger.csv  — full per-trigger detail
  accuracy_summary.txt     — per-station and aggregate stats + delta histogram
"""
import csv
from collections import defaultdict, Counter
from pathlib import Path

OUT_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a")
TRIG_CSV = OUT_DIR / "triggers_summary.csv"
BR_CSV = OUT_DIR / "brackets.csv"

# Series → (station, kind) inverse: (station, kind) → series
KIND_SERIES = {
    ("KNYC", "high"): "KXHIGHNY",
    ("KNYC", "low"):  "KXLOWTNYC",
    ("KORD", "high"): "KXHIGHCHI",
    ("KORD", "low"):  "KXLOWTCHI",
    ("KMIA", "high"): "KXHIGHMIA",
    ("KMIA", "low"):  "KXLOWTMIA",
    ("KDEN", "high"): "KXHIGHDEN",
    ("KDEN", "low"):  "KXLOWTDEN",
}


def load_triggers():
    rows = []
    with TRIG_CSV.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def load_brackets():
    by_event = defaultdict(list)
    with BR_CSV.open("r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            key = (r["series"], r["event_date"])
            r["low"] = float(r["low"]) if r["low"] != "" else None
            r["high"] = float(r["high"]) if r["high"] != "" else None
            by_event[key].append(r)
    return by_event


def find_bracket(brackets, value):
    """Return the bracket dict whose [low, high] contains value (None = -inf or +inf)."""
    if value is None:
        return None
    for b in brackets:
        lo = b["low"]
        hi = b["high"]
        # "X or above": lo set, hi None → value >= lo
        # "X or below": lo None, hi set → value <= hi
        # "X to Y":     both set → lo <= value <= hi
        if lo is None and hi is not None:
            if value <= hi:
                return b
        elif lo is not None and hi is None:
            if value >= lo:
                return b
        elif lo is not None and hi is not None:
            if lo <= value <= hi:
                return b
    return None


triggers = load_triggers()
brackets = load_brackets()

per_station = defaultdict(lambda: {
    "total": 0, "fired": 0, "no_event": 0,
    "exact": 0, "within_1": 0, "off": 0,
    "bracket_match": 0, "bracket_mismatch": 0, "bracket_no_pick": 0,
    "deltas": [],
})

# We'll also dump a detailed per-trigger CSV
out_rows = []

for r in triggers:
    station = r["station"]
    kind = r["kind"]
    date = r["date"]
    fired = r["fired"] == "True"
    per_station[station]["total"] += 1
    if not fired:
        per_station[station]["no_pick"] = per_station[station].get("no_pick", 0)
        # not counted further; record minimal row
        out_rows.append({**r, "delta_F": "", "trigger_pick": "", "actual_winner": "",
                         "bracket_match": "", "trigger_pick_subtitle": "",
                         "actual_winner_subtitle": "", "settled_bracket_subtitle": "",
                         "abs_delta": ""})
        continue
    per_station[station]["fired"] += 1

    series = KIND_SERIES[(station, kind)]
    event_brackets = brackets.get((series, date))

    obs = float(r["observed_ext_at_trigger"]) if r["observed_ext_at_trigger"] else None
    settled = float(r["settled_ext"]) if r["settled_ext"] else None
    delta = (settled - obs) if (obs is not None and settled is not None) else None
    if delta is not None:
        per_station[station]["deltas"].append(delta)
        if delta == 0:
            per_station[station]["exact"] += 1
            per_station[station]["within_1"] += 1
        elif abs(delta) <= 1:
            per_station[station]["within_1"] += 1
        else:
            per_station[station]["off"] += 1

    if not event_brackets:
        per_station[station]["no_event"] += 1
        out_rows.append({**r, "delta_F": delta, "trigger_pick": "no_kalshi_event",
                         "actual_winner": "", "bracket_match": "",
                         "trigger_pick_subtitle": "",
                         "actual_winner_subtitle": "",
                         "settled_bracket_subtitle": "",
                         "abs_delta": abs(delta) if delta is not None else ""})
        continue

    pick_b = find_bracket(event_brackets, obs)
    settled_b = find_bracket(event_brackets, settled)
    winner_b = next((b for b in event_brackets if b["result"] == "yes"), None)

    if pick_b and winner_b:
        if pick_b["market_ticker"] == winner_b["market_ticker"]:
            per_station[station]["bracket_match"] += 1
            match_flag = True
        else:
            per_station[station]["bracket_mismatch"] += 1
            match_flag = False
    else:
        per_station[station]["bracket_no_pick"] += 1
        match_flag = None

    out_rows.append({
        **r,
        "delta_F": delta,
        "abs_delta": abs(delta) if delta is not None else "",
        "trigger_pick": pick_b["market_ticker"] if pick_b else "",
        "actual_winner": winner_b["market_ticker"] if winner_b else "",
        "bracket_match": str(match_flag) if match_flag is not None else "",
        "trigger_pick_subtitle": pick_b["subtitle"] if pick_b else "",
        "actual_winner_subtitle": winner_b["subtitle"] if winner_b else "",
        "settled_bracket_subtitle": settled_b["subtitle"] if settled_b else "",
    })


# Write per-trigger detail CSV
keys = ["station", "date", "kind", "peak_hour_local", "fired", "trigger_local",
        "trigger_temp", "observed_ext_at_trigger", "observed_ext_time_local",
        "settled_ext", "settled_local", "delta_F", "abs_delta",
        "trigger_pick", "actual_winner", "bracket_match",
        "trigger_pick_subtitle", "actual_winner_subtitle", "settled_bracket_subtitle",
        "trigger_utc", "settled_utc"]
with (OUT_DIR / "accuracy_by_trigger.csv").open("w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(out_rows)


# Aggregate report
def fmt_pct(num, den):
    if den == 0:
        return "n/a"
    return f"{100*num/den:.1f}%"


def hist(deltas, bins):
    c = Counter()
    for d in deltas:
        for lo, hi, label in bins:
            if lo <= d < hi:
                c[label] += 1
                break
    return c


BINS = [
    (-100, -5, "<= -5"),
    (-5, -3,   "-5..-3"),
    (-3, -1,   "-3..-1"),
    (-1, 0,    "-1..0"),
    (0, 0.0001,"0"),
    (0.0001, 1+0.0001, "0..1"),
    (1+0.0001, 3+0.0001, "1..3"),
    (3+0.0001, 5+0.0001, "3..5"),
    (5+0.0001, 100, ">5"),
]

lines = []
lines.append("=" * 76)
lines.append("Phase 14a — trigger accuracy and Kalshi bracket cross-reference")
lines.append("=" * 76)

agg = {
    "total": 0, "fired": 0, "no_event": 0,
    "exact": 0, "within_1": 0, "off": 0,
    "bracket_match": 0, "bracket_mismatch": 0, "bracket_no_pick": 0,
    "deltas": [],
}

for station in ["KNYC", "KORD", "KMIA", "KDEN"]:
    s = per_station[station]
    fired = s["fired"]
    lines.append(f"\n--- {station} ---")
    lines.append(f"  total trigger checks      : {s['total']}")
    lines.append(f"  triggers fired            : {fired}  ({fmt_pct(fired, s['total'])})")
    lines.append(f"  fired but no Kalshi event : {s['no_event']}")
    lines.append(f"  Step 4 — accuracy proxy (settled vs trigger-time obs):")
    lines.append(f"    exact (delta=0)         : {s['exact']:>3}  ({fmt_pct(s['exact'], fired)})")
    lines.append(f"    within ±1°F             : {s['within_1']:>3}  ({fmt_pct(s['within_1'], fired)})")
    lines.append(f"    off >1°F                : {s['off']:>3}  ({fmt_pct(s['off'], fired)})")
    lines.append(f"  Step 5 — Kalshi bracket cross-reference:")
    lines.append(f"    pick == actual winner   : {s['bracket_match']:>3}  ({fmt_pct(s['bracket_match'], fired - s['no_event'])})")
    lines.append(f"    pick != actual winner   : {s['bracket_mismatch']:>3}  ({fmt_pct(s['bracket_mismatch'], fired - s['no_event'])})")
    lines.append(f"    no pick / no event      : {s['bracket_no_pick']:>3}")
    lines.append(f"  delta histogram (settled - trigger-time obs):")
    h = hist(s["deltas"], BINS)
    for _, _, label in BINS:
        if h[label]:
            lines.append(f"    {label:>8} : {'#' * h[label]} ({h[label]})")

    for k in ("total", "fired", "no_event", "exact", "within_1", "off",
              "bracket_match", "bracket_mismatch", "bracket_no_pick"):
        agg[k] += s[k]
    agg["deltas"].extend(s["deltas"])

lines.append("\n" + "=" * 76)
lines.append("AGGREGATE (4 stations, both kinds)")
lines.append("=" * 76)
fired = agg["fired"]
lines.append(f"  total trigger checks      : {agg['total']}")
lines.append(f"  triggers fired            : {fired}  ({fmt_pct(fired, agg['total'])})")
lines.append(f"  Step 4 — accuracy proxy:")
lines.append(f"    exact (delta=0)         : {agg['exact']}  ({fmt_pct(agg['exact'], fired)})")
lines.append(f"    within ±1°F             : {agg['within_1']}  ({fmt_pct(agg['within_1'], fired)})")
lines.append(f"    off >1°F                : {agg['off']}  ({fmt_pct(agg['off'], fired)})")
lines.append(f"  Step 5 — Kalshi bracket match (where Kalshi event exists):")
lines.append(f"    pick == actual winner   : {agg['bracket_match']}  ({fmt_pct(agg['bracket_match'], fired - agg['no_event'])})")
lines.append(f"    pick != actual winner   : {agg['bracket_mismatch']}  ({fmt_pct(agg['bracket_mismatch'], fired - agg['no_event'])})")
lines.append(f"    fired but no Kalshi event: {agg['no_event']}")
lines.append(f"  aggregate delta histogram (signed °F):")
h = hist(agg["deltas"], BINS)
for _, _, label in BINS:
    if h[label]:
        lines.append(f"    {label:>8} : {'#' * h[label]} ({h[label]})")

# basic stats on delta
if agg["deltas"]:
    deltas = agg["deltas"]
    abs_deltas = [abs(d) for d in deltas]
    lines.append(f"\n  delta stats (signed)  mean={sum(deltas)/len(deltas):+.2f}°F  max={max(deltas):+.1f}°F  min={min(deltas):+.1f}°F")
    lines.append(f"  delta stats (abs)     mean={sum(abs_deltas)/len(abs_deltas):.2f}°F  p90={sorted(abs_deltas)[int(0.9*len(abs_deltas))]:.1f}°F  max={max(abs_deltas):.1f}°F")

text = "\n".join(lines)
print(text)
(OUT_DIR / "accuracy_summary.txt").write_text(text + "\n", encoding="utf-8")
print(f"\nWrote {OUT_DIR / 'accuracy_summary.txt'}")
print(f"Wrote {OUT_DIR / 'accuracy_by_trigger.csv'}")
