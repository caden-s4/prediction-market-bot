"""Scan weather event open/close times to confirm the date window."""
import json
import re
from collections import defaultdict
from pathlib import Path

SRC = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")

WEATHER_HINTS = re.compile(r"(HIGH|LOW|TEMP|WEATHER|SNOW|RAIN|HOT|COLD|CHILL|HEAT|HURRICANE)", re.IGNORECASE)

# Series we care about for top-4 candidates
TOP_TIER = {
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHLAX", "KXHIGHPHIL",
    "KXHIGHTNOLA", "KXHIGHTSFO", "KXHIGHTLV", "KXHIGHTSEA",
    "KXLOWTNYC", "KXLOWTCHI", "KXLOWTMIA", "KXLOWTDEN",
    "KXLOWTAUS", "KXLOWTLAX", "KXLOWTPHIL",
}

per_series_dates = defaultdict(set)
per_series_close = defaultdict(set)
per_series_subtitles = defaultdict(set)
total_volume = defaultdict(float)
total_oi = defaultdict(float)

n = 0
with SRC.open("r", encoding="utf-8") as f:
    for line in f:
        n += 1
        # quick string filter: only parse if line mentions any of our prefixes
        hit = False
        for p in TOP_TIER:
            if p in line:
                hit = True
                break
        if not hit:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        ticker = row.get("ticker", "")
        prefix = ticker.split("-", 1)[0]
        if prefix not in TOP_TIER:
            continue
        ev = row.get("event_ticker", "")
        # event_ticker format: KXHIGHNY-26FEB12 → date in ev
        m = re.search(r"-(\d{2}[A-Z]{3}\d{1,2})$", ev)
        if m:
            per_series_dates[prefix].add(m.group(1))
        if row.get("close_time"):
            per_series_close[prefix].add(row["close_time"])
        sub = row.get("yes_sub_title", "") or ""
        if sub:
            per_series_subtitles[prefix].add(sub[:60])
        try:
            total_volume[prefix] += float(row.get("volume_24h", 0) or 0)
        except Exception:
            pass
        try:
            total_oi[prefix] += float(row.get("open_interest_fp", 0) or 0)
        except Exception:
            pass
        if n % 200000 == 0:
            print(f"  scanned {n:,}")

print("\nPER-SERIES DATE WINDOW + STATS:")
for prefix in sorted(per_series_dates, key=lambda p: -len(per_series_dates[p])):
    dates = sorted(per_series_dates[prefix])
    close_times = sorted(per_series_close[prefix])
    sample_sub = list(per_series_subtitles[prefix])[:6]
    print(f"\n  {prefix}")
    print(f"    distinct event dates: {len(dates)} | first: {dates[0] if dates else '-'} | last: {dates[-1] if dates else '-'}")
    print(f"    close_time samples: first={close_times[0] if close_times else '-'} last={close_times[-1] if close_times else '-'}")
    print(f"    total volume_24h sum: {total_volume[prefix]:.0f}")
    print(f"    total open_interest sum: {total_oi[prefix]:.0f}")
    print(f"    bracket subtitle samples: {sample_sub[:4]}")
