"""
signals.cross_exchange – cross-exchange mispricing signal.

Logic
-----
For each pair of markets representing the same underlying event on
Polymarket and Kalshi, compare the YES ask price on both platforms.
If buying on the cheaper side and simultaneously selling YES (via NO)
on the more expensive side produces a net positive return after fees,
fire a cross-exchange arbitrage signal.

The check is:
    gross_spread = sell_price - buy_price
    net_spread   = gross_spread - total_fees
    if net_spread >= min_spread_threshold → fire signal

Fees used
---------
Polymarket maker: ~0% (zero fee for maker orders)
Polymarket taker: ~2% of notional
Kalshi:           ~7% of profit (we use a conservative 7% of notional)

True arbitrage requires simultaneous fills on both legs.  This signal
flags the opportunity; the execution engine handles simultaneous orders.
"""

from __future__ import annotations

import logging
from typing import Optional

from adapters.base import LiveOrderBook
from .base import BaseSignal, Signal, SignalDirection, SignalType

logger = logging.getLogger(__name__)

# Conservative one-way fee estimates per platform
_FEES: dict = {
    "polymarket": 0.02,
    "kalshi": 0.07,
}


class CrossExchangeSignal(BaseSignal):
    """
    Fires when the same YES outcome is priced at least `min_spread`
    cheaper on one exchange than on the other, net of fees.

    Usage
    -----
    signal = CrossExchangeSignal(min_spread=0.015)
    result = signal.evaluate(
        poly_book=<LiveOrderBook from Polymarket>,
        kalshi_book=<LiveOrderBook from Kalshi>,
        poly_market_id="0xabc...",
        kalshi_market_id="KXWEATHER-SEA-24DEC-T50",
    )
    """

    @property
    def signal_type(self) -> SignalType:
        return SignalType.CROSS_EXCHANGE_ARB

    def __init__(self, min_spread: float = 0.015) -> None:
        """
        Parameters
        ----------
        min_spread : minimum net spread (after fees) to fire the signal.
                     0.015 = 1.5 percentage points.
        """
        self._min_spread = min_spread

    def evaluate(
        self,
        poly_book: LiveOrderBook,
        kalshi_book: LiveOrderBook,
        poly_market_id: str,
        kalshi_market_id: str,
    ) -> Optional[Signal]:
        """
        Compare the YES price on both platforms and fire if an arb exists.

        Parameters
        ----------
        poly_book        : live Polymarket order book
        kalshi_book      : live Kalshi order book
        poly_market_id   : Polymarket market/condition ID
        kalshi_market_id : Kalshi market ticker
        """
        if not poly_book.is_synced or not kalshi_book.is_synced:
            return None

        poly_ask = poly_book.get_best_ask()    # cost to buy YES on Polymarket
        poly_bid = poly_book.get_best_bid()    # revenue from selling YES on Polymarket
        kalshi_ask = kalshi_book.get_best_ask()
        kalshi_bid = kalshi_book.get_best_bid()

        if None in (poly_ask, poly_bid, kalshi_ask, kalshi_bid):
            return None

        poly_fee = _FEES["polymarket"]
        kalshi_fee = _FEES["kalshi"]

        # Leg 1: buy YES on Polymarket, sell YES (buy NO) on Kalshi
        # Net: kalshi_bid - poly_ask - fees
        spread_poly_buy = kalshi_bid - poly_ask - poly_fee - kalshi_fee

        # Leg 2: buy YES on Kalshi, sell YES (buy NO) on Polymarket
        # Net: poly_bid - kalshi_ask - fees
        spread_kalshi_buy = poly_bid - kalshi_ask - kalshi_fee - poly_fee

        best_spread = max(spread_poly_buy, spread_kalshi_buy)

        if best_spread < self._min_spread:
            return None

        if spread_poly_buy >= spread_kalshi_buy:
            # Buy YES cheaper on Polymarket, sell YES on Kalshi
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_market_id, sell_market_id = poly_market_id, kalshi_market_id
            buy_price, sell_price = poly_ask, kalshi_bid
            trade_platform = "polymarket"   # execution starts here
            trade_market_id = poly_market_id
            direction = SignalDirection.BUY_YES
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_market_id, sell_market_id = kalshi_market_id, poly_market_id
            buy_price, sell_price = kalshi_ask, poly_bid
            trade_platform = "kalshi"
            trade_market_id = kalshi_market_id
            direction = SignalDirection.BUY_YES

        # Strength = how far above the minimum threshold the spread is
        strength = min(best_spread / (self._min_spread * 5), 1.0)

        logger.info(
            "CrossExchangeSignal: buy=%s@%.4f sell=%s@%.4f net_spread=%.4f (%.2f%%)",
            buy_platform, buy_price, sell_platform, sell_price,
            best_spread, best_spread * 100,
        )

        return Signal(
            signal_type=self.signal_type,
            direction=direction,
            platform=trade_platform,
            market_id=trade_market_id,
            edge_estimate=best_spread,
            strength=strength,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            buy_market_id=buy_market_id,
            sell_market_id=sell_market_id,
            buy_price=buy_price,
            sell_price=sell_price,
            metadata={
                "poly_ask": poly_ask,
                "poly_bid": poly_bid,
                "kalshi_ask": kalshi_ask,
                "kalshi_bid": kalshi_bid,
                "gross_spread": sell_price - buy_price,
                "net_spread": best_spread,
                "min_spread_threshold": self._min_spread,
            },
        )
