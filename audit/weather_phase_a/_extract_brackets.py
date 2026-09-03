"""Extract per-event bracket bounds from kalshi_markets.jsonl for the 8 selected series.

For each (series, event_date) tuple, dump all brackets with parsed bounds.

Output: audit/weather_phase_a/brackets.csv
  series,event_date_local,event_ticker,market_ticker,subtitle,low,high,result
  - low=null for "X or below" brackets, high=null for "X or above" brackets
  - result: 'yes' if bracket was a winner, 'no' otherwise (from row['result'])
"""
import csv
import json
import re
from pathlib import Path

SRC = Path(r"C:\Users\caden\Desktop\prediction_market_bot\data\historical\kalshi_markets.jsonl")
OUT = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a\brackets.csv")

SERIES = {
    "KXHIGHNY":  ("KNYC", "high"),
    "KXLOWTNYC": ("KNYC", "low"),
    "KXHIGHCHI": ("KORD", "high"),
    "KXLOWTCHI": ("KORD", "low"),
    "KXHIGHMIA": ("KMIA", "high"),
    "KXLOWTMIA": ("KMIA", "low"),
    "KXHIGHDEN": ("KDEN", "high"),
    "KXLOWTDEN": ("KDEN", "low"),
}

# Date in event_ticker is e.g. "26FEB12" → map to "2026-02-12"
MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
    "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def parse_event_date(event_ticker: str):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{1,2})$", event_ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in MONTH_MAP:
        return None
    return f"20{yy}-{MONTH_MAP[mon]}-{dd.zfill(2)}"


def parse_subtitle(sub: str):
    """Return (low, high) bounds in °F, with None for unbounded ends."""
    if not sub:
        return (None, None)
    s = sub.replace("°", "").replace("°", "").strip()
    # "X or above"
    m = re.match(r"^(-?\d+)\s*or\s*above\s*$", s, re.IGNORECASE)
    if m:
        return (int(m.group(1)), None)
    # "X or below"
    m = re.match(r"^(-?\d+)\s*or\s*below\s*$", s, re.IGNORECASE)
    if m:
        return (None, int(m.group(1)))
    # "X to Y"
    m = re.match(r"^(-?\d+)\s*to\s*(-?\d+)\s*$", s, re.IGNORECASE)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (None, None)


rows_out = []
n = 0
n_hits = 0
with SRC.open("r", encoding="utf-8") as f:
    for line in f:
        n += 1
        hit_prefix = None
        for p in SERIES:
            if p in line:
                hit_prefix = p
                break
        if hit_prefix is None:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        ticker = row.get("ticker", "")
        prefix = ticker.split("-", 1)[0]
        if prefix != hit_prefix:
            continue
        n_hits += 1
        event_ticker = row.get("event_ticker", "")
        sub = row.get("yes_sub_title", "") or ""
        low, high = parse_subtitle(sub)
        result = row.get("result", "")
        event_date = parse_event_date(event_ticker)
        rows_out.append({
            "series": prefix,
            "station": SERIES[prefix][0],
            "kind": SERIES[prefix][1],
            "event_date": event_date,
            "event_ticker": event_ticker,
            "market_ticker": ticker,
            "subtitle": sub,
            "low": low if low is not None else "",
            "high": high if high is not None else "",
            "result": result,
        })

with OUT.open("w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=[
        "series", "station", "kind", "event_date", "event_ticker",
        "market_ticker", "subtitle", "low", "high", "result"
    ])
    w.writeheader()
    w.writerows(rows_out)

print(f"Scanned {n:,} rows, {n_hits:,} matched our 8 series.")
print(f"Wrote {OUT} ({len(rows_out)} bracket rows)")

# Quick sanity: how many distinct (series, event_date) tuples and how many winners
from collections import Counter
events = Counter()
winners_per_event = Counter()
for r in rows_out:
    events[(r["series"], r["event_date"])] += 1
    if r["result"] == "yes":
        winners_per_event[(r["series"], r["event_date"])] += 1
print(f"Distinct events: {len(events)}")
nbrack = Counter(events.values())
print(f"Brackets per event: {dict(nbrack)}")
nwin = Counter(winners_per_event.values())
print(f"Winners per event: {dict(nwin)}")
