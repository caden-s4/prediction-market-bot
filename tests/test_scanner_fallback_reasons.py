"""Phase 2B-1 — per-fallback-reason instrumentation regression tests.

Validates that each REST fallback path increments exactly one
``_rest_reasons`` bucket and that the extended ``ws=%d rest=%d total=%d
(rest: no_entry=%d stale=%d empty=%d disabled=%d)`` log line is emitted.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from data.markets.base import Market, OrderBook, PriceLevel
from resolution.scanner import ResolutionScanner
from shared.exclusion_list import ExclusionList


def _mk_market(market_id: str, platform: str = "kalshi") -> Market:
    rd = datetime.now(timezone.utc) + timedelta(hours=6)
    return Market(
        market_id=market_id,
        platform=platform,
        question="Will X happen?",
        category="sports",
        tags=[],
        resolution_date=rd,
        yes_price=0.5,
        no_price=0.5,
    )


def _book(market_id: str, bids: List[Tuple[float, float]], asks: List[Tuple[float, float]]) -> OrderBook:
    return OrderBook(
        market_id=market_id,
        platform="kalshi",
        yes_bids=[PriceLevel(price=p, size=s) for p, s in bids],
        yes_asks=[PriceLevel(price=p, size=s) for p, s in asks],
    )


class _FakeKalshiClient:
    """Minimal client; get_market echoes the id, get_order_book returns a usable REST book."""
    def get_market(self, market_id: str) -> Optional[Market]:
        return _mk_market(market_id)

    def get_order_book(self, market_id: str) -> Optional[OrderBook]:
        return _book(market_id, bids=[(0.49, 100.0)], asks=[(0.51, 100.0)])


class _FakeKalshiWS:
    """Duck-types KalshiWebSocket.get_book_age / get_book."""
    def __init__(self) -> None:
        self._ages: Dict[str, Optional[float]] = {}
        self._books: Dict[str, Optional[OrderBook]] = {}

    def set_state(self, market_id: str, age: Optional[float], book: Optional[OrderBook]) -> None:
        self._ages[market_id] = age
        self._books[market_id] = book

    def get_book_age(self, market_id: str) -> Optional[float]:
        return self._ages.get(market_id)

    def get_book(self, market_id: str) -> Optional[OrderBook]:
        return self._books.get(market_id)


def _scanner(tmp_path: Path, kalshi_ws=None) -> ResolutionScanner:
    return ResolutionScanner(
        kalshi_client=_FakeKalshiClient(),
        poly_client=None,
        exclusions=ExclusionList(path=tmp_path / "exclusions.json"),
        kalshi_ws=kalshi_ws,
    )


_LOG_RE = re.compile(
    r"ws=(\d+) rest=(\d+) total=(\d+) "
    r"\(rest: no_entry=(\d+) stale=(\d+) empty=(\d+) disabled=(\d+)\)"
)


def _parse_log(caplog) -> Tuple[int, int, int, int, int, int, int]:
    """Return (ws, rest, total, no_entry, stale, empty, disabled) from the latest emit."""
    matches = _LOG_RE.findall(caplog.text)
    assert matches, f"log line not emitted; caplog text:\n{caplog.text}"
    return tuple(int(x) for x in matches[-1])


# ── Single-reason paths ──────────────────────────────────────────────────────

def test_no_ws_entry_reason(tmp_path, caplog):
    ws = _FakeKalshiWS()  # nothing set → get_book_age returns None
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-A")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (0, 1, 1)
    assert (no_entry, stale, empty, disabled) == (1, 0, 0, 0)


def test_stale_age_reason(tmp_path, caplog):
    ws = _FakeKalshiWS()
    ws.set_state("KX-B", age=45.0, book=_book("KX-B", [(0.49, 100.0)], [(0.51, 100.0)]))
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-B")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (0, 1, 1)
    assert (no_entry, stale, empty, disabled) == (0, 1, 0, 0)


def test_stale_age_boundary_at_30s_counts_as_stale(tmp_path, caplog):
    """ws_age >= 30.0 trips the gate (the boundary value is stale, not fresh)."""
    ws = _FakeKalshiWS()
    ws.set_state("KX-B30", age=30.0, book=_book("KX-B30", [(0.49, 100.0)], [(0.51, 100.0)]))
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-B30")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n) == (0, 1)
    assert (no_entry, stale, empty, disabled) == (0, 1, 0, 0)


def test_empty_book_reason(tmp_path, caplog):
    ws = _FakeKalshiWS()
    ws.set_state("KX-C", age=5.0, book=_book("KX-C", [], []))  # fresh but empty
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-C")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (0, 1, 1)
    assert (no_entry, stale, empty, disabled) == (0, 0, 1, 0)


def test_empty_book_reason_handles_race_get_book_returns_none(tmp_path, caplog):
    """Fresh ws_age but get_book() returns None (eviction race) classified as empty_book."""
    ws = _FakeKalshiWS()
    ws.set_state("KX-CR", age=5.0, book=None)
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-CR")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n) == (0, 1)
    assert (no_entry, stale, empty, disabled) == (0, 0, 1, 0)


def test_ws_disabled_reason(tmp_path, caplog):
    scanner = _scanner(tmp_path, kalshi_ws=None)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-D")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (0, 1, 1)
    assert (no_entry, stale, empty, disabled) == (0, 0, 0, 1)


# ── WS hit path: no fallback, no reason increment ────────────────────────────

def test_ws_hit_no_fallback(tmp_path, caplog):
    ws = _FakeKalshiWS()
    ws.set_state("KX-E", age=5.0, book=_book("KX-E", [(0.49, 100.0)], [(0.51, 100.0)]))
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-E")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (1, 0, 1)
    assert (no_entry, stale, empty, disabled) == (0, 0, 0, 0)


# ── Multi-market: counters track independently and sum to rest ───────────────

def test_multi_market_each_reason_independent(tmp_path, caplog):
    ws = _FakeKalshiWS()
    # KX-1: no entry         → no_ws_entry
    # KX-2: age 45.0         → stale_age
    # KX-3: fresh empty book → empty_book
    # KX-4: fresh non-empty  → ws hit (no fallback)
    ws.set_state("KX-2", age=45.0, book=_book("KX-2", [(0.49, 100.0)], [(0.51, 100.0)]))
    ws.set_state("KX-3", age=5.0, book=_book("KX-3", [], []))
    ws.set_state("KX-4", age=5.0, book=_book("KX-4", [(0.49, 100.0)], [(0.51, 100.0)]))
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    markets = [_mk_market(f"KX-{i}") for i in (1, 2, 3, 4)]
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets(markets)
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert ws_n == 1
    assert rest_n == 3
    assert total_n == 4
    assert (no_entry, stale, empty, disabled) == (1, 1, 1, 0)
    # Classification is exhaustive within Kalshi-platform fallbacks.
    assert no_entry + stale + empty + disabled == rest_n


# ── Polymarket scope: not classified, doesn't increment any reason ───────────

def test_polymarket_market_not_classified(tmp_path, caplog):
    """Polymarket markets aren't WS-eligible; existing _rest_fallbacks scope is Kalshi-only.

    Counters must mirror that scope: polymarket markets don't increment any
    reason bucket and don't appear in the rest count.
    """
    ws = _FakeKalshiWS()  # ws set, but the polymarket market is the only input
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        # poly_client is None on the scanner (see _scanner), so _refresh_one
        # returns the market unchanged without entering the WS/REST path.
        scanner.refresh_markets([_mk_market("POLY-X", platform="polymarket")])
    ws_n, rest_n, total_n, no_entry, stale, empty, disabled = _parse_log(caplog)
    assert (ws_n, rest_n, total_n) == (0, 0, 0)
    assert (no_entry, stale, empty, disabled) == (0, 0, 0, 0)


# ── Log-format regression: structure is stable for downstream grep tooling ───

def test_log_line_format_matches_grep_pattern(tmp_path, caplog):
    ws = _FakeKalshiWS()
    scanner = _scanner(tmp_path, kalshi_ws=ws)
    with caplog.at_level(logging.INFO, logger="resolution.scanner"):
        scanner.refresh_markets([_mk_market("KX-FMT")])
    assert _LOG_RE.search(caplog.text), (
        f"extended log format not found in caplog:\n{caplog.text}"
    )
    assert "ResolutionScanner: refresh book sources:" in caplog.text
