"""Tests for data.ground_truth.weather_cli"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.ground_truth.weather_cli import (
    fetch_asos_running_extreme,
    fetch_cli_for_date,
    parse_cli_text,
)

_FAKE_ISSUANCE = datetime(2026, 4, 29, 1, 40, tzinfo=timezone.utc)

# Real LAX preliminary report (640 PM PDT April 28 2026) — source of truth for test 1
LAX_PRELIM_TEXT = """\
000
CDUS46 KLOX 290140
CLILAX

CLIMATE REPORT
NATIONAL WEATHER SERVICE LOS ANGELES/OXNARD
640 PM PDT TUE APR 28 2026

...................................

...THE LOS ANGELES INTL AIRPORT CA CLIMATE SUMMARY FOR APRIL 28 2026...
VALID TODAY AS OF 0500 PM LOCAL TIME.

CLIMATE NORMAL PERIOD: 1991 TO 2020
CLIMATE RECORD PERIOD: 1944 TO 2026


WEATHER ITEM   OBSERVED TIME   RECORD YEAR NORMAL DEPARTURE LAST
                VALUE   (LST)  VALUE       VALUE  FROM      YEAR
                                                  NORMAL
...................................................................
TEMPERATURE (F)
 TODAY
  MAXIMUM         69  11:20 AM  93    2008  69      0       64
  MINIMUM         53   5:19 AM  46    1984  56     -3       51
  AVERAGE         61                        62     -1       58
