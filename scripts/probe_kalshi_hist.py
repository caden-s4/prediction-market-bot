"""
Backtest the simple closing-convergence strategy on 1.56M historical markets.

Strategy: For each resolved market, if last_price_dollars >= 0.85 (or <= 0.15
for NO side), simulate buying the corresponding side at that price and holding
to settlement.

LIMITATION: Dataset only has final price, not full price history. This is an
upper-bound estimate — assumes entry at the final observed price. Real entry
at "1h before close" would likely be at a worse price.

Output:
  - Overall stats (gross + net of fees)
  - Per-category breakdown
  - Comparison vs "trade everything" baseline
"""
import json
from pathlib import Path
from collections import defaultdict
import math

INPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")
OUTPUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\backtest_results.csv")

# Same blacklist as the upset analysis
EXCLUDE_PREFIXES = {
    "KXNBAGAME", "KXNCAAMBGAME", "KXNCAAWBGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
    "KXEPLGAME", "KXLALIGA", "KXBUNDESLIGA", "KXSERIEA", "KXLIGUE1", "KXMLS",
    "KXCHAMPIONSLEAGUE", "KXEPL", "KXEPLGOAL", "KXEPLFIRSTGOAL",
    "KXATPMATCH", "KXWTAMATCH", "KXMMA", "KXBOXING", "KXTABLETENNIS", "KXDARTSMATCH",
    "KXMVE", "KXMVESPORTSMULTIGAMEEXTENDED", "KXMULTIGAME",
    "KXLOL", "KXCSGO", "KXDOTA",
    "KXNBASPREAD", "KXNCAAMBSPREAD", "KXNCAAWBSPREAD", "KXNFLSPREAD",
}

import re
PREFIX_RE = re.compile(r"^(KX[A-Z0-9]+?)(?:-|$)")

def extract_category(market_id: str) -> str | None:
    m = PREFIX_RE.match(market_id)
    return m.group(1) if m else None

def to_float(v, d=0.0):
    try: return float(v)
    except: return d

def kalshi_fee_per_contract(price: float) -> float:
    """Round-trip fee per contract: round_up(0.07 × P × (1-P)) × 2, min $0.01 each side."""
    if price <= 0.0 or price >= 1.0:
        return 0.02  # 2× min
    one_way_raw = 0.07 * price * (1.0 - price)
    one_way = max(math.ceil(one_way_raw * 100) / 100.0, 0.01)
    return one_way * 2

# Per-category accumulator
def make_stats():
    return {
        "yes_n": 0,           # markets bought YES at >=0.85
        "yes_won": 0,         # of those, settled YES
        "yes_gross_pnl": 0.0, # per-contract gross
        "yes_net_pnl": 0.0,   # per-contract net (after round-trip fees)
        "no_n": 0,
        "no_won": 0,
        "no_gross_pnl": 0.0,
        "no_net_pnl": 0.0,
        "yes_volume_total": 0.0,
        "no_volume_total": 0.0,
    }

stats = defaultdict(make_stats)
overall = make_stats()

# Time-to-close filter — only markets where final price was within last 1 hour
# Using close_time vs assumption that last_price observation was close to close_time
# Since we don't have observation timestamp, use ALL with last_price set
PRICE_HIGH = 0.85
PRICE_LOW = 0.15
MIN_VOLUME = 50

processed = 0
excluded_blacklist = 0
excluded_low_volume = 0
no_useful_price = 0

print("Streaming and backtesting...")
with open(INPUT, "r", encoding="utf-8") as f:
    for line_num, line in enumerate(f, 1):
        if line_num % 100_000 == 0:
            print(f"  {line_num:,} processed")
        try:
            m = json.loads(line)
        except:
            continue

        if m.get("status") != "finalized":
            continue
        result = m.get("result")
        if result not in ("yes", "no"):
            continue

        ticker = m.get("ticker") or m.get("market_id")
        if not ticker:
            continue
        cat = extract_category(ticker)
        if cat is None:
            continue
        if cat in EXCLUDE_PREFIXES:
            excluded_blacklist += 1
            continue

        volume = to_float(m.get("volume_fp"))
        if volume < MIN_VOLUME:
            excluded_low_volume += 1
            continue

        last_price = to_float(m.get("last_price_dollars"))
        if last_price <= 0:
            no_useful_price += 1
            continue

        s = stats[cat]
        # YES side: bought at last_price if >=0.85
        if last_price >= PRICE_HIGH:
            s["yes_n"] += 1
            overall["yes_n"] += 1
            s["yes_volume_total"] += volume
            overall["yes_volume_total"] += volume
            # Gross pnl per contract: payoff - cost
            payoff = 1.0 if result == "yes" else 0.0
            gross = payoff - last_price
            net = gross - kalshi_fee_per_contract(last_price)
            s["yes_gross_pnl"] += gross
            s["yes_net_pnl"] += net
            overall["yes_gross_pnl"] += gross
            overall["yes_net_pnl"] += net
            if result == "yes":
                s["yes_won"] += 1
                overall["yes_won"] += 1

        # NO side: bought NO at (1 - last_price) if last_price <= 0.15
        elif last_price <= PRICE_LOW:
            no_cost = 1.0 - last_price
            s["no_n"] += 1
            overall["no_n"] += 1
            s["no_volume_total"] += volume
            overall["no_volume_total"] += volume
            payoff = 1.0 if result == "no" else 0.0
            gross = payoff - no_cost
            # Fee for NO side uses YES price symmetry
            net = gross - kalshi_fee_per_contract(last_price)
            s["no_gross_pnl"] += gross
            s["no_net_pnl"] += net
            overall["no_gross_pnl"] += gross
            overall["no_net_pnl"] += net
            if result == "no":
                s["no_won"] += 1
                overall["no_won"] += 1

        processed += 1

