from __future__ import annotations

from datetime import datetime, timedelta, timezone

from resolution.gap_detector import GapDetector, SLIPPAGE_BUFFER


class StubFeeCache:
    def __init__(self, fees=None):
        self.fees = fees or {}

    def get_taker_fee(self, platform, market_id, price=0.5, force_refresh=False):
        return self.fees.get((platform, market_id), 0.0)


def test_detect_information_signal_actionable(make_market):
    detector = GapDetector(StubFeeCache())
    market = make_market(
        market_id="INFO-1",
        category="sports",
        yes_price=0.40,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    signal = detector.detect_information_signal(market, ground_truth_prob=0.60)

    assert signal is not None
    assert signal.signal_type == "information"
    assert signal.action == "buy_yes"
    # fee=0 (stub), round_trip=0, effective_gap = 0.20 - 0.0 = 0.20
    assert round(signal.effective_gap, 2) == 0.20


def test_detect_information_signal_blocked_when_too_close_to_resolution(make_market):
    detector = GapDetector(StubFeeCache())
    market = make_market(
        market_id="INFO-2",
        resolution_date=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    assert detector.detect_information_signal(market, ground_truth_prob=0.9) is None


def test_detect_information_signal_respects_force_test_min_gap(make_market):
    detector = GapDetector(StubFeeCache(), force_test=True)
    market = make_market(
        market_id="INFO-3",
        category="politics",
        yes_price=0.50,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=12),
    )

    signal = detector.detect_information_signal(market, ground_truth_prob=0.52)

    assert signal is not None
    # fee=0 (stub), effective_gap = 0.02 > 0.01 force-test buffer
    assert round(signal.effective_gap, 2) == 0.02


def test_detect_cross_platform_uses_lagging_platform_fee(make_market):
    fees = {
        ("polymarket", "PM-1"): 0.01,
        ("kalshi", "K-1"): 0.03,
    }
    detector = GapDetector(StubFeeCache(fees))
    poly = make_market(
        market_id="PM-1",
        platform="polymarket",
        yes_price=0.40,
        no_price=0.60,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    kalshi = make_market(
        market_id="K-1",
        platform="kalshi",
        yes_price=0.55,
        no_price=0.45,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=1),
    )

    signals = detector.detect_cross_platform([(poly, kalshi)])

    assert len(signals) == 1
    signal = signals[0]
    assert signal.market_to_buy.market_id == "PM-1"
    # poly lags: raw_gap=0.15, one_way_fee=0.01 (poly stub), round_trip=0.02
    # effective_gap = 0.15 - 0.02 = 0.13
    assert round(signal.effective_gap, 2) == 0.13
    assert signal.action == "buy_yes"


def test_detect_cross_platform_filters_below_slippage_buffer(make_market):
    """A gap that is positive but below SLIPPAGE_BUFFER should be blocked."""
    # Use a tiny stub fee so the only blocker is the 3% buffer.
    # raw_gap = 0.02 (below 3% buffer) → should be filtered.
    detector = GapDetector(StubFeeCache())
    poly = make_market(
        market_id="PM-2",
        platform="polymarket",
        yes_price=0.49,
        no_price=0.51,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    kalshi = make_market(
        market_id="K-2",
        platform="kalshi",
        yes_price=0.51,
        no_price=0.49,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=4),
    )

    # raw_gap=0.02, round_trip_fee=0.0 (stub), effective_gap=0.02 < SLIPPAGE_BUFFER=0.03
    assert detector.detect_cross_platform([(poly, kalshi)]) == []


def test_detect_cross_platform_passes_sufficient_gap(make_market):
    """A gap above fees + slippage buffer should produce a signal."""
    detector = GapDetector(StubFeeCache())
    poly = make_market(
        market_id="PM-3",
        platform="polymarket",
        yes_price=0.49,
        no_price=0.51,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    kalshi = make_market(
        market_id="K-3",
        platform="kalshi",
        yes_price=0.55,
        no_price=0.45,
        resolution_date=datetime.now(timezone.utc) + timedelta(hours=4),
    )

    # raw_gap=0.06, round_trip_fee=0.0 (stub), effective_gap=0.06 > SLIPPAGE_BUFFER=0.03
    signals = detector.detect_cross_platform([(poly, kalshi)])
    assert len(signals) == 1