"""


def _make_cli_text(
    station: str = "LAX",
    month: str = "APRIL",
    day: int = 28,
    year: int = 2026,
    is_preliminary: bool = True,
    prelim_time: str = "0500 PM",
    block_label: str = "TODAY",
    max_val: str = "69",
    max_time: str = "11:20 AM",
    min_val: str = "53",
    min_time: str = "5:19 AM",
) -> str:
    prelim_line = f"VALID TODAY AS OF {prelim_time} LOCAL TIME." if is_preliminary else ""
    return (
        f"000\nCDUS46 KLOX 290140\nCLI{station}\n\n"
        f"CLIMATE REPORT\nNATIONAL WEATHER SERVICE TEST\n\n"
        f"...THE {station} CLIMATE SUMMARY FOR {month} {day} {year}...\n"
        f"{prelim_line}\n\n"
        f"TEMPERATURE (F)\n"
        f" {block_label}\n"
        f"  MAXIMUM         {max_val}  {max_time}  93    2008  69      0       64\n"
        f"  MINIMUM         {min_val}  {min_time}  46    1984  56     -3       51\n"
    )


@pytest.fixture(scope="session")
def lax_final_text() -> str:
    """Most recent final (non-preliminary) LAX CLI text, cached to disk after first fetch."""
    cache_path = Path(__file__).parent / "_cli_cache_lax_final.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    try:
        import requests
        headers = {"User-Agent": "prediction-market-bot test"}
        resp = requests.get(
            "https://api.weather.gov/products/types/CLI/locations/LAX",
            headers=headers, timeout=10,
        )
        for meta in resp.json().get("@graph", []):
            pid = meta.get("id")
            if not pid:
                continue
            r2 = requests.get(
                f"https://api.weather.gov/products/{pid}",
                headers=headers, timeout=10,
            )
            text = r2.json().get("productText", "")
            if text and "VALID TODAY AS OF" not in text:
                cache_path.write_text(text, encoding="utf-8")
                return text
    except Exception:
        pass
    pytest.skip("No final LAX CLI report available (network required on first run)")


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_parse_lax_prelim():
    report = parse_cli_text(LAX_PRELIM_TEXT, _FAKE_ISSUANCE)
    assert report is not None
    assert report.station == "LAX"
    assert report.report_date == date(2026, 4, 28)
    assert report.is_preliminary is True
    assert report.valid_through_local == time(17, 0)
    assert report.max_temp_f == 69
    assert report.max_temp_time == "11:20 AM"
    assert report.min_temp_f == 53
    assert report.min_temp_time == "5:19 AM"


def test_parse_final_report(lax_final_text: str):
    report = parse_cli_text(lax_final_text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.station == "LAX"
    assert report.is_preliminary is False
    assert report.valid_through_local is None
    assert isinstance(report.report_date, date)


def test_missing_data():
    text = _make_cli_text(max_val="MM", max_time="")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_f is None
    assert report.min_temp_f == 53


def test_record_marker():
    text = _make_cli_text(max_val="95R", max_time="2:30 PM")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_f == 95


def test_yesterday_block():
    """YESTERDAY block in a final report: date comes from header, not block label."""
    text = _make_cli_text(
        month="APRIL", day=28, year=2026,
        is_preliminary=False,
        block_label="YESTERDAY",
        max_val="72",
        max_time="3:45 PM",
        min_val="48",
        min_time="6:10 AM",
    )
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.report_date == date(2026, 4, 28)
    assert report.is_preliminary is False
    assert report.max_temp_f == 72
    assert report.min_temp_f == 48


def test_time_no_colon():
    """'454 PM' (no colon) normalizes to '4:54 PM'."""
    text = _make_cli_text(max_val="79", max_time="454 PM", min_val="55", min_time="612 AM")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_time == "4:54 PM"
    assert report.min_temp_time == "6:12 AM"


def test_time_with_colon():
    """'5:16 PM' (with colon) normalizes to '5:16 PM'."""
    text = _make_cli_text(max_val="60", max_time="5:16 PM", min_val="44", min_time="6:02 AM")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_time == "5:16 PM"
    assert report.min_temp_time == "6:02 AM"


def test_time_packed_four_digit():
    """'1130 AM' (4-digit packed, no colon) normalizes to '11:30 AM'."""
    text = _make_cli_text(max_val="85", max_time="1130 AM", min_val="60", min_time="702 AM")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_time == "11:30 AM"
    assert report.min_temp_time == "7:02 AM"


def test_time_single_digit_minutes():
    """'302 AM' — ensure single-digit minutes are zero-padded: '3:02 AM'."""
    text = _make_cli_text(max_val="70", max_time="302 AM", min_val="50", min_time="137 AM")
    report = parse_cli_text(text, _FAKE_ISSUANCE)
    assert report is not None
    assert report.max_temp_time == "3:02 AM"
    assert report.min_temp_time == "1:37 AM"


@pytest.mark.network
def test_fetch_cli_for_date_network():
    from datetime import timedelta
    target = date.today() - timedelta(days=2)
    report = fetch_cli_for_date("LAX", target)
    assert report is not None, f"No CLI report found for LAX on {target}"
    assert report.station == "LAX"
    assert report.report_date == target


# ── ASOS running extreme tests ────────────────────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for requests.Response used by _get()."""
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _patch_requests_get(monkeypatch, payload, status_code: int = 200):
    captured = {}

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        return _FakeResponse(payload, status_code=status_code)

    monkeypatch.setattr(
        "data.ground_truth.weather_cli.requests.get",
        fake_get,
    )
    return captured


