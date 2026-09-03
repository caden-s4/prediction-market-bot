"""Add bracket-distance metric: how far is trigger_pick from actual_winner?

Proposed trigger trades winner + ±2 adjacent. So if |distance| ≤ 2, our portfolio
includes the actual winner. Compute that.
"""
import csv
from collections import defaultdict, Counter
from pathlib import Path

OUT_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a")
BR_CSV = OUT_DIR / "brackets.csv"
ACC_CSV = OUT_DIR / "accuracy_by_trigger.csv"


def order_brackets(rows):
    """Order brackets by temperature ascending. Returns list of (idx, bracket_dict).

    Brackets are: 1 with low=None hi=Y (≤Y), several with low=X hi=Y (X to Y),
                  1 with low=X hi=None (≥X). Sort by midpoint where possible,
                  treating bounded ends as below all and above all the others.
    """
    def sort_key(b):
        lo = float(b["low"]) if b["low"] != "" else None
        hi = float(b["high"]) if b["high"] != "" else None
        if lo is None and hi is not None:
            return (-1e9, hi)  # ≤Y → lowest temperatures
        if lo is not None and hi is None:
            return (1e9, lo)   # ≥X → highest temperatures
        return ((lo + hi) / 2, lo)
    return sorted(rows, key=sort_key)


# Load brackets, group by event
events = defaultdict(list)
with BR_CSV.open("r", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        events[(r["series"], r["event_date"])].append(r)

# Order each event's brackets
for k in events:
    events[k] = order_brackets(events[k])

# Build map: market_ticker → ordered_index
ticker_to_idx = {}
for k, lst in events.items():
    for i, b in enumerate(lst):
        ticker_to_idx[b["market_ticker"]] = (k, i, len(lst))

# Walk accuracy CSV
out_rows = []
with ACC_CSV.open("r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames + ["pick_idx", "winner_idx", "bracket_distance"]
    for r in reader:
        pick = r.get("trigger_pick", "")
        winner = r.get("actual_winner", "")
        if pick and winner and pick in ticker_to_idx and winner in ticker_to_idx:
            _, pick_idx, _ = ticker_to_idx[pick]
            _, winner_idx, _ = ticker_to_idx[winner]
            r["pick_idx"] = pick_idx
            r["winner_idx"] = winner_idx
            r["bracket_distance"] = abs(pick_idx - winner_idx)
        else:
            r["pick_idx"] = ""
            r["winner_idx"] = ""
            r["bracket_distance"] = ""
        out_rows.append(r)

with (OUT_DIR / "accuracy_by_trigger.csv").open("w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    w.writerows(out_rows)


# Per-station distance distribution
per_station = defaultdict(Counter)
for r in out_rows:
    if r["bracket_distance"] != "":
        per_station[r["station"]][int(r["bracket_distance"])] += 1

print("Bracket distance distribution (|pick_idx - winner_idx|), per station:")
print(f"  {'station':<8} {'n':>4} {'d=0':>5} {'d=1':>5} {'d=2':>5} {'d=3':>5} {'d=4':>5} {'d=5':>5}   in_band(±2)")
agg = Counter()
agg_n = 0
for s in ["KNYC", "KORD", "KMIA", "KDEN"]:
    c = per_station[s]
    n = sum(c.values())
    in_band = sum(c.get(d, 0) for d in (0, 1, 2))
    pct = 100 * in_band / n if n else 0
    print(f"  {s:<8} {n:>4} {c.get(0,0):>5} {c.get(1,0):>5} {c.get(2,0):>5} "
          f"{c.get(3,0):>5} {c.get(4,0):>5} {c.get(5,0):>5}   {in_band}/{n} = {pct:.1f}%")
    for k, v in c.items():
        agg[k] += v
    agg_n += n

n = sum(agg.values())
in_band = sum(agg.get(d, 0) for d in (0, 1, 2))
pct = 100 * in_band / n if n else 0
print(f"  {'AGG':<8} {n:>4} {agg.get(0,0):>5} {agg.get(1,0):>5} {agg.get(2,0):>5} "
      f"{agg.get(3,0):>5} {agg.get(4,0):>5} {agg.get(5,0):>5}   {in_band}/{n} = {pct:.1f}%")

# Append summary to accuracy_summary.txt
extra = ["", "=" * 76,
         "Bracket-distance distribution (|pick_idx - winner_idx|)",
         "=" * 76,
         f"  {'station':<8} {'n':>4} {'d=0':>5} {'d=1':>5} {'d=2':>5} {'d=3':>5} {'d=4':>5} {'d=5':>5}   in_band(±2)"]
for s in ["KNYC", "KORD", "KMIA", "KDEN"]:
    c = per_station[s]
    n = sum(c.values())
    in_band = sum(c.get(d, 0) for d in (0, 1, 2))
    pct = 100 * in_band / n if n else 0
    extra.append(f"  {s:<8} {n:>4} {c.get(0,0):>5} {c.get(1,0):>5} {c.get(2,0):>5} "
                 f"{c.get(3,0):>5} {c.get(4,0):>5} {c.get(5,0):>5}   {in_band}/{n} = {pct:.1f}%")
n = sum(agg.values())
in_band = sum(agg.get(d, 0) for d in (0, 1, 2))
pct = 100 * in_band / n if n else 0
extra.append(f"  {'AGG':<8} {n:>4} {agg.get(0,0):>5} {agg.get(1,0):>5} {agg.get(2,0):>5} "
             f"{agg.get(3,0):>5} {agg.get(4,0):>5} {agg.get(5,0):>5}   {in_band}/{n} = {pct:.1f}%")

ftxt = OUT_DIR / "accuracy_summary.txt"
existing = ftxt.read_text(encoding="utf-8")
ftxt.write_text(existing + "\n".join(extra) + "\n", encoding="utf-8")
print(f"\nAppended bracket-distance summary to {ftxt}")
