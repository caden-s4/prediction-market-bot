from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.markets.base import OrderBook, PriceLevel, Side


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

    assert round(book.slippage_adjusted_price(Side.YES, 100), 3) == 0.525


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