def test_asos_running_extreme_synthetic(monkeypatch):
    """Mixed-quality, mixed-time obs: only V-quality within today's local window aggregated.

    Phoenix tz is UTC-7 year-round (no DST). With now_utc = 2026-04-29T20:00Z,
    local-now = 13:00 PHX on 2026-04-29; local midnight = 2026-04-29T00:00 PHX
    = 2026-04-29T07:00Z. Window is [07:00Z, 20:00Z].
    """
    fixed_now_utc = datetime(2026, 4, 29, 20, 0, tzinfo=timezone.utc)

    fake_features = [
        # V-quality, within window: 25C → 77F
        {"properties": {
            "temperature": {"value": 25.0, "qualityControl": "V"},
            "timestamp": "2026-04-29T15:00:00+00:00",
        }},
        # V-quality, within window: 30C → 86F (max)
        {"properties": {
            "temperature": {"value": 30.0, "qualityControl": "V"},
            "timestamp": "2026-04-29T18:00:00+00:00",
        }},
        # V-quality, within window: 20C → 68F (min)
        {"properties": {
            "temperature": {"value": 20.0, "qualityControl": "V"},
            "timestamp": "2026-04-29T08:00:00+00:00",
        }},
        # Z-quality (automated): would be max-ish; must be rejected
        {"properties": {
            "temperature": {"value": 50.0, "qualityControl": "Z"},
            "timestamp": "2026-04-29T17:00:00+00:00",
        }},
        # V-quality but BEFORE window (before local midnight): must be rejected
        {"properties": {
            "temperature": {"value": 100.0, "qualityControl": "V"},
            "timestamp": "2026-04-29T05:00:00+00:00",
        }},
        # S-quality: must be rejected
        {"properties": {
            "temperature": {"value": -10.0, "qualityControl": "S"},
            "timestamp": "2026-04-29T16:00:00+00:00",
        }},
        # null temperature value: must be skipped
        {"properties": {
            "temperature": {"value": None, "qualityControl": "V"},
            "timestamp": "2026-04-29T19:00:00+00:00",
        }},
    ]
    captured = _patch_requests_get(monkeypatch, {"features": fake_features})

    result = fetch_asos_running_extreme(
        "PHX", "America/Phoenix", now_utc=fixed_now_utc
    )

    assert result is not None
    assert result.station == "PHX"
    assert result.local_date == date(2026, 4, 29)
    assert result.running_max_f == pytest.approx(86.0)  # 30C
    assert result.running_min_f == pytest.approx(68.0)  # 20C
    assert result.observation_count == 3
    assert result.last_observation_utc == datetime(2026, 4, 29, 18, 0, tzinfo=timezone.utc)
    # ICAO form must appear in the URL.
    assert "/stations/KPHX/observations" in captured["url"]
    assert "start=" in captured["url"]


def test_asos_running_extreme_no_obs_returns_none(monkeypatch):
    fixed_now_utc = datetime(2026, 4, 29, 20, 0, tzinfo=timezone.utc)
    _patch_requests_get(monkeypatch, {"features": []})
    assert fetch_asos_running_extreme("PHX", "America/Phoenix", now_utc=fixed_now_utc) is None


def test_asos_running_extreme_only_z_quality_returns_none(monkeypatch):
    """All obs are Z-quality (unvalidated): function must return None."""
    fixed_now_utc = datetime(2026, 4, 29, 20, 0, tzinfo=timezone.utc)
    fake_features = [
        {"properties": {
            "temperature": {"value": 25.0, "qualityControl": "Z"},
            "timestamp": "2026-04-29T15:00:00+00:00",
        }},
        {"properties": {
            "temperature": {"value": 28.0, "qualityControl": "Z"},
            "timestamp": "2026-04-29T18:00:00+00:00",
        }},
    ]
    _patch_requests_get(monkeypatch, {"features": fake_features})
    assert fetch_asos_running_extreme("PHX", "America/Phoenix", now_utc=fixed_now_utc) is None


def test_asos_running_extreme_unknown_timezone_returns_none(monkeypatch):
    """Bad timezone string: function must return None without an HTTP call."""
    called = {"hit": False}

    def boom(*args, **kwargs):
        called["hit"] = True
        raise AssertionError("HTTP should not be reached")

    monkeypatch.setattr("data.ground_truth.weather_cli.requests.get", boom)
    result = fetch_asos_running_extreme("PHX", "Not/A_Real_Zone")
    assert result is None
    assert called["hit"] is False


@pytest.mark.network
def test_asos_running_extreme_network():
    """Real ASOS fetch for PHX. Either returns a result or None (e.g., overnight,
    no V-quality obs yet). Just verifies the call shape works end-to-end."""
    result = fetch_asos_running_extreme("PHX", "America/Phoenix")
    if result is None:
        return  # acceptable — V-quality obs may not exist for today yet
    assert result.station == "PHX"
    assert result.observation_count >= 1
    assert result.running_max_f is not None
    assert result.running_min_f is not None
    assert result.running_min_f <= result.running_max_f
