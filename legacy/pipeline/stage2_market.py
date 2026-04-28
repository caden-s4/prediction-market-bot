"""
Stage 2 – Market Analysis and Implied Probability Extraction.

Inputs  : Market + OrderBook + desired position size in USD
Outputs : MarketAnalysis dataclass

Logic:
- Extract best YES ask (cost to go long YES) and YES bid (cost to go long NO)
- Calculate slippage-adjusted entry price for a given notional
- Detect cross-platform arbitrage opportunities (if both books passed)
- Summarise liquidity depth
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from data.markets.base import Market, OrderBook, Side

logger = logging.getLogger(__name__)


@dataclass
class MarketAnalysis:
    """
    Fully analysed view of a single market's current pricing and depth.

    best_yes_ask     : cheapest YES you can buy          [0–1]
    best_no_ask      : cheapest NO you can buy           [0–1] = 1 - best_yes_bid
    mid_price        : mid-market YES probability        [0–1]
    implied_prob     : mid price used as implied prob    [0–1]
    slippage_yes     : slippage-adjusted YES price for target_size
    slippage_no      : slippage-adjusted NO price for target_size
    spread           : (ask - bid) in probability points
    depth_yes_usd    : total USD available in YES ask side
    depth_no_usd     : total USD available in NO ask side (= YES bid depth)
    preferred_side   : which side has better slippage-adjusted EV vs our_prob
    arb_spread       : cross-platform arbitrage spread (0 if only one book given)
    """

    market_id: str
    platform: str

    best_yes_ask: Optional[float] = None
    best_no_ask: Optional[float] = None
    mid_price: Optional[float] = None
    implied_prob: Optional[float] = None

    slippage_yes: Optional[float] = None
    slippage_no: Optional[float] = None

    spread: float = 0.0
    depth_yes_usd: float = 0.0
    depth_no_usd: float = 0.0
    preferred_side: Optional[Side] = None
    arb_spread: float = 0.0

    notes: list = field(default_factory=list)


class Stage2Market:
    """
    Analyses an order book snapshot and produces a MarketAnalysis.
    """

    def run(
        self,
        market: Market,
        order_book: OrderBook,
        our_prob: float,
        target_size_usd: float = 100.0,
        alt_order_book: Optional[OrderBook] = None,
    ) -> MarketAnalysis:
        """
        Parameters
        ----------
        market          : the Market object
        order_book      : primary platform order book
        our_prob        : our Stage-1 probability estimate [0–1]
        target_size_usd : intended position size for slippage calculation
        alt_order_book  : order book from alternate platform (for arb check)
        """
        analysis = MarketAnalysis(
            market_id=market.market_id,
            platform=market.platform,
        )

        # ── Basic price extraction ─────────────────────────────────────────
        analysis.best_yes_ask = order_book.best_yes_ask
        analysis.best_no_ask = order_book.implied_no_ask    # 1 - best_yes_bid
        analysis.mid_price = order_book.mid_price
        analysis.implied_prob = analysis.mid_price or market.yes_price

        if analysis.best_yes_ask is not None and order_book.best_yes_bid is not None:
            analysis.spread = analysis.best_yes_ask - order_book.best_yes_bid

        # ── Depth calculation ──────────────────────────────────────────────
        analysis.depth_yes_usd = sum(
            lvl.size for lvl in order_book.yes_asks
        )
        analysis.depth_no_usd = sum(
            lvl.size for lvl in order_book.yes_bids
        )

        # ── Slippage-adjusted prices ───────────────────────────────────────
        if order_book.yes_asks:
            analysis.slippage_yes = order_book.slippage_adjusted_price(
                Side.YES, target_size_usd
            )
        if order_book.yes_bids:
            analysis.slippage_no = order_book.slippage_adjusted_price(
                Side.NO, target_size_usd
            )

        # ── Preferred side selection ───────────────────────────────────────
        # Compare EV of buying YES vs buying NO at slippage-adjusted prices
        ev_yes = None
        ev_no = None
        if analysis.slippage_yes is not None:
            ev_yes = our_prob - analysis.slippage_yes      # edge if we buy YES
        if analysis.slippage_no is not None:
            ev_no = (1.0 - our_prob) - (1.0 - analysis.slippage_no)   # edge if buy NO

        if ev_yes is not None and ev_no is not None:
            analysis.preferred_side = Side.YES if ev_yes >= ev_no else Side.NO
        elif ev_yes is not None:
            analysis.preferred_side = Side.YES
        elif ev_no is not None:
            analysis.preferred_side = Side.NO

        # ── Cross-platform arbitrage check ────────────────────────────────
        if alt_order_book is not None:
            alt_yes_ask = alt_order_book.best_yes_ask
            if analysis.best_yes_ask is not None and alt_yes_ask is not None:
                # Buy YES on cheaper platform, sell YES on dearer platform
                # Guaranteed profit if sum of YES costs < $1
                arb = alt_yes_ask - analysis.best_yes_ask
                if arb > 0:
                    analysis.arb_spread = arb
                    analysis.notes.append(
                        f"ARB: buy YES on {market.platform} at "
                        f"{analysis.best_yes_ask:.2f}, sell on "
                        f"{alt_order_book.platform} at {alt_yes_ask:.2f} "
                        f"→ {arb:.3f} gross spread"
                    )

        logger.debug(
            "Stage2: market=%s implied_prob=%.2f our_prob=%.2f preferred=%s spread=%.3f",
            market.market_id,
            analysis.implied_prob or 0,
            our_prob,
            analysis.preferred_side,
            analysis.spread,
        )
        return analysis
