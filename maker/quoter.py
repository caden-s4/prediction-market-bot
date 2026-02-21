"""
maker.quoter – Bot 1: Maker Rebate Harvester.

Posts two-sided limit orders on Polymarket 5-min and 15-min crypto markets.
Earns daily USDC rebates by providing liquidity. Never takes.

Hard constraints:
  - Quote update latency < 200ms from fair value change (hard deadline)
  - Never let a quote sit > 500ms without refreshing against current fair value
  - Always fetch feeRateBps dynamically before quoting – never hardcode
  - Never post a taker order (only limit/maker orders)
  - If net position on one side > cap, stop quoting that side

Architecture:
  - Runs as a fully async event loop (asyncio)
  - FairValueFeed drives quote updates via callbacks
  - WebSocket order book adapter watches for fills
  - REST calls go to Polymarket CLOB for order placement/cancellation
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional

from data.markets.base import Market
from maker.fair_value import FairValueFeed, symbol_for_market
from maker.position_tracker import MarketPosition, PositionTracker
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache

logger = logging.getLogger(__name__)

# How far from fair value to set each side (expressed as probability delta)
DEFAULT_HALF_SPREAD = 0.010        # 1.0 cent on a $1 binary = 1%
MIN_HALF_SPREAD_AFTER_FEES = 0.005 # net spread must exceed this after fees

# Quote refresh deadlines
MAX_QUOTE_AGE_SECONDS = 0.500      # 500ms max before forced refresh
FV_MOVE_THRESHOLD = 0.005          # refresh if fair value moves more than 0.5%

# Polymarket 5/15-min crypto market detection
CRYPTO_MARKET_KEYWORDS = (
    "5-minute", "15-minute", "5 minute", "15 minute",
    "5min", "15min", "next 5", "next 15",
)


class MakerBot:
    """
    Async maker quoting bot for Polymarket short-duration crypto markets.

    Parameters
    ----------
    markets      : list of 5-min / 15-min crypto markets to quote
    poly_client  : Polymarket REST client (for order placement/cancellation)
    fee_cache    : shared fee rate cache
    bankroll     : shared bankroll manager
    exclusions   : shared exclusion list
    half_spread  : half-spread to post around fair value (default 1%)
    cap_usd      : per-market per-side exposure cap (default $75)
    dry_run      : if True, log orders but don't place them
    """

    def __init__(
        self,
        markets: List[Market],
        poly_client,
        fee_cache: FeeCache,
        bankroll: Bankroll,
        exclusions: ExclusionList,
        half_spread: float = DEFAULT_HALF_SPREAD,
        cap_usd: float = 75.0,
        dry_run: bool = True,
    ) -> None:
        self._poly = poly_client
        self._fee_cache = fee_cache
        self._bankroll = bankroll
        self._exclusions = exclusions
        self._half_spread = half_spread
        self._cap_usd = cap_usd
        self._dry_run = dry_run

        # Filter to crypto short-duration markets only
        self._markets: List[Market] = [
            m for m in markets if self._is_maker_market(m)
        ]
        logger.info("MakerBot: %d maker markets identified", len(self._markets))

        # Detect required symbols and build price feed
        symbols = set()
        for m in self._markets:
            sym = symbol_for_market(m.question)
            if sym:
                symbols.add(sym)
        self._feed = FairValueFeed(list(symbols))
        self._tracker = PositionTracker(default_cap_usd=cap_usd)

        # Track last-known fair value per market to detect moves
        self._last_fv: Dict[str, float] = {}

        # Register fair value callbacks → triggers re-quote on every tick
        for sym in symbols:
            self._feed.on_update(sym, self._on_price_update)

        self._running = False

    # ── Entry point ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start the maker bot. Runs until stop() is called."""
        self._running = True
        logger.info("MakerBot: starting (dry_run=%s)", self._dry_run)

        # Launch price feed and stale-quote watchdog concurrently
        await asyncio.gather(
            self._feed.run(),
            self._stale_quote_watchdog(),
        )

    def stop(self) -> None:
        self._running = False
        self._feed.stop()
        logger.info("MakerBot: stopped")

    # ── Price update callback (fires < 1ms after Binance tick) ───────────────

    def _on_price_update(self, symbol: str, bid: float, ask: float) -> None:
        """Called by FairValueFeed on every Binance price tick."""
        if not self._running:
            return
        fv = (bid + ask) / 2.0

        # Find all markets anchored to this symbol
        for market in self._markets:
            if symbol_for_market(market.question) != symbol:
                continue
            if self._exclusions.is_excluded("polymarket", market.market_id):
                continue

            last = self._last_fv.get(market.market_id)
            if last is not None and abs(fv - last) / last < FV_MOVE_THRESHOLD:
                continue  # price hasn't moved enough to re-quote

            self._last_fv[market.market_id] = fv
            # Schedule re-quote without blocking the callback
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda mid=market.market_id, f=fv: asyncio.ensure_future(
                    self._requote(mid, f)
                )
            )

    # ── Stale quote watchdog ──────────────────────────────────────────────────

    async def _stale_quote_watchdog(self) -> None:
        """
        Every 100ms, check for quotes older than 500ms and refresh them.
        This is the safety net in case price didn't move but time did.
        """
        while self._running:
            await asyncio.sleep(0.1)
            stale = self._tracker.stale_markets(max_age_seconds=MAX_QUOTE_AGE_SECONDS)
            for pos in stale:
                fv = self._get_fv_for_market(pos.market_id)
                if fv is not None:
                    await self._requote(pos.market_id, fv)

    # ── Core quoting logic ────────────────────────────────────────────────────

    async def _requote(self, market_id: str, fair_value: float) -> None:
        """
        Cancel existing quotes and post new ones around the given fair value.
        Target latency: < 200ms end-to-end.
        """
        start = time.monotonic()

        pos = self._tracker.get_or_create(market_id, cap_usd=self._cap_usd)
        market = next((m for m in self._markets if m.market_id == market_id), None)
        if not market:
            return

        if self._bankroll.is_halted():
            logger.warning("MakerBot: bankroll halted – pausing quotes for %s", market_id)
            return

        # Fetch fee rate (cached, refreshes every 15 min)
        fee = self._fee_cache.get_taker_fee("polymarket", market_id)
        effective_half_spread = max(self._half_spread, MIN_HALF_SPREAD_AFTER_FEES + fee)

        bid_price = round(fair_value - effective_half_spread, 4)
        ask_price = round(fair_value + effective_half_spread, 4)

        # Clamp to [0.01, 0.99]
        bid_price = max(0.01, min(0.99, bid_price))
        ask_price = max(0.01, min(0.99, ask_price))

        # Cancel existing quotes (fire-and-forget, don't wait)
        cancel_tasks = []
        if pos.bid_order_id:
            cancel_tasks.append(self._cancel_order(pos.bid_order_id))
            pos.bid_order_id = None
        if pos.ask_order_id:
            cancel_tasks.append(self._cancel_order(pos.ask_order_id))
            pos.ask_order_id = None
        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)

        # Determine order size
        order_size_usd = min(
            self._bankroll.available_usd("maker") * 0.05,  # 5% of available
            self._cap_usd * 0.5,
        )
        if order_size_usd < 1.0:
            return  # too small to bother

        # Post bid (buy YES) unless YES-side is capped
        if not pos.yes_capped:
            bid_id = await self._place_limit_order(
                market_id=market_id,
                side="buy",
                price=bid_price,
                size_usd=order_size_usd,
            )
            if bid_id:
                pos.bid_order_id = bid_id
                pos.last_bid_price = bid_price
                pos.last_quoted_at = time.monotonic()

        # Post ask (sell YES / buy NO) unless NO-side is capped
        if not pos.no_capped:
            ask_id = await self._place_limit_order(
                market_id=market_id,
                side="sell",
                price=ask_price,
                size_usd=order_size_usd,
            )
            if ask_id:
                pos.ask_order_id = ask_id
                pos.last_ask_price = ask_price
                pos.last_quoted_at = time.monotonic()

        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > 200:
            logger.warning(
                "MakerBot: requote latency %.0fms exceeds 200ms deadline for %s",
                elapsed_ms, market_id,
            )
        else:
            logger.debug(
                "MakerBot: %s quoted bid=%.4f ask=%.4f fv=%.4f (%.0fms)",
                market_id, bid_price, ask_price, fair_value, elapsed_ms,
            )

    async def _cancel_order(self, order_id: str) -> None:
        try:
            if not self._dry_run:
                await asyncio.get_event_loop().run_in_executor(
                    None, self._poly.cancel_order, order_id
                )
            logger.debug("MakerBot: cancelled order %s", order_id)
        except Exception as exc:
            logger.debug("MakerBot: cancel failed for %s: %s", order_id, exc)

    async def _place_limit_order(
        self,
        market_id: str,
        side: str,
        price: float,
        size_usd: float,
    ) -> Optional[str]:
        """Place a maker limit order. Returns order_id or None on failure."""
        if self._dry_run:
            fake_id = f"dry_{market_id}_{side}_{int(time.monotonic()*1000)}"
            logger.info(
                "MakerBot [DRY]: %s %s @ %.4f size=$%.2f → %s",
                side, market_id, price, size_usd, fake_id,
            )
            return fake_id

        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._poly.create_order(
                    market_id=market_id,
                    side=side,
                    price=price,
                    size=size_usd,
                    order_type="limit",
                )
            )
            order_id = result.get("id") or result.get("order_id")
            logger.info(
                "MakerBot: %s %s @ %.4f size=$%.2f → order=%s",
                side, market_id, price, size_usd, order_id,
            )
            return order_id
        except Exception as exc:
            logger.warning(
                "MakerBot: failed to place %s order on %s: %s",
                side, market_id, exc,
            )
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_fv_for_market(self, market_id: str) -> Optional[float]:
        market = next((m for m in self._markets if m.market_id == market_id), None)
        if not market:
            return None
        sym = symbol_for_market(market.question)
        return self._feed.get_midpoint(sym) if sym else None

    @staticmethod
    def _is_maker_market(market: Market) -> bool:
        """Return True if this is a short-duration crypto market suitable for making."""
        text = market.question.lower()
        title = getattr(market, "title", "").lower()
        combined = text + " " + title
        is_short = any(kw in combined for kw in CRYPTO_MARKET_KEYWORDS)
        is_crypto = (
            market.category.lower() in ("crypto", "cryptocurrency")
            or any(kw in combined for kw in ("btc", "eth", "bitcoin", "ethereum", "sol"))
        )
        return is_short and is_crypto
