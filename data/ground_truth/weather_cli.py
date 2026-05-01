"""
data.ground_truth.weather_cli — NWS Daily Climatological Report (CLI) fetcher and parser.

Kalshi daily temperature markets (KXHIGHT*, KXLOWT*) resolve against the NWS
Daily Climatological Report (CLI text product), not raw ASOS observations.
This module fetches and parses that product for a given station and date.

Not wired into the GT router — standalone infrastructure only.
"""

from __future__ import annotations

import logging
import re
import time as _time_module
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

logger = logging.getLogger(__name__)

_NWS_BASE = "https://api.weather.gov"
_HEADERS = {"User-Agent": "prediction-market-bot (contact@example.com)"}
_TIMEOUT = 10
_RETRY_BACKOFF = 2.0

_MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}


@dataclass
class CLIReport:
    station: str
    report_date: date
    is_preliminary: bool
    valid_through_local: Optional[time]
    max_temp_f: Optional[int]
    max_temp_time: Optional[str]
    min_temp_f: Optional[int]
    min_temp_time: Optional[str]
    issuance_time: datetime
    raw_text: str


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _get(url: str) -> Optional[dict]:
    """GET → parsed JSON. Returns None on 4xx. Single retry on 5xx or connection error."""
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
            if 400 <= resp.status_code < 500:
                logger.debug("NWS 4xx for %s: %s", url, resp.status_code)
                return None
            if resp.status_code >= 500 and attempt == 0:
                logger.warning("NWS 5xx for %s, retrying", url)
                _time_module.sleep(_RETRY_BACKOFF)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError:
            if attempt == 0:
                logger.warning("NWS connection error for %s, retrying", url)
                _time_module.sleep(_RETRY_BACKOFF)
                continue
            logger.error("NWS connection error for %s after retry", url)
            return None
        except Exception as exc:
            logger.error("NWS request failed for %s: %s", url, exc)
            return None
    return None


def list_cli_products(station: str) -> list:
    """Return list of recent CLI product metadata dicts for station (newest first)."""
    url = f"{_NWS_BASE}/products/types/CLI/locations/{station.upper()}"
    data = _get(url)
    return data.get("@graph", []) if data else []


def fetch_product_text(product_id: str) -> Optional[tuple]:
    """Fetch (text, issuance_time) for a product UUID, or None."""
    data = _get(f"{_NWS_BASE}/products/{product_id}")
    if not data:
        return None
    text = data.get("productText")
    iso = data.get("issuanceTime")
    if not text or not iso:
        return None
    try:
        issuance_time = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Could not parse issuance time: %s", iso)
        return None
    return text, issuance_time


# ── Parser ─────────────────────────────────────────────────────────────────────

_STATION_RE = re.compile(r"^CLI([A-Z]{2,4})\s*$", re.MULTILINE)
_DATE_RE = re.compile(r"CLIMATE SUMMARY FOR ([A-Z]+)\s+(\d{1,2})\s+(\d{4})", re.IGNORECASE)

# Handles "0500 PM", "05:00 PM", "500 PM", "5:00 PM"
_PRELIM_RE = re.compile(r"VALID TODAY AS OF\s+(\d{1,2}):?(\d{2})\s*(AM|PM)", re.IGNORECASE)

_TEMP_VAL = r"(MM|-?\d+R?)"
# Time groups: hour (\d{1,2}), optional colon, minute (\d{2}), AM/PM — matches both
# "454 PM" (no colon) and "4:54 PM". Groups: (val)(hour)(min)(ampm).
_TIME_GROUPS = r"(?:\s+(\d{1,2}):?(\d{2})\s+([AP]M))?"
_MAX_RE = re.compile(
    r"^\s+MAXIMUM\s+" + _TEMP_VAL + _TIME_GROUPS,
    re.MULTILINE | re.IGNORECASE,
)
_MIN_RE = re.compile(
    r"^\s+MINIMUM\s+" + _TEMP_VAL + _TIME_GROUPS,
    re.MULTILINE | re.IGNORECASE,
)
_NEXT_SECTION_RE = re.compile(
    r"\n(?:PRECIPITATION|SNOWFALL|DEGREE DAYS|WINDS|SKY COVER|RELATIVE HUMIDITY|SUNSHINE)",
    re.IGNORECASE,
)


def _parse_temp_value(raw: str) -> Optional[int]:
    if raw.upper() == "MM":
        return None
    try:
        return int(raw.rstrip("Rr"))
    except ValueError:
        logger.warning("Unexpected CLI temp value: %r", raw)
        return None


