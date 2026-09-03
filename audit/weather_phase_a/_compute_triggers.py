"""Phase 14a Step 3: simulate the proposed weather trigger on historical ASOS.

For each (station, local_day):
  1. Identify recorded HIGH of day and its timestamp (resolution-window definition).
  2. Identify recorded LOW of day and its timestamp.
  3. Estimate climatological peak hour from the empirical hour-of-max distribution.
  4. Simulate the trigger walking forward through the day's observations:
       - Trigger eligibility: current local-time hour ≥ peak_hour + 1
       - "Monotonic decline" surrogate (since ASOS is ~hourly): the running max
         has not been re-broken (within ≤1°F tolerance) for ≥30 min,
         AND the latest obs is below running_max - tolerance.
       - First eligible observation that satisfies the above is the trigger.
  5. Record trigger time, trigger-time observed extremum, settled extremum.

Outputs (per station): audit/weather_phase_a/triggers_K<STATION>.csv
Aggregate: audit/weather_phase_a/triggers_summary.csv
"""
import csv
import datetime as dt
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(r"C:\Users\caden\Desktop\prediction_market_bot\audit\weather_phase_a")

STATIONS = {
    "KNYC": {"tz": ZoneInfo("America/New_York"),     "label": "NYC"},
    "KORD": {"tz": ZoneInfo("America/Chicago"),      "label": "Chicago"},
    "KMIA": {"tz": ZoneInfo("America/New_York"),     "label": "Miami"},  # MIA observes ET
    "KDEN": {"tz": ZoneInfo("America/Denver"),       "label": "Denver"},
}

TOLERANCE_F = 1.0  # ≤1°F bounce tolerance per spec
LOOKBACK_MIN_FOR_PASSED_PEAK = 30  # max must have been at least 30 min ago


def parse_csv(path: Path):
    """Yield (utc_dt, temp_f) for each row in IEM CSV."""
    rows = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            v = row.get("valid", "")
            t = row.get("tmpf", "")
            if not v or t in ("", "null", "M"):
                continue
            try:
                # IEM "valid" looks like "2026-01-15 00:51"
                ts = dt.datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            try:
                temp = float(t)
            except ValueError:
                continue
            rows.append((ts, temp))
    rows.sort(key=lambda x: x[0])
    return rows


def group_by_local_day(obs, tz):
    """Group obs into local-time calendar days. Day-of-record is local date.

    Returns dict: local_date_str(YYYY-MM-DD) -> list of (utc_dt, local_dt, temp).
    """
    by_day = defaultdict(list)
    for utc_dt, temp in obs:
        local_dt = utc_dt.astimezone(tz)
        by_day[local_dt.strftime("%Y-%m-%d")].append((utc_dt, local_dt, temp))
    for k in by_day:
        by_day[k].sort(key=lambda x: x[0])
    return dict(by_day)


def estimate_peak_hour(by_day, kind):
    """Return mean local hour at which the daily extremum occurs.

    kind = 'high' (use max) or 'low' (use min).
    """
    hours = []
    for d, day_obs in by_day.items():
        if len(day_obs) < 6:  # require enough data
            continue
        if kind == "high":
            ext = max(day_obs, key=lambda x: x[2])
        else:
            ext = min(day_obs, key=lambda x: x[2])
        hours.append(ext[1].hour)
    if not hours:
        return None
    # mean (ignoring sub-hour minutes here — peak hour is the integer hour bin)
    return round(sum(hours) / len(hours))


