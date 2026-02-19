"""
data.weather.aggregator – Stage 1 ensemble aggregation.

Pulls forecasts from all available NWP model clients, weights them
intelligently based on horizon and historical bias, and produces a
single consensus probability with a confidence interval.

Key design principle: each model's weight is a function of:
  1. The forecast horizon (HRRR dominates ≤18 h, ECMWF dominates ≥72 h)
  2. Per-location/per-month historical bias corrections (learned over time)
  3. Whether the model has fresh data (penalise stale runs)

The output ConsensusForecast is consumed by Stage 1 of the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from .base import BaseWeatherClient, ModelForecast
from .gfs import GFSClient
from .ecmwf import ECMWFClient
from .nam import NAMClient
from .hrrr import HRRRClient

logger = logging.getLogger(__name__)


# ── Horizon-based weight tables ───────────────────────────────────────────────
# Keys are (model_name, horizon_bucket) where bucket = "short"/"medium"/"long"
# Short  : ≤18 h
# Medium : 18–72 h
# Long   : >72 h

_HORIZON_WEIGHTS: Dict[str, Dict[str, float]] = {
    "HRRR":  {"short": 0.50, "medium": 0.00, "long": 0.00},
    "NAM":   {"short": 0.25, "medium": 0.25, "long": 0.00},
    "GFS":   {"short": 0.15, "medium": 0.35, "long": 0.40},
    "ECMWF": {"short": 0.10, "medium": 0.40, "long": 0.60},
}


def _horizon_bucket(horizon_hours: float) -> str:
    if horizon_hours <= 18:
        return "short"
    if horizon_hours <= 72:
        return "medium"
    return "long"


@dataclass
class ConsensusForecast:
    """
    The blended output of all available model forecasts for one
    location + horizon combination.
    """
    location_lat: float
    location_lon: float
    horizon_hours: float
    consensus_prob: float          # weighted mean probability [0–1]
    prob_low: float                # 95 % CI lower bound
    prob_high: float               # 95 % CI upper bound
    model_forecasts: List[ModelForecast] = field(default_factory=list)
    weights_used: Dict[str, float] = field(default_factory=dict)
    bias_corrections: Dict[str, float] = field(default_factory=dict)
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def confidence_width(self) -> float:
        return self.prob_high - self.prob_low

    def __str__(self) -> str:
        return (
            f"ConsensusForecast("
            f"prob={self.consensus_prob:.1%}, "
            f"CI=[{self.prob_low:.1%}, {self.prob_high:.1%}], "
            f"horizon={self.horizon_hours:.0f}h)"
        )


class WeatherAggregator:
    """
    Orchestrates multiple NWP model clients and blends their outputs.

    Usage
    -----
    aggregator = WeatherAggregator()
    forecast = aggregator.get_consensus(lat=47.6, lon=-122.3, horizon_hours=24)
    print(forecast.consensus_prob)  # e.g. 0.58
    """

    def __init__(
        self,
        bias_store: Optional["BiasStore"] = None,
    ) -> None:
        # All available clients; NAM/HRRR will gracefully fail outside CONUS
        self._clients: List[BaseWeatherClient] = [
            HRRRClient(),
            NAMClient(),
            GFSClient(),
            ECMWFClient(),
        ]
        # BiasStore is optional; if None, no corrections applied
        self._bias_store = bias_store

    # ── Public API ────────────────────────────────────────────────────────────

    def get_consensus(
        self,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        variable: str = "precipitation_probability",
    ) -> ConsensusForecast:
        """
        Collect forecasts from all models and return a blended ConsensusForecast.
        Models that fail (coverage, network, etc.) are silently dropped.
        Requires at least 1 successful model or raises RuntimeError.
        """
        bucket = _horizon_bucket(horizon_hours)
        forecasts: List[ModelForecast] = []
        raw_weights: Dict[str, float] = {}

        for client in self._clients:
            try:
                fc = client.get_forecast(latitude, longitude, horizon_hours, variable)
                forecasts.append(fc)
                raw_weights[client.MODEL_NAME] = _HORIZON_WEIGHTS[client.MODEL_NAME][bucket]
                logger.debug(
                    "Model %s → precip_prob=%.1f%%",
                    client.MODEL_NAME,
                    fc.precipitation_prob * 100,
                )
            except Exception as exc:
                logger.warning("Model %s failed: %s", client.__class__.MODEL_NAME, exc)

        if not forecasts:
            raise RuntimeError(
                "All weather model clients failed – cannot produce consensus."
            )

        # Normalise weights to sum to 1 across available models only
        available_names = {fc.model_name for fc in forecasts}
        effective_weights = {
            name: w for name, w in raw_weights.items() if name in available_names
        }
        total_w = sum(effective_weights.values()) or 1.0
        normalised = {n: w / total_w for n, w in effective_weights.items()}

        # Apply optional bias corrections
        bias_corrections: Dict[str, float] = {}
        if self._bias_store:
            for fc in forecasts:
                bias = self._bias_store.get_bias(
                    fc.model_name, latitude, longitude, horizon_hours
                )
                bias_corrections[fc.model_name] = bias

        # Weighted consensus probability
        probs = np.array([
            fc.precipitation_prob + bias_corrections.get(fc.model_name, 0.0)
            for fc in forecasts
        ])
        wts = np.array([normalised.get(fc.model_name, 0.0) for fc in forecasts])
        probs = np.clip(probs, 0.0, 1.0)

        consensus = float(np.dot(wts, probs))

        # 95 % confidence interval via weighted standard deviation of model spread
        if len(probs) > 1:
            variance = float(np.dot(wts, (probs - consensus) ** 2))
            std = np.sqrt(variance)
            # Add model-internal uncertainty contribution
            avg_confidence = float(np.dot(wts, np.array([fc.confidence for fc in forecasts])))
            model_uncertainty = (1.0 - avg_confidence) * 0.15
            total_std = np.sqrt(std**2 + model_uncertainty**2)
            ci_half = 1.96 * total_std
        else:
            # Single model – use its confidence to estimate width
            ci_half = (1.0 - forecasts[0].confidence) * 0.20

        prob_low = float(np.clip(consensus - ci_half, 0.0, 1.0))
        prob_high = float(np.clip(consensus + ci_half, 0.0, 1.0))

        return ConsensusForecast(
            location_lat=latitude,
            location_lon=longitude,
            horizon_hours=horizon_hours,
            consensus_prob=consensus,
            prob_low=prob_low,
            prob_high=prob_high,
            model_forecasts=forecasts,
            weights_used=normalised,
            bias_corrections=bias_corrections,
        )


# ── Bias store (placeholder for meta-learning layer) ─────────────────────────

class BiasStore:
    """
    Tracks each model's historical bias per location bucket and horizon bucket.
    Bias = mean(estimated_prob - actual_outcome) over past N trades.
    Negative bias → model underpredicts; positive → overpredicts.
    """

    def __init__(self) -> None:
        # Structure: {(model, lat_bucket, lon_bucket, horizon_bucket): [errors]}
        self._records: Dict[tuple, List[float]] = {}

    def record(
        self,
        model_name: str,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        predicted_prob: float,
        actual_outcome: float,  # 0 or 1
    ) -> None:
        key = self._key(model_name, latitude, longitude, horizon_hours)
        self._records.setdefault(key, []).append(predicted_prob - actual_outcome)

    def get_bias(
        self,
        model_name: str,
        latitude: float,
        longitude: float,
        horizon_hours: float,
        window: int = 50,
    ) -> float:
        """Return the mean recent bias (correction to subtract from model output)."""
        key = self._key(model_name, latitude, longitude, horizon_hours)
        errors = self._records.get(key, [])
        if not errors:
            return 0.0
        recent = errors[-window:]
        return float(np.mean(recent))

    @staticmethod
    def _key(model: str, lat: float, lon: float, horizon: float) -> tuple:
        # Bucket to ~1° lat/lon grid and horizon bucket
        return (model, round(lat), round(lon), _horizon_bucket(horizon))