def _parse_12h_time(hour_str: str, minute_str: str, ampm: str) -> time:
    h = int(hour_str) % 12 + (12 if ampm.upper() == "PM" else 0)
    return time(h, int(minute_str))


def _format_obs_time(hour_str: str, minute_str: str, ampm: str) -> str:
    """Normalize colon-less or colon format to canonical 'H:MM AM/PM'."""
    return f"{int(hour_str)}:{minute_str} {ampm.upper()}"


def _extract_temp_block(temp_section: str, is_preliminary: bool) -> str:
    """Slice the temperature section to the relevant TODAY or YESTERDAY block."""
    yesterday_m = re.search(r"^\s+YESTERDAY\b", temp_section, re.MULTILINE)
    today_m = re.search(r"^\s+TODAY\b", temp_section, re.MULTILINE)

    # Final reports with a YESTERDAY block: parse that block (not the partial TODAY)
    if not is_preliminary and yesterday_m:
        start = yesterday_m.end()
        if today_m and today_m.start() > yesterday_m.start():
            return temp_section[start : today_m.start()]
        return temp_section[start:]

    if today_m:
        return temp_section[today_m.end():]

    return temp_section


def parse_cli_text(text: str, issuance_time: datetime) -> Optional[CLIReport]:
    """Parse a CLI product text string into a CLIReport. Returns None on fatal failure."""
    station_m = _STATION_RE.search(text)
    if not station_m:
        logger.warning("No CLI station code found in product text")
        return None
    station = station_m.group(1)

    date_m = _DATE_RE.search(text)
    if not date_m:
        logger.warning("No CLIMATE SUMMARY FOR date found in CLI text")
        return None
    month = _MONTHS.get(date_m.group(1).upper())
    if month is None:
        logger.warning("Unknown month: %r", date_m.group(1))
        return None
    try:
        report_date = date(int(date_m.group(3)), month, int(date_m.group(2)))
    except ValueError as exc:
        logger.warning("Invalid date in CLI: %s", exc)
        return None

    prelim_m = _PRELIM_RE.search(text)
    is_preliminary = prelim_m is not None
    valid_through_local: Optional[time] = None
    if prelim_m:
        valid_through_local = _parse_12h_time(
            prelim_m.group(1), prelim_m.group(2), prelim_m.group(3)
        )

    temp_sec_m = re.search(r"TEMPERATURE \(F\)", text, re.IGNORECASE)
    if not temp_sec_m:
        logger.warning("No TEMPERATURE (F) section in CLI text")
        return None
    temp_section = text[temp_sec_m.start():]
    next_m = _NEXT_SECTION_RE.search(temp_section)
    if next_m:
        temp_section = temp_section[: next_m.start()]

    block = _extract_temp_block(temp_section, is_preliminary)

    max_m = _MAX_RE.search(block)
    min_m = _MIN_RE.search(block)

    return CLIReport(
        station=station,
        report_date=report_date,
        is_preliminary=is_preliminary,
        valid_through_local=valid_through_local,
        max_temp_f=_parse_temp_value(max_m.group(1)) if max_m else None,
        max_temp_time=(
            _format_obs_time(max_m.group(2), max_m.group(3), max_m.group(4))
            if (max_m and max_m.group(2)) else None
        ),
        min_temp_f=_parse_temp_value(min_m.group(1)) if min_m else None,
        min_temp_time=(
            _format_obs_time(min_m.group(2), min_m.group(3), min_m.group(4))
            if (min_m and min_m.group(2)) else None
        ),
        issuance_time=issuance_time,
        raw_text=text,
    )


# ── High-level interface ───────────────────────────────────────────────────────

