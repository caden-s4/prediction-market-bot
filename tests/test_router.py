from __future__ import annotations

from dataclasses import dataclass

from data.ground_truth.base import DataSource, GroundTruthResult, SourceType
from data.ground_truth.router import GroundTruthRouter


@dataclass
class FakeSource(DataSource):
    name: str
    handled: bool = True
    result: GroundTruthResult | None = None
    raises: bool = False

    def can_handle(self, market):
        return self.handled

    def fetch(self, market):
        if self.raises:
            raise RuntimeError(f"{self.name} exploded")
        return self.result


def test_router_returns_highest_confidence_tradeable(make_market):
    market = make_market()
    weak = FakeSource(
        name="weak",
        result=GroundTruthResult(
            ground_truth_prob=0.9,
            confidence=0.81,
            source_type=SourceType.HARD,
            source_name="Weak",
        ),
    )
    strong = FakeSource(
        name="strong",
        result=GroundTruthResult(
            ground_truth_prob=0.88,
            confidence=0.93,
            source_type=SourceType.HARD,
            source_name="Strong",
        ),
    )
    router = GroundTruthRouter(sources=[weak, strong])

    result = router.fetch(market)

    assert result is not None
    assert result.source_name == "Strong"


def test_router_returns_best_non_tradeable_when_no_tradeable_sources(make_market):
    market = make_market()
    candidate = GroundTruthResult(
        ground_truth_prob=0.7,
        confidence=0.6,
        source_type=SourceType.AGGREGATED,
        source_name="Candidate",
    )
    router = GroundTruthRouter(sources=[FakeSource(name="candidate", result=candidate)])

    result = router.fetch(market)

    assert result is candidate


def test_router_handles_source_exceptions(make_market):
    market = make_market()
    good = GroundTruthResult(
        ground_truth_prob=0.75,
        confidence=0.9,
        source_type=SourceType.HARD,
        source_name="Good",
    )
    router = GroundTruthRouter(
        sources=[
            FakeSource(name="bad", raises=True),
            FakeSource(name="good", result=good),
        ]
    )

    result = router.fetch(market)

    assert result is not None
    assert result.source_name == "Good"


def test_router_large_divergence_flags_human_review(make_market):
    market = make_market(yes_price=0.1)
    router = GroundTruthRouter(sources=[])
    result = GroundTruthResult(
        ground_truth_prob=0.9,
        confidence=0.95,
        source_type=SourceType.HARD,
        source_name="GT",
        raw_data={"k": "v"},
        reasoning="base",
    )

    validated = router.validate_result(result, market)

    assert validated.raw_data["requires_human_review"] is True
    assert "LARGE_DIVERGENCE" in validated.reasoning


def test_router_detects_novelty_markets(make_market):
    novelty = make_market(
        market_id="KXNBAMENTION-26MAR31-YES",
        question="What will the announcers say during the game?",
    )
    normal = make_market(question="Will Team A win?")

    assert GroundTruthRouter.is_novelty_market(novelty) is True
    assert GroundTruthRouter.is_novelty_market(normal) is False
