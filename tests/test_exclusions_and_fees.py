from __future__ import annotations

import json
import time

import requests

from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_exclusion_list_ttl_expiry(tmp_path):
    path = tmp_path / "exclusions.json"
    exclusions = ExclusionList(path=path)
    exclusions.add("kalshi", "M1", "temporary", ttl_seconds=0.01)
    assert exclusions.is_excluded("kalshi", "M1") is True

    time.sleep(0.03)

    assert exclusions.is_excluded("kalshi", "M1") is False


def test_exclusion_list_persists_to_disk(tmp_path):
    path = tmp_path / "exclusions.json"
    exclusions = ExclusionList(path=path)
    exclusions.add_low_depth("polymarket", "PM1")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "polymarket:PM1" in payload


def test_fee_cache_uses_cache_before_refresh(monkeypatch):
    calls = {"count": 0}

    def fake_get(url, timeout):
        calls["count"] += 1
        return DummyResponse({"feeRateBps": 125})

    monkeypatch.setattr(requests, "get", fake_get)
    cache = FeeCache(ttl=1000)

    first = cache.get_taker_fee("polymarket", "M1")
    second = cache.get_taker_fee("polymarket", "M1")

    assert first == 0.0125
    assert second == 0.0125
    assert calls["count"] == 1


def test_fee_cache_returns_zero_on_request_failure(monkeypatch):
    def boom(url, timeout):
        raise requests.RequestException("network down")

    monkeypatch.setattr(requests, "get", boom)
    cache = FeeCache()

    assert cache.get_taker_fee("polymarket", "M1", force_refresh=True) == 0.0


def test_fee_cache_kalshi_missing_field_defaults_zero(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: DummyResponse({"market": {}}))
    cache = FeeCache()

    assert cache.get_taker_fee("kalshi", "K1", force_refresh=True) == 0.0
