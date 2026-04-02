from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.ground_truth.base import GroundTruthResult, SourceType
from data.markets.base import Market
from resolution.gap_detector import GapSignal


@pytest.fixture
def make_market():
    def _make_market(**overrides: Any) -> Market:
        base = {
            "market_id": "TEST-MKT",
            "platform": "kalshi",
            "question": "Will Team A win?",
            "category": "sports",
            "tags": ["nba"],
            "resolution_date": datetime.now(timezone.utc) + timedelta(hours=6),
            "yes_price": 0.45,
            "no_price": 0.55,
            "volume_usd": 1000.0,
            "open_interest": 250.0,
            "location": None,
            "raw": {},
        }
        base.update(overrides)
        return Market(**base)

    return _make_market


@pytest.fixture
def make_ground_truth():
    def _make_ground_truth(**overrides: Any) -> GroundTruthResult:
        base = {
            "ground_truth_prob": 0.8,
            "confidence": 0.9,
            "source_type": SourceType.HARD,
            "source_name": "TestSource",
            "source_url": "https://example.com/source",
            "raw_data": {},
            "reasoning": "test reasoning",
            "data_published_at": None,
            "directional_confidence": None,
        }
        base.update(overrides)
        return GroundTruthResult(**base)

    return _make_ground_truth


@pytest.fixture
def make_signal(make_market):
    def _make_signal(**overrides: Any) -> GapSignal:
        market = overrides.pop("market_to_buy", make_market())
        base = {
            "signal_type": "information",
            "market_to_buy": market,
            "market_reference": None,
            "target_price": market.yes_price,
            "reference_price": 0.8,
            "ground_truth_prob": 0.8,
            "raw_gap": 0.35,
            "effective_gap": 0.35,
            "taker_fee": 0.0,
            "ground_truth_result": None,
            "depth_ratio": None,
            "reasoning": "test signal",
        }
        base.update(overrides)
        return GapSignal(**base)

    return _make_signal
