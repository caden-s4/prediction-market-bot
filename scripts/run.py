"""
Find EVERY rejection reason inside _try_execute for ACTIONABLE signals.
Broader pattern matching than the previous trace.
"""
from pathlib import Path
from collections import Counter

LOG_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\logs")

# Rejection patterns that _try_execute might log
REJECTION_PATTERNS = [
    ("extreme_entry_price", "extreme_entry_price"),
    ("gt_stale_at_entry", "gt_stale_at_entry"),
    ("large_div_extreme", "large_divergence_extreme_market"),
    ("confidence_fail", "ConfidenceScorer: SKIP"),
    ("confidence_blocked", "confidence_blocked"),
    ("ev_fail", "EV check FAIL"),
    ("ev_recheck_fail", "stale_price_ev_recheck"),
    ("orderbook_empty", "order book empty"),
    ("orderbook_empty2", "empty book"),
    ("cooldown", "cooldown"),
    ("exit_cooldown", "exit cooldown"),
    ("position_exists", "position already"),
    ("bankroll_full", "bankroll"),
    ("excluded", "excluded"),
    ("novelty", "novelty_prop"),
    ("perm_skip", "perm_skip"),
    ("no_ask", "no YES ask"),
    ("no_bid", "no YES bid"),
    ("stale_corrected_killed", "STALE corrected"),
    ("ghost_trade", "GHOST TRADE"),
    ("trade", "TRADE buy"),
]

# Count per-market what happened after ACTIONABLE
fates = Counter()
stale_then_what = Counter()

for log in sorted(LOG_DIR.glob("bot.log*")):
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if "[SIGNAL] ACTIONABLE" not in line:
            continue

        # Extract market_id
        try:
            market_id = line.split("ACTIONABLE ")[1].split(" |")[0].split()[0].strip()
        except:
            continue

        # Look ahead further — up to 100 lines
        fate = "unknown"
        stale_hit = False
        for j in range(i + 1, min(i + 100, len(lines))):
            next_line = lines[j]
            if market_id not in next_line:
                continue

            # Track stale correction (not a rejection, but changes the signal)
            if "STALE corrected" in next_line:
                stale_hit = True
                continue

            for label, pattern in REJECTION_PATTERNS:
                if pattern in next_line:
                    fate = label
                    if stale_hit:
                        stale_then_what[label] += 1
                    break
            if fate != "unknown":
                break

            # Also check for generic SKIP with the market
            if "SKIP " + market_id in next_line or "SKIP" in next_line:
                # Extract reason after SKIP
                if market_id in next_line:
                    after_skip = next_line.split("SKIP")[1][:100].strip()
                    fate = f"skip_raw: {after_skip[:60]}"
                    break

        fates[fate] += 1

print("=== _try_execute REJECTION BREAKDOWN ===")
print(f"Total ACTIONABLE signals: {sum(fates.values())}")
print()
for fate, count in fates.most_common():
    pct = count / sum(fates.values()) * 100
    print(f"  {fate:45s}: {count:>5}  ({pct:.1f}%)")

if stale_then_what:
    print()
    print("=== STALE CORRECTED -> then what? ===")
    for label, count in stale_then_what.most_common():
        print(f"  stale -> {label:35s}: {count:>5}")