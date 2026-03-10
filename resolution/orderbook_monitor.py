"""
resolution.orderbook_monitor – lightweight order book dislocation check.

Runs only when a sports signal is already actionable (after shock detection
and confidence gating). Pulls the Kalshi order book for the target market and
checks for three structural flags:

  thin_book    : YES side has fewer than 50 contracts within 5¢ of best ask
  large_order  : a single order represents > 60% of visible depth on one side
  wide_spread  : bid-ask spread wider than 8 cents (0.08)

Position sizing effects:
  thin_book OR large_order → size down 50% (you don't want to move the market)
  wide_spread              → logged only (may reflect genuine illiquidity)

These flags NEVER block a trade — they only reduce size.

All checks and flags are logged for every call regardless of outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from data.markets.base import OrderBook, PriceLevel

logger = logging.getLogger(__name__)

# Thresholds
_THIN_BOOK_CONTRACTS = 50   # fewer than this within 5¢ of best ask = thin
_THIN_BOOK_SPREAD = 0.05    # 5 cents — "near the best ask"
_LARGE_ORDER_FRACTION = 0.60  # single order > 60% of total visible depth
_WIDE_SPREAD_THRESHOLD = 0.08  # 8 cents


@dataclass
class OrderBookFlags:
    thin_book: bool = False
    large_order: bool = False
    wide_spread: bool = False
    size_multiplier: float = 1.0   # 1.0 = normal, 0.5 = size down
    best_ask: Optional[float] = None
    best_bid: Optional[float] = None
    spread: Optional[float] = None
    visible_depth_yes: float = 0.0
    near_ask_depth: float = 0.0
    notes: list = field(default_factory=list)


def check_order_book(order_book: OrderBook, market_id: str) -> OrderBookFlags:
    """
    Run all order book dislocation checks for a single Kalshi sports market.

    Parameters
    ----------
    order_book : OrderBook snapshot from the market client
    market_id  : used for log messages only

    Returns
    -------
    OrderBookFlags with size_multiplier already computed.
    """
    flags = OrderBookFlags()

    best_ask = order_book.best_yes_ask
    best_bid = order_book.best_yes_bid
    flags.best_ask = best_ask
    flags.best_bid = best_bid

    # ── Spread check ──────────────────────────────────────────────────────────
    if best_ask is not None and best_bid is not None:
        spread = best_ask - best_bid
        flags.spread = spread
        if spread > _WIDE_SPREAD_THRESHOLD:
            flags.wide_spread = True
            flags.notes.append(
                f"wide_spread: ask={best_ask:.3f} bid={best_bid:.3f} spread={spread:.3f}"
            )

    # ── Thin book check (YES ask side within 5¢ of best ask) ─────────────────
    if best_ask is not None and order_book.yes_asks:
        cutoff = best_ask + _THIN_BOOK_SPREAD
        near_ask: list[PriceLevel] = [
            lvl for lvl in order_book.yes_asks if lvl.price <= cutoff
        ]
        near_ask_contracts = sum(lvl.size for lvl in near_ask)
        flags.near_ask_depth = near_ask_contracts

        if near_ask_contracts < _THIN_BOOK_CONTRACTS:
            flags.thin_book = True
            flags.notes.append(
                f"thin_book: {near_ask_contracts:.0f} contracts within "
                f"{_THIN_BOOK_SPREAD*100:.0f}¢ of ask={best_ask:.3f}"
            )

    # ── Large order check (YES side) ──────────────────────────────────────────
    total_yes_depth = sum(lvl.size for lvl in order_book.yes_asks)
    flags.visible_depth_yes = total_yes_depth
    if total_yes_depth > 0 and order_book.yes_asks:
        largest_ask = max(order_book.yes_asks, key=lambda lvl: lvl.size)
        ask_fraction = largest_ask.size / total_yes_depth
        if ask_fraction > _LARGE_ORDER_FRACTION:
            flags.large_order = True
            flags.notes.append(
                f"large_order: single YES ask {largest_ask.size:.0f} contracts "
                f"= {ask_fraction:.0%} of {total_yes_depth:.0f} total depth "
                f"at price={largest_ask.price:.3f}"
            )

    # Also check bid side for large order
    total_bid_depth = sum(lvl.size for lvl in order_book.yes_bids)
    if total_bid_depth > 0 and order_book.yes_bids:
        largest_bid = max(order_book.yes_bids, key=lambda lvl: lvl.size)
        bid_fraction = largest_bid.size / total_bid_depth
        if bid_fraction > _LARGE_ORDER_FRACTION:
            flags.large_order = True
            flags.notes.append(
                f"large_order: single YES bid {largest_bid.size:.0f} contracts "
                f"= {bid_fraction:.0%} of {total_bid_depth:.0f} total depth "
                f"at price={largest_bid.price:.3f}"
            )

    # ── Size multiplier ───────────────────────────────────────────────────────
    if flags.thin_book or flags.large_order:
        flags.size_multiplier = 0.5

    # ── Log everything ────────────────────────────────────────────────────────
    logger.info(
        "OrderBookMonitor: %s | ask=%.3f bid=%.3f spread=%.3f | "
        "near_ask_depth=%.0f total_yes_depth=%.0f | "
        "thin=%s large=%s wide=%s | size_multiplier=%.1f%s",
        market_id,
        best_ask or 0.0,
        best_bid or 0.0,
        flags.spread or 0.0,
        flags.near_ask_depth,
        flags.visible_depth_yes,
        flags.thin_book,
        flags.large_order,
        flags.wide_spread,
        flags.size_multiplier,
        (" | flags: " + "; ".join(flags.notes)) if flags.notes else "",
    )

    return flags


def apply_book_sizing(base_size_usd: float, flags: OrderBookFlags) -> float:
    """
    Apply the order book size multiplier to a base position size.

    Parameters
    ----------
    base_size_usd : position size before book-based adjustment
    flags         : result from check_order_book()

    Returns
    -------
    float — adjusted size in USD
    """
    adjusted = base_size_usd * flags.size_multiplier
    if flags.size_multiplier < 1.0:
        logger.info(
            "OrderBookMonitor: sizing down %.2f → %.2f (multiplier=%.1f)",
            base_size_usd, adjusted, flags.size_multiplier,
        )
    return adjusted
