from __future__ import annotations

import logging

from data.markets.kalshi_ws import KalshiWebSocket


def _make_ws() -> KalshiWebSocket:
    # Constructor only stores credentials; the network thread isn't started.
    return KalshiWebSocket(api_key="test-key", api_secret="test-secret")


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
