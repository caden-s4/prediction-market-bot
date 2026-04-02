from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.ground_truth.base import SourceType
from resolution.confidence import (
    ConfidenceScorer,
    _category_based_clarity,
    _depth_penalty,
    _freshness_multiplier,
)


def test_confidence_passes_for_high_quality_signal(make_market, make_ground_truth, make_signal):
    market = make_market(category="sports", tags=["nba"], question="Will Team A win?")
    gt = make_ground_truth(
        confidence=0.92,
        source_type=SourceType.HARD,
        data_published_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    signal = make_signal(market_to_buy=market)

    score = ConfidenceScorer().score(market, gt, signal)

    assert score.passes is True
    assert score.skip_reason is None
    assert score.source_confidence >= 0.9
    assert score.resolution_clarity >= 0.95


def test_confidence_blocks_ambiguous_direction(make_market, make_ground_truth, make_signal):
    market = make_market()
    gt = make_ground_truth(directional_confidence="ambiguous")

    score = ConfidenceScorer().score(market, gt, make_signal(market_to_buy=market))

    assert score.passes is False
    assert "directionally ambiguous" in score.skip_reason


def test_confidence_cross_platform_uses_depth_penalty(make_market, make_signal):
    market = make_market(category="politics", tags=[])
    signal = make_signal(
        signal_type="cross_platform",
        market_to_buy=market,
        effective_gap=0.05,
        depth_ratio=0.2,
        reference_price=0.60,
        target_price=0.50,
    )

    score = ConfidenceScorer().score(market, None, signal)

    assert score.passes is False
    assert score.source_confidence < 0.8
    assert "source_confidence" in score.skip_reason


def test_confidence_marks_marginal_but_passing_signals_for_depth_check(make_market, make_ground_truth, make_signal):
    market = make_market(category="financial", tags=[])
    gt = make_ground_truth(confidence=0.84)
    signal = make_signal(market_to_buy=market)

    score = ConfidenceScorer(threshold=0.80).score(market, gt, signal)

    assert score.passes is True
    assert score.requires_depth_check is True


def test_confidence_oracle_dispute_caps_clarity(make_market, make_ground_truth, make_signal):
    market = make_market(
        category="economics",
        tags=[],
        question="Will the result be as announced by officials?",
    )
    gt = make_ground_truth(confidence=0.95)

    score = ConfidenceScorer().score(market, gt, make_signal(market_to_buy=market))

    assert score.passes is False
    assert "oracle dispute" in score.skip_reason


def test_freshness_multiplier_buckets(make_ground_truth):
    assert _freshness_multiplier(make_ground_truth(data_published_at=None)) == 1.0
    assert _freshness_multiplier(
        make_ground_truth(data_published_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    ) == 0.9
    assert _freshness_multiplier(
        make_ground_truth(data_published_at=datetime.now(timezone.utc) - timedelta(minutes=90))
    ) == 0.8
    assert _freshness_multiplier(
        make_ground_truth(data_published_at=datetime.now(timezone.utc) - timedelta(hours=3))
    ) == 0.75


def test_category_helpers(make_market, make_signal):
    market = make_market(category="politics", tags=["nba"])
    assert _category_based_clarity(market) == 0.95

    signal = make_signal(depth_ratio=0.25)
    assert round(_depth_penalty(signal), 3) == 0.075
