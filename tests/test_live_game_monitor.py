from __future__ import annotations

import requests

from data.sports import live_game_monitor as lgm


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_sport_uses_cache(monkeypatch):
    lgm._sport_cache.clear()
    lgm._cycle_fetch_ms.clear()
    calls = {"count": 0}

    def fake_get(url, timeout):
        calls["count"] += 1
        return DummyResponse({"events": [{"id": "1"}]})

    monkeypatch.setattr(lgm.requests, "get", fake_get)

    first = lgm._fetch_sport("nba")
    second = lgm._fetch_sport("nba")

    assert first == [{"id": "1"}]
    assert second == [{"id": "1"}]
    assert calls["count"] == 1
    assert lgm._cycle_fetch_ms["nba"] == 0.0


def test_fetch_sport_preserves_last_data_on_failure(monkeypatch):
    lgm._sport_cache.clear()
    lgm._cycle_fetch_ms.clear()
    lgm._sport_cache["nba"] = lgm._SportCache(fetched_at=0, raw_events=[{"id": "cached"}], stale=False)

    def boom(url, timeout):
        raise requests.RequestException("espn down")

    monkeypatch.setattr(lgm.requests, "get", boom)

    result = lgm._fetch_sport("nba")

    assert result == [{"id": "cached"}]
    assert lgm._sport_cache["nba"].stale is True
