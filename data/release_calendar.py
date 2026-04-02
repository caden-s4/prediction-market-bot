"""
data.release_calendar – FRED release calendar with pre/post release scan windows.

Tracks upcoming BLS/BEA data release dates for monitored FRED series and
manages a four-state window machine around each release:

  [idle] → [pre_release] → [hold] → [hunt] → [idle]

  pre_release : T-5min  to T+0       – release imminent, scan aggressively
  hold        : T+0     to FRED-updated OR T+45min  – DO NOT TRADE
  hunt        : FRED-updated to T+3h – hunt for stale markets
  idle        : no active window

The FRED API updates 20-45 minutes after the official BLS/BEA release, which
is fine because the target markets are stale for hours.  The hold → hunt
transition is triggered by the EARLIER of:
  * is_fred_updated() returning True (FRED actually has the new data)
  * HOLD_WINDOW_MINUTES elapsed since release (safety timeout)

Release dates are fetched from the FRED /release/dates endpoint.  The endpoint
returns dates only (no times); release TIME is always 08:30 ET for BLS/BEA
releases and is injected from HARDCODED_SCHEDULES.

Only the three confirmed series (CPIAUCSL, CPILFESL, PAYEMS) are tracked.
Other series can be added to SERIES_TO_RELEASE when their Kalshi market
mappings are validated.

Requires: FRED_API_KEY env var.  Falls back to hardcoded date patterns if the
API is unreachable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_FRED_API_KEY: str = os.environ.get("FRED_API_KEY", "")
_FRED_RELEASE_DATES_URL = "https://api.stlouisfed.org/fred/release/dates"
_FRED_OBS_URL = "https://api.stlouisfed.org/fred/series/observations"
_TIMEOUT = 5  # seconds

# ── Window timing constants ────────────────────────────────────────────────────
PRE_RELEASE_MINUTES = 5       # Start pre-release scan 5 min before release
HOLD_WINDOW_MINUTES = 45      # Assume FRED needs up to 45 min to update
HUNT_WINDOW_HOURS   = 3       # Hunt for stale markets for 3 hours after release
FRED_CHECK_INTERVAL = 120     # During hold window, check FRED every 2 min

# ── Series → FRED release ID mapping ──────────────────────────────────────────
# Only series with confirmed Kalshi market mappings are listed here.
# Full FRED release list: https://fred.stlouisfed.org/releases
SERIES_TO_RELEASE: Dict[str, int] = {
    "CPIAUCSL": 10,   # CPI release
    "CPILFESL": 10,   # CPI release (same release as headline)
    "PAYEMS":   50,   # Employment Situation
}

# Human-readable names for each FRED release ID (used in log messages).
_RELEASE_NAMES: Dict[int, str] = {
    10: "CPI",
    50: "Employment Situation (Nonfarm Payrolls)",
}

# ── Hardcoded fallback schedules ───────────────────────────────────────────────
# Used when the FRED API is unreachable.  The 'typical_day' pattern computes an
# approximate next occurrence; the actual date can shift by ±1 day.
HARDCODED_SCHEDULES: Dict[str, dict] = {
    "CPIAUCSL": {
        "typical_day": "second_wednesday",
        "typical_time": "08:30",
        "timezone": "US/Eastern",
    },
    "CPILFESL": {
        "typical_day": "second_wednesday",
        "typical_time": "08:30",
        "timezone": "US/Eastern",
    },
    "PAYEMS": {
        "typical_day": "first_friday",
        "typical_time": "08:30",
        "timezone": "US/Eastern",
    },
}

# Window states
_WINDOW_IDLE        = None
_WINDOW_PRE_RELEASE = "pre_release"
_WINDOW_HOLD        = "hold"
_WINDOW_HUNT        = "hunt"


def _et_to_utc(dt_naive_et: datetime) -> datetime:
    """Convert a naive Eastern Time datetime to UTC.

    Uses a fixed -5h offset for standard time and -4h for daylight saving time.
    DST is in effect from the second Sunday in March to the first Sunday in November.
    """
    year = dt_naive_et.year
    # Second Sunday of March (DST start)
    march1 = datetime(year, 3, 1)
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
    # First Sunday of November (DST end)
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)

    if dst_start <= dt_naive_et < dst_end:
        offset = timedelta(hours=4)  # EDT = UTC-4
    else:
        offset = timedelta(hours=5)  # EST = UTC-5

    return (dt_naive_et + offset).replace(tzinfo=timezone.utc)


def _next_occurrence_of_pattern(pattern: str) -> datetime:
    """Compute the next calendar date that matches a weekday-in-month pattern.

    Supported patterns:
      first_friday, second_friday, second_wednesday, first_wednesday, etc.

    Returns a naive datetime (date only, midnight) in Eastern Time.
    """
    ordinals = {"first": 1, "second": 2, "third": 3, "fourth": 4}
    weekdays = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    parts = pattern.lower().split("_")
    if len(parts) != 2:
        raise ValueError(f"Unsupported pattern: {pattern!r}")
    ordinal = ordinals.get(parts[0])
    weekday = weekdays.get(parts[1])
    if ordinal is None or weekday is None:
        raise ValueError(f"Unsupported pattern: {pattern!r}")

    today = datetime.utcnow().date()

    # Try current month, then subsequent months (up to 3 to handle edge cases).
    for month_offset in range(3):
        year = today.year + (today.month + month_offset - 1) // 12
        month = (today.month + month_offset - 1) % 12 + 1
        # Find the first occurrence of `weekday` in this month.
        first_of_month = datetime(year, month, 1)
        days_to_weekday = (weekday - first_of_month.weekday()) % 7
        first_occurrence = first_of_month + timedelta(days=days_to_weekday)
        nth_occurrence = first_occurrence + timedelta(weeks=ordinal - 1)
        # Only accept dates in the future (including today).
        if nth_occurrence.date() >= today:
            return nth_occurrence
    # Fallback: return 35 days from now (should never reach here).
    return datetime.utcnow() + timedelta(days=35)


def _hardcoded_next_release(series_id: str) -> Optional[datetime]:
    """Compute the next approximate release datetime (UTC) using the hardcoded pattern.

    Returns None if no hardcoded schedule exists for the series.
    """
    sched = HARDCODED_SCHEDULES.get(series_id)
    if not sched:
        return None
    try:
        next_date_et = _next_occurrence_of_pattern(sched["typical_day"])
        h, m = (int(x) for x in sched["typical_time"].split(":"))
        release_et = next_date_et.replace(hour=h, minute=m, second=0, microsecond=0)
        return _et_to_utc(release_et)
    except Exception as exc:
        logger.warning("[CALENDAR] Hardcoded schedule error for %s: %s", series_id, exc)
        return None


class FREDReleaseCalendar:
    """
    Tracks upcoming FRED data release dates and manages the pre/hold/hunt
    window state machine around each release.

    Parameters
    ----------
    fred_api_key : FRED API key (reads FRED_API_KEY env var by default)
    series_ids   : list of FRED series IDs to monitor
    """

    def __init__(
        self,
        fred_api_key: str = "",
        series_ids: Optional[List[str]] = None,
    ) -> None:
        self._api_key = fred_api_key or _FRED_API_KEY
        self._series_ids: List[str] = list(series_ids or SERIES_TO_RELEASE.keys())

        # {series_id: next_release_datetime_utc}
        self._schedule: Dict[str, Optional[datetime]] = {}
        self._last_refresh: Optional[datetime] = None

        # State machine: {series_id: window_state}
        # "idle" / "pre_release" / "hold" / "hunt"
        self._state: Dict[str, Optional[str]] = {}

        # Timestamps for state transitions: {series_id: utc datetime}
        self._release_time: Dict[str, Optional[datetime]] = {}
        self._hold_started_at: Dict[str, Optional[datetime]] = {}
        self._hunt_started_at: Dict[str, Optional[datetime]] = {}

        # Last time we checked FRED for an update during hold window.
        # Prevents hammering the API every cycle.
        self._last_fred_check: Dict[str, Optional[datetime]] = {}

        # Cache: {series_id: latest_obs_date} — populated by is_fred_updated()
        self._latest_obs_date: Dict[str, Optional[datetime]] = {}

        # Initialize all series as idle.
        for sid in self._series_ids:
            self._state[sid] = _WINDOW_IDLE
            self._release_time[sid] = None
            self._hold_started_at[sid] = None
            self._hunt_started_at[sid] = None
            self._last_fred_check[sid] = None
            self._latest_obs_date[sid] = None

    # ── Schedule refresh ───────────────────────────────────────────────────────

    def refresh_schedule(self) -> None:
        """Fetch upcoming release dates from FRED API.  Cached for 24 hours.

        For each unique release_id in SERIES_TO_RELEASE, calls the FRED
        /release/dates endpoint and finds the next date >= today.
        Falls back to hardcoded patterns on API failure.
        """
        now = datetime.now(timezone.utc)
        if (
            self._last_refresh is not None
            and (now - self._last_refresh).total_seconds() < 86_400
        ):
            return  # Fresh enough — skip.

        if not self._api_key:
            logger.warning(
                "[CALENDAR] FRED_API_KEY not set — using hardcoded fallback schedules"
            )
            self._apply_hardcoded_fallbacks()
            self._last_refresh = now
            return

        # Deduplicate: each release_id may cover multiple series (e.g. CPI covers both
        # CPIAUCSL and CPILFESL).  Fetch each release_id once, then fan out to series.
        release_ids: Dict[int, List[str]] = {}  # release_id → [series_ids]
        for sid in self._series_ids:
            rid = SERIES_TO_RELEASE.get(sid)
            if rid is not None:
                release_ids.setdefault(rid, []).append(sid)

        fetched: Dict[int, Optional[datetime]] = {}  # release_id → next_date (UTC)

        today_str = now.strftime("%Y-%m-%d")
        for release_id in release_ids:
            try:
                resp = requests.get(
                    _FRED_RELEASE_DATES_URL,
                    params={
                        "release_id": release_id,
                        "api_key": self._api_key,
                        "file_type": "json",
                        "include_release_dates_with_no_data": "true",
                        "sort_order": "asc",
                        "realtime_start": today_str,
                        "realtime_end": "9999-12-31",
                    },
                    timeout=_TIMEOUT,
                    proxies={},
                )
                resp.raise_for_status()
                release_dates = resp.json().get("release_dates", [])
                # Find the next date >= today.
                next_date_str: Optional[str] = None
                for entry in release_dates:
                    d = entry.get("date", "")
                    if d >= today_str:
                        next_date_str = d
                        break
                if next_date_str:
                    fetched[release_id] = next_date_str
                    logger.debug(
                        "[CALENDAR] Release %d next date: %s", release_id, next_date_str
                    )
                else:
                    logger.warning(
                        "[CALENDAR] No future dates found for release_id=%d "
                        "— falling back to hardcoded",
                        release_id,
                    )
                    fetched[release_id] = None
            except Exception as exc:
                logger.warning(
                    "[CALENDAR] Failed to fetch release dates for release_id=%d: %s "
                    "— falling back to hardcoded",
                    release_id, exc,
                )
                fetched[release_id] = None

        # Combine API dates (date-only strings) with hardcoded times (08:30 ET).
        schedule_parts = []
        for release_id, series_list in release_ids.items():
            raw = fetched.get(release_id)
            for sid in series_list:
                if raw is not None:
                    # The FRED endpoint returns dates only; inject the release time
                    # from HARDCODED_SCHEDULES (always 08:30 ET for BLS/BEA).
                    sched = HARDCODED_SCHEDULES.get(sid)
                    if sched:
                        h, m = (int(x) for x in sched["typical_time"].split(":"))
                        naive_et = datetime.strptime(raw, "%Y-%m-%d").replace(
                            hour=h, minute=m, second=0, microsecond=0
                        )
                        self._schedule[sid] = _et_to_utc(naive_et)
                    else:
                        # No time info — assume 13:30 UTC (08:30 ET standard)
                        naive_et = datetime.strptime(raw, "%Y-%m-%d").replace(
                            hour=13, minute=30, second=0, microsecond=0,
                            tzinfo=timezone.utc,
                        )
                        self._schedule[sid] = naive_et
                    schedule_parts.append(
                        f"{sid}={self._schedule[sid].strftime('%Y-%m-%dT%H:%M:%SZ')}"
                    )
                else:
                    # API failed — fall back to hardcoded pattern
                    fallback = _hardcoded_next_release(sid)
                    self._schedule[sid] = fallback
                    if fallback:
                        schedule_parts.append(
                            f"{sid}={fallback.strftime('%Y-%m-%dT%H:%M:%SZ')}(fallback)"
                        )
                    else:
                        schedule_parts.append(f"{sid}=unknown")

        # Series not in SERIES_TO_RELEASE — no dynamic fetching
        for sid in self._series_ids:
            if sid not in SERIES_TO_RELEASE:
                self._schedule[sid] = None

        self._last_refresh = now
        if schedule_parts:
            logger.info(
                "[CALENDAR] Refreshed schedule: %s", ", ".join(schedule_parts)
            )

    def _apply_hardcoded_fallbacks(self) -> None:
        """Populate _schedule entirely from hardcoded patterns."""
        for sid in self._series_ids:
            if sid in SERIES_TO_RELEASE:
                self._schedule[sid] = _hardcoded_next_release(sid)

    # ── Upcoming releases query ────────────────────────────────────────────────

    def get_upcoming_releases(self, within_hours: float = 24.0) -> List[dict]:
        """Return releases happening within the next N hours.

        Returns a list of dicts:
          {
            "series_ids": ["CPIAUCSL", "CPILFESL"],
            "release_id": 10,
            "release_time": datetime (UTC),
            "release_name": "CPI",
          }
        Multiple series sharing a release are grouped into one entry.
        """
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=within_hours)
        results: Dict[int, dict] = {}  # release_id → result entry

        for sid, release_time in self._schedule.items():
            if release_time is None:
                continue
            if now <= release_time <= cutoff:
                rid = SERIES_TO_RELEASE.get(sid, -1)
                if rid not in results:
                    results[rid] = {
                        "series_ids": [],
                        "release_id": rid,
                        "release_time": release_time,
                        "release_name": _RELEASE_NAMES.get(rid, f"release_{rid}"),
                    }
                if sid not in results[rid]["series_ids"]:
                    results[rid]["series_ids"].append(sid)

        return list(results.values())

    # ── State machine ──────────────────────────────────────────────────────────

    def get_active_window(self, series_id: str) -> Optional[str]:
        """Return which release window we're in for this series right now.

        Returns: 'pre_release' | 'hold' | 'hunt' | None

        Drives state transitions as a side-effect so state is always current
        when this method is called.
        """
        release_time = self._schedule.get(series_id)
        if release_time is None:
            return _WINDOW_IDLE

        now = datetime.now(timezone.utc)
        current_state = self._state.get(series_id, _WINDOW_IDLE)

        # ── Transition: any state → idle (hunt window expired) ────────────────
        hunt_started = self._hunt_started_at.get(series_id)
        if current_state == _WINDOW_HUNT and hunt_started is not None:
            if (now - hunt_started).total_seconds() >= HUNT_WINDOW_HOURS * 3600:
                self._transition(series_id, _WINDOW_IDLE,
                                 "hunt window expired after 3 hours")
                current_state = _WINDOW_IDLE

        # ── Transition: hold → hunt ────────────────────────────────────────────
        if current_state == _WINDOW_HOLD:
            hold_started = self._hold_started_at.get(series_id)
            # Safety timeout: 45 minutes
            if (
                hold_started is not None
                and (now - hold_started).total_seconds() >= HOLD_WINDOW_MINUTES * 60
            ):
                self._transition(series_id, _WINDOW_HUNT,
                                 f"FRED updated OR hold timeout after {HOLD_WINDOW_MINUTES} minutes")
                current_state = _WINDOW_HUNT
            else:
                # Check if FRED has actually updated (rate-limited to every 2 min)
                last_check = self._last_fred_check.get(series_id)
                if (
                    last_check is None
                    or (now - last_check).total_seconds() >= FRED_CHECK_INTERVAL
                ):
                    self._last_fred_check[series_id] = now
                    if self.is_fred_updated(series_id):
                        hold_elapsed = (
                            (now - hold_started).total_seconds() / 60
                            if hold_started else 0
                        )
                        self._transition(
                            series_id, _WINDOW_HUNT,
                            f"FRED updated with new data after {hold_elapsed:.0f} minutes",
                        )
                        current_state = _WINDOW_HUNT

        # ── Transition: pre_release → hold ────────────────────────────────────
        if current_state == _WINDOW_PRE_RELEASE and now >= release_time:
            self._transition(series_id, _WINDOW_HOLD,
                             "release time reached, waiting for FRED update")
            current_state = _WINDOW_HOLD

        # ── Transition: idle → pre_release ────────────────────────────────────
        if current_state == _WINDOW_IDLE:
            pre_release_start = release_time - timedelta(minutes=PRE_RELEASE_MINUTES)
            if now >= pre_release_start and now < release_time:
                self._transition(series_id, _WINDOW_PRE_RELEASE,
                                 f"release in {PRE_RELEASE_MINUTES} minutes")
                current_state = _WINDOW_PRE_RELEASE
            elif now >= release_time:
                # We missed the pre_release window — jump straight to hold/hunt
                elapsed = (now - release_time).total_seconds() / 60
                if elapsed < HOLD_WINDOW_MINUTES:
                    # Likely restarted during hold window
                    self._release_time[series_id] = release_time
                    self._transition(series_id, _WINDOW_HOLD,
                                     f"resuming hold window ({elapsed:.0f}min since release)")
                    current_state = _WINDOW_HOLD
                elif elapsed < HUNT_WINDOW_HOURS * 60:
                    # Restarted during hunt window
                    self._transition(series_id, _WINDOW_HUNT,
                                     f"resuming hunt window ({elapsed:.0f}min since release)")
                    current_state = _WINDOW_HUNT

        return current_state

    def _transition(self, series_id: str, new_state: Optional[str], reason: str) -> None:
        """Execute a state transition and log it."""
        old_state = self._state.get(series_id, _WINDOW_IDLE) or "idle"
        new_state_label = new_state or "idle"
        logger.info(
            "[CALENDAR] %s: %s → %s (%s)",
            series_id, old_state, new_state_label, reason,
        )
        self._state[series_id] = new_state
        if new_state == _WINDOW_HOLD:
            self._hold_started_at[series_id] = datetime.now(timezone.utc)
        elif new_state == _WINDOW_HUNT:
            self._hunt_started_at[series_id] = datetime.now(timezone.utc)
        elif new_state == _WINDOW_IDLE:
            # Reset all timestamps; advance schedule to the NEXT release
            self._hold_started_at[series_id] = None
            self._hunt_started_at[series_id] = None
            self._last_fred_check[series_id] = None
            self._latest_obs_date[series_id] = None
            # Force a schedule refresh so we pick up the next month's date.
            self._schedule[series_id] = None
            self._last_refresh = None  # allow refresh_schedule() to re-fetch

    # ── FRED update check ──────────────────────────────────────────────────────

    def is_fred_updated(self, series_id: str) -> bool:
        """Check if FRED has actually updated with new data since the release.

        Calls the FRED observations endpoint and checks whether the latest
        observation date is more recent than the last cached observation.
        Returns True when a new observation is detected (indicating FRED
        has published the release).

        During the hold window, we're watching for a NEW observation that
        wasn't there before the release.  We detect this by comparing the
        current latest obs date to what we had at window entry.
        """
        if not self._api_key:
            return False

        release_time = self._schedule.get(series_id)
        if release_time is None:
            return False

        try:
            resp = requests.get(
                _FRED_OBS_URL,
                params={
                    "series_id": series_id,
                    "api_key": self._api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": 1,
                },
                timeout=_TIMEOUT,
                proxies={},
            )
            resp.raise_for_status()
            observations = resp.json().get("observations", [])
            if not observations:
                return False

            obs = observations[0]
            raw_val = obs.get("value", "")
            if raw_val in (".", ""):
                return False

            obs_date = datetime.strptime(obs["date"], "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )

            # If this is the first check in this hold window, record the baseline.
            cached = self._latest_obs_date.get(series_id)
            if cached is None:
                self._latest_obs_date[series_id] = obs_date
                logger.debug(
                    "[CALENDAR] %s FRED baseline obs_date=%s",
                    series_id, obs_date.strftime("%Y-%m-%d"),
                )
                return False

            # FRED updated if the obs date advanced since we started watching.
            if obs_date > cached:
                logger.info(
                    "[CALENDAR] %s FRED updated: obs_date advanced %s → %s",
                    series_id,
                    cached.strftime("%Y-%m-%d"),
                    obs_date.strftime("%Y-%m-%d"),
                )
                self._latest_obs_date[series_id] = obs_date
                return True

            return False

        except Exception as exc:
            logger.debug(
                "[CALENDAR] %s is_fred_updated check failed: %s", series_id, exc
            )
            return False
