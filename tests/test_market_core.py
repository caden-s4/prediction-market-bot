from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.markets.base import FillResult, OrderBook, PriceLevel, Side


def test_orderbook_properties_and_mid_price(make_market):
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[PriceLevel(price=0.42, size=100)],
        yes_asks=[PriceLevel(price=0.48, size=120)],
    )

    assert book.best_yes_bid == 0.42
    assert book.best_yes_ask == 0.48
    assert round(book.implied_no_ask, 2) == 0.58
    assert round(book.mid_price, 2) == 0.45


def test_orderbook_slippage_adjusted_price_consumes_multiple_levels():
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[PriceLevel(price=0.40, size=200)],
        yes_asks=[
            PriceLevel(price=0.50, size=50),
            PriceLevel(price=0.55, size=100),
        ],
    )

    fill = book.slippage_adjusted_price(Side.YES, 100)
    assert isinstance(fill, FillResult)
    assert fill.filled_size_usd == 100
    assert round(fill.vwap, 3) == 0.525
    assert fill.levels_consumed == 2
    assert fill.clamped is False


def test_slippage_walk_fits_top_level_no_slippage():
    """Request fits entirely in the best level → vwap = best price, clamped=False."""
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[PriceLevel(price=0.40, size=500)],
        yes_asks=[
            PriceLevel(price=0.50, size=500),
            PriceLevel(price=0.55, size=500),
        ],
    )

    fill = book.slippage_adjusted_price(Side.YES, 200)
    assert fill.filled_size_usd == 200
    assert fill.vwap == 0.50
    assert fill.levels_consumed == 1
    assert fill.clamped is False


def test_slippage_walk_three_levels():
    """Request walks 3 levels → weighted-average vwap, clamped=False."""
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[PriceLevel(price=0.40, size=200)],
        yes_asks=[
            PriceLevel(price=0.50, size=50),
            PriceLevel(price=0.55, size=50),
            PriceLevel(price=0.60, size=100),
        ],
    )

    fill = book.slippage_adjusted_price(Side.YES, 150)
    # 50 @ 0.50 + 50 @ 0.55 + 50 @ 0.60 = 25 + 27.5 + 30 = 82.5, /150 = 0.55
    assert fill.filled_size_usd == 150
    assert round(fill.vwap, 4) == 0.55
    assert fill.levels_consumed == 3
    assert fill.clamped is False


def test_slippage_walk_clamps_when_request_exceeds_depth():
    """Request > total depth → fill total depth, clamped=True; no phantom residual."""
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[PriceLevel(price=0.40, size=100)],
        yes_asks=[
            PriceLevel(price=0.50, size=50),
            PriceLevel(price=0.55, size=50),
        ],
    )

    fill = book.slippage_adjusted_price(Side.YES, 500)
    # Total depth = 100; vwap = (50*0.50 + 50*0.55)/100 = 52.5/100 = 0.525.
    # Pre-fix bug would have over-filled by 400 at 0.55 → vwap ≈ 0.545.
    assert fill.filled_size_usd == 100
    assert round(fill.vwap, 4) == 0.525
    assert fill.levels_consumed == 2
    assert fill.clamped is True


def test_slippage_walk_empty_book():
    """No depth on requested side → all zeros + clamped=True."""
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        yes_bids=[],
        yes_asks=[],
    )

    fill = book.slippage_adjusted_price(Side.YES, 100)
    assert fill.filled_size_usd == 0
    assert fill.vwap == 0.0
    assert fill.levels_consumed == 0
    assert fill.clamped is True

    fill_no = book.slippage_adjusted_price(Side.NO, 100)
    assert fill_no.filled_size_usd == 0
    assert fill_no.vwap == 0.0
    assert fill_no.levels_consumed == 0
    assert fill_no.clamped is True


def test_slippage_walk_no_side_iterates_best_bid_first():
    """Regression: NO walk must hit the HIGHEST bid first (best seller price),
    not the lowest. Pre-fix code reversed yes_bids and walked from worst bid up."""
    book = OrderBook(
        market_id="M1",
        platform="kalshi",
        # yes_bids stored descending (best bid first)
        yes_bids=[
            PriceLevel(price=0.60, size=50),   # best — should fill first
            PriceLevel(price=0.55, size=50),
            PriceLevel(price=0.50, size=200),  # worst
        ],
        yes_asks=[PriceLevel(price=0.70, size=500)],
    )

    fill = book.slippage_adjusted_price(Side.NO, 100)
    # Correct: 50 @ 0.60 + 50 @ 0.55 = 30 + 27.5 = 57.5, /100 = 0.575
    # Pre-fix (reversed): 100 @ 0.50 = 50, /100 = 0.50 — undersells YES.
    assert fill.filled_size_usd == 100
    assert round(fill.vwap, 4) == 0.575
    assert fill.levels_consumed == 2
    assert fill.clamped is False


def test_market_hours_to_resolution_handles_naive_datetime(make_market):
    market = make_market(
        resolution_date=datetime.utcnow() + timedelta(hours=3),
    )

    hours = market.hours_to_resolution
    assert 2.9 < hours < 3.1


def test_market_is_weather_market_by_prefix(make_market):
    market = make_market(
        market_id="KXHIGHCHI-26MAR31-B70",
        category="general",
        tags=[],
    )

    assert market.is_weather_market() is True
