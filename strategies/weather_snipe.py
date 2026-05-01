"""Weather resolution sniping strategy.

Triggered when a weather market is within 60 minutes of close.
Fetches today's running max/min temperature for the market's city,
compares against the market's strike, and emits a signal if the
outcome is essentially determined and Kalshi has not fully repriced.

This is a scheduled-trigger strategy, not a continuous-monitoring one.
It evaluates each market only in its final hour. The strategy bypasses
the standard GT router / gap detector / scorer pipeline — those assume
continuous-monitoring strategies. Sniping has its own gap/edge logic
here; the executor's safety gates still apply downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import logging

from data.ground_truth.weather_cli import fetch_asos_running_extreme
from data.ground_truth.weather_kalshi import parse_weather_ticker
from data.ground_truth.weather_timezones import CITY_TZ_MAP
from data.markets.base import Market

logger = logging.getLogger(__name__)


_SNIPE_WINDOW_MINUTES = 60
_MIN_OBSERVATIONS = 6
_SAFETY_MARGIN_F = 1.0
_REMAINING_MOVEMENT_F = 1.0
_BRACKET_NO_MARGIN_F = 0.5
_DECISIVE_PROB = 0.99
_SNIPE_CONFIDENCE = 0.99
_YES_FULLY_PRICED = 0.97
_NO_FULLY_PRICED = 0.03


@dataclass
class SnipeSignal:
    market_id: str
    action: str          # "buy_yes" or "buy_no"
    target_price: float  # the price we're willing to pay
    edge: float          # actual_prob - market_implied_prob (cost basis)
    confidence: float    # always >= 0.95 for snipes (decisive)
    rationale: str       # human-readable explanation


def evaluate_snipe(
    market: Market,
    now_utc: datetime,
) -> Optional[SnipeSignal]:
    """Evaluate a weather market for a sniping signal.

    Returns None if:
    - Market is outside the 60-min snipe window (or already closed)
    - Ticker isn't a parseable weather ticker
    - Today's running max/min isn't available, or has too few observations
    - Outcome is not decisively determined
    - Kalshi market is already priced near certainty (no edge left)
    """
    if not _within_snipe_window(market, now_utc):
        return None

    mid = market.market_id

    wm = parse_weather_ticker(mid, market.question)
    if wm is None:
        logger.info("WeatherSnipe: %s — no ASOS data (unparseable ticker)", mid)
        return None

    tz_name = CITY_TZ_MAP.get(wm.city)
    if tz_name is None:
        logger.info("WeatherSnipe: %s — no ASOS data (no timezone for city %r)", mid, wm.city)
        return None

    extreme = fetch_asos_running_extreme(
        wm.cli_station, tz_name, now_utc=now_utc
    )
    if extreme is None:
        logger.info("WeatherSnipe: %s — no ASOS data (fetch returned None, station=%s)", mid, wm.cli_station)
        return None
    if extreme.observation_count < _MIN_OBSERVATIONS:
        logger.info(
            "WeatherSnipe: %s — insufficient observations (%d < %d, station=%s)",
            mid, extreme.observation_count, _MIN_OBSERVATIONS, wm.cli_station,
        )
        return None

    if wm.market_type == "high":
        temp = extreme.running_max_f
    elif wm.market_type == "low":
        temp = extreme.running_min_f
    else:
        logger.info("WeatherSnipe: %s — no ASOS data (unknown market_type %r)", mid, wm.market_type)
        return None
    if temp is None:
        logger.info("WeatherSnipe: %s — no ASOS data (running temp is None, station=%s)", mid, wm.cli_station)
        return None

    decision = _decide_outcome(wm, temp)
    if decision is None:
        logger.info(
            "WeatherSnipe: %s — outcome not decisive (temp=%.1fF, type=%s, threshold=%.1f)",
            mid, temp, wm.threshold_type, getattr(wm, "threshold_value", float("nan")),
        )
        return None

    return _build_signal(market, wm, temp, decision)


# ── internals ────────────────────────────────────────────────────────────────

def _within_snipe_window(market: Market, now_utc: datetime) -> bool:
    rd = market.resolution_date
    if rd.tzinfo is None:
        rd = rd.replace(tzinfo=timezone.utc)
    delta = rd - now_utc
    return timedelta(0) < delta <= timedelta(minutes=_SNIPE_WINDOW_MINUTES)


def _decide_outcome(wm, temp: float) -> Optional[str]:
    """Return 'yes', 'no', or None for not decisive."""
    if wm.threshold_type == "above":
        if temp > wm.threshold_value + _SAFETY_MARGIN_F:
            return "yes"
        if temp + _REMAINING_MOVEMENT_F < wm.threshold_value:
            return "no"
        return None
    if wm.threshold_type == "below":
        if temp + _REMAINING_MOVEMENT_F < wm.threshold_value:
            return "yes"
        if temp > wm.threshold_value + _SAFETY_MARGIN_F:
            return "no"
        return None
    if wm.threshold_type == "bracket":
        lo = wm.bracket_low
        hi = wm.bracket_high
        if lo is None or hi is None:
            return None
        # Brackets are 1°F wide and CLI temps are integers, so a symmetric
        # safety margin would collapse the YES zone. By the snipe window
        # (final 60 min before close), the day's extreme is locked in,
        # so the inclusive Phase 1B rule is correct for resolution.
        if lo <= temp <= hi:
            return "yes"
        if temp < lo - _BRACKET_NO_MARGIN_F or temp > hi + _BRACKET_NO_MARGIN_F:
            return "no"
        return None
    return None


def _build_signal(
    market: Market, wm, temp: float, decision: str,
) -> Optional[SnipeSignal]:
    yes_ask = _resolve_price(getattr(market, "yes_ask", None), market.yes_price)
    yes_bid = _resolve_price(getattr(market, "yes_bid", None), market.yes_price)

    mid = market.market_id
    if decision == "yes":
        if yes_ask >= _YES_FULLY_PRICED:
            logger.info(
                "WeatherSnipe: %s — already priced no edge (buy_yes: yes_ask=%.4f >= %.2f)",
                mid, yes_ask, _YES_FULLY_PRICED,
            )
            return None
        action = "buy_yes"
        target_price = yes_ask
        edge = _DECISIVE_PROB - yes_ask
    else:  # "no"
        # NO is fully priced when YES is near zero.
        if yes_bid <= _NO_FULLY_PRICED:
            logger.info(
                "WeatherSnipe: %s — already priced no edge (buy_no: yes_bid=%.4f <= %.2f)",
                mid, yes_bid, _NO_FULLY_PRICED,
            )
            return None
        action = "buy_no"
        no_ask = 1.0 - yes_bid
        target_price = no_ask
        edge = _DECISIVE_PROB - no_ask

    rationale = _format_rationale(wm, temp, decision)
    return SnipeSignal(
        market_id=market.market_id,
        action=action,
        target_price=target_price,
        edge=edge,
        confidence=_SNIPE_CONFIDENCE,
        rationale=rationale,
    )


def _resolve_price(primary: Optional[float], fallback: float) -> float:
    if primary is None:
        return fallback
    return float(primary)


def _format_rationale(wm, temp: float, decision: str) -> str:
    label = "max" if wm.market_type == "high" else "min"
    if wm.threshold_type == "bracket":
        return (
            f"{label}={temp:.1f}F vs bracket "
            f"[{wm.bracket_low:.1f}, {wm.bracket_high:.1f}], "
            f"certain {decision.upper()}"
        )
    op = ">" if wm.threshold_type == "above" else "<"
    return (
        f"{label}={temp:.1f}F, strike {op}{wm.threshold_value:.1f}F, "
        f"certain {decision.upper()}"
    )
