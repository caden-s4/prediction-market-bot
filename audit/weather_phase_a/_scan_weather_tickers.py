"""Phase 14a Step 1: scan kalshi_markets.jsonl for weather bracket series.

Streams the file, extracts ticker prefix, counts distinct markets per series.
Filters to weather-looking series (HIGH/LOW/TEMP/SNOW/RAIN/WEATHER prefixes).
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

SRC = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")
OUT_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Weather-related substrings that might appear in series names
WEATHER_HINTS = re.compile(
    r"(HIGH|LOW|TEMP|WEATHER|SNOW|RAIN|HOT|COLD|CHILL|HEAT|DEGREE|HURRICANE)",
    re.IGNORECASE,
)

# Group all ticker prefixes (text before first hyphen) so we can see the namespace
prefix_count = Counter()
weather_prefix_count = Counter()
weather_event_count = defaultdict(set)
weather_examples = defaultdict(list)

n = 0
n_weather = 0
with SRC.open("r", encoding="utf-8") as f:
    for line in f:
        n += 1
        try:
            row = json.loads(line)
        except Exception:
            continue
        ticker = row.get("ticker") or ""
        event_ticker = row.get("event_ticker") or ""
        if not ticker:
            continue
        prefix = ticker.split("-", 1)[0]
        prefix_count[prefix] += 1
        if WEATHER_HINTS.search(prefix) or WEATHER_HINTS.search(event_ticker):
            n_weather += 1
            weather_prefix_count[prefix] += 1
            if event_ticker:
                weather_event_count[prefix].add(event_ticker)
            if len(weather_examples[prefix]) < 3:
                weather_examples[prefix].append({
                    "ticker": ticker,
                    "event_ticker": event_ticker,
                    "open_time": row.get("open_time"),
                    "close_time": row.get("close_time"),
                    "subtitle": (row.get("yes_sub_title") or "")[:120],
                })
        if n % 200000 == 0:
            print(f"  scanned {n:,} rows, {n_weather:,} weather hits so far")

print(f"\nTOTAL ROWS: {n:,}")
print(f"DISTINCT PREFIXES: {len(prefix_count):,}")
print(f"WEATHER-MATCHING ROWS: {n_weather:,}")
print(f"WEATHER PREFIX COUNT: {len(weather_prefix_count):,}")

# Top 50 weather prefixes by market count
print("\nTOP WEATHER PREFIXES (by market count):")
print(f"  {'prefix':<35} {'markets':>10} {'events':>10}")
for prefix, count in weather_prefix_count.most_common(50):
    nev = len(weather_event_count.get(prefix, set()))
    print(f"  {prefix:<35} {count:>10,} {nev:>10,}")

# Save detailed output
with (OUT_DIR / "weather_prefixes.csv").open("w", encoding="utf-8") as out:
    out.write("prefix,total_markets,distinct_events\n")
    for prefix, count in weather_prefix_count.most_common():
        nev = len(weather_event_count.get(prefix, set()))
        out.write(f"{prefix},{count},{nev}\n")

with (OUT_DIR / "weather_examples.json").open("w", encoding="utf-8") as out:
    json.dump({k: v for k, v in weather_examples.items()}, out, indent=2)

print(f"\nWrote {OUT_DIR / 'weather_prefixes.csv'}")
print(f"Wrote {OUT_DIR / 'weather_examples.json'}")
