"""
Stream the 1.56M Kalshi historical markets, compute upset rates by category
at extreme prices (>=0.85 and <=0.15). Output CSV ranked by sample size.
Excludes obvious-upset categories (team sports, individual sports, parlays).
"""
import json
import re
from pathlib import Path
from collections import defaultdict
from decimal import Decimal, InvalidOperation

INPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")
OUTPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\upset_rates.csv")

# Category prefixes to skip entirely. Add to this list as you discover others.
EXCLUDE_PREFIXES = {
    # Team sports — high upset
    "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
    "KXEPLGAME", "KXLALIGA", "KXBUNDESLIGA", "KXSERIEA", "KXLIGUE1", "KXMLS",
    "KXCHAMPIONSLEAGUE", "KXEPL", "KXEPLGOAL", "KXEPLFIRSTGOAL",
    # Individual sports / props — huge upset
    "KXATPMATCH", "KXWTAMATCH", "KXMMA", "KXBOXING", "KXTABLETENNIS", "KXDARTSMATCH",
    # Parlays / multi-game
    "KXMVE", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMULTIGAME",
    # Esports
    "KXLOL", "KXCSGO", "KXDOTA",
    # Generic spread / props
    "KXNBASPREAD", "KXNCAAMBSPREAD", "KXNCAAWBSPREAD", "KXNFLSPREAD",
}

PREFIX_RE = re.compile(r"^(KX[A-Z]+)")

def extract_category(market_id: str) -> str | None:
    m = PREFIX_RE.match(market_id)
    return m.group(1) if m else None

def to_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(Decimal(str(v)))
    except (InvalidOperation, ValueError):
        return None

# Stats per category
stats = defaultdict(lambda: {
    "total_resolved": 0,
    "yes_resolved": 0,
    "no_resolved": 0,
    "high_price_count": 0,         # final price >= 0.85
    "high_price_resolved_yes": 0,  # of those, settled YES (correct)
    "low_price_count": 0,          # final price <= 0.15
    "low_price_resolved_no": 0,    # of those, settled NO (correct)
    "volume_sum": 0.0,
    "high_price_volume_sum": 0.0,
    "low_price_volume_sum": 0.0,
})

excluded_count = 0
unparseable_count = 0
no_volume_count = 0
processed = 0

print(f"Streaming {INPUT}...")
with open(INPUT, "r", encoding="utf-8") as f:
    for line_num, line in enumerate(f, 1):
        if line_num % 100_000 == 0:
            print(f"  {line_num:,} lines processed")
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            unparseable_count += 1
            continue

        # Need a resolved binary market
        if m.get("status") != "finalized":
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue

        # Need a market_id and category
        ticker = m.get("ticker") or m.get("market_id")
        if not ticker:
            continue
        category = extract_category(ticker)
        if category is None:
            unparseable_count += 1
            continue
        if category in EXCLUDE_PREFIXES:
            excluded_count += 1
            continue

        # Need volume to be a real market
        volume = to_float(m.get("volume_fp")) or 0.0
        if volume <= 0:
            no_volume_count += 1
            continue

        # Final price
        last_price = to_float(m.get("last_price_dollars"))
        if last_price is None:
            continue

        s = stats[category]
        s["total_resolved"] += 1
        s["volume_sum"] += volume
        if result == "yes":
            s["yes_resolved"] += 1
        else:
            s["no_resolved"] += 1

        if last_price >= 0.85:
            s["high_price_count"] += 1
            s["high_price_volume_sum"] += volume
            if result == "yes":
                s["high_price_resolved_yes"] += 1
        elif last_price <= 0.15:
            s["low_price_count"] += 1
            s["low_price_volume_sum"] += volume
            if result == "no":
                s["low_price_resolved_no"] += 1

        processed += 1

print(f"\nDone. Processed {processed:,} qualifying markets.")
print(f"  Excluded by category: {excluded_count:,}")
print(f"  No volume: {no_volume_count:,}")
print(f"  Unparseable: {unparseable_count:,}")
print(f"  Categories found: {len(stats)}")

# Compute and write CSV
import csv

rows = []
for cat, s in stats.items():
    high_n = s["high_price_count"]
    low_n = s["low_price_count"]
    high_correct_pct = (s["high_price_resolved_yes"] / high_n * 100) if high_n else None
    low_correct_pct = (s["low_price_resolved_no"] / low_n * 100) if low_n else None
    high_upset_pct = (100 - high_correct_pct) if high_correct_pct is not None else None
    low_upset_pct = (100 - low_correct_pct) if low_correct_pct is not None else None
    avg_vol = s["volume_sum"] / s["total_resolved"] if s["total_resolved"] else 0
    avg_high_vol = s["high_price_volume_sum"] / high_n if high_n else 0
    avg_low_vol = s["low_price_volume_sum"] / low_n if low_n else 0

    rows.append({
        "category": cat,
        "total_resolved": s["total_resolved"],
        "yes_pct": round(s["yes_resolved"] / s["total_resolved"] * 100, 1) if s["total_resolved"] else 0,
        "high_price_n": high_n,
        "high_price_correct_pct": round(high_correct_pct, 1) if high_correct_pct is not None else "",
        "high_price_upset_pct": round(high_upset_pct, 1) if high_upset_pct is not None else "",
        "low_price_n": low_n,
        "low_price_correct_pct": round(low_correct_pct, 1) if low_correct_pct is not None else "",
        "low_price_upset_pct": round(low_upset_pct, 1) if low_upset_pct is not None else "",
        "avg_volume": round(avg_vol, 1),
        "avg_high_price_volume": round(avg_high_vol, 1),
        "avg_low_price_volume": round(avg_low_vol, 1),
    })

# Sort by total_resolved desc so biggest categories surface first
rows.sort(key=lambda r: r["total_resolved"], reverse=True)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)

print(f"\nWrote {len(rows)} categories to {OUTPUT}")
print(f"Top 20 by sample size:")
for r in rows[:20]:
    print(f"  {r['category']}: n={r['total_resolved']} "
          f"high_n={r['high_price_n']} high_upset={r['high_price_upset_pct']}% "
          f"low_n={r['low_price_n']} low_upset={r['low_price_upset_pct']}%")