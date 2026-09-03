from __future__ import annotations

import asyncio
import json
import logging
from typing import List

from data.markets.kalshi_ws import KalshiWebSocket


def _make_ws() -> KalshiWebSocket:
    # Constructor only stores credentials; the network thread isn't started.
    return KalshiWebSocket(api_key="test-key", api_secret="test-secret")


class _FakeWebSocket:
    """Minimal fake satisfying the ``ws.send`` interface used by the client."""
    def __init__(self) -> None:
        self.sent: List[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


# ── Error handler (WS-Fix-A regression) ─────────────────────────────────────

def test_error_handler_logs_nested_code(caplog):
    ws = _make_ws()
    payload = {"type": "error", "msg": {"code": 4, "msg": "Subscription IDs required"}}

    with caplog.at_level(logging.WARNING, logger="data.markets.kalshi_ws"):
        ws._process_message(payload)

    assert "code=4" in caplog.text
    assert "code=?" not in caplog.text
    assert "Subscription IDs required" in caplog.text


def test_error_handler_falls_back_when_msg_missing(caplog):
    # Defensive: pre-v2 / malformed frames without nested msg dict shouldn't crash.
    ws = _make_ws()
    payload = {"type": "error", "code": 7, "message": "legacy shape"}

    with caplog.at_level(logging.WARNING, logger="data.markets.kalshi_ws"):
        ws._process_message(payload)

    assert "code=7" in caplog.text
    assert "legacy shape" in caplog.text


# ── SID tracking from acks (Part 1) ─────────────────────────────────────────

def test_ok_ack_populates_ticker_to_sid_for_inflight():
    ws = _make_ws()
    ws.subscribe(["A", "B"])
    payload = {"type": "ok", "id": 1, "sid": 42,
               "msg": {"market_tickers": ["A", "B"]}}

    ws._process_message(payload)

    assert ws._ticker_to_sid == {"A": 42, "B": 42}
    assert ws._sid_to_tickers == {42: {"A", "B"}}


def test_subscribed_ack_populates_ticker_to_sid():
    ws = _make_ws()
    ws.subscribe(["X"])
    payload = {"type": "subscribed",
               "msg": {"sid": 7, "channel": "orderbook_delta",
                       "market_tickers": ["X"]}}

    ws._process_message(payload)

    assert ws._ticker_to_sid == {"X": 7}
    assert ws._sid_to_tickers == {7: {"X"}}


def test_sub_ack_only_confirms_inflight_tickers():
    # Defensive: if a server ack echoes a ticker we never asked for (cumulative
    # semantics, stale frame), don't fabricate state for it.
    ws = _make_ws()
    ws.subscribe(["A"])
    payload = {"type": "ok", "id": 1, "sid": 99,
               "msg": {"market_tickers": ["A", "STRAY"]}}

    ws._process_message(payload)

    assert "A" in ws._subscribed
    assert "STRAY" not in ws._subscribed
    assert "STRAY" not in ws._ticker_to_sid


# ── Unsubscribe payload format (Part 2) ─────────────────────────────────────

def test_send_unsubscribe_uses_sids_array():
    ws = _make_ws()
    fake = _FakeWebSocket()

    asyncio.run(ws._send_unsubscribe(fake, [11, 22]))

    assert len(fake.sent) == 1
    frame = fake.sent[0]
    assert frame["cmd"] == "unsubscribe"
    assert "sids" in frame["params"]
    assert "market_ticker" not in frame["params"]
    assert frame["params"]["sids"] == [11, 22]


def test_send_unsubscribe_empty_sends_nothing():
    ws = _make_ws()
    fake = _FakeWebSocket()

    asyncio.run(ws._send_unsubscribe(fake, []))

    assert fake.sent == []


def test_resolve_unsubscribe_sids_skips_unknown(caplog):
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.add("A")
        ws._ticker_to_sid["A"] = 11
        ws._sid_to_tickers[11] = {"A"}

    with caplog.at_level(logging.DEBUG, logger="data.markets.kalshi_ws"):
        sids = ws._resolve_unsubscribe_sids(["A", "UNKNOWN"])

    assert sids == [11]
    assert "no sid known" in caplog.text


def test_resolve_unsubscribe_sids_all_unknown_returns_empty():
    ws = _make_ws()

    assert ws._resolve_unsubscribe_sids(["UNKNOWN1", "UNKNOWN2"]) == []


def test_resolve_unsubscribe_sids_dedupes_shared_sids():
    # If the server bound multiple tickers to one sid (per-batch semantics),
    # the resolver should yield that sid exactly once.
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.update({"A", "B"})
        ws._ticker_to_sid.update({"A": 5, "B": 5})
        ws._sid_to_tickers[5] = {"A", "B"}

    assert ws._resolve_unsubscribe_sids(["A", "B"]) == [5]


# ── Batched unsubscribe (Part 3) ────────────────────────────────────────────

def test_unsubscribe_batches_100_per_frame():
    ws = _make_ws()
    tickers = [f"T{i}" for i in range(250)]
    with ws._sub_lock:
        ws._subscribed.update(tickers)
        for i, t in enumerate(tickers):
            ws._ticker_to_sid[t] = 1000 + i
            ws._sid_to_tickers[1000 + i] = {t}
    fake = _FakeWebSocket()

    async def drive():
        sids = ws._resolve_unsubscribe_sids(tickers)
        for i in range(0, len(sids), 100):
            await ws._send_unsubscribe(fake, sids[i:i + 100])
    asyncio.run(drive())

    assert len(fake.sent) == 3
    assert len(fake.sent[0]["params"]["sids"]) == 100
    assert len(fake.sent[1]["params"]["sids"]) == 100
    assert len(fake.sent[2]["params"]["sids"]) == 50


def test_resolve_unsubscribe_sids_dedupes_across_chunks():
    # Regression for the code=7 burst: a sid bound to tickers spanning
    # multiple 100-chunks must be sent exactly once across the whole batch.
    # 250 tickers, all sharing 3 sids whose ticker groupings straddle the
    # 100-ticker boundaries.
    ws = _make_ws()
    tickers = [f"T{i}" for i in range(250)]
    with ws._sub_lock:
        ws._subscribed.update(tickers)
        for i, t in enumerate(tickers):
            sid = 100 + (i // 90)  # sids 100, 101, 102; groups of 90 → cross 100-boundaries
            ws._ticker_to_sid[t] = sid
            ws._sid_to_tickers.setdefault(sid, set()).add(t)

    sids = ws._resolve_unsubscribe_sids(tickers)

    assert sorted(sids) == [100, 101, 102]
    assert len(sids) == len(set(sids))


# ── Ack-driven _subscribed mutation (Part 4) ────────────────────────────────

def test_subscribe_does_not_populate_subscribed_before_ack():
    ws = _make_ws()
    ws.subscribe(["A"])

    assert ws._subscribed == set()
    assert ws._subscribing_in_flight == {"A"}


def test_ok_ack_moves_inflight_to_subscribed():
    ws = _make_ws()
    ws.subscribe(["A"])

    ws._process_message({"type": "ok", "id": 1, "sid": 99,
                         "msg": {"market_tickers": ["A"]}})

    assert ws._subscribing_in_flight == set()
    assert ws._subscribed == {"A"}


def test_unsubscribe_keeps_subscribed_until_ack():
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.add("A")
        ws._ticker_to_sid["A"] = 5
        ws._sid_to_tickers[5] = {"A"}

    ws.unsubscribe(["A"])

    assert ws._subscribed == {"A"}
    assert ws._ticker_to_sid["A"] == 5


def test_unsubscribed_ack_clears_state_by_sid():
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.add("A")
        ws._ticker_to_sid["A"] = 5
        ws._sid_to_tickers[5] = {"A"}

    ws._process_message({"type": "unsubscribed", "msg": {"sids": [5]}})

    assert ws._subscribed == set()
    assert "A" not in ws._ticker_to_sid
    assert 5 not in ws._sid_to_tickers


def test_unsubscribed_ack_clears_state_by_market_tickers():
    # Defensive: ack might carry market_tickers instead of sids.
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.add("A")
        ws._ticker_to_sid["A"] = 5
        ws._sid_to_tickers[5] = {"A"}

    ws._process_message({"type": "unsubscribed",
                         "msg": {"market_tickers": ["A"]}})

    assert ws._subscribed == set()
    assert "A" not in ws._ticker_to_sid


def test_failed_unsubscribe_preserves_state():
    # The server returns code=4; no unsubscribed ack ever fires. State must be
    # preserved so the next sync_subscriptions cycle retries (audit §8.2).
    ws = _make_ws()
    with ws._sub_lock:
        ws._subscribed.add("A")
        ws._ticker_to_sid["A"] = 5
        ws._sid_to_tickers[5] = {"A"}

    ws._process_message({"type": "error",
                         "msg": {"code": 4, "msg": "Subscription IDs required"}})

    assert ws._subscribed == {"A"}
    assert ws._ticker_to_sid["A"] == 5


# ── In-flight tracking (Part 4 step 9 option α) ─────────────────────────────

def test_subscribe_skips_inflight_duplicates():
    # A second subscribe call for an in-flight ticker must not queue a duplicate
    # frame — otherwise we'd race a duplicate sub against the pending ack.
    ws = _make_ws()
    ws.subscribe(["A"])
    queue_size_after_first = ws._cmd_queue.qsize()

    ws.subscribe(["A"])

    assert ws._cmd_queue.qsize() == queue_size_after_first
    assert ws._subscribing_in_flight == {"A"}


def test_sync_subscriptions_skips_inflight_for_subscribe():
    ws = _make_ws()
    ws.subscribe(["A"])  # A is now in-flight
    queue_size_before = ws._cmd_queue.qsize()

    ws.sync_subscriptions(["A", "B"])

    # Only B should have been queued; A was skipped because in-flight.
    assert ws._cmd_queue.qsize() == queue_size_before + 1
    assert "B" in ws._subscribing_in_flight
