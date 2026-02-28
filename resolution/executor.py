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
import math
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from data.ground_truth.router import GroundTruthRouter
from data.markets.base import BaseMarketClient, Market, Order, Side
from data.markets.polymarket_ws import PolymarketWSManager
from resolution.confidence import ConfidenceScorer
from resolution.decay_monitor import (
    DecayAction, DecayMonitor, OpenResolutionPosition,
)
from resolution.gap_detector import GapDetector, GapSignal
from resolution.scanner import ResolutionScanner
from resolution.tier_registry import TierRegistry
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache
from utils.storage import StateStore

logger = logging.getLogger(__name__)

KELLY_FRACTION = 0.12             # 12% fractional Kelly (conservative for thin books)
MAX_POSITION_FRACTION = 0.20      # hard cap: 20% of total bankroll per position

# ── Tiered scan intervals ──────────────────────────────────────────────────────
# The main loop sleeps TIER_1_INTERVAL between cycles.  Within each cycle,
# only the tiers that are "due" based on elapsed time actually run.

TIER_1_INTERVAL = 15       # seconds – active-watch markets (<2h remaining)
TIER_2_INTERVAL = 300      # seconds – regular-scan markets (2–24h remaining)
TIER_3_INTERVAL = 1800     # seconds – discovery markets (>24h remaining)

# Full platform fetch to discover markets not yet in the registry.
# Runs at the same cadence as Tier 3 (30 min) by default.
DISCOVERY_INTERVAL = 1800  # seconds

# Stagger API calls within a tier batch to avoid burst patterns that
# look like abuse to rate limiters.
TIER_REQUEST_STAGGER_S = 0.15    # base sleep between per-market calls
TIER_STAGGER_JITTER_S  = 0.10    # max random addition to stagger

# ── Other constants ────────────────────────────────────────────────────────────

# Do not enter new positions during the first 60 seconds after startup.
# On restart the in-memory state is freshly loaded from disk but the
# market scanner hasn't run yet — gap calculations in the first cycle may
# use stale price data from the last run.  Position monitoring still runs
# during this window so open positions are tracked from the first second.
STARTUP_STABILIZATION_SECONDS = 60

# Warn when a Tier-1 cycle takes more than 80% of TIER_1_INTERVAL — it means
# the VPS is struggling and the next cycle will start late.
CYCLE_DURATION_WARN_FRACTION = 0.80

# Max signals kept per (source_name, action) bucket.
# Prevents correlated overexposure when a single data source (e.g. Yahoo Finance/NQ=F)
# generates dozens of "Nasdaq below 27000" contracts all expressing the same bet.
MAX_SIGNALS_PER_SOURCE_ACTION = 2

# If the live order-book mid-price deviates more than this from the scanner price,
# the scanner data is stale. Skip the trade rather than entering at a price that
# no longer exists in the real order book.
STALE_PRICE_THRESHOLD = 0.12     # 12 cents


