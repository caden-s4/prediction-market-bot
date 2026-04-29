"""Tests for data.ground_truth.weather_kalshi"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.ground_truth.weather_kalshi import _CITY_TO_CLI, WeatherMarket, parse_weather_ticker


# ── Core parse cases ──────────────────────────────────────────────────────────

def test_above_high_phx():
    m = parse_weather_ticker(
        "KXHIGHTPHX-26APR29-T94",
        question="Will the maximum temperature be greater than 94° on Apr 29, 2026?",
    )
    assert m is not None
    assert m.ticker == "KXHIGHTPHX-26APR29-T94"
    assert m.city == "PHX"
    assert m.cli_station == "PHX"
    assert m.market_type == "high"
    assert m.threshold_type == "above"
    assert m.threshold_value == 94.0
    assert m.target_date == date(2026, 4, 29)
    assert m.bracket_low is None
    assert m.bracket_high is None


def test_low_dal_maps_to_dfw():
    m = parse_weather_ticker(
        "KXLOWTDAL-26APR29-T62",
        question="Will the minimum temperature be above 62° on Apr 29, 2026?",
    )
    assert m is not None
    assert m.city == "DAL"
    assert m.cli_station == "DFW"
    assert m.market_type == "low"
    assert m.threshold_type == "above"
    assert m.threshold_value == 62.0
    assert m.target_date == date(2026, 4, 29)


def test_bracket_phx():
    m = parse_weather_ticker("KXHIGHTPHX-26APR29-B89.5")
    assert m is not None
    assert m.city == "PHX"
    assert m.cli_station == "PHX"
    assert m.market_type == "high"
    assert m.threshold_type == "bracket"
    assert m.threshold_value == 89.5
    assert m.bracket_low == 89.0
    assert m.bracket_high == 90.0


def test_bracket_integer_strike():
    # B94 → [93.5, 94.5]
    m = parse_weather_ticker("KXHIGHTPHX-26APR29-B94")
    assert m is not None
    assert m.threshold_type == "bracket"
    assert m.threshold_value == 94.0
    assert m.bracket_low == 93.5
    assert m.bracket_high == 94.5


# ── None cases ────────────────────────────────────────────────────────────────

def test_unknown_city_returns_none():
    # "UNKNOWN" is 7 chars — regex won't match (city must be 2–4 letters)
    result = parse_weather_ticker("KXHIGHTUNKNOWN-26APR29-T94")
    assert result is None


def test_unknown_short_city_returns_none_with_warning(caplog):
    # "ZZ" matches regex but is not in _CITY_TO_CLI → warning + None
    import logging
    with caplog.at_level(logging.WARNING, logger="data.ground_truth.weather_kalshi"):
        result = parse_weather_ticker("KXHIGHTZZ-26APR29-T94")
    assert result is None
    assert "ZZ" in caplog.text


def test_not_weather_ticker_returns_none():
    assert parse_weather_ticker("KXNBAGAME-26APR29-PORSAS-SAS") is None


def test_bad_month_returns_none():
    assert parse_weather_ticker("KXHIGHTPHX-26XYZ29-T94") is None


def test_empty_string_returns_none():
    assert parse_weather_ticker("") is None


def test_partial_ticker_returns_none():
    assert parse_weather_ticker("KXHIGHTPHX") is None


# ── City mapping coverage ─────────────────────────────────────────────────────

def test_all_cities_parse_without_error():
    """All 20 cities in _CITY_TO_CLI must produce a valid WeatherMarket."""
    question = "Will the maximum temperature be greater than 90° on Apr 29, 2026?"
    for city, expected_cli in _CITY_TO_CLI.items():
        result = parse_weather_ticker(f"KXHIGHT{city}-26APR29-T90", question=question)
        assert result is not None, f"parse_weather_ticker returned None for city {city!r}"
        assert isinstance(result, WeatherMarket)
        assert result.city == city
        assert result.cli_station == expected_cli
        assert result.market_type == "high"
        assert result.threshold_type == "above"
        assert result.threshold_value == 90.0
        assert result.target_date == date(2026, 4, 29)


def test_all_cities_low_parse():
    """All cities also parse for low-temp series."""
    question = "Will the minimum temperature be above 50° on Apr 29, 2026?"
    for city in _CITY_TO_CLI:
        result = parse_weather_ticker(f"KXLOWT{city}-26APR29-T50", question=question)
        assert result is not None, f"KXLOWT parse failed for city {city!r}"
        assert result.market_type == "low"
        assert result.cli_station == _CITY_TO_CLI[city]


# ── Specific city mapping checks ──────────────────────────────────────────────

def test_chi_maps_to_mdw():
    m = parse_weather_ticker("KXHIGHTCHI-26APR29-T75", question="Will the high be above 75°?")
    assert m is not None
    assert m.cli_station == "MDW"


def test_min_maps_to_msp():
    m = parse_weather_ticker("KXHIGHTMIN-26APR29-T75", question="Will the high be above 75°?")
    assert m is not None
    assert m.cli_station == "MSP"


def test_lv_maps_to_las():
    m = parse_weather_ticker("KXHIGHTLV-26APR29-T100", question="Will the high be above 100°?")
    assert m is not None
    assert m.cli_station == "LAS"


def test_phil_maps_to_phl():
    m = parse_weather_ticker("KXHIGHTPHIL-26APR29-T75", question="Will the high be above 75°?")
    assert m is not None
    assert m.cli_station == "PHL"


def test_nola_maps_to_msy():
    m = parse_weather_ticker("KXHIGHTNOLA-26APR29-T90", question="Will the high be above 90°?")
    assert m is not None
    assert m.cli_station == "MSY"


def test_dc_maps_to_dca():
    m = parse_weather_ticker("KXHIGHTDC-26APR29-T85", question="Will the high be above 85°?")
    assert m is not None
    assert m.cli_station == "DCA"


# ── Date parsing ──────────────────────────────────────────────────────────────

def test_date_parsing_jan():
    m = parse_weather_ticker(
        "KXHIGHTPHX-26JAN15-T65",
        question="Will the maximum temperature be greater than 65° on Jan 15, 2026?",
    )
    assert m is not None
    assert m.target_date == date(2026, 1, 15)


def test_date_parsing_dec():
    m = parse_weather_ticker(
        "KXHIGHTPHX-26DEC31-T50",
        question="Will the maximum temperature be greater than 50° on Dec 31, 2026?",
    )
    assert m is not None
    assert m.target_date == date(2026, 12, 31)


# ── T-direction parsing ───────────────────────────────────────────────────────

def test_t_below_low_aus():
    m = parse_weather_ticker(
        "KXLOWTAUS-26APR28-T68",
        question="Will the minimum temperature be <68° on Apr 28, 2026?",
    )
    assert m is not None
    assert m.threshold_type == "below"
    assert m.threshold_value == 68.0
    assert m.market_type == "low"


def test_t_below_high_phx():
    m = parse_weather_ticker(
        "KXHIGHTPHX-26APR29-T87",
        question="Will the maximum temperature be <87° on Apr 29, 2026?",
    )
    assert m is not None
    assert m.threshold_type == "below"
    assert m.market_type == "high"


def test_t_above_low_aus():
    m = parse_weather_ticker(
        "KXLOWTAUS-26APR28-T75",
        question="Will the minimum temperature be >75° on Apr 28, 2026?",
    )
    assert m is not None
    assert m.threshold_type == "above"
    assert m.threshold_value == 75.0


def test_t_no_question_returns_none():
    # T-prefix without question must return None (cannot determine direction)
    assert parse_weather_ticker("KXLOWTAUS-26APR28-T75") is None


def test_t_no_direction_in_question_returns_none(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="data.ground_truth.weather_kalshi"):
        result = parse_weather_ticker(
            "KXHIGHTPHX-26APR29-T94",
            question="Some malformed text without direction words",
        )
    assert result is None
    assert "Could not determine direction" in caplog.text


def test_bracket_no_question_still_parses():
    # B-prefix does not need a question — behavior unchanged
    m = parse_weather_ticker("KXHIGHTPHX-26APR29-B89.5")
    assert m is not None
    assert m.threshold_type == "bracket"
    assert m.threshold_value == 89.5
    assert m.bracket_low == 89.0
    assert m.bracket_high == 90.0


def test_t_no_question_logs_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="data.ground_truth.weather_kalshi"):
        result = parse_weather_ticker("KXHIGHTPHX-26APR29-T94")
    assert result is None
    assert "requires question text" in caplog.text
