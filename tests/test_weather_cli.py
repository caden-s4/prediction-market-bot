"""Tests for data.ground_truth.weather_cli"""

from __future__ import annotations

import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.ground_truth.weather_cli import (
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


@pytest.mark.network
def test_fetch_cli_for_date_network():
    from datetime import timedelta
    target = date.today() - timedelta(days=2)
    report = fetch_cli_for_date("LAX", target)
    assert report is not None, f"No CLI report found for LAX on {target}"
    assert report.station == "LAX"
    assert report.report_date == target