def fetch_cli_for_date(station: str, target_date: date) -> Optional[CLIReport]:
    """Return the most recent CLI report covering target_date, or None.

    Prefers final (non-preliminary) over preliminary if both exist for the date.
    A final report is issued the morning of target_date+1, so the search window
    covers products issued from target_date-1 to target_date+2 (UTC).
    """
    products = list_cli_products(station)
    if not products:
        logger.warning("No CLI products listed for station %s", station)
        return None

    candidates: list = []

    for meta in products:
        product_id = meta.get("id")
        if not product_id:
            continue

        # Prune by issuance date to avoid fetching old products unnecessarily.
        # Final reports for target_date are issued the next morning, so window
        # spans [target_date - 1 day, target_date + 2 days] in UTC.
        iso = meta.get("issuanceTime", "")
        try:
            issued = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            delta = (target_date - issued.date()).days
            if delta > 3 or delta < -1:
                continue
        except (ValueError, AttributeError):
            pass

        result = fetch_product_text(product_id)
        if result is None:
            continue
        text, issuance_time = result

        report = parse_cli_text(text, issuance_time)
        if report is None or report.report_date != target_date:
            continue

        candidates.append(report)
        if not report.is_preliminary:
            break  # final report is definitive

    if not candidates:
        return None
    finals = [r for r in candidates if not r.is_preliminary]
    return finals[0] if finals else candidates[0]


# ── ASOS running max/min from raw observations ────────────────────────────────

@dataclass
class ASOSDailyExtreme:
    station: str                    # NWS station code (bare, e.g. "PHX")
    local_date: date                # date in city's local time
    running_max_f: Optional[float]  # max temp observed since local midnight
    running_min_f: Optional[float]  # min temp observed since local midnight
    last_observation_utc: datetime  # timestamp of most recent obs
    observation_count: int          # how many V-quality obs in the window


def fetch_asos_running_extreme(
    station: str,
    timezone_name: str,
    now_utc: Optional[datetime] = None,
) -> Optional[ASOSDailyExtreme]:
    """Fetch today's running max/min temperature from ASOS observations.

    "Today" is the local calendar day at the station's timezone — observations
    are aggregated from local-midnight up through ``now_utc``.

    Only observations with ``qualityControl == 'V'`` (validated) are accepted;
    'Z' (automated/unvalidated), 'S' (subjective), and other codes are skipped
    to avoid trading on bad data. NOTE: 'V' is rare in real-time — most live
    obs are 'Z'. Callers must check ``observation_count`` before trusting the
    extremes.

    :param station: bare NWS station code, e.g. "PHX". The observations
        endpoint requires the ICAO form, so this function prepends 'K'
        (PHX → KPHX). The CLI module elsewhere uses bare codes — this
        asymmetry will trip up anyone mixing the two endpoints.
    :param timezone_name: IANA tz name from CITY_TZ_MAP, e.g. "America/Phoenix".
    :param now_utc: override for current time (UTC); defaults to now.
    :return: ASOSDailyExtreme, or None if no valid observations exist for today.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown timezone %r for station %s", timezone_name, station)
        return None

    local_now = now_utc.astimezone(tz)
    local_date_today = local_now.date()
    local_midnight = datetime.combine(local_date_today, time.min, tzinfo=tz)
    midnight_utc = local_midnight.astimezone(timezone.utc)

    # NWS observations endpoint requires the ICAO form (K-prefixed). The CLI
    # module elsewhere uses bare 3-letter codes ('PHX') — keep that asymmetry
    # contained here.
    icao_station = f"K{station.upper()}"

    start_iso = midnight_utc.isoformat().replace("+00:00", "Z")
    url = (
        f"{_NWS_BASE}/stations/{icao_station}/observations"
        f"?start={quote(start_iso, safe='')}"
    )
    data = _get(url)
    if not data:
        return None

    features = data.get("features") or []
    max_f: Optional[float] = None
    min_f: Optional[float] = None
    last_obs: Optional[datetime] = None
    count = 0

    for feat in features:
        props = feat.get("properties") or {}
        temp = props.get("temperature") or {}
        c_value = temp.get("value")
        if c_value is None:
            continue
        if temp.get("qualityControl") != "V":
            continue
        ts_iso = props.get("timestamp")
        if not ts_iso:
            continue
        try:
            ts_utc = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts_utc.tzinfo is None:
            ts_utc = ts_utc.replace(tzinfo=timezone.utc)
        if ts_utc < midnight_utc or ts_utc > now_utc:
            continue
        f_value = (float(c_value) * 9.0 / 5.0) + 32.0
        if max_f is None or f_value > max_f:
            max_f = f_value
        if min_f is None or f_value < min_f:
            min_f = f_value
        if last_obs is None or ts_utc > last_obs:
            last_obs = ts_utc
        count += 1

    if count == 0 or last_obs is None:
        return None

    return ASOSDailyExtreme(
        station=station.upper(),
        local_date=local_date_today,
        running_max_f=max_f,
        running_min_f=min_f,
        last_observation_utc=last_obs,
        observation_count=count,
    )
