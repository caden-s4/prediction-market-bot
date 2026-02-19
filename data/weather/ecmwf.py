"""
data.weather.ecmwf – ECMWF IFS model client.

Uses Open-Meteo's free ECMWF Open Data endpoint ("ecmwf_ifs04").
No API key required for the open data tier.

Update cadence: twice daily (00Z, 12Z).
Strengths: most accurate medium-range global model, best 3–7 day skill.
Weakness: coarser resolution (0.4°) than HRRR/NAM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import requests

from .base import BaseWeatherClient, ModelForecast


class ECMWFClient(BaseWeatherClient):
    MODEL_NAME = "ECMWF"
    UPDATE_INTERVAL_HOURS = 12.0
    DEFAULT_WEIGHT = 0.35   # highest base weight: best verified accuracy

    _BASE_URL = "https://api.open-meteo.com/v1/forecast"
    _OM_MODEL = "ecmwf_ifs04"

    def _fetch(
        self,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        variable: str = "precipitation_probability",
    ) -> ModelForecast:
        days = max(1, int(horizon_hours // 24) + 1)
        params: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": (
                "precipitation_probability,"
                "precipitation,"
                "temperature_2m,"
                "windspeed_10m,"
                "windgusts_10m,"
                "cloudcover"
            ),
            "models": self._OM_MODEL,
            "forecast_days": min(days, 10),   # ECMWF open data: 10-day horizon
            "timezone": "UTC",
        }

        resp = requests.get(self._BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return self._parse(data, horizon_hours)

    def _parse(self, data: dict, horizon_hours: float) -> ModelForecast:
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        now_utc = datetime.now(timezone.utc)
        target_idx = min(int(horizon_hours), len(times) - 1)

        def _safe(key: str, idx: int) -> float | None:
            vals = hourly.get(key, [])
            if vals and idx < len(vals) and vals[idx] is not None:
                return float(vals[idx])
            return None

        precip_prob = (_safe("precipitation_probability", target_idx) or 0.0) / 100.0
        valid_time_str = times[target_idx] if target_idx < len(times) else None
        valid_time = (
            datetime.fromisoformat(valid_time_str).replace(tzinfo=timezone.utc)
            if valid_time_str
            else now_utc
        )

        return ModelForecast(
            model_name=self.MODEL_NAME,
            valid_time=valid_time,
            issued_time=now_utc,
            horizon_hours=horizon_hours,
            precipitation_prob=min(max(precip_prob, 0.0), 1.0),
            precip_amount_mm=_safe("precipitation", target_idx) or 0.0,
            temp_c=_safe("temperature_2m", target_idx),
            wind_speed_ms=_safe("windspeed_10m", target_idx),
            wind_gust_ms=_safe("windgusts_10m", target_idx),
            cloud_cover_pct=_safe("cloudcover", target_idx),
            confidence=0.75,  # ECMWF typically highest skill; ensemble spread
            raw=data,
        )
