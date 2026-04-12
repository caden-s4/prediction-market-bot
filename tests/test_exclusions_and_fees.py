from __future__ import annotations

import json
import time

import requests

from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache, kalshi_fee_per_contract


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

    def fake_get(url, timeout, proxies=None):
        calls["count"] += 1
        return DummyResponse({"feeRateBps": 125})

    monkeypatch.setattr(requests, "get", fake_get)
    cache = FeeCache(ttl=1000)

    first = cache.get_taker_fee("polymarket", "M1", price=0.5)
    second = cache.get_taker_fee("polymarket", "M1", price=0.5)

    assert first == 0.0125
    assert second == 0.0125
    assert calls["count"] == 1


def test_fee_cache_returns_zero_on_request_failure(monkeypatch):
    def boom(url, timeout, proxies=None):
        raise requests.RequestException("network down")

    monkeypatch.setattr(requests, "get", boom)
    cache = FeeCache()

    assert cache.get_taker_fee("polymarket", "M1", price=0.5, force_refresh=True) == 0.0


def test_fee_cache_kalshi_uses_formula_not_api(monkeypatch):
    """Kalshi fee must be computed from formula, never hitting the API."""
    api_called = {"count": 0}

    def boom(url, timeout, proxies=None):
        api_called["count"] += 1
        raise AssertionError("Kalshi fee should not hit API")

    monkeypatch.setattr(requests, "get", boom)
    cache = FeeCache()

    fee = cache.get_taker_fee("kalshi", "K1", price=0.50, force_refresh=True)
    # kalshi_fee_per_contract(0.50) = round_up(0.07*0.25*100)/100 = round_up(1.75)/100 = 0.02
    assert fee == 0.02
    assert api_called["count"] == 0


# ── kalshi_fee_per_contract formula unit tests ─────────────────────────────────

def test_kalshi_fee_formula_mid_price():
    assert kalshi_fee_per_contract(0.50) == 0.02   # round_up(0.0175)


def test_kalshi_fee_formula_low_price():
    assert kalshi_fee_per_contract(0.10) == 0.01   # round_up(0.0063)


def test_kalshi_fee_formula_high_price():
    assert kalshi_fee_per_contract(0.90) == 0.01   # round_up(0.0063)


def test_kalshi_fee_formula_extreme_price():
    assert kalshi_fee_per_contract(0.01) == 0.01   # minimum


def test_kalshi_fee_formula_boundary_zero():
    assert kalshi_fee_per_contract(0.0) == 0.01    # minimum for edge cases


def test_kalshi_fee_formula_boundary_one():
    assert kalshi_fee_per_contract(1.0) == 0.01    # minimum for edge cases
