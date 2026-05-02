"""Tests for ResolutionBot.place_snipe_trade.

Constructs a minimal stub bot (SimpleNamespace + bound method) covering only
the attributes and helper methods that place_snipe_trade reads.  Avoids the
full ResolutionBot.__init__ chain (Kalshi client, scanner, GT router, etc.)
since this test targets the snipe placement logic in isolation.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from data.markets.base import Market, OrderBook, PriceLevel
from resolution.executor import ResolutionBot, TradeRecord
from strategies.weather_snipe import SnipeSignal


# ── Builders ──────────────────────────────────────────────────────────────────

def _ob(yes_ask: Optional[float] = 0.05, yes_bid: Optional[float] = 0.04) -> OrderBook:
    asks: List[PriceLevel] = [PriceLevel(price=yes_ask, size=1000.0)] if yes_ask is not None else []
    bids: List[PriceLevel] = [PriceLevel(price=yes_bid, size=1000.0)] if yes_bid is not None else []
    return OrderBook(
        market_id="KXHIGHTPHX-26APR30-T80",
        platform="kalshi",
        yes_bids=bids,
        yes_asks=asks,
    )


def _market() -> Market:
    rd = datetime.now(timezone.utc) + timedelta(minutes=30)
    return Market(
        market_id="KXHIGHTPHX-26APR30-T80",
        platform="kalshi",
        question="Will Phoenix high be > 80F?",
        category="weather",
        tags=[],
        resolution_date=rd,
        yes_price=0.05,
        no_price=0.95,
    )


def _signal(action: str = "buy_yes", target_price: float = 0.05) -> SnipeSignal:
    return SnipeSignal(
        market_id="KXHIGHTPHX-26APR30-T80",
        action=action,
        target_price=target_price,
        edge=0.94,
        confidence=0.99,
        rationale="max=85F, strike >80F, certain YES",
    )


class _PaperLogStub:
    def __init__(self):
        self.entries: list = []

    def log_entry(self, **kwargs):
        self.entries.append(kwargs)


class _BankrollStub:
    def __init__(self, total_usd: float = 500.0, reserve_ok: bool = True):
        self.total_usd = total_usd
        self._reserve_ok = reserve_ok
        self.reserved: list = []
        self.released: list = []

    def reserve(self, mid: str, amount: float) -> bool:
        if not self._reserve_ok:
            return False
        self.reserved.append((mid, amount))
        return True

    def release(self, mid: str, realized_pnl_usd: float = 0.0) -> None:
        self.released.append((mid, realized_pnl_usd))


class _ExclStub:
    def __init__(self, excluded: bool = False):
        self._excluded = excluded

    def is_excluded(self, platform: str, mid: str) -> bool:
        return self._excluded


def _make_bot(
    *,
    dry_run: bool = True,
    excluded: bool = False,
    positions=None,
    bankroll: Optional[_BankrollStub] = None,
    paper_log: Optional[_PaperLogStub] = None,
    ob: Optional[OrderBook] = None,
    size_usd: float = 25.0,
    place_order_id: Optional[str] = "ghost_KXHIGHTPHX-26APR30-T80_1700000000",
):
    bot = types.SimpleNamespace()
    bot._dry_run = dry_run
    bot._positions = positions if positions is not None else {}
    bot._exclusions = _ExclStub(excluded=excluded)
    bot._bankroll = bankroll if bankroll is not None else _BankrollStub()
    bot._paper_log = paper_log
    bot._registry = types.SimpleNamespace(_entries={})

    bot._snipes_placed_cum = 0

    bot._get_live_book = lambda m: ob
    bot._compute_size = lambda gap, conf: size_usd
    bot._place_order = lambda m, gap, size, fee=0.0, limit_price=None: place_order_id
    bot._save_positions = lambda: None

    bot.place_snipe_trade = types.MethodType(
        ResolutionBot.place_snipe_trade, bot,
    )
    return bot


# ── Gate tests ────────────────────────────────────────────────────────────────

def test_exclusion_gate_blocks(caplog):
    bot = _make_bot(excluded=True, ob=_ob())
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "SKIP_SNIPE" in caplog.text and "exclusion" in caplog.text
    assert bot._positions == {}


def test_position_dedup_blocks(caplog):
    bot = _make_bot(
        positions={"KXHIGHTPHX-26APR30-T80": "<existing>"},
        ob=_ob(),
    )
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "already holding" in caplog.text


def test_invalid_action_returns_none(caplog):
    bot = _make_bot(ob=_ob())
    with caplog.at_level("WARNING", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal(action="sell_yes"))
    assert result is None
    assert "invalid action" in caplog.text


def test_empty_book_skips_snipe(caplog):
    bot = _make_bot(ob=None)  # _get_live_book returns None
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "SKIP_EMPTY_BOOK_SNIPE" in caplog.text
    assert bot._positions == {}              # no PENDING record
    assert bot._bankroll.reserved == []      # no reserve attempted


def test_no_yes_ask_skips_buy_yes(caplog):
    # Book exists but has no YES asks (mid_price won't be None as long as a bid exists,
    # but for buy_yes we still need an ask).
    bot = _make_bot(ob=_ob(yes_ask=None, yes_bid=0.04))
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal(action="buy_yes"))
    assert result is None
    assert "SKIP_EMPTY_BOOK_SNIPE" in caplog.text
    assert "YES ask" in caplog.text


def test_size_too_small_returns_none(caplog):
    bot = _make_bot(ob=_ob(), size_usd=0.5)
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "size too small" in caplog.text


def test_series_cap_exceeded(caplog):
    # bankroll 500 × ghost cap 50% = $250 max per series.
    # Pre-populate one position from same series at $240; new $25 → 265 > 250.
    existing = TradeRecord(
        market_id="KXHIGHTPHX-26APR29-T78",
        platform="kalshi",
        market=_market(),
        signal=None,
        action="buy_yes",
        entry_price=0.05,
        size_usd=240.0,
        ground_truth_prob=0.99,
        source_confidence=0.99,
    )
    bot = _make_bot(
        ob=_ob(),
        positions={"KXHIGHTPHX-26APR29-T78": existing},
        size_usd=25.0,
    )
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "series exposure cap" in caplog.text
    # New position must NOT have been recorded.
    assert "KXHIGHTPHX-26APR30-T80" not in bot._positions


def test_bankroll_reserve_fails(caplog):
    bot = _make_bot(
        ob=_ob(),
        bankroll=_BankrollStub(total_usd=500.0, reserve_ok=False),
    )
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "bankroll reserve failed" in caplog.text


# ── Happy-path tests ──────────────────────────────────────────────────────────

def test_happy_path_ghost_records_position_and_paper_log(caplog):
    paper = _PaperLogStub()
    bot = _make_bot(dry_run=True, ob=_ob(), paper_log=paper)
    with caplog.at_level("INFO", logger="resolution.executor"):
        result = bot.place_snipe_trade(_market(), _signal())
    assert result is not None
    assert result.startswith("ghost_")

    # Position recorded
    rec = bot._positions["KXHIGHTPHX-26APR30-T80"]
    assert isinstance(rec, TradeRecord)
    assert rec.action == "buy_yes"
    assert rec.entry_price == 0.05
    assert rec.size_usd == 25.0
    assert rec.signal is None              # SnipeSignal isn't a GapSignal
    assert rec.fill_status == "filled"     # never PENDING
    assert rec.source_confidence == 0.99
    assert rec.ground_truth_prob == 0.99   # decisive, buy_yes side

    # Paper log entry written
    assert len(paper.entries) == 1
    e = paper.entries[0]
    assert e["market_id"] == "KXHIGHTPHX-26APR30-T80"
    assert e["action"] == "buy_yes"
    assert e["source"] == "WeatherSnipe"
    assert e["confidence"] == 0.99
    assert e["entry_price"] == 0.05

    # Bankroll reserved
    assert bot._bankroll.reserved == [("KXHIGHTPHX-26APR30-T80", 25.0)]
    assert "SNIPE buy_yes" in caplog.text


def test_happy_path_live_records_position_and_paper_log():
    paper = _PaperLogStub()
    bot = _make_bot(
        dry_run=False,
        ob=_ob(),
        paper_log=paper,
        place_order_id="kalshi_real_order_42",
    )
    result = bot.place_snipe_trade(_market(), _signal())
    assert result == "kalshi_real_order_42"

    rec = bot._positions["KXHIGHTPHX-26APR30-T80"]
    assert rec.order_id == "kalshi_real_order_42"
    assert rec.fill_status == "filled"
    assert len(paper.entries) == 1
    assert paper.entries[0]["source"] == "WeatherSnipe"


def test_happy_path_buy_no_uses_yes_bid_as_limit():
    paper = _PaperLogStub()
    bot = _make_bot(
        dry_run=True,
        ob=_ob(yes_ask=0.97, yes_bid=0.96),
        paper_log=paper,
    )
    sig = _signal(action="buy_no", target_price=0.04)  # NO target = 1 - yes_bid = 0.04
    result = bot.place_snipe_trade(_market(), sig)
    assert result is not None

    rec = bot._positions["KXHIGHTPHX-26APR30-T80"]
    assert rec.action == "buy_no"
    assert rec.entry_price == 0.96   # YES bid, as the limit_price
    assert rec.ground_truth_prob == 0.01   # decisive, buy_no side


def test_live_place_order_failure_releases_no_position():
    bot = _make_bot(
        dry_run=False,
        ob=_ob(),
        place_order_id=None,             # _place_order signals failure in live mode
    )
    result = bot.place_snipe_trade(_market(), _signal())
    assert result is None
    assert "KXHIGHTPHX-26APR30-T80" not in bot._positions