print(f"\nDone. {processed:,} resolved markets passed filters.")
print(f"  Excluded by blacklist: {excluded_blacklist:,}")
print(f"  Excluded low volume:   {excluded_low_volume:,}")
print(f"  No useful price:       {no_useful_price:,}")
print()

def report(name, s):
    if s["yes_n"] == 0 and s["no_n"] == 0:
        return None
    yes_winrate = (s["yes_won"] / s["yes_n"] * 100) if s["yes_n"] else 0
    no_winrate = (s["no_won"] / s["no_n"] * 100) if s["no_n"] else 0
    yes_avg_gross = (s["yes_gross_pnl"] / s["yes_n"]) if s["yes_n"] else 0
    yes_avg_net = (s["yes_net_pnl"] / s["yes_n"]) if s["yes_n"] else 0
    no_avg_gross = (s["no_gross_pnl"] / s["no_n"]) if s["no_n"] else 0
    no_avg_net = (s["no_net_pnl"] / s["no_n"]) if s["no_n"] else 0
    total_n = s["yes_n"] + s["no_n"]
    total_net = s["yes_net_pnl"] + s["no_net_pnl"]
    avg_net = total_net / total_n if total_n else 0
    return {
        "category": name,
        "yes_n": s["yes_n"],
        "yes_winrate_pct": round(yes_winrate, 1),
        "yes_avg_gross_per_contract": round(yes_avg_gross, 4),
        "yes_avg_net_per_contract": round(yes_avg_net, 4),
        "no_n": s["no_n"],
        "no_winrate_pct": round(no_winrate, 1),
        "no_avg_gross_per_contract": round(no_avg_gross, 4),
        "no_avg_net_per_contract": round(no_avg_net, 4),
        "total_n": total_n,
        "total_net_pnl_per_contract": round(avg_net, 4),
        "total_net_pnl_dollars": round(total_net, 2),
    }

# === OVERALL ===
print("=" * 70)
print("OVERALL STRATEGY PERFORMANCE")
print("=" * 70)
o = report("OVERALL", overall)
print(f"YES side (entered at >={PRICE_HIGH}):")
print(f"  Trades: {o['yes_n']:,}")
print(f"  Win rate: {o['yes_winrate_pct']}%")
print(f"  Avg gross/contract: ${o['yes_avg_gross_per_contract']}")
print(f"  Avg net/contract: ${o['yes_avg_net_per_contract']}")
print(f"NO side (entered at <={PRICE_LOW}):")
print(f"  Trades: {o['no_n']:,}")
print(f"  Win rate: {o['no_winrate_pct']}%")
print(f"  Avg gross/contract: ${o['no_avg_gross_per_contract']}")
print(f"  Avg net/contract: ${o['no_avg_net_per_contract']}")
print()
print(f"TOTAL: {o['total_n']:,} trades, ${o['total_net_pnl_dollars']:,} net P&L per contract-equivalent")
print(f"Average per trade: ${o['total_net_pnl_per_contract']}")
print()

# === PER CATEGORY ===
print("=" * 70)
print("PER-CATEGORY BREAKDOWN (top 30 by volume)")
print("=" * 70)
rows = [report(c, s) for c, s in stats.items()]
rows = [r for r in rows if r is not None]
rows.sort(key=lambda r: r["total_n"], reverse=True)

print(f"{'Category':30s} {'N':>7} {'YES_W%':>7} {'YES$net':>9} {'NO_W%':>7} {'NO$net':>9} {'Total$':>10}")
for r in rows[:30]:
    print(f"{r['category']:30s} {r['total_n']:>7,} "
          f"{r['yes_winrate_pct']:>6}% {r['yes_avg_net_per_contract']:>9} "
          f"{r['no_winrate_pct']:>6}% {r['no_avg_net_per_contract']:>9} "
          f"{r['total_net_pnl_dollars']:>10}")

# Save full CSV
import csv
OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)

print(f"\nFull results: {OUTPUT}")