def simulate_trigger(day_obs, peak_hour, kind):
    """Walk through the day's observations and find the first eligible trigger.

    Trigger conditions (interpretation of spec adapted to ~hourly METAR cadence):
      A. Local clock-hour ≥ peak_hour + 1
      B. running_ext (max for high / min for low) was set ≥30 min ago
      C. Current obs is past_ext - tolerance below running_ext (clearly declined)
      D. Post-peak monotonicity: between running_ext and now, no upward bounce
         exceeding TOLERANCE_F above the post-peak running minimum (or downward
         bounce below post-peak running max for lows).
    """
    if kind == "high":
        settled = max(day_obs, key=lambda x: x[2])
        better = lambda a, b: a > b  # noqa: E731
    else:
        settled = min(day_obs, key=lambda x: x[2])
        better = lambda a, b: a < b  # noqa: E731

    settled_temp = settled[2]
    settled_utc = settled[0]
    settled_local = settled[1]

    running_ext_idx = None
    running_ext = None  # (utc_dt, local_dt, temp)
    for i, (utc_dt, local_dt, temp) in enumerate(day_obs):
        if running_ext is None or better(temp, running_ext[2]):
            running_ext = (utc_dt, local_dt, temp)
            running_ext_idx = i

        # A: clock past peak_hour + 1
        if local_dt.hour < peak_hour + 1:
            continue
        # B: running_ext ≥ 30 min ago
        time_since_ext = (utc_dt - running_ext[0]).total_seconds() / 60
        if time_since_ext < LOOKBACK_MIN_FOR_PASSED_PEAK:
            continue
        # C: current obs clearly below running_ext
        if kind == "high":
            if temp >= running_ext[2] - TOLERANCE_F:
                continue
        else:
            if temp <= running_ext[2] + TOLERANCE_F:
                continue
        # D: post-peak monotonicity (allow ≤TOLERANCE_F bounces)
        rebound = False
        post_peak_extreme = None  # min after peak (for high); max after trough (for low)
        for j in range(running_ext_idx + 1, i + 1):
            t = day_obs[j][2]
            if kind == "high":
                if post_peak_extreme is None or t < post_peak_extreme:
                    post_peak_extreme = t
                elif t > post_peak_extreme + TOLERANCE_F:
                    rebound = True
                    break
            else:
                if post_peak_extreme is None or t > post_peak_extreme:
                    post_peak_extreme = t
                elif t < post_peak_extreme - TOLERANCE_F:
                    rebound = True
                    break
        if rebound:
            continue

        return {
            "fired": True,
            "trigger_utc": utc_dt.isoformat(),
            "trigger_local": local_dt.strftime("%H:%M"),
            "trigger_temp": temp,
            "observed_ext_at_trigger": running_ext[2],
            "observed_ext_time_local": running_ext[1].strftime("%H:%M"),
            "settled_ext": settled_temp,
            "settled_local": settled_local.strftime("%H:%M"),
            "settled_utc": settled_utc.isoformat(),
        }

    return {
        "fired": False,
        "trigger_utc": None,
        "trigger_local": None,
        "trigger_temp": None,
        "observed_ext_at_trigger": running_ext[2] if running_ext else None,
        "observed_ext_time_local": running_ext[1].strftime("%H:%M") if running_ext else None,
        "settled_ext": settled_temp,
        "settled_local": settled_local.strftime("%H:%M"),
        "settled_utc": settled_utc.isoformat(),
    }


summary_rows = []
for station, info in STATIONS.items():
    src = OUT_DIR / f"asos_{station}.csv"
    if not src.exists():
        print(f"  missing {src}")
        continue
    obs = parse_csv(src)
    by_day = group_by_local_day(obs, info["tz"])
    print(f"\n=== {station} ({info['label']}) ===")
    print(f"  total obs: {len(obs):,}, days: {len(by_day)}")

    high_peak = estimate_peak_hour(by_day, "high")
    low_peak = estimate_peak_hour(by_day, "low")
    print(f"  empirical peak hours (local): high={high_peak}, low={low_peak}")

    out_rows = []
    for kind, peak in (("high", high_peak), ("low", low_peak)):
        if peak is None:
            continue
        for d in sorted(by_day):
            day_obs = by_day[d]
            if len(day_obs) < 6:
                continue
            res = simulate_trigger(day_obs, peak, kind)
            row = {
                "date": d,
                "kind": kind,
                "peak_hour_local": peak,
                **res,
            }
            out_rows.append(row)
            summary_rows.append({"station": station, **row})

    out_path = OUT_DIR / f"triggers_{station}.csv"
    if out_rows:
        keys = ["date", "kind", "peak_hour_local", "fired", "trigger_local",
                "trigger_temp", "observed_ext_at_trigger", "observed_ext_time_local",
                "settled_ext", "settled_local", "trigger_utc", "settled_utc"]
        with out_path.open("w", encoding="utf-8", newline="") as out:
            w = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(out_rows)
        n_fired = sum(1 for r in out_rows if r["fired"])
        print(f"  triggers fired: {n_fired}/{len(out_rows)} ({100*n_fired/len(out_rows):.1f}%)  -> {out_path.name}")

# Aggregate summary CSV
summary_path = OUT_DIR / "triggers_summary.csv"
keys = ["station", "date", "kind", "peak_hour_local", "fired", "trigger_local",
        "trigger_temp", "observed_ext_at_trigger", "observed_ext_time_local",
        "settled_ext", "settled_local", "trigger_utc", "settled_utc"]
with summary_path.open("w", encoding="utf-8", newline="") as out:
    w = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
    w.writeheader()
    w.writerows(summary_rows)
print(f"\nWrote {summary_path}  ({len(summary_rows)} rows total)")
