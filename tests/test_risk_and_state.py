from __future__ import annotations

import json
from datetime import datetime, timezone

from shared.bankroll import Bankroll
from shared.paper_log import PaperTradeLog
from utils.storage import StateStore


def test_bankroll_reserve_release_and_halt():
    bankroll = Bankroll(total_usd=100.0, max_daily_loss_usd=10.0)

    assert bankroll.reserve("m1", 25.0) is True
    assert bankroll.available_usd == 75.0

    bankroll.release("m1", realized_pnl_usd=-12.0)

    assert bankroll.total_usd == 88.0
    assert bankroll.is_halted() is True
    assert bankroll.reserve("m2", 1.0) is False


def test_bankroll_sports_caps_exposure():
    bankroll = Bankroll(total_usd=1000.0)
    size = bankroll.sports_size_usd(
        base_size_usd=150.0,
        game_id="game-1",
        sport="nba",
        shock_magnitude=0.30,
    )

    assert size == 80.0
    assert bankroll.reserve_sports("m1", "game-1", "nba", size) is True
    assert bankroll.sports_size_usd(50.0, "game-1", "nba", shock_magnitude=0.30) == 0.0


def test_paper_trade_log_writes_and_summarizes(tmp_path):
    log = PaperTradeLog(path=str(tmp_path / "ghost_trades.jsonl"))
    log.log_entry(
        market_id="m1",
        platform="kalshi",
        action="buy_yes",
        entry_price=0.4,
        size_usd=10.0,
        gt_prob=0.8,
        gap=0.4,
        confidence=0.9,
        source="TestSource",
        tier=1,
        question="Will X happen?",
    )
    log.log_exit(
        market_id="m1",
        exit_price=0.9,
        pnl=5.0,
        pnl_pct=0.5,
        exit_reason="resolved",
        hold_duration_minutes=30.0,
    )

    summary = log.get_daily_summary(datetime.now(timezone.utc))

    assert summary["total_entries"] == 1
    assert summary["exits"] == 1
    assert summary["wins"] == 1
    assert summary["total_pnl"] == 5.0


def test_state_store_handles_invalid_json_and_persists(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{invalid json", encoding="utf-8")

    store = StateStore(path=path)
    assert store.all() == {}

    store.set("bankroll", 500)
    assert json.loads(path.read_text(encoding="utf-8"))["bankroll"] == 500

    store.delete("bankroll")
    assert store.get("bankroll") is None