@dataclass
class TradeRecord:
    """Tracks a live resolution drift position."""
    market_id: str
    platform: str
    market: Market
    signal: Optional[GapSignal]   # None for positions restored from disk
    action: str                   # "buy_yes" | "buy_no" – cached from signal.action
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
        kalshi_window_hours: Optional[float] = None,
        poly_window_hours: Optional[float] = None,
        scan_interval: int = 300,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._fee_cache = fee_cache
        self._bankroll = bankroll
        self._exclusions = exclusions
        self._dry_run = dry_run
        self._scan_interval = scan_interval
        self._state = state_store

        self._scanner = ResolutionScanner(
            kalshi_client, poly_client, exclusions,
            window_hours=window_hours,
            kalshi_window_hours=kalshi_window_hours,
            poly_window_hours=poly_window_hours,
        )
        self._gap_detector = GapDetector(fee_cache)
        self._ground_truth = GroundTruthRouter()
        self._confidence = ConfidenceScorer()
        self._decay = DecayMonitor()

        # ── Tiered market registry ─────────────────────────────────────────────
        # Tracks every known market and which scan tier it belongs to.
        # Populated on the first discovery cycle and kept current thereafter.
        self._registry = TierRegistry()

        # When each tier / discovery scan last ran (monotonic seconds).
        self._last_discovery_at: float = 0.0   # force discovery on first cycle
        self._last_tier2_at: float = 0.0
        self._last_tier3_at: float = 0.0

        # Cursor positions for rotating tier batches (avoid burst patterns).
        # key: tier number → index into the sorted tier-entry list.
        self._tier_cursors: Dict[int, int] = {2: 0, 3: 0}

        # Optional Polymarket WebSocket manager for Tier 1 markets.
        # Delivers sub-second order-book updates without burning REST budget.
        self._ws = PolymarketWSManager(poly_client)

        # ── Other state ────────────────────────────────────────────────────────
        # Active positions: market_id → TradeRecord
        self._positions: Dict[str, TradeRecord] = {}
        # Per-cycle circuit breaker: set True when Kalshi's backend returns
        # "service unavailable" so remaining signals skip immediately rather
        # than each burning ~14s on retries.
        self._kalshi_backend_down: bool = False
        # Signals from the most recent cycle — used by get_last_signals() so
        # the dry-run summary and the 'signals' CLI command can display them.
        self._last_signals: List[GapSignal] = []
        # Timestamp of when this instance was created — used by the startup
        # stabilization guard to delay new trade entry until the first full
        # scan cycle has completed and in-memory state is populated.
        self._startup_time: float = time.time()
        self._load_positions()
        self._reconcile_with_exchange()

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        logger.info(
            "ResolutionBot: starting tiered scan "
            "(T1=%ds T2=%ds T3=%ds discovery=%ds dry_run=%s)",
            TIER_1_INTERVAL, TIER_2_INTERVAL, TIER_3_INTERVAL,
            DISCOVERY_INTERVAL, self._dry_run,
        )
        self._ws.start()
        try:
            while True:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    logger.exception("ResolutionBot: cycle error: %s", exc)
                time.sleep(TIER_1_INTERVAL)
        finally:
            self._ws.stop()

    def run_once(self, skip_stabilization: bool = False) -> dict:
        """
        Execute one tiered scan cycle.  Returns summary dict.

        Called every TIER_1_INTERVAL (15s) by run_forever().  Within each call
        only the work that is actually *due* in this cycle runs:

          Always:     Tier 1 market refresh + gap detection (small fast set)
                      Rotating Tier 2 batch (covers all T2 in TIER_2_INTERVAL)
                      Rotating Tier 3 batch (covers all T3 in TIER_3_INTERVAL)
                      Position monitoring
          Periodically: Discovery scan (full platform fetch, every DISCOVERY_INTERVAL)

        Parameters
        ----------
        skip_stabilization : if True, bypass the startup stabilization window
            (intended for --once / single-shot mode so a fresh startup doesn't
            silently scan 0 markets).
        """
        logger.debug("=== ResolutionBot tier-1 cycle start ===")
        self._kalshi_backend_down = False   # reset circuit breaker each cycle
        cycle_start = time.monotonic()
        now_mono = time.monotonic()
        summary: dict = {
            "markets_scanned": 0,
            "pairs_found": 0,
            "signals_flagged": 0,
            "trades_fired": 0,
            "trade_details": [],
            "positions_monitored": len(self._positions),
            "exits_triggered": 0,
            "registry": self._registry.stats(),
        }

        # ── Halt: monitor open positions but skip new entry ────────────────
        if self._bankroll.is_halted():
            logger.warning(
                "ResolutionBot: bankroll HALTED – skipping new entries; "
                "monitoring open positions"
            )
            exits = self._monitor_positions()
            summary["exits_triggered"] = exits
            elapsed = (time.monotonic() - cycle_start) * 1000
            summary["cycle_ms"] = round(elapsed)
            return summary

        # ── Startup stabilization: monitor only, no new trades ────────────
        # Skipped in --once / single-shot mode: there is only one cycle so the
        # stabilization window would always fire, producing an empty scan.
        startup_elapsed = time.time() - self._startup_time
        if not skip_stabilization and startup_elapsed < STARTUP_STABILIZATION_SECONDS:
            logger.info(
                "ResolutionBot: startup stabilization (%ds remaining) – "
                "monitoring open positions only",
                int(STARTUP_STABILIZATION_SECONDS - startup_elapsed),
            )
            exits = self._monitor_positions()
            summary["exits_triggered"] = exits
            elapsed = (time.monotonic() - cycle_start) * 1000
            summary["cycle_ms"] = round(elapsed)
            return summary

        # ── Discovery scan (every DISCOVERY_INTERVAL) ──────────────────────
        # Full platform fetch: finds new markets and seeds/refreshes the registry.
        if now_mono - self._last_discovery_at >= DISCOVERY_INTERVAL:
            self._run_discovery()
            self._last_discovery_at = time.monotonic()

        # Promote markets that have crossed tier boundaries since last cycle.
        self._registry.evict_expired()
        promoted = self._registry.promote_due()
        if promoted:
            logger.info("TierRegistry: %d market(s) promoted to a higher tier", promoted)

        # Sync Polymarket WebSocket subscriptions with current Tier 1 set.
        t1_ids = {e.market_id for e in self._registry.get_tier(1)}
        self._ws.sync_subscriptions(t1_ids)

        # ── Tier 1: refresh all active-watch markets (every cycle) ─────────
        t1_entries = self._registry.get_tier(1)
        if t1_entries:
            # Pull any order-book updates from the WebSocket first (free, fast).
            ws_prices = self._ws.get_pending_updates()
            t1_raw = [e.market for e in t1_entries]
            t1_markets = self._scanner.refresh_markets(t1_raw)
            for m in t1_markets:
                # Apply WebSocket price override where available.
                if m.market_id in ws_prices:
                    m.yes_price = ws_prices[m.market_id]
                self._registry.ingest(m)
            self._stagger()
        else:
            t1_markets = []

        # ── Tier 2: rotating batch (full set covered every TIER_2_INTERVAL) ─
        t2_batch_raw = self._next_tier_batch(2)
        if t2_batch_raw:
            t2_markets = self._scanner.refresh_markets(t2_batch_raw)
            for m in t2_markets:
                self._registry.ingest(m)
            self._stagger()
        else:
            t2_markets = []

        # ── Tier 3: rotating batch (refresh + promote; no GT eval) ──────────
        t3_batch_raw = self._next_tier_batch(3)
        if t3_batch_raw:
            t3_markets = self._scanner.refresh_markets(t3_batch_raw)
            for m in t3_markets:
                self._registry.ingest(m)   # ingest recomputes tier → may promote

        # Active markets for this cycle = Tier 1 (all) + Tier 2 batch.
        # These are the only markets that get full gap detection + GT evaluation.
        active_markets = t1_markets + t2_markets
        summary["markets_scanned"] = len(active_markets)
        summary["scanned_sample"] = [
            {
                "question": m.question,
                "category": m.category,
                "hours_left": round(m.hours_to_resolution, 1),
                "yes_price": m.yes_price,
                "market_id": m.market_id,
                "tier": 1 if m in t1_markets else 2,
            }
            for m in (t1_markets[:2] + t2_markets[:1])
        ]

        # ── Cross-platform gap detection (Tier 1 + all Tier 2) ─────────────
        # Use ALL Tier 2 entries (not just the batch) so cross-platform pairs
        # across tiers are detected even when only one side was refreshed.
        all_t2_markets = [e.market for e in self._registry.get_tier(2)]
        pair_eligible = t1_markets + all_t2_markets
        pairs = self._scanner.scan_cross_platform_pairs(pair_eligible)
        summary["pairs_found"] = len(pairs)
        cross_signals = self._gap_detector.detect_cross_platform(pairs)

        # Urgent-promote markets with detected cross-platform gaps.
        for sig in cross_signals:
            self._registry.mark_urgent(sig.market_to_buy.market_id)

        # ── Information signals (GT fetch for active markets) ───────────────
        info_signals = self._fetch_info_signals(active_markets)

        # Urgent-promote markets with detected info gaps.
        for sig in info_signals:
            self._registry.mark_urgent(sig.market_to_buy.market_id)

        all_signals = cross_signals + info_signals
        self._last_signals = all_signals
        summary["signals_flagged"] = len(all_signals)
        logger.info(
            "ResolutionBot: %d cross-platform + %d info signals = %d total "
            "(T1=%d T2_batch=%d/%d total_t2=%d)",
            len(cross_signals), len(info_signals), len(all_signals),
            len(t1_markets), len(t2_markets),
            len(self._registry.get_tier(2)), len(all_t2_markets),
        )

        # ── Clear urgent flag for markets where the gap has closed ──────────
        active_signal_ids = {s.market_to_buy.market_id for s in all_signals}
        for entry in list(self._registry.get_tier(1)):
            if entry.signal_urgent and entry.market_id not in active_signal_ids:
                self._registry.clear_urgent(entry.market_id)

        # ── Confidence gate pre-screen (for display/logging only) ───────────
        display_signals: List[GapSignal] = []
        confidence_blocked = 0
        for signal in all_signals:
            mid = signal.market_to_buy.market_id
            if (
                mid in self._positions
                or self._exclusions.is_excluded(signal.market_to_buy.platform, mid)
            ):
                continue
            gt = signal.ground_truth_result
            score = self._confidence.score(signal.market_to_buy, gt, signal)
            if score.passes:
                display_signals.append(signal)
            else:
                confidence_blocked += 1

        summary["confidence_blocked"] = confidence_blocked
        logger.info(
            "ResolutionBot: confidence gate: %d pass, %d blocked",
            len(display_signals), confidence_blocked,
        )
        summary["signals_detail"] = [
            {
                "question":      s.market_to_buy.question,
                "market_id":     s.market_to_buy.market_id,
                "platform":      s.market_to_buy.platform,
                "action":        s.action,
                "price":         s.target_price,
                "effective_gap": round(s.effective_gap, 4),
                "signal_type":   s.signal_type,
                "hours_left":    round(s.market_to_buy.hours_to_resolution, 1),
                "source": (
                    s.ground_truth_result.source_name
                    if s.ground_truth_result
                    else f"vs {s.market_reference.platform}"
                    if s.market_reference
                    else "cross-platform"
                ),
            }
            for s in display_signals
        ]

        # ── Execute signals ──────────────────────────────────────────────────
        for signal in all_signals:
            detail = self._try_execute(signal)
            if detail is not None:
                summary["trades_fired"] += 1
                summary["trade_details"].append(detail)

        # ── Monitor open positions ───────────────────────────────────────────
        exits = self._monitor_positions()
        summary["exits_triggered"] = exits

        # Refresh registry stats at end so cycle #1 reflects post-discovery state.
        summary["registry"] = self._registry.stats()

        elapsed = (time.monotonic() - cycle_start) * 1000
        summary["cycle_ms"] = round(elapsed)
        logger.debug("ResolutionBot: cycle done in %.0fms | %s", elapsed, summary)
        if elapsed > TIER_1_INTERVAL * 1000 * CYCLE_DURATION_WARN_FRACTION:
            logger.warning(
                "ResolutionBot: cycle took %.0fms — exceeds %.0f%% of Tier-1 "
                "interval (%ds). VPS may be under load; consider reducing scan "
                "scope or upgrading instance.",
                elapsed, CYCLE_DURATION_WARN_FRACTION * 100, TIER_1_INTERVAL,
            )
        return summary

    # ── Tiered scan helpers ───────────────────────────────────────────────────

    def _run_discovery(self) -> None:
        """
        Full platform fetch — discovers new markets and refreshes the registry.

        Called every DISCOVERY_INTERVAL.  Uses the existing scanner which is
        already configured with the correct per-platform window hours.  All
        returned markets are ingested into the tier registry; the registry
        assigns tiers based on hours_to_resolution.
        """
        logger.info("ResolutionBot: running discovery scan (full platform fetch)")
        try:
            markets = self._scanner.scan()
            counts = self._registry.ingest_many(markets)
            logger.info(
                "ResolutionBot: discovery ingested %d markets → T1=%d T2=%d T3=%d "
                "(registry total=%d)",
                sum(counts.values()),
                counts.get(1, 0), counts.get(2, 0), counts.get(3, 0),
                len(self._registry),
            )
        except Exception as exc:
            logger.warning("ResolutionBot: discovery scan failed: %s", exc)

    def _next_tier_batch(self, tier: int) -> List[Market]:
        """
        Return the next rotating batch of markets for a given tier.

        The batch size is chosen so that all markets in the tier are covered
        within one tier interval (TIER_2_INTERVAL or TIER_3_INTERVAL), with
        one sub-batch processed per TIER_1_INTERVAL cycle.  Requests within
        each batch are staggered by _stagger() in the caller.

        Uses a stable sort (by market_id) so the cursor advances through the
        same order every cycle even as markets are promoted/demoted.
        """
        entries = self._registry.get_tier(tier)   # already sorted by market_id
        if not entries:
            return []

        interval = TIER_2_INTERVAL if tier == 2 else TIER_3_INTERVAL
        # How many markets to process per cycle to cover the full tier within
        # one tier interval.  Use the actual configured scan interval (e.g. 60s
        # from main.py) rather than TIER_1_INTERVAL (15s); if we used 15s here
        # but cycles only fire every 60s, we'd cover 4× fewer markets per cycle
        # and need 4× as long to sweep the full T2/T3 pool.
        cycle_s = max(1, self._scan_interval)
        cycles_per_interval = max(1, interval // cycle_s)
        batch_size = max(1, math.ceil(len(entries) / cycles_per_interval))

        cursor = self._tier_cursors.get(tier, 0) % len(entries)
        end = cursor + batch_size

        if end <= len(entries):
            batch = entries[cursor:end]
        else:
            # Wrap around the end of the list.
            batch = entries[cursor:] + entries[: end - len(entries)]

        self._tier_cursors[tier] = end % len(entries)
        return [e.market for e in batch]

    def _stagger(self) -> None:
        """
        Sleep a short, randomised interval between tier-batch API calls.

        This spreads requests across the cycle window rather than firing them
        all in a burst at the start, which avoids the pattern that rate
        limiters flag as abusive and reduces peak memory/CPU pressure.
        """
        sleep_s = TIER_REQUEST_STAGGER_S + random.uniform(0, TIER_STAGGER_JITTER_S)
        time.sleep(sleep_s)

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

        # Coverage diagnostic counters
        n_no_source: int = 0        # GT router returned None — no data source covers this market
        n_no_prob: int = 0          # GT source found but couldn't extract a probability
        n_covered: int = 0          # GT source returned a usable probability
        n_gap_too_small: int = 0    # Covered but gap < minimum trading threshold
        coverage_sources: Dict[str, int] = {}  # source_name → count of markets covered

        for i, market in enumerate(candidates, 1):
            if i % 25 == 0 or i == total:
                logger.info(
                    "ResolutionBot: ground truth progress %d/%d (signals so far: %d)",
                    i, total, len(signals),
                )
            gt = self._ground_truth.fetch(market)
            if gt is None:
                n_no_source += 1
                continue
            if gt.ground_truth_prob is None:
                n_no_prob += 1
                continue

            n_covered += 1
            coverage_sources[gt.source_name] = coverage_sources.get(gt.source_name, 0) + 1

            signal = self._gap_detector.detect_information_signal(
                market, gt.ground_truth_prob
            )
            if signal:
                signal.ground_truth_prob = gt.ground_truth_prob
                signal.ground_truth_result = gt  # preserve real source confidence
                signals.append(signal)
            else:
                n_gap_too_small += 1

        # Emit a single summary line so operators can distinguish
        # "no data sources cover these markets" from "markets are efficiently priced".
        sources_str = (
            ", ".join(f"{src}={cnt}" for src, cnt in sorted(coverage_sources.items()))
            if coverage_sources else "none"
        )
        logger.info(
            "ResolutionBot: GT coverage summary — "
            "no_source=%d no_prob=%d covered=%d (sources: %s) "
            "gap_too_small=%d actionable=%d",
            n_no_source, n_no_prob, n_covered, sources_str,
            n_gap_too_small, len(signals),
        )

        # Deduplicate: cap at MAX_SIGNALS_PER_SOURCE_ACTION per (source, direction)
        # bucket so a single instrument (e.g. Nasdaq) can't consume the whole
        # bankroll with 40 correlated contracts expressing the same directional bet.
        buckets: dict = defaultdict(list)
        for sig in signals:
            src = (
                sig.ground_truth_result.source_name
                if sig.ground_truth_result
                else "unknown"
            )
            buckets[(src, sig.action)].append(sig)

        deduped: List[GapSignal] = []
        for bucket_sigs in buckets.values():
            bucket_sigs.sort(key=lambda s: -s.effective_gap)
            deduped.extend(bucket_sigs[:MAX_SIGNALS_PER_SOURCE_ACTION])

        if len(deduped) < len(signals):
            logger.info(
                "ResolutionBot: deduplication reduced signals %d → %d "
                "(max %d per source+direction bucket)",
                len(signals), len(deduped), MAX_SIGNALS_PER_SOURCE_ACTION,
            )
        return deduped

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

        # Fetch the live order book before the confidence gate so we can:
        #   a) populate depth_ratio on cross-platform signals (liquidity penalty)
        #   b) run the stale-price guard afterwards without a second API call
        # The Kalshi bulk /markets endpoint often returns stale yes_bid/yes_ask
        # (e.g. 49/50 by default) while the real order book is at 0.99 or 0.01.
        ob_live = self._get_live_book(market)
        if ob_live is None:
            # Empty order book — scanner price is unverifiable. Placing an order
            # based on stale bulk-API data (e.g. Kalshi's default yes_bid=99)
            # creates unfillable limit orders. Skip entirely.
            logger.info(
                "ResolutionBot: SKIP %s – order book empty, cannot verify scanner "
                "price %.3f (would create unfillable limit order)",
                mid, signal.target_price,
            )
            return None

        # For cross-platform signals, populate depth_ratio now so the confidence
        # scorer can apply the liquidity penalty before deciding to pass/skip.
        if signal.signal_type == "cross_platform":
            signal.depth_ratio = self._compute_depth_ratio(signal, ob_live)

        # Confidence gate
        score = self._confidence.score(market, gt, signal)
        if not score.passes:
            logger.info(
                "ResolutionBot: SKIP %s – %s", mid, score.skip_reason
            )
            return None

        # Combined floor check: both scores pass but are both marginal (< 0.85).
        # Require order-book depth >= 3x the estimated position size before proceeding.
        if score.requires_depth_check:
            size_estimate = self._compute_size(
                signal, score.source_confidence, score.resolution_clarity
            )
            required_depth = size_estimate * 3.0
            avail_depth = self._book_depth_usd(signal, ob_live)
            if avail_depth < required_depth:
                logger.info(
                    "ResolutionBot: SKIP %s – marginal confidence both axes "
                    "(src=%.2f clarity=%.2f) insufficient depth "
                    "(need $%.0f, have $%.0f)",
                    mid, score.source_confidence, score.resolution_clarity,
                    required_depth, avail_depth,
                )
                return None

        # Stale-price guard: verify the scanner price hasn't drifted far from
        # reality.  If the scanner price is stale, recalculate the gap:
        #   - Gap disappears  → was a stale-data artifact, skip
        #   - Gap still real  → update signal to live price and proceed
        live_price = ob_live.mid_price
        drift = abs(live_price - signal.target_price)
        if drift > STALE_PRICE_THRESHOLD:
            gt_prob = (
                gt.ground_truth_prob
                if gt and gt.ground_truth_prob is not None
                else signal.reference_price
            )
            live_gap = abs(live_price - gt_prob)
            live_effective_gap = live_gap - signal.taker_fee
            if live_effective_gap < 0.04:
                logger.info(
                    "ResolutionBot: SKIP %s – stale scanner %.3f → live %.3f, "
                    "recalc gap %.3f below threshold (no real edge)",
                    mid, signal.target_price, live_price, live_effective_gap,
                )
                return None
            logger.info(
                "ResolutionBot: STALE corrected %s – scanner %.3f → live %.3f, "
                "live effective_gap=%.3f – proceeding",
                mid, signal.target_price, live_price, live_effective_gap,
            )
            signal.target_price = live_price
            signal.effective_gap = live_effective_gap

        # Fee check: re-verify with live fee (fee may have changed since gap detection).
        # Do NOT subtract fee from signal.effective_gap — the gap detector already
        # subtracted it once.  Recompute fresh from current target price + current fee
        # so we use one consistent fee value throughout.
        fee = self._fee_cache.get_taker_fee(market.platform, mid, force_refresh=True)
        gt_prob_for_gap = (
            gt.ground_truth_prob
            if gt and gt.ground_truth_prob is not None
            else signal.reference_price
        )
        live_effective_gap = abs(signal.target_price - gt_prob_for_gap) - fee
        if live_effective_gap < 0.04:
            logger.info(
                "ResolutionBot: SKIP %s – live effective_gap=%.3f below threshold "
                "(fee=%.4f target=%.3f gt=%.3f)",
                mid, live_effective_gap, fee, signal.target_price, gt_prob_for_gap,
            )
            if fee > signal.taker_fee:
                # Fee increased since signal generation — exclude to avoid repeat surprises
                self._exclusions.add_fee_surprise(market.platform, mid)
            return None
        # Update signal with live-computed values so _compute_size gets consistent data
        signal.effective_gap = live_effective_gap
        signal.taker_fee = fee

        # Size using fractional Kelly (time-to-resolution weighting applied inside)
        size_usd = self._compute_size(
            signal, score.source_confidence, score.resolution_clarity
        )
        if size_usd < 1.0:
            logger.info("ResolutionBot: SKIP %s – size too small ($%.2f)", mid, size_usd)
            return None

        # Determine taker limit price from the live order book.
        #
        # Using signal.target_price (the scanner mid) as the limit creates
        # resting orders whenever the spread is non-zero — the mid is always
        # below the ask.  A market that drifted only 8¢ in YES-space (below
        # the 12¢ stale-price threshold) can be 8× wrong in NO-price space
        # near 0%/100%, producing 1¢ NO bids that never fill.
        #
        # Fix: bid at the live ask (BUY_YES) or live bid (BUY_NO) so the
        # order crosses the spread and executes as a taker immediately.
        if signal.action == "buy_yes":
            limit_price = ob_live.best_yes_ask
            if limit_price is None:
                logger.info(
                    "ResolutionBot: SKIP %s – no YES ask in order book "
                    "(no sellers to fill against)", mid
                )
                return None
        else:
            # BUY_NO: order.price is the YES price field for Kalshi.
            # Using best_yes_bid places our NO bid at (1 - best_yes_bid),
            # which exactly matches the current NO ask → taker fill.
            limit_price = ob_live.best_yes_bid
            if limit_price is None:
                logger.info(
                    "ResolutionBot: SKIP %s – no YES bid in order book "
                    "(no NO sellers to fill against)", mid
                )
                return None
        logger.info(
            "ResolutionBot: limit_price=%.4f (action=%s ask=%.4f bid=%.4f) for %s",
            limit_price, signal.action,
            ob_live.best_yes_ask or 0.0, ob_live.best_yes_bid or 0.0, mid,
        )

        # Reserve capital
        if not self._bankroll.reserve(mid, size_usd):
            return None

        # Place order
        order_id = self._place_order(market, signal, size_usd, fee,
                                     limit_price=limit_price)

        # In live mode _place_order returns None when the order fails and has
        # already released the bankroll reserve.  Do NOT add a phantom position.
        if order_id is None and not self._dry_run:
            logger.warning(
                "ResolutionBot: order placement failed for %s – no position recorded", mid
            )
            return None

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
            action=signal.action,
            entry_price=signal.target_price,
            size_usd=size_usd,
            ground_truth_prob=gt_prob,
            source_confidence=score.source_confidence,
            order_id=order_id,
        )
        self._save_positions()
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
        self, market: Market, signal: GapSignal, size_usd: float, fee: float,
        limit_price: Optional[float] = None,
    ) -> Optional[str]:
        # limit_price: the taker limit (live ask for YES, live bid for NO).
        # Falls back to signal.target_price only when not provided (dry-run log).
        order_price = limit_price if limit_price is not None else signal.target_price
        if self._dry_run:
            logger.info(
                "ResolutionBot [DRY]: %s %s @ %.4f size=$%.2f fee=%.4f",
                signal.action, market.market_id, order_price, size_usd, fee,
            )
            return f"dry_{market.market_id}_{int(time.time())}"

        client = self._poly if market.platform == "polymarket" else self._kalshi
        if not client:
            return None

        # Circuit breaker: if a prior order this cycle hit Kalshi's "service
        # unavailable" error, skip all subsequent orders immediately rather
        # than each burning 14s of retries on a backend that's already known down.
        if self._kalshi_backend_down and market.platform == "kalshi":
            logger.warning(
                "ResolutionBot: Kalshi backend down this cycle – skipping %s",
                market.market_id,
            )
            self._bankroll.release(market.market_id, realized_pnl_usd=0.0)
            return None

        side = Side.YES if signal.action == "buy_yes" else Side.NO
        # Convert the taker fee rate to basis points for the Polymarket EIP-712
        # signed payload.  fee is already in decimal (e.g. 0.02 = 2%), so
        # multiply by 10_000 to get bps.  This must be in the Order so the
        # CLOB client can include it in the signature — not just the request header.
        fee_bps = int(round(fee * 10_000)) if market.platform == "polymarket" else 0
        order = Order(
            market_id=market.market_id,
            platform=market.platform,
            side=side,
            price=order_price,
            size_usd=size_usd,
            fee_rate_bps=fee_bps,
        )
        try:
            result = client.place_order(order)
            if not result.order_id:
                logger.warning(
                    "ResolutionBot: order placed for %s but response had no order_id "
                    "(treating as failure)", market.market_id
                )
                self._bankroll.release(market.market_id, realized_pnl_usd=0.0)
                return None
            return result.order_id
        except Exception as exc:
            # Detect Kalshi backend routing failures ("service unavailable")
            # and open the circuit breaker so remaining signals skip fast.
            _detail = ""
            if hasattr(exc, "response") and exc.response is not None:
                try:
                    _detail = exc.response.json().get("error", {}).get("details", "")
                except Exception:
                    pass
            if "service unavailable" in _detail and market.platform == "kalshi":
                self._kalshi_backend_down = True
                logger.warning(
                    "ResolutionBot: Kalshi backend unavailable – circuit breaker "
                    "open for remainder of this cycle"
                )
            else:
                logger.warning(
                    "ResolutionBot: order failed for %s: %s", market.market_id, exc
                )
            self._bankroll.release(market.market_id, realized_pnl_usd=0.0)
            return None

    def _compute_size(
        self,
        signal: GapSignal,
        source_confidence: float,
        resolution_clarity: float = 1.0,
    ) -> float:
        """
        Fractional Kelly sizing: 12% of Kelly, capped at 20% of bankroll.

        Time-to-resolution weighting (Fix 6):
        Under 2 hours to resolution the stakes of a wrong call are higher
        (less time to exit). When either confidence dimension is below 0.85,
        cap the Kelly fraction at 50% of normal to limit exposure.
        At 0.90+ on both dimensions the full fraction is used even near expiry.
        """
        # Kelly formula: f* = (b*p - (1-p)) / b
        # Computed from the perspective of the side being bought.
        gt_prob = signal.ground_truth_prob
        if gt_prob is None:
            gt_prob = signal.reference_price

        if signal.action == "buy_yes":
            p = gt_prob
            entry = signal.target_price
        else:
            p = 1.0 - gt_prob
            entry = 1.0 - signal.target_price

        if entry <= 0 or entry >= 1:
            return 0.0
        b = (1.0 - entry) / entry
        kelly = max((b * p - (1 - p)) / b, 0.0)

        # Scale Kelly fraction by source confidence
        frac = KELLY_FRACTION * min(source_confidence, 1.0)

        # Time-to-resolution cap: under 2h with marginal confidence → 50% Kelly
        hours_left = signal.market_to_buy.hours_to_resolution
        if hours_left < 2.0 and (
            source_confidence < 0.85 or resolution_clarity < 0.85
        ):
            frac *= 0.50
            logger.debug(
                "_compute_size: time-to-resolution cap applied "
                "(%.1fh left, src=%.2f clarity=%.2f) → frac=%.4f",
                hours_left, source_confidence, resolution_clarity, frac,
            )

        size = kelly * frac * self._bankroll.total_usd
        max_size = self._bankroll.total_usd * MAX_POSITION_FRACTION
        return round(min(size, max_size), 2)

    def _compute_depth_ratio(self, signal: GapSignal, ob: "OrderBook") -> float:
        """
        depth_ratio = available book depth for the intended side / max position size.
        Capped at 1.0 — we don't reward unusually deep books, only penalise thin ones.
        Uses the top-5 price levels to represent realistic fillable liquidity.
        """
        depth_usd = self._book_depth_usd(signal, ob)
        max_pos = self._bankroll.total_usd * MAX_POSITION_FRACTION
        if max_pos <= 0:
            return 1.0
        return min(depth_usd / max_pos, 1.0)

    def _book_depth_usd(self, signal: GapSignal, ob: "OrderBook") -> float:
        """Sum of the top-5 book levels for the side we intend to buy."""
        if signal.action == "buy_yes":
            levels = ob.yes_asks[:5]
        else:
            levels = ob.yes_bids[:5]
        return sum(lv.size for lv in levels)

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
                action=rec.action,
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
        self._save_positions()
        if not self._dry_run:
            client = self._poly if rec.platform == "polymarket" else self._kalshi
            if client is None:
                logger.warning(
                    "ResolutionBot: cannot place exit order for %s – "
                    "%s client not initialised", market_id, rec.platform,
                )
            else:
                try:
                    client.close_position(market_id)
                except Exception as exc:
                    logger.warning(
                        "ResolutionBot: exit order failed for %s: %s", market_id, exc
                    )
        logger.info(
            "ResolutionBot: EXITED %s pnl=$%.2f", market_id, realized_pnl_usd
        )

    def clear_positions(self) -> int:
        """
        Remove all in-memory positions and wipe them from the state store.
        Returns the number of positions cleared.  Does NOT place any exit orders.
        Use this to discard phantom positions that exist in state but not on the exchange.
        """
        count = len(self._positions)
        for mid, rec in list(self._positions.items()):
            self._bankroll.release(mid, realized_pnl_usd=0.0)
        self._positions.clear()
        self._save_positions()
        logger.info("ResolutionBot: cleared %d position(s) from state", count)
        return count

    def get_last_signals(self) -> list:
        """Return signal details from the most recent scan cycle."""
        result = []
        for s in self._last_signals:
            m = s.market_to_buy
            result.append({
                "question":      m.question,
                "market_id":     m.market_id,
                "platform":      m.platform,
                "action":        s.action,
                "price":         s.target_price,
                "effective_gap": round(s.effective_gap, 4),
                "signal_type":   s.signal_type,
                "hours_left":    round(m.hours_to_resolution, 1),
                "already_held":  m.market_id in self._positions,
                "source": (
                    s.ground_truth_result.source_name
                    if s.ground_truth_result
                    else f"vs {s.market_reference.platform}"
                    if s.market_reference
                    else "cross-platform"
                ),
            })
        return result

    def get_open_positions(self) -> list:
        """Return all open positions with live mark-to-market prices."""
        result = []
        for mid, rec in self._positions.items():
            current_price = self._get_current_price(rec.market)
            if rec.action == "buy_yes":
                theo_max = (rec.ground_truth_prob - rec.entry_price) * rec.size_usd
                current_gain = (
                    (current_price - rec.entry_price) * rec.size_usd
                    if current_price is not None else 0.0
                )
            else:
                theo_max = (rec.entry_price - rec.ground_truth_prob) * rec.size_usd
                current_gain = (
                    (rec.entry_price - current_price) * rec.size_usd
                    if current_price is not None else 0.0
                )
            capture = current_gain / theo_max if theo_max > 1e-6 else 0.0
            result.append({
                "market_id": mid,
                "platform": rec.platform,
                "question": rec.market.question,
                "action": rec.action,
                "entry_price": rec.entry_price,
                "current_price": current_price,
                "size_usd": rec.size_usd,
                "ground_truth_prob": rec.ground_truth_prob,
                "source_confidence": rec.source_confidence,
                "hours_left": round(rec.market.hours_to_resolution, 1),
                "current_gain_usd": round(current_gain, 2),
                "capture_ratio": round(capture, 3),
                "order_id": rec.order_id,
            })
        return result

    def _get_live_book(self, market):
        """
        Fetch the live order book.  Returns None if the book is empty
        (mid_price is None) or if the fetch fails — both are treated as
        'unverifiable price, skip the trade'.
        """
        try:
            client = self._poly if market.platform == "polymarket" else self._kalshi
            if not client:
                return None
            ob = client.get_order_book(market.market_id)
            return ob if ob.mid_price is not None else None
        except Exception:
            return None

    def _get_current_price(self, market: Market) -> Optional[float]:
        try:
            client = self._poly if market.platform == "polymarket" else self._kalshi
            if not client:
                return None
            ob = client.get_order_book(market.market_id)
            return ob.mid_price
        except Exception:
            return None

    # ── Position persistence ───────────────────────────────────────────────────

    def _save_positions(self) -> None:
        """Persist all open positions to the state store (called on every open/close)."""
        if not self._state:
            return
        data: dict = {}
        for mid, rec in self._positions.items():
            rd = rec.market.resolution_date
            if rd.tzinfo is None:
                rd = rd.replace(tzinfo=timezone.utc)
            data[mid] = {
                "market_id": rec.market_id,
                "platform": rec.platform,
                "action": rec.action,
                "entry_price": rec.entry_price,
                "size_usd": rec.size_usd,
                "ground_truth_prob": rec.ground_truth_prob,
                "source_confidence": rec.source_confidence,
                "entry_time": rec.entry_time,
                "order_id": rec.order_id,
                "resolution_date_iso": rd.isoformat(),
                "question": rec.market.question,
                "category": rec.market.category,
                "tags": rec.market.tags,
            }
        self._state.set("open_positions", data)

    def _load_positions(self) -> None:
        """Reload open positions from the state store on startup."""
        if not self._state:
            return
        data: dict = self._state.get("open_positions", {})
        if not data:
            return

        loaded = 0
        skipped = 0
        for mid, saved in data.items():
            try:
                # Drop positions that were placed in a dry-run session when we are
                # now running live.  Dry-run order IDs are prefixed with "dry_";
                # they were never real orders on any exchange.
                order_id = saved.get("order_id", "")
                if not self._dry_run and isinstance(order_id, str) and order_id.startswith("dry_"):
                    logger.info(
                        "ResolutionBot: skipping phantom position %s "
                        "(saved from a dry-run session, now running live)", mid,
                    )
                    skipped += 1
                    continue

                rd = datetime.fromisoformat(saved["resolution_date_iso"])

                # Reconstruct a minimal Market object from saved fields.
                # yes_price is approximate (entry price); the decay monitor
                # fetches a fresh live price from the order book every cycle.
                market = Market(
                    market_id=saved["market_id"],
                    platform=saved["platform"],
                    question=saved["question"],
                    category=saved["category"],
                    tags=saved["tags"],
                    resolution_date=rd,
                    yes_price=saved["entry_price"],
                    no_price=round(1.0 - saved["entry_price"], 4),
                )

                if market.hours_to_resolution <= 0:
                    logger.info(
                        "ResolutionBot: skipping expired position %s "
                        "(market already resolved)", mid,
                    )
                    skipped += 1
                    continue

                # Re-reserve capital so the bankroll accounting stays correct.
                if not self._bankroll.reserve(mid, saved["size_usd"]):
                    logger.warning(
                        "ResolutionBot: cannot re-reserve $%.2f for %s "
                        "(bankroll too low – position ignored)", saved["size_usd"], mid,
                    )
                    skipped += 1
                    continue

                self._positions[mid] = TradeRecord(
                    market_id=saved["market_id"],
                    platform=saved["platform"],
                    market=market,
                    signal=None,          # not needed for monitoring
                    action=saved["action"],
                    entry_price=saved["entry_price"],
                    size_usd=saved["size_usd"],
                    ground_truth_prob=saved["ground_truth_prob"],
                    source_confidence=saved["source_confidence"],
                    entry_time=saved.get("entry_time", time.time()),
                    order_id=saved.get("order_id"),
                )
                loaded += 1

            except Exception as exc:
                logger.warning(
                    "ResolutionBot: failed to restore position %s: %s", mid, exc,
                )
                skipped += 1

        if loaded or skipped:
            logger.info(
                "ResolutionBot: restored %d open position(s) from disk "
                "(%d skipped/expired)", loaded, skipped,
            )
        if skipped:
            # Rewrite state without the entries we couldn't load
            self._save_positions()

    def _reconcile_with_exchange(self) -> None:
        """
        Cross-check bot-tracked positions against what the exchange actually holds.

        Called once on startup after _load_positions().  Two things are fixed:
          1. Bot tracks a position the exchange doesn't know about → drop it (phantom).
          2. Exchange holds a position the bot isn't tracking → warn so the user knows.

        Skipped in dry-run mode (dry-run positions are never real orders).
        Skipped if the exchange API call fails (log a warning but don't touch state).
        """
        if self._dry_run or not self._positions:
            return

        # ── Fetch live Kalshi positions AND open orders ───────────────────────
        # A bot-tracked position is valid if it appears in EITHER:
        #   - /portfolio/positions  (filled contracts)
        #   - /portfolio/orders?status=open  (limit orders resting on the book)
        # Only drop it as a phantom if it's absent from both.
        kalshi_live_ids: Optional[set] = None
        filled: list = []
        resting: list = []
        if self._kalshi:
            try:
                filled = self._kalshi.get_positions()
                resting = self._kalshi.get_open_orders()
                kalshi_live_ids = (
                    {p.market_id for p in filled}
                    | {o.market_id for o in resting}
                )
                logger.info(
                    "ResolutionBot reconcile: Kalshi reports %d filled position(s) "
                    "and %d resting order(s): %s",
                    len(filled), len(resting),
                    sorted(kalshi_live_ids) or "(none)",
                )
            except Exception as exc:
                logger.warning(
                    "ResolutionBot reconcile: could not fetch Kalshi positions "
                    "(skipping reconciliation): %s", exc,
                )

        if kalshi_live_ids is None:
            return   # API failed – don't touch anything

        # ── Drop phantoms (bot knows about them, exchange doesn't) ───────────
        phantoms = [
            mid for mid, rec in self._positions.items()
            if rec.platform == "kalshi" and mid not in kalshi_live_ids
        ]
        if phantoms:
            logger.warning(
                "ResolutionBot reconcile: dropping %d phantom position(s) not found "
                "on Kalshi – they were never real orders or have already resolved: %s",
                len(phantoms), phantoms,
            )
            print(
                f"\n  [RECONCILE] Dropped {len(phantoms)} phantom position(s) "
                f"not found on Kalshi: {phantoms}"
            )
            for mid in phantoms:
                self._bankroll.release(mid, realized_pnl_usd=0.0)
                del self._positions[mid]
            self._save_positions()
        else:
            logger.info("ResolutionBot reconcile: all bot positions confirmed on Kalshi.")

        # ── Auto-adopt exchange positions the bot isn't tracking ─────────────
        # This happens when an order was accepted by Kalshi but the bot crashed /
        # errored before saving the TradeRecord — the classic cause of the
        # "4 trades on exchange, 3 tracked by bot" discrepancy.
        bot_kalshi_ids = {
            mid for mid, rec in self._positions.items() if rec.platform == "kalshi"
        }
        untracked = kalshi_live_ids - bot_kalshi_ids
        if untracked:
            logger.warning(
                "ResolutionBot reconcile: %d Kalshi position(s) on the exchange "
                "are NOT tracked by this bot – attempting auto-adopt: %s",
                len(untracked), sorted(untracked),
            )
            fill_map = {o.market_id: o for o in filled}
            rest_map = {o.market_id: o for o in resting}
            adopted = []
            for mid in sorted(untracked):
                if self._adopt_exchange_position(
                    mid, fill_map.get(mid), rest_map.get(mid)
                ):
                    adopted.append(mid)
            if adopted:
                self._save_positions()
                logger.info(
                    "ResolutionBot reconcile: auto-adopted %d position(s): %s",
                    len(adopted), adopted,
                )
                print(
                    f"\n  [RECONCILE] Auto-adopted {len(adopted)} untracked "
                    f"Kalshi position(s): {adopted}"
                )
            failed = sorted(set(untracked) - set(adopted))
            if failed:
                logger.warning(
                    "ResolutionBot reconcile: could not auto-adopt %d position(s): %s "
                    "(market fetch failed or already expired)",
                    len(failed), failed,
                )
                print(
                    f"\n  [RECONCILE] WARNING: {len(failed)} Kalshi position(s) could "
                    f"not be auto-adopted (market fetch failed or expired): {failed}"
                )

    def _adopt_exchange_position(
        self,
        market_id: str,
        fill_order: Optional[Order],
        rest_order: Optional[Order],
    ) -> bool:
        """
        Reconstruct a TradeRecord for a Kalshi position that the exchange holds
        but the bot is not tracking.

        This happens when an order was accepted by Kalshi but the bot
        crashed / errored before saving the TradeRecord — the classic
        "N trades on exchange, N-1 tracked in bot" discrepancy.

        Returns True if successfully adopted and added to self._positions.
        """
        if not self._kalshi:
            return False

        # Fetch market details (resolution_date, question, etc.)
        market = self._kalshi.get_market(market_id)
        if market is None:
            logger.warning(
                "ResolutionBot: cannot auto-adopt %s – market fetch failed", market_id
            )
            return False
        if market.hours_to_resolution <= 0:
            logger.info(
                "ResolutionBot: skipping adopt of already-expired market %s", market_id
            )
            return False

        # Prefer resting order data (has a clean USD size and order_id).
        # Fall back to filled position data.
        source = rest_order or fill_order
        if source is None:
            logger.warning(
                "ResolutionBot: cannot auto-adopt %s – no order/position data", market_id
            )
            return False

        action = "buy_yes" if source.side == Side.YES else "buy_no"
        order_id = rest_order.order_id if rest_order else None

        if rest_order:
            # size_usd is correctly computed in get_open_orders()
            # (remaining_contracts × per-contract cost in USD).
            size_usd = rest_order.size_usd
            entry_price = rest_order.price      # already a [0–1] fraction
        else:
            # get_positions() stores contract_count in size_usd (not dollars).
            # Estimate the USD cost from contract_count × current market price.
            n_contracts = abs(fill_order.size_usd)
            if source.side == Side.YES:
                entry_price = market.yes_price
                size_usd = round(n_contracts * entry_price, 2)
            else:
                entry_price = max(1.0 - market.yes_price, 0.01)
                size_usd = round(n_contracts * entry_price, 2)

        size_usd = round(size_usd, 2)

        # Reserve capital so bankroll accounting stays consistent.
        # If the bankroll is too tight, track the position with $0 reserved —
        # the money is already spent on the exchange; tracking without reservation
        # is better than not tracking at all.
        if size_usd > 0 and not self._bankroll.reserve(market_id, size_usd):
            logger.warning(
                "ResolutionBot: bankroll too low to reserve $%.2f for adopted "
                "position %s – tracking without capital reserve", size_usd, market_id,
            )
            size_usd = 0.0

        self._positions[market_id] = TradeRecord(
            market_id=market_id,
            platform="kalshi",
            market=market,
            signal=None,
            action=action,
            entry_price=entry_price,
            size_usd=size_usd,
            # Conservative default: assume current price ≈ fair value
            # (we don't know the original ground-truth probability).
            ground_truth_prob=entry_price,
            source_confidence=0.8,
            order_id=order_id,
        )
        logger.info(
            "ResolutionBot reconcile: auto-adopted Kalshi %s %s "
            "action=%s entry=%.3f size=$%.2f order_id=%s",
            "resting-order" if rest_order else "filled-position",
            market_id, action, entry_price, size_usd, order_id,
        )
        return True
