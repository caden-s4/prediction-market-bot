"""
Stage 1 – Data Aggregation and Probability Estimation.

Inputs  : a Market + a WeatherAggregator
Outputs : a ConsensusForecast with consensus_prob and 95% CI

Logic:
- If the market has a location → query all NWP model clients via
  WeatherAggregator.get_consensus()
- Weight models by horizon (HRRR for short-range, ECMWF for long-range)
- Apply historical bias corrections from BiasStore (if available)
- Return the ConsensusForecast; caller passes it to Stage 2+
"""

from __future__ import annotations

import logging
from typing import Optional

from data.markets.base import Market
from data.weather.aggregator import ConsensusForecast, WeatherAggregator

logger = logging.getLogger(__name__)


class Stage1Probability:
    """
    Wraps WeatherAggregator and resolves horizon from market resolution date.
    """

    def __init__(self, aggregator: Optional[WeatherAggregator] = None) -> None:
        self._aggregator = aggregator or WeatherAggregator()

    def run(self, market: Market) -> Optional[ConsensusForecast]:
        """
        Produce a ConsensusForecast for the given market.

        Returns None if:
        - The market has no location data
        - All weather model clients fail
        """
        if not market.location:
            logger.debug(
                "Stage1: market %s has no location – skipping weather forecast",
                market.market_id,
            )
            return None

        lat = market.location["lat"]
        lon = market.location["lon"]
        horizon_hours = market.hours_to_resolution

        if horizon_hours <= 0:
            logger.warning(
                "Stage1: market %s already past resolution date", market.market_id
            )
            return None

        # Cap horizon at 240 h (10 days) – beyond that model skill is near zero
        horizon_hours = min(horizon_hours, 240.0)

        try:
            fc = self._aggregator.get_consensus(
                latitude=lat,
                longitude=lon,
                horizon_hours=horizon_hours,
            )
            logger.info(
                "Stage1: market=%s loc=(%s,%.1f,%.1f) horizon=%.0fh → %s",
                market.market_id,
                market.location.get("city", "?"),
                lat, lon,
                horizon_hours,
                fc,
            )
            return fc
        except Exception as exc:
            logger.error("Stage1: WeatherAggregator failed for market %s: %s",
                         market.market_id, exc)
            return None
