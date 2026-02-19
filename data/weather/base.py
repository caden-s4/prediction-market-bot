"""
data.weather.base – shared types and base class for NWP model clients.

All weather clients return a ModelForecast which is consumed downstream by
the WeatherAggregator (Stage 1).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ModelForecast:
    """
    Single model output for one location / variable / horizon combination.

    precipitation_prob : probability of measurable precipitation  [0-1]
    precip_amount_mm   : expected precipitation amount in mm
    temp_c             : temperature in Celsius
    wind_speed_ms      : wind speed in m/s
    confidence         : model self-reported confidence [0-1] (if available)
    model_name         : e.g. "GFS", "ECMWF", "NAM", "HRRR"
    valid_time         : UTC datetime this forecast is valid for
    issued_time        : UTC datetime the model run was issued
    horizon_hours      : forecast lead time in hours
    """

    model_name: str
    valid_time: datetime
    issued_time: datetime
    horizon_hours: float

    # Meteorological values
    precipitation_prob: float          # 0–1
    precip_amount_mm: float = 0.0
    temp_c: Optional[float] = None
    wind_speed_ms: Optional[float] = None
    wind_gust_ms: Optional[float] = None
    cloud_cover_pct: Optional[float] = None

    # Confidence / spread (higher spread → lower confidence)
    confidence: float = 0.5           # 0–1; populated by ensemble spread if avail.

    # Raw payload for debugging
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.precipitation_prob <= 1.0:
            raise ValueError(
                f"precipitation_prob must be in [0,1], got {self.precipitation_prob}"
            )


class BaseWeatherClient(ABC):
    """Abstract base for NWP model HTTP clients."""

    MODEL_NAME: str = "UNKNOWN"
    # Typical update cadence in hours (used by timing stage)
    UPDATE_INTERVAL_HOURS: float = 6.0
    # Hard-coded reliability weight at 24h; aggregator can override per horizon
    DEFAULT_WEIGHT: float = 0.25

    def __init__(self, session_cache_seconds: int = 300) -> None:
        self._session_cache_seconds = session_cache_seconds
        self._cache: dict[str, tuple[float, ModelForecast]] = {}  # key → (ts, value)

    # ── Public interface ──────────────────────────────────────────────────────

    def get_forecast(
        self,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        variable: str = "precipitation_probability",
    ) -> ModelForecast:
        """
        Return a ModelForecast for the given location and horizon.
        Results are cached for `session_cache_seconds` to avoid hammering APIs.
        """
        cache_key = f"{self.MODEL_NAME}:{latitude:.3f}:{longitude:.3f}:{horizon_hours}:{variable}"
        now = time.monotonic()
        if cache_key in self._cache:
            cached_ts, cached_val = self._cache[cache_key]
            if now - cached_ts < self._session_cache_seconds:
                return cached_val

        forecast = self._fetch(latitude, longitude, horizon_hours, variable)
        self._cache[cache_key] = (now, forecast)
        return forecast

    # ── Subclass responsibility ───────────────────────────────────────────────

    @abstractmethod
    def _fetch(
        self,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        variable: str,
    ) -> ModelForecast:
        """Hit the real API and return a ModelForecast."""
        ...
