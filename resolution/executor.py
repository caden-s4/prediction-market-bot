"""
resolution.executor – Bot 2: Resolution Drift Arbitrage main loop.

Runs every SCAN_INTERVAL_SECONDS (default 300s / 5 minutes).

Full cycle:
  1. Scan both platforms for non-crypto markets expiring within 24h
  2. Find cross-platform pairs; detect gaps > 4% after fees
  3. For single-platform flagged markets, fetch ground truth
  4. Score confidence on two dimensions; skip if either < 0.80
  5. Check exclusion list and fee rates before any order
  6. Execute taker orders on lagging platform (fractional Kelly sizing)
  7. On every cycle, run decay monitor on open positions

Sizing:
  - Fractional Kelly at 10-15% (per strategy spec; these markets are thin)
  - Hard cap: never more than 20% of total bankroll in a single position
  - Prefer smaller positions to preserve optionality across multiple signals
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from data.ground_truth.router import GroundTruthRouter
from data.markets.base import BaseMarketClient, Market, Order, Side
from resolution.confidence import ConfidenceScorer
from resolution.decay_monitor import (
    DecayAction, DecayMonitor, OpenResolutionPosition,
)
from resolution.gap_detector import GapDetector, GapSignal
from resolution.scanner import ResolutionScanner
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache

logger = logging.getLogger(__name__)

KELLY_FRACTION = 0.12             # 12% fractional Kelly (conservative for thin books)
MAX_POSITION_FRACTION = 0.20      # hard cap: 20% of total bankroll per position


@dataclass
class TradeRecord:
    """Tracks a live resolution drift position."""
    market_id: str
    platform: str
    market: Market
    signal: GapSignal
    entry_price: float
    size_usd: float
    ground_truth_prob: float
    source_confidence: float
    entry_time: float = field(default_factory=time.time)
    order_id: Optional[str] = None


class ResolutionBot:
    """
    Resolution Drift Arbitrage bot.

    Parameters
    ----------
    kalshi_client    : Kalshi REST client (None = disabled)
    poly_client      : Polymarket REST client (None = disabled)
    fee_cache        : shared fee rate cache
    bankroll         : shared bankroll
    exclusions       : shared exclusion list
    dry_run          : if True, log orders but don't place them
    window_hours     : scan only markets expiring within this many hours
    scan_interval    : seconds between scan cycles
    """

    def __init__(
        self,
        kalshi_client: Optional[BaseMarketClient],
        poly_client: Optional[BaseMarketClient],
        fee_cache: FeeCache,
        bankroll: Bankroll,
        exclusions: ExclusionList,
        dry_run: bool = True,
        window_hours: float = 168.0,
        scan_interval: int = 300,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._fee_cache = fee_cache
        self._bankroll = bankroll
        self._exclusions = exclusions
        self._dry_run = dry_run
        self._scan_interval = scan_interval

        self._scanner = ResolutionScanner(
            kalshi_client, poly_client, exclusions, window_hours=window_hours
        )
        self._gap_detector = GapDetector(fee_cache)
        self._ground_truth = GroundTruthRouter()
        self._confidence = ConfidenceScorer()
        self._decay = DecayMonitor()

        # Active positions: market_id → TradeRecord
        self._positions: Dict[str, TradeRecord] = {}

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        logger.info("ResolutionBot: starting (dry_run=%s)", self._dry_run)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                logger.exception("ResolutionBot: cycle error: %s", exc)
            time.sleep(self._scan_interval)

    def run_once(self) -> dict:
        """Execute one full scan-and-evaluate cycle. Returns summary dict."""
        logger.info("=== ResolutionBot cycle start ===")
        cycle_start = time.monotonic()
        summary = {
            "markets_scanned": 0,
            "pairs_found": 0,
            "signals_flagged": 0,
            "trades_fired": 0,
            "trade_details": [],
            "positions_monitored": len(self._positions),
            "exits_triggered": 0,
        }

        if self._bankroll.is_halted():
            logger.warning("ResolutionBot: bankroll HALTED – skipping cycle")
            return summary

        # ── Step 1: Scan ──────────────────────────────────────────────────
        markets = self._scanner.scan()
        summary["markets_scanned"] = len(markets)
        summary["scanned_sample"] = [
            {
                "question": m.question,
                "category": m.category,
                "hours_left": round(m.hours_to_resolution, 1),
                "yes_price": m.yes_price,
                "market_id": m.market_id,
            }
            for m in markets[:3]
        ]

        # ── Step 2: Cross-platform gap detection ──────────────────────────
        pairs = self._scanner.scan_cross_platform_pairs(markets)
        summary["pairs_found"] = len(pairs)
        cross_signals = self._gap_detector.detect_cross_platform(pairs)

        # ── Step 3: Information signals (single-platform + ground truth) ──
        info_signals = self._fetch_info_signals(markets)

        all_signals = cross_signals + info_signals
        summary["signals_flagged"] = len(all_signals)
        logger.info(
            "ResolutionBot: %d cross-platform + %d info signals = %d total",
            len(cross_signals), len(info_signals), len(all_signals),
        )

        # ── Step 4 + 5: Confidence score and execute ──────────────────────
        for signal in all_signals:
            detail = self._try_execute(signal)
            if detail is not None:
                summary["trades_fired"] += 1
                summary["trade_details"].append(detail)

        # ── Step 6: Monitor open positions ────────────────────────────────
        exits = self._monitor_positions()
        summary["exits_triggered"] = exits

        elapsed = (time.monotonic() - cycle_start) * 1000
        summary["cycle_ms"] = round(elapsed)
        logger.info(
            "ResolutionBot: cycle done in %.0fms | %s",
            elapsed, summary,
        )
        return summary

    # ── Signal execution ──────────────────────────────────────────────────────

    def _fetch_info_signals(self, markets: List[Market]) -> List[GapSignal]:
        """For each single-platform market, try fetching ground truth and detect gaps."""
        signals = []
        candidates = [
            m for m in markets
            if m.market_id not in self._positions
            and not self._exclusions.is_excluded(m.platform, m.market_id)
        ]
        total = len(candidates)
        logger.info(
            "ResolutionBot: fetching ground truth for %d candidate markets…", total
        )
        for i, market in enumerate(candidates, 1):
            if i % 25 == 0 or i == total:
                logger.info(
                    "ResolutionBot: ground truth progress %d/%d (signals so far: %d)",
                    i, total, len(signals),
                )
            gt = self._ground_truth.fetch(market)
            if gt is None or gt.ground_truth_prob is None:
                continue

            signal = self._gap_detector.detect_information_signal(
                market, gt.ground_truth_prob
            )
            if signal:
                signal.ground_truth_prob = gt.ground_truth_prob
                signal.ground_truth_result = gt  # preserve real source confidence
                signals.append(signal)
        return signals

    def _try_execute(self, signal: GapSignal) -> Optional[dict]:
        """
        Run confidence check and execute the trade if it passes.
        Returns a trade detail dict on success, None if skipped.
        """
        market = signal.market_to_buy
        mid = market.market_id

        if mid in self._positions:
            return None  # already in this market
        if self._exclusions.is_excluded(market.platform, mid):
            return None

        # Use the ground truth result carried on the signal (info signals) or
        # re-fetch it (cross-platform signals that didn't go through the router).
        gt = signal.ground_truth_result or self._ground_truth.fetch(market)

        # Confidence gate
        score = self._confidence.score(market, gt, signal)
        if not score.passes:
            logger.info(
                "ResolutionBot: SKIP %s – %s", mid, score.skip_reason
            )
            return None

        # Fee check: re-verify after confidence pass
        fee = self._fee_cache.get_taker_fee(market.platform, mid, force_refresh=True)
        if signal.effective_gap - fee < 0.04:
            logger.info(
                "ResolutionBot: SKIP %s – gap below threshold after live fee refresh "
                "(fee=%.4f eff_gap=%.4f)", mid, fee, signal.effective_gap,
            )
            self._exclusions.add_fee_surprise(market.platform, mid)
            return None

        # Size using fractional Kelly
        size_usd = self._compute_size(signal, score.source_confidence)
        if size_usd < 1.0:
            logger.info("ResolutionBot: SKIP %s – size too small ($%.2f)", mid, size_usd)
            return None

        # Reserve capital
        if not self._bankroll.reserve(mid, size_usd):
            return None

        # Place order
        order_id = self._place_order(market, signal, size_usd, fee)

        gt_prob = (
            gt.ground_truth_prob
            if gt and gt.ground_truth_prob is not None
            else signal.reference_price
        )

        self._positions[mid] = TradeRecord(
            market_id=mid,
            platform=market.platform,
            market=market,
            signal=signal,
            entry_price=signal.target_price,
            size_usd=size_usd,
            ground_truth_prob=gt_prob,
            source_confidence=score.source_confidence,
            order_id=order_id,
        )
        logger.info(
            "ResolutionBot: TRADE %s %s @ %.4f size=$%.2f (order=%s)\n  → \"%s\"",
            signal.action, mid, signal.target_price, size_usd, order_id,
            market.question,
        )
        return {
            "action": signal.action,
            "question": market.question,
            "market_id": mid,
            "platform": market.platform,
            "price": signal.target_price,
            "size_usd": size_usd,
            "hours_left": round(market.hours_to_resolution, 1),
            "source": gt.source_name if gt else "unknown",
        }

    def _place_order(
        self, market: Market, signal: GapSignal, size_usd: float, fee: float
    ) -> Optional[str]:
        if self._dry_run:
            logger.info(
                "ResolutionBot [DRY]: %s %s @ %.4f size=$%.2f fee=%.4f",
                signal.action, market.market_id, signal.target_price, size_usd, fee,
            )
            return f"dry_{market.market_id}_{int(time.time())}"

        client = self._poly if market.platform == "polymarket" else self._kalshi
        if not client:
            return None

        side = Side.YES if signal.action == "buy_yes" else Side.NO
        order = Order(
            market_id=market.market_id,
            platform=market.platform,
            side=side,
            price=signal.target_price,
            size_usd=size_usd,
        )
        try:
            result = client.place_order(order)
            return result.order_id
        except Exception as exc:
            logger.warning(
                "ResolutionBot: order failed for %s: %s", market.market_id, exc
            )
            self._bankroll.release(market.market_id, realized_pnl_usd=0.0)
            return None

    def _compute_size(self, signal: GapSignal, source_confidence: float) -> float:
        """Fractional Kelly sizing: conservative 12% of Kelly, capped at 20% bankroll."""
        # Kelly formula: f* = (b*p - (1-p)) / b
        # Must be computed from the perspective of the side being bought.
        #
        # BUY YES: buy YES at target_price; wins with prob = ground_truth_prob
        # BUY NO:  buy NO at (1 - target_price); wins with prob = 1 - ground_truth_prob
        gt_prob = signal.ground_truth_prob
        if gt_prob is None:
            # Fall back to reference_price for cross-platform signals without GT
            gt_prob = signal.reference_price

        if signal.action == "buy_yes":
            p = gt_prob
            entry = signal.target_price
        else:
            # BUY NO: flip perspective — probability NO wins, NO price is entry cost
            p = 1.0 - gt_prob
            entry = 1.0 - signal.target_price

        if entry <= 0 or entry >= 1:
            return 0.0
        b = (1.0 - entry) / entry
        kelly = max((b * p - (1 - p)) / b, 0.0)

        # Scale Kelly fraction by source confidence (higher conf = larger fraction)
        frac = KELLY_FRACTION * min(source_confidence, 1.0)
        size = kelly * frac * self._bankroll.total_usd
        max_size = self._bankroll.total_usd * MAX_POSITION_FRACTION
        return round(min(size, max_size), 2)

    # ── Position monitoring ───────────────────────────────────────────────────

    def _monitor_positions(self) -> int:
        """Run decay monitor on all open positions. Returns # of exits triggered."""
        if not self._positions:
            return 0

        open_positions = []
        for rec in self._positions.values():
            current_price = self._get_current_price(rec.market)
            if current_price is None:
                continue
            open_positions.append(OpenResolutionPosition(
                market_id=rec.market_id,
                platform=rec.platform,
                market=rec.market,
                entry_price=rec.entry_price,
                current_price=current_price,
                ground_truth_prob=rec.ground_truth_prob,
                size_usd=rec.size_usd,
                source_confidence=rec.source_confidence,
                action=rec.signal.action,
            ))

        decisions = self._decay.evaluate(open_positions)
        exits = 0
        for decision in decisions:
            mid = decision.position.market_id
            logger.info(
                "DecayMonitor: %s %s capture=%.0f%% gain=$%.2f – %s",
                decision.action, mid,
                decision.capture_ratio * 100,
                decision.current_gain_usd,
                decision.reason,
            )
            if decision.action != DecayAction.HOLD:
                self._exit_position(mid, decision.current_gain_usd)
                exits += 1
        return exits

    def _exit_position(self, market_id: str, realized_pnl_usd: float) -> None:
        rec = self._positions.pop(market_id, None)
        if not rec:
            return
        self._bankroll.release(market_id, realized_pnl_usd=realized_pnl_usd)
        if not self._dry_run:
            client = self._poly if rec.platform == "polymarket" else self._kalshi
            try:
                client.close_position(market_id)
            except Exception as exc:
                logger.warning("ResolutionBot: exit order failed for %s: %s", market_id, exc)
        logger.info(
            "ResolutionBot: EXITED %s pnl=$%.2f", market_id, realized_pnl_usd
        )

    def _get_current_price(self, market: Market) -> Optional[float]:
        try:
            client = self._poly if market.platform == "polymarket" else self._kalshi
            if not client:
                return None
            ob = client.get_order_book(market.market_id)
            return ob.mid_price
        except Exception:
            return None
