from __future__ import annotations

from datetime import datetime, timedelta, timezone

from resolution.tier_registry import TierRegistry


def test_tier_registry_ingest_and_promotions(make_market):
    registry = TierRegistry()
    tier3 = make_market(
        market_id="M3",
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=30),
    )
    tier2 = make_market(
        market_id="M2",
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=8),
    )

    assert registry.ingest(tier3) == 3
    assert registry.ingest(tier2) == 2

    refreshed = make_market(
        market_id="M3",
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    registry.ingest(refreshed)

    assert registry.get_tier(1)[0].market_id == "M3"


def test_tier_registry_mark_and_clear_urgent(make_market):
    registry = TierRegistry()
    market = make_market(
        market_id="M1",
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=10),
    )
    registry.ingest(market)

    registry.mark_urgent("M1")
    assert registry.get_tier(1)[0].market_id == "M1"

    registry.clear_urgent("M1")
    assert registry.get_tier(1)[0].market_id == "M1"

    registry.clear_sticky_t1("M1")
    assert registry.get_tier(2)[0].market_id == "M1"


def test_tier_registry_evicts_expired(make_market):
    registry = TierRegistry()
    expired = make_market(
        market_id="OLD",
        resolution_date=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    registry.ingest(expired)

    assert registry.evict_expired() == 1
    assert registry.stats()["total"] == 0
