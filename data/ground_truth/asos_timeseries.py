"""ASOS hourly temperature timeseries fetcher (IEM source).

Used by the WeatherPeakSnipe strategy to detect post-peak monotonic
temperature movement. Returns the most recent N hours of routine +
special METAR temperature observations as a list of (utc_dt, temp_f).

Endpoint: https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py
- data=tmpf, report_type=3,4 (METAR + special), format=onlycomma, tz=UTC.

Different from data.ground_truth.weather_cli.fetch_asos_running_extreme,
which queries the NWS observations endpoint and returns a single
day-extreme. This module needs the timeseries to evaluate monotonicity.

Per-process TTL cache keyed by (station, lookback_hours) so multiple
series (HIGH/LOW × bracket markets) sharing a city in one cycle do
not duplicate the IEM call.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

_IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_HEADERS = {"User-Agent": "prediction-market-bot weather-peak-snipe/1.0"}
_TIMEOUT = 20
_CACHE_TTL_SEC = 300  # 5 minutes — IEM updates hourly + specials, this is plenty
# IEM throttles bursts; Phase 14a's puller used 20-second spacing for batch
# pulls. For per-cycle live use we space network calls by ≥6s to stay under
# the 10-req/min soft limit while still finishing all 4 stations within 30s.
_MIN_INTERVAL_SEC = 6.0
_last_request_at: float = 0.0
_rate_limit_lock = threading.Lock()


@dataclass(frozen=True)
class _CacheKey:
    station: str
    lookback_hours: int


_cache: Dict[_CacheKey, Tuple[float, List[Tuple[datetime, float]]]] = {}
_cache_lock = threading.Lock()


def _strip_k_prefix(station: str) -> str:
    s = station.upper()
    return s[1:] if s.startswith("K") and len(s) == 4 else s


def _parse_csv(body: str) -> List[Tuple[datetime, float]]:
    """Parse IEM onlycomma CSV into [(utc_dt, temp_f), ...] sorted ascending."""
    rows: List[Tuple[datetime, float]] = []
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(lines) < 2:
        return rows

    header = [h.strip() for h in lines[0].split(",")]
    try:
        valid_idx = header.index("valid")
        tmpf_idx = header.index("tmpf")
    except ValueError:
        logger.warning("IEM CSV header missing valid/tmpf columns: %s", header)
        return rows

    for line in lines[1:]:
        cols = line.split(",")
        if len(cols) <= max(valid_idx, tmpf_idx):
            continue
        v = cols[valid_idx].strip()
        t = cols[tmpf_idx].strip()
        if not v or t in ("", "null", "M", "T"):
            continue
        try:
            ts = datetime.strptime(v, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            temp = float(t)
        except ValueError:
            continue
        rows.append((ts, temp))

    rows.sort(key=lambda x: x[0])
    return rows


def fetch_asos_timeseries(
    station: str,
    lookback_hours: int = 12,
    *,
    now_utc: Optional[datetime] = None,
) -> Optional[List[Tuple[datetime, float]]]:
    """Return [(utc_dt, temp_f), ...] for the past ``lookback_hours`` from IEM.

    ``station`` may be either bare ("NYC") or ICAO ("KNYC") form — both are
    accepted. IEM expects bare form internally.

    Returns None on hard failure (network error, empty response). Returns []
    when the request succeeded but no observations were within the window.
    Caller should treat None and [] differently: None = retry next cycle, []
    = station is online but observations are stale.

    Cached per-process for ``_CACHE_TTL_SEC`` (5 min). Safe to call repeatedly
    in one cycle; only one network call per (station, lookback_hours) per
    cache window.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    bare = _strip_k_prefix(station)
    key = _CacheKey(bare, lookback_hours)

    now_mono = _time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            cached_at, cached_rows = hit
            if now_mono - cached_at < _CACHE_TTL_SEC:
                return cached_rows

    start_utc = now_utc - timedelta(hours=lookback_hours)
    params = [
        ("station", bare),
        ("data", "tmpf"),
        ("year1", str(start_utc.year)),
        ("month1", str(start_utc.month)),
        ("day1", str(start_utc.day)),
        ("hour1", str(start_utc.hour)),
        ("minute1", str(start_utc.minute)),
        ("year2", str(now_utc.year)),
        ("month2", str(now_utc.month)),
        ("day2", str(now_utc.day)),
        ("hour2", str(now_utc.hour)),
        ("minute2", str(now_utc.minute)),
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
    url = f"{_IEM_URL}?{urlencode(params)}"

    # Inter-request throttle: serialize across threads, sleep until
    # ≥_MIN_INTERVAL_SEC has elapsed since the last call.
    global _last_request_at
    with _rate_limit_lock:
        wait = _MIN_INTERVAL_SEC - (_time.monotonic() - _last_request_at)
        if wait > 0:
            _time.sleep(wait)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            resp.raise_for_status()
            body = resp.text
        except requests.RequestException as exc:
            logger.warning("IEM ASOS fetch failed for %s: %s", bare, exc)
            _last_request_at = _time.monotonic()
            return None
        _last_request_at = _time.monotonic()

    rows = _parse_csv(body)
    with _cache_lock:
        _cache[key] = (now_mono, rows)
    logger.debug(
        "IEM ASOS %s lookback=%dh → %d obs (cached %ds)",
        bare, lookback_hours, len(rows), _CACHE_TTL_SEC,
    )
    return rows


def _clear_cache_for_test() -> None:
    """Test-only: clear the per-process cache between unit tests."""
    with _cache_lock:
        _cache.clear()
