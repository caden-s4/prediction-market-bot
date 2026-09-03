"""Phase 14a Step 2: pull 30-day ASOS observations for selected stations from IEM.

Endpoint: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
- data=tmpf (temperature F)
- report_type=3,4  → routine METAR + special obs (≈ every 5–60 min)
- format=onlycomma → CSV
- tz=Etc/UTC

Window: 2026-01-15 00:00 UTC → 2026-02-15 00:00 UTC (31 days, 1-day buffer past Kalshi window).

Output: audit/weather_phase_a/asos_<station>.csv
"""
import time
import urllib.parse
import urllib.request
from pathlib import Path

OUT_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a")
OUT_DIR.mkdir(parents=True, exist_ok=True)

STATIONS = ["NYC", "ORD", "MIA", "DEN"]  # IEM uses bare ID (no leading K) in its station list

START = ("2026", "1", "15", "0", "0")    # UTC
END   = ("2026", "2", "15", "0", "0")    # UTC

URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"


def fetch_station(station: str) -> bytes:
    params = [
        ("station", station),
        ("data", "tmpf"),
        ("year1", START[0]), ("month1", START[1]), ("day1", START[2]),
        ("hour1", START[3]), ("minute1", START[4]),
        ("year2", END[0]),   ("month2", END[1]),   ("day2", END[2]),
        ("hour2", END[3]),   ("minute2", END[4]),
        ("tz", "Etc/UTC"),
        ("format", "onlycomma"),
        ("latlon", "no"),
        ("elev", "no"),
        ("missing", "null"),
        ("trace", "null"),
        ("direct", "no"),
        ("report_type", "3"),
        ("report_type", "4"),
    ]
    qs = urllib.parse.urlencode(params)
    full = f"{URL}?{qs}"
    print(f"  GET {full}")
    req = urllib.request.Request(full, headers={"User-Agent": "phase14a-audit/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


for i, station in enumerate(STATIONS):
    out_path = OUT_DIR / f"asos_K{station}.csv"
    if out_path.exists() and out_path.stat().st_size > 1000:
        print(f"\nSkipping {station} (already saved {out_path.stat().st_size:,} bytes)")
        continue
    if i > 0:
        time.sleep(20)  # respect IEM rate limiter
    print(f"\nFetching {station}...")
    try:
        body = fetch_station(station)
    except Exception as e:
        print(f"  FAILED: {e}")
        continue
    out_path.write_bytes(body)
    n_lines = body.count(b"\n")
    print(f"  saved {out_path.name}  ({len(body):,} bytes, {n_lines:,} lines)")
    # show first + last 2 data lines
    text = body.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 3:
        print(f"  header: {lines[0]}")
        print(f"  first:  {lines[1]}")
        print(f"  last:   {lines[-1]}")
