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
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from data.ground_truth.router import GroundTruthRouter
from data.markets.base import BaseMarketClient, Market, Order, Side
from data.markets.polymarket_ws import PolymarketWSManager
from monitoring.alerts import AlertManager
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
TIER_2_INTERVAL = 150      # seconds – regular-scan markets (2–24h remaining)
                            # Halved from 300s: doubles the T2 batch per cycle so
                            # the full T2 pool is swept twice as fast at any scan rate.
TIER_3_INTERVAL = 1800     # seconds – discovery markets (>24h remaining)

# Full platform fetch to discover markets not yet in the registry.
# 900s (15 min): new markets enter the pool every 15 min instead of 30 min,
# halving the window where fresh listings are invisible to the scanner.
DISCOVERY_INTERVAL = 900   # seconds

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

# Cycle duration thresholds (multiples of TIER_1_INTERVAL).
# >100% → WARNING  (cycle exceeded the interval; next cycle starts late)
# >150% → ERROR    (severely over-budget; T1 markets are under-surveilled)
CYCLE_WARN_FRACTION  = 1.00
CYCLE_ERROR_FRACTION = 1.50

# Max signals kept per (source_name, action) bucket.
# Prevents correlated overexposure when a single data source (e.g. Yahoo Finance/NQ=F)
# generates dozens of "Nasdaq below 27000" contracts all expressing the same bet.
MAX_SIGNALS_PER_SOURCE_ACTION = 2

# If the live order-book mid-price deviates more than this from the scanner price,
# the scanner data is stale. Skip the trade rather than entering at a price that
# no longer exists in the real order book.
STALE_PRICE_THRESHOLD = 0.12     # 12 cents

# Minimum absolute expected value per contract before a trade is fired.
# EV is computed as: for BUY YES → gt_prob - price; for BUY NO → (1-gt_prob) - price.
# This is the edge in dollars per contract (not a ratio).  2¢ floor blocks near-
# exhausted trades (e.g. BUY NO @99¢ where the max win is 1¢ per contract).
MIN_EV = 0.02

# Confidence gate thresholds used in _try_execute().
# In live mode BOTH source_confidence AND resolution_clarity must reach the live
# gate.  In ghost-trade (dry_run) mode, cross-platform signals are allowed
# through at the lower ghost gate so pair-matching accuracy can be validated
# before real money is committed.
CONFIDENCE_GATE_LIVE                = 0.80
CONFIDENCE_GATE_GHOST_CROSS_PLATFORM = 0.50

# Time-adjusted entry gap uses a tiered curve (see _minimum_gap_for_entry).

# Per-series exposure cap: all markets sharing a root ticker (e.g. KXPAYROLLS)
# are driven by the same underlying data point and are 100% correlated.  Without
# a cap a single bad FRED observation can commit the entire bankroll across all
# strike levels and expiry dates.  Cap at 15% of bankroll across the root series.
MAX_SERIES_EXPOSURE_FRACTION = 0.15


def _minimum_gap_for_entry(hours_remaining: float) -> float:
    """
    Minimum fee-adjusted gap required at this time horizon — tiered curve.

    Steeper requirements further from resolution; cliff steps at tier
    boundaries intentionally penalise long-horizon signals hard.

      < 1h             0.04                      (4.0% floor)
      1 – 4 h          0.04 + h × 0.015          (2h → 7.0%,  4h → 10.0%)
      4 – 8 h          0.10 + h × 0.020          (4h → 18.0%, 8h → 26.0%)
      > 8 h            0.25 + h × 0.030          (8h → 49.0%, 15h → 70.0%)
    """
    if hours_remaining < 1.0:
        return 0.04
    elif hours_remaining < 4.0:
        return 0.04 + hours_remaining * 0.015
    elif hours_remaining <= 8.0:
        return 0.10 + hours_remaining * 0.020
    else:
        return 0.25 + hours_remaining * 0.030


def _check_minimum_ev(
    market_price: float, gt_prob: float, action: str
) -> "tuple[bool, float]":
    """
    Compute absolute expected value per contract and check against MIN_EV.

    For BUY YES at YES price p with ground-truth probability g:
        EV = g*(1-p) - (1-g)*p  =  g - p

    For BUY NO at YES price p with ground-truth probability g:
        EV = (1-g)*(1-p) - g*p  =  (1-g) - p

    EV is dollars of edge per contract, independent of position size.

    Returns (passes_gate, ev).

    Validation examples:
      Gas BUY YES at 0.99, gt=0.00 → EV = 0.00-0.99 = -0.99 → BLOCKED ✓
      S&P BUY NO at 0.095, gt=0.00 → EV = 1.00-0.095 = 0.905 → PASS  ✓
    """
    if action == "buy_yes":
        ev = gt_prob - market_price
    else:  # buy_no
        ev = (1.0 - gt_prob) - market_price
    return ev >= MIN_EV, ev


# ── Order-book cooldown ────────────────────────────────────────────────────────
# When a market's order book is empty (no YES ask, no YES bid, or mid_price=None)
# we set a 30-minute cooldown so the same market doesn't waste a T1 slot on every
# cycle.  The cooldown also demotes signal_urgent T1 markets back to their natural
# tier so the T1 pool doesn't fill up with perpetually-illiquid markets.
_orderbook_cooldowns: Dict[str, datetime] = {}


def _is_in_orderbook_cooldown(market_id: str) -> bool:
    expiry = _orderbook_cooldowns.get(market_id)
    return expiry is not None and datetime.utcnow() < expiry


def _set_orderbook_cooldown(market_id: str, minutes: int = 30) -> None:
    _orderbook_cooldowns[market_id] = datetime.utcnow() + timedelta(minutes=minutes)
    logger.info(
        "ResolutionBot: order-book cooldown set for %s (%d min — empty book)",
        market_id, minutes,
    )


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
    requires_human_review: bool = False   # set on startup if gt_prob flipped >50% since entry


@dataclass
class ResolvedPosition:
    """A closed trade — appended to _resolved_positions for session-level history."""
    market_id: str
    action: str          # "buy_yes" | "buy_no"
    size_usd: float      # dollars wagered
    entry_price: float   # YES price at entry
    exit_price: float    # YES price at exit (or 1.0/0.0 at market resolution)
    pnl: float           # realized P&L in dollars
    capture: float       # pnl / theoretical_max  (positive = with the signal)
    confidence: float    # source_confidence at entry
    source: str          # e.g. "FRED/PAYEMS", "cross-platform"
    resolved_at: datetime


def _series_root(market_id: str) -> Optional[str]:
    """Return the root ticker for any hyphenated market ID, or None for bare IDs.

    Groups all markets that share the same underlying data series regardless of
    expiry date or strike level.  Used for the per-series exposure cap.

      KXPAYROLLS-26FEB-T100000  →  "KXPAYROLLS"
      KXAAAGASW-26MAR02-2.888   →  "KXAAAGASW"
      KXNASDAQ100U-26MAR02H1600 →  "KXNASDAQ100U"
      KXSOME-SINGLETON          →  "KXSOME"
      SOMEMARKET                →  None  (no hyphen — not part of a series)
    """
    idx = market_id.find("-")
    return market_id[:idx] if idx != -1 else None


def _bracket_prefix(market_id: str) -> Optional[str]:
    """Return the ticker prefix for bracket-series markets, or None for singletons.

    Bracket markets share one underlying data point at different strike levels:
      KXAAAGASW-26MAR02-2.888          →  "KXAAAGASW-26MAR02"          (gas, numeric)
      KXAAAGASW-26MAR02-2.898          →  "KXAAAGASW-26MAR02"          (same group)
      KXNASDAQ100U-26MAR02H1600-T22099.99 →  "KXNASDAQ100U-26MAR02H1600" (financial, T-prefix)
      KXSOME-MARKET                    →  None                          (singleton)

    A suffix is treated as a bracket threshold when it matches either:
      ^[\\d.]+$      — pure numeric (e.g. "2.888" for gas prices)
      ^T[\\d.]+$     — T-prefixed numeric (e.g. "T22099.99" for Nasdaq brackets)

    Calendar codes like "26MAR02" and time codes like "H1600" are not matched.
    """
    parts = market_id.split("-")
    if len(parts) >= 2 and re.match(r"^(?:[\d.]+|T[\d.]+)$", parts[-1]):
        return "-".join(parts[:-1])
    return None


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
        alert_manager: Optional[AlertManager] = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._fee_cache = fee_cache
        self._bankroll = bankroll
        self._exclusions = exclusions
        self._dry_run = dry_run
        self._scan_interval = scan_interval
        self._state = state_store
        self._alert_manager: Optional[AlertManager] = alert_manager

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
        # float("-inf") guarantees the discovery condition fires on the very
        # first call regardless of how small time.monotonic() is (e.g. on a
        # freshly booted machine where monotonic < DISCOVERY_INTERVAL = 1800s,
        # the old 0.0 sentinel would cause discovery to be silently skipped).
        self._last_discovery_at: float = float("-inf")   # force discovery on first cycle
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
        # Closed positions — session-only (not persisted); drives 'history' command.
        self._resolved_positions: List[ResolvedPosition] = []
        # Per-cycle circuit breaker: set True when Kalshi's backend returns
        # "service unavailable" so remaining signals skip immediately rather
        # than each burning ~14s on retries.
        self._kalshi_backend_down: bool = False
        # Signals from the most recent cycle — used by get_last_signals() so
        # the dry-run summary and the 'signals' CLI command can display them.
        self._last_signals: List[GapSignal] = []
        # Signals blocked for human review (LARGE_DIVERGENCE, live mode only).
        # market_id → GapSignal.  Cleared when approve_human_review() is called.
        self._pending_human_review: Dict[str, GapSignal] = {}
        # Set of market IDs for which GT was successfully fetched in the most recent
        # cycle (ground_truth_prob was not None).  Used by the clear_urgent guard so
        # urgent T1 flags are only cleared when GT actually returned a usable signal
        # (not when GT returned None due to the series-mismatch buffer or no source).
        self._last_gt_evaluated_ids: frozenset = frozenset()
        # Timestamp of when this instance was created — used by the startup
        # stabilization guard to delay new trade entry until the first full
        # scan cycle has completed and in-memory state is populated.
        self._startup_time: float = time.time()
        self._load_positions()
        self._reconcile_with_exchange()
        self._validate_open_positions()

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
                      Rotating Tier 2 batch (covers all T2 in TIER_2_INTERVAL=150s)
                      Rotating Tier 3 batch (covers all T3 in TIER_3_INTERVAL)
                      Position monitoring
          Periodically: Discovery scan (full platform fetch, every DISCOVERY_INTERVAL=900s)

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

        # Active markets for this cycle = Tier 1 (all) + Tier 2 batch + Tier 3 batch.
        # T3 markets are included so that long-dated markets receive GT evaluation
        # every cycle instead of only during discovery.  The T3 batch is already
        # a small rotating slice (TIER_3_INTERVAL=1800s ÷ cycle), so the added
        # API load per cycle is modest.
        active_markets = t1_markets + t2_markets + (t3_markets if t3_batch_raw else [])
        summary["markets_scanned"] = len(active_markets)
        summary["t1_scanned"]      = len(t1_markets)
        summary["t2_scanned"]      = len(t2_markets)
        summary["t2_total"]        = len(self._registry.get_tier(2))
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

        # ── Fuzzy cross-platform scan (using cached pairs from discovery) ────
        # Uses Polymarket prices as GT for Kalshi markets via SequenceMatcher
        # title pairing.  Pairs are rebuilt at discovery intervals; this call
        # evaluates gaps using whatever pairs are currently cached.
        #
        # IMPORTANT: include ALL tiers (T1+T2+T3) on both sides, not just the
        # T1+T2 active batch.  Long-dated markets (T3, e.g. Trump presidency
        # Sep-2026) only appear in T3 between discovery cycles.  Using pair_eligible
        # (T1+T2 only) drops ~90 Polymarket markets every regular cycle and means
        # the Trump signal can never be found outside the 30-min discovery window.
        # The active_markets restriction above applies to GT-source evaluation
        # and order sizing; the fuzzy scan is price-discovery only — it can safely
        # reach into T3 and the urgency system will promote a T3 Kalshi market to
        # T1 when a gap is found.
        fuzzy_kalshi = [
            e.market
            for tier in (1, 2, 3)
            for e in self._registry.get_tier(tier)
            if e.market.platform == "kalshi"
        ]
        fuzzy_poly = [
            e.market
            for tier in (1, 2, 3)
            for e in self._registry.get_tier(tier)
            if e.market.platform == "polymarket"
        ]
        fuzzy_signals: list = []
        if fuzzy_kalshi and fuzzy_poly:
            try:
                fuzzy_signals = self._gap_detector.run_cross_platform_scan(
                    fuzzy_kalshi, fuzzy_poly
                )
            except Exception as fxp_exc:
                logger.warning(
                    "ResolutionBot: fuzzy cross-platform scan failed: %s", fxp_exc
                )

        # Urgent-promote markets with detected cross-platform gaps.
        for sig in cross_signals + fuzzy_signals:
            self._registry.mark_urgent(sig.market_to_buy.market_id)

        # ── Information signals (GT fetch for active markets) ───────────────
        info_signals = self._fetch_info_signals(active_markets)

        # Urgent-promote markets with detected info gaps.
        for sig in info_signals:
            self._registry.mark_urgent(sig.market_to_buy.market_id)

        # Persist sticky-T1 promotions so they survive a restart.
        self._save_sticky_t1()

        all_signals = cross_signals + fuzzy_signals + info_signals
        self._last_signals = all_signals
        summary["signals_flagged"] = len(all_signals)
        logger.info(
            "ResolutionBot: %d exact + %d fuzzy cross-platform + %d info signals = %d total "
            "(T1=%d T2_batch=%d/%d total_t2=%d)",
            len(cross_signals), len(fuzzy_signals), len(info_signals), len(all_signals),
            len(t1_markets), len(t2_markets),
            len(self._registry.get_tier(2)), len(all_t2_markets),
        )

        # ── Clear urgent flag for markets where the gap has closed ──────────
        # Only remove the urgent T1 override when GT was fetched and returned
        # a usable signal this cycle (ground_truth_prob is not None) but the
        # gap turned out to be below threshold.  If GT returned None (e.g.
        # GASREGCOVW asymmetric buffer fired, or no source covers the market),
        # we cannot conclude the gap has closed — keep the urgent flag alive.
        active_signal_ids = {s.market_to_buy.market_id for s in all_signals}
        for entry in list(self._registry.get_tier(1)):
            if entry.signal_urgent and entry.market_id not in active_signal_ids:
                if entry.market_id in self._last_gt_evaluated_ids:
                    self._registry.clear_urgent(entry.market_id)
                else:
                    logger.debug(
                        "TierRegistry: keeping urgent for %s — GT returned None "
                        "this cycle (buffer or no source); gap may still be open",
                        entry.market_id,
                    )

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
        t1_ms = TIER_1_INTERVAL * 1000
        if elapsed > t1_ms * CYCLE_ERROR_FRACTION:
            logger.error(
                "ResolutionBot: cycle took %.0fms — severely over Tier-1 interval (%ds). "
                "T1 markets are under-surveilled; investigate bottleneck.",
                elapsed, TIER_1_INTERVAL,
            )
        elif elapsed > t1_ms * CYCLE_WARN_FRACTION:
            logger.warning(
                "ResolutionBot: cycle took %.0fms — exceeded Tier-1 interval (%ds).",
                elapsed, TIER_1_INTERVAL,
            )
        return summary

    # ── Tiered scan helpers ───────────────────────────────────────────────────

    def _run_discovery(self) -> None:
        """
        Full platform fetch — discovers new markets and refreshes the registry.

        Called every DISCOVERY_INTERVAL.  Uses the existing scanner which is
        already configured with the correct per-platform window hours.

        Novelty prop-bet markets (announcers, word counts, etc.) are filtered
        OUT before registry ingest and added to the persistent exclusion list
        so they never consume T1 slots or reach the GT routing loop.
        """
        logger.info("ResolutionBot: running discovery scan (full platform fetch)")
        try:
            markets = self._scanner.scan()

            # Filter novelty markets before registry ingest.
            clean_markets = []
            novelty_count = 0
            for m in markets:
                if self._ground_truth.is_novelty_market(m):
                    # Persist the exclusion so re-discovery skips this market.
                    # TTL expires 1 day after the market's own resolution so the
                    # exclusion list doesn't grow unboundedly with dead entries.
                    ttl = m.hours_to_resolution * 3600 + 86400
                    self._exclusions.add(
                        m.platform, m.market_id,
                        reason="novelty_prop",
                        ttl_seconds=ttl if ttl > 0 else None,
                    )
                    novelty_count += 1
                else:
                    clean_markets.append(m)
            if novelty_count:
                logger.info(
                    "ResolutionBot: filtered %d novelty markets at ingest "
                    "(added to exclusion list)",
                    novelty_count,
                )

            counts = self._registry.ingest_many(clean_markets)
            logger.info(
                "ResolutionBot: discovery ingested %d markets → T1=%d T2=%d T3=%d "
                "(registry total=%d)",
                sum(counts.values()),
                counts.get(1, 0), counts.get(2, 0), counts.get(3, 0),
                len(self._registry),
            )

            # Restore sticky-T1 promotions from the previous session so recently-
            # promoted markets aren't re-tiered back to T2/T3 after a restart.
            self._restore_sticky_t1()

            # Rebuild fuzzy cross-platform pairs now that we have a fresh full
            # market list from both platforms.  Actual gap evaluation and signal
            # generation happens in run_once() (every T1 cycle) using the cached
            # pairs, so this rebuild only happens at DISCOVERY_INTERVAL.
            try:
                kalshi_markets  = [m for m in clean_markets if m.platform == "kalshi"]
                poly_markets    = [m for m in clean_markets if m.platform == "polymarket"]
                if kalshi_markets and poly_markets:
                    # Force pair rebuild by clearing the cache flag
                    if hasattr(self._gap_detector, "_cross_platform"):
                        self._gap_detector._cross_platform._last_built = None
                    self._gap_detector.run_cross_platform_scan(
                        kalshi_markets, poly_markets
                    )
            except Exception as xp_exc:
                logger.warning(
                    "ResolutionBot: cross-platform pair rebuild failed: %s", xp_exc
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

        # Coverage diagnostic counters — reset to 0 on every call; these are
        # per-cycle (per-batch) counts, never cumulative across cycles.
        n_novelty: int = 0              # novelty prop-bets filtered at ingest; always 0 here
        n_no_source_fast_skip: int = 0  # skipped instantly — no source claims this market
        n_no_source: int = 0            # source claimed it but fetch() returned None (slow)
        n_no_prob: int = 0              # source found but couldn't extract a probability
        n_covered: int = 0       # source returned a usable probability
        n_gap_too_small: int = 0 # covered but gap < minimum trading threshold
        coverage_sources: Dict[str, int] = {}  # source_name → count of markets covered

        # Per-source aggregate timing: source_name → cumulative fetch time (s)
        # Tracked only for the normal GT fetch path (not bracket cache hits).
        # Used at the end to log a "slow source" summary for cycle-time diagnosis.
        _source_timing: Dict[str, float] = {}
        _source_fetch_count: Dict[str, int] = {}

        # Markets with a valid GT signal this cycle (gt.ground_truth_prob is not None
        # OR gap was too small).  Used by run_once to gate the clear_urgent logic:
        # urgent T1 flags are only cleared when GT was evaluated successfully — not
        # when GT returned None (buffer, no source, or stale data).
        gt_evaluated: set = set()

        # Bracket GT cache: maps ticker prefix → first GT result for that bracket series.
        # Markets like KXAAAGASW-26MAR02-2.888 / -2.898 / -2.908 … all resolve against
        # the same underlying data point.  We fetch GT once for the first market in each
        # group, then re-derive the probability for every remaining bracket using the
        # cached raw value — no additional API calls needed.
        #
        # This cache is cycle-scoped (local variable) and covers ALL tiers together:
        # T1 and T2 bracket markets are both present in `active_markets` and are
        # processed in the same call, so a T1 bracket market that populates the cache
        # saves the FRED/EIA call for every other T1 or T2 sibling in that cycle.
        # The underlying FRED HTTP response is also cached for 5 min (_FRED_CACHE_TTL)
        # so even the first bracket lookup per cycle is usually served from memory.
        _bracket_gt: Dict[str, Optional["GroundTruthResult"]] = {}

        # ── Uniform-50 illiquid filter ────────────────────────────────────────
        # Real mispricings are bracket-specific: only the bracket(s) straddling the
        # current underlying value diverge from 50¢.  When every bracket in a series
        # simultaneously shows ~50¢ it means the series has never been traded and
        # Kalshi initialised all contracts with a placeholder 50-cent quote.
        # Acting on these produces ghost signals — the "gap" is just stale default
        # pricing, not a real market-vs-reality divergence.
        #
        # Rule: if more than 3 brackets in the same series all have yes_price within
        # ±0.02 of 0.50 (i.e. 0.48–0.52), flag the entire series as illiquid and
        # skip every bracket in it with reason='uniform_50_pricing'.
        _illiquid_prefixes: set = set()
        _prefix_prices: Dict[str, list] = {}
        for m in candidates:
            pfx = _bracket_prefix(m.market_id)
            if pfx is not None:
                _prefix_prices.setdefault(pfx, []).append(m.yes_price)
        for pfx, prices in _prefix_prices.items():
            near_50 = sum(1 for p in prices if abs(p - 0.50) <= 0.02)
            if near_50 > 3:
                _illiquid_prefixes.add(pfx)
                logger.info(
                    "ResolutionBot: series %s flagged illiquid — "
                    "%d/%d brackets priced within 2¢ of 0.50 "
                    "(uniform_50_pricing; skipping whole series)",
                    pfx, near_50, len(prices),
                )
        # ─────────────────────────────────────────────────────────────────────

        for i, market in enumerate(candidates, 1):
            if i % 25 == 0 or i == total:
                logger.info(
                    "ResolutionBot: ground truth progress %d/%d (signals so far: %d)",
                    i, total, len(signals),
                )

            prefix = _bracket_prefix(market.market_id)

            # ── Uniform-50 illiquid series skip ───────────────────────────────
            if prefix is not None and prefix in _illiquid_prefixes:
                logger.debug(
                    "ResolutionBot: skipping %s — series %s is illiquid "
                    "(uniform_50_pricing)",
                    market.market_id, prefix,
                )
                n_no_source += 1
                continue

            # ── Bracket deduplication path ────────────────────────────────────
            if prefix and prefix in _bracket_gt:
                ref_gt = _bracket_gt[prefix]
                if ref_gt is None:
                    # First market in this group had no source — skip siblings too.
                    n_no_source += 1
                    continue
                raw_val = (ref_gt.raw_data or {}).get("latest_value")
                if raw_val is None:
                    n_no_prob += 1
                    continue
                # Re-derive prob for this bracket's specific threshold.
                prob = self._ground_truth.recompute_bracket_prob(raw_val, market)
                if prob is None:
                    n_no_prob += 1
                    continue
                # Build a new validated GT result with the recalculated probability.
                new_gt = dc_replace(ref_gt, ground_truth_prob=prob)
                gt = self._ground_truth.validate_result(new_gt, market)

            # ── Normal GT fetch path ──────────────────────────────────────────
            else:
                if not self._ground_truth.can_any_source_handle(market):
                    # Fast-path skip: zero network calls, zero wait.
                    n_no_source_fast_skip += 1
                    _source_timing["no_source_fast_skip"] = _source_timing.get(
                        "no_source_fast_skip", 0.0
                    )  # stays 0.0 — pure in-memory, no elapsed time
                    _source_fetch_count["no_source_fast_skip"] = (
                        _source_fetch_count.get("no_source_fast_skip", 0) + 1
                    )
                    if prefix is not None:
                        _bracket_gt[prefix] = None
                    continue
                _gt_t0 = time.monotonic()
                gt = self._ground_truth.fetch(market)
                _gt_elapsed = time.monotonic() - _gt_t0
                if _gt_elapsed > 2.0:
                    logger.warning(
                        "ResolutionBot: slow GT fetch — %s took %.1fs",
                        market.market_id, _gt_elapsed,
                    )
                # Accumulate per-source timing for the end-of-cycle summary log.
                _src_key = gt.source_name if gt is not None else "no_source"
                _source_timing[_src_key] = _source_timing.get(_src_key, 0.0) + _gt_elapsed
                _source_fetch_count[_src_key] = _source_fetch_count.get(_src_key, 0) + 1

                if prefix is not None:
                    _bracket_gt[prefix] = gt
                if gt is None:
                    n_no_source += 1
                    continue
                if gt.ground_truth_prob is None:
                    n_no_prob += 1
                    continue

            # ── Per-market illiquid check ─────────────────────────────────────
            # The series-level filter above catches whole bracket series at 50¢.
            # This guard catches individual singletons (e.g. KXNASDAQ100-T23600)
            # where NQ is at 24,669 yet the market still shows ~49.5¢ because it
            # has never been traded (Kalshi placeholder price).
            #
            # Condition: yes_price within ±0.02 of 0.50 AND GT says the outcome is
            # near-certain (prob ≤ 0.10 or ≥ 0.90).  Real mispricings near certainty
            # get arbed quickly; a 95%+ GT-prob market stuck at 50¢ is almost always
            # untraded, not a real opportunity.
            if (
                abs(market.yes_price - 0.50) <= 0.02
                and gt.ground_truth_prob is not None
                and (gt.ground_truth_prob <= 0.10 or gt.ground_truth_prob >= 0.90)
            ):
                logger.info(
                    "ResolutionBot: skipping %s — illiquid single market "
                    "(yes_price=%.3f ≈ 0.50 but gt_prob=%.3f is extreme; "
                    "uniform_50_pricing)",
                    market.market_id, market.yes_price, gt.ground_truth_prob,
                )
                n_no_source += 1
                continue
            # ─────────────────────────────────────────────────────────────────

            n_covered += 1
            coverage_sources[gt.source_name] = coverage_sources.get(gt.source_name, 0) + 1

            signal = self._gap_detector.detect_information_signal(
                market, gt.ground_truth_prob
            )
            if signal:
                signal.ground_truth_prob = gt.ground_truth_prob
                signal.ground_truth_result = gt  # preserve real source confidence
                signals.append(signal)
                gt_evaluated.add(market.market_id)
            else:
                n_gap_too_small += 1
                gt_evaluated.add(market.market_id)

        # Emit a single summary line so operators can distinguish
        # "no data sources cover these markets" from "markets are efficiently priced".
        sources_str = (
            ", ".join(f"{src}={cnt}" for src, cnt in sorted(coverage_sources.items()))
            if coverage_sources else "none"
        )
        logger.info(
            "ResolutionBot: GT coverage summary — "
            "excluded_novelty=%d no_source_fast_skip=%d no_source=%d "
            "no_prob=%d covered=%d (sources: %s) "
            "gap_too_small=%d actionable=%d",
            n_novelty, n_no_source_fast_skip, n_no_source, n_no_prob,
            n_covered, sources_str, n_gap_too_small, len(signals),
        )

        # Aggregate per-source timing log (only real HTTP fetches, not cache hits).
        # Helps operators quickly identify which source is responsible for slow cycles.
        if _source_timing:
            timing_parts = sorted(
                (
                    (src, _source_fetch_count[src], _source_timing[src])
                    for src in _source_timing
                ),
                key=lambda x: -x[2],
            )
            timing_str = "  ".join(
                # Per-market average: no_source_fast_skip:N×0.0s vs no_source:N×4.7s
                # makes the cost-per-market directly comparable across sources.
                f"{src}:{cnt}x{elapsed / max(cnt, 1):.1f}s"
                for src, cnt, elapsed in timing_parts
            )
            logger.info("ResolutionBot: GT source timing — %s", timing_str)

        # Persist evaluated set so run_once can guard the clear_urgent logic.
        self._last_gt_evaluated_ids = frozenset(gt_evaluated)

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
            # Sort: prefer actionable markets (YES price 0.15–0.85) over near-certain
            # ones (price > 0.85 or < 0.15).  A 99% gap on a 0.99-priced market is
            # almost always a data-quality mismatch rather than a real edge — the
            # market is already near-resolved and likely correctly priced at that
            # extreme.  The genuine opportunities sit in the uncertain middle where
            # the GT data and market price meaningfully disagree.
            bucket_sigs.sort(
                key=lambda s: (
                    0 if 0.15 <= s.target_price <= 0.85 else 1,  # actionable first
                    -s.effective_gap,
                )
            )
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
        if _is_in_orderbook_cooldown(mid):
            logger.debug(
                "ResolutionBot: %s in order-book cooldown — skipping this cycle", mid
            )
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
            # creates unfillable limit orders. Skip entirely and suppress for 30 min.
            logger.info(
                "ResolutionBot: SKIP %s – order book empty, cannot verify scanner "
                "price %.3f (would create unfillable limit order)",
                mid, signal.target_price,
            )
            _set_orderbook_cooldown(mid, minutes=30)
            self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
            return None

        # For cross-platform signals, populate depth_ratio now so the confidence
        # scorer can apply the liquidity penalty before deciding to pass/skip.
        if signal.signal_type == "cross_platform":
            signal.depth_ratio = self._compute_depth_ratio(signal, ob_live)

        # Confidence gate
        score = self._confidence.score(market, gt, signal)
        is_cross = signal.signal_type == "cross_platform"
        if not score.passes:
            # Ghost-mode exception for cross-platform signals.
            # The standard 0.80 gate blocks many valid cross-platform pairs
            # because resolution_clarity scores political / geopolitical markets
            # at 0.65–0.70 (subjective resolution risk).  In dry_run mode we
            # want these to fire as ghost trades so we can validate whether the
            # fuzzy title-matching is actually finding the same underlying
            # question before ever committing real money to this signal type.
            ghost_exception = (
                self._dry_run
                and is_cross
                and score.source_confidence  >= CONFIDENCE_GATE_GHOST_CROSS_PLATFORM
                and score.resolution_clarity >= CONFIDENCE_GATE_GHOST_CROSS_PLATFORM
            )
            if ghost_exception:
                logger.warning(
                    "ResolutionBot: CROSS-PLATFORM GHOST TRADE FIRED: %s "
                    "source=%.2f clarity=%.2f (ghost gate=%.2f, live gate=%.2f) "
                    "kalshi=%.3f polymarket=%.3f gap=%.1f%% — "
                    "MANUALLY VERIFY SAME QUESTION BEFORE GOING LIVE",
                    mid,
                    score.source_confidence,
                    score.resolution_clarity,
                    CONFIDENCE_GATE_GHOST_CROSS_PLATFORM,
                    CONFIDENCE_GATE_LIVE,
                    signal.target_price,
                    signal.reference_price,
                    signal.effective_gap * 100,
                )
                # Fall through — fire as a ghost trade for accuracy tracking.
            else:
                logger.info(
                    "ResolutionBot: SKIP %s – %s", mid, score.skip_reason
                )
                return None

        # ── Time-adjusted gap gate ─────────────────────────────────────────────
        # Require a larger mispricing for early entries: the further from
        # resolution, the more time for the world to change and the edge to
        # disappear.  A 9.5% gap at 3.8 h (min=9.7%) is NOT the same edge as
        # 9.5% at 30 min (min=4.4%).  This gate runs after confidence (so a
        # high-confidence early signal still needs a real gap) and before the
        # order-book + EV checks (no point fetching books for a gap we'll reject).
        #
        # Cross-platform ghost bypass: in dry_run mode, cross-platform signals
        # skip this gate entirely.  The gap formula grows unboundedly (>24h needs
        # 97%+), which would block all long-dated cross-platform pairs like Trump
        # presidency markets before we can validate whether they're profitable.
        # Ghost trades are zero-risk; we want the track record.  Live execution
        # still enforces the full gate.
        _min_gap = _minimum_gap_for_entry(market.hours_to_resolution)
        _cross_ghost_bypass = self._dry_run and is_cross
        if not _cross_ghost_bypass and signal.effective_gap < _min_gap:
            logger.info(
                "ResolutionBot: SKIP %s — gap %.1f%% below time-adjusted "
                "minimum %.1f%% (%.2fh remaining)",
                mid,
                signal.effective_gap * 100,
                _min_gap * 100,
                market.hours_to_resolution,
            )
            return None
        if _cross_ghost_bypass and signal.effective_gap < _min_gap:
            logger.info(
                "ResolutionBot: CROSS-PLATFORM GHOST GAP BYPASS %s — "
                "gap %.1f%% below live minimum %.1f%% (%.2fh remaining) "
                "— recording ghost trade for accuracy validation",
                mid,
                signal.effective_gap * 100,
                _min_gap * 100,
                market.hours_to_resolution,
            )
        # ──────────────────────────────────────────────────────────────────────

        # ── Human review check (LARGE_DIVERGENCE) ─────────────────────────────
        # When the router flagged requires_human_review=True it means gap > 40%.
        # Previously this was handled by capping confidence to 0.70 (blocking the
        # signal entirely).  Now confidence is preserved and we decide here:
        #   • dry_run / ghost-trade mode → log and continue as a ghost trade so we
        #     can track whether these large-gap signals would have been correct.
        #   • live mode → alert via Telegram, stash in _pending_human_review, skip.
        #     The trade executes only when approve_human_review(market_id) is called.
        requires_review = bool(
            gt is not None
            and gt.raw_data.get("requires_human_review", False)
        )
        if requires_review:
            gap_pct = gt.raw_data.get("validator_gap_pct", 0.0) if gt else 0.0
            if self._dry_run:
                logger.info(
                    "ResolutionBot: LARGE_DIVERGENCE ghost trade for %s "
                    "(gt_prob=%.2f market_price=%.2f gap=%.1f%%) — "
                    "firing ghost trade for accuracy tracking",
                    mid,
                    gt.ground_truth_prob if gt else float("nan"),
                    signal.target_price,
                    gap_pct,
                )
                # Fall through and fire as a ghost trade below.
            else:
                # Live mode: alert operator and hold for manual approval.
                logger.warning(
                    "ResolutionBot: LARGE_DIVERGENCE signal for %s requires human "
                    "review (gt_prob=%.2f market_price=%.2f gap=%.1f%%) — "
                    "pending approval, NOT auto-trading",
                    mid,
                    gt.ground_truth_prob if gt else float("nan"),
                    signal.target_price,
                    gap_pct,
                )
                self._pending_human_review[mid] = signal
                if self._alert_manager is not None and gt is not None:
                    self._alert_manager.alert_human_review(
                        market_id=mid,
                        question=market.question,
                        action=signal.action,
                        target_price=signal.target_price,
                        gt_prob=gt.ground_truth_prob or 0.0,
                        gap_pct=gap_pct,
                        source_name=gt.source_name,
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
            if live_effective_gap < _minimum_gap_for_entry(market.hours_to_resolution):
                logger.info(
                    "ResolutionBot: SKIP %s – stale scanner %.3f → live %.3f, "
                    "recalc gap %.3f below time-adjusted threshold (no real edge)",
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
        if live_effective_gap < _minimum_gap_for_entry(market.hours_to_resolution):
            logger.info(
                "ResolutionBot: SKIP %s – live effective_gap=%.3f below "
                "time-adjusted threshold (fee=%.4f target=%.3f gt=%.3f)",
                mid, live_effective_gap, fee, signal.target_price, gt_prob_for_gap,
            )
            if fee > signal.taker_fee:
                # Fee increased since signal generation — exclude to avoid repeat surprises
                self._exclusions.add_fee_surprise(market.platform, mid)
            return None
        # Update signal with live-computed values so _compute_size gets consistent data
        signal.effective_gap = live_effective_gap
        signal.taker_fee = fee

        # Minimum expected-value check: require at least MIN_EV cents of edge per
        # contract.  Filters out near-exhausted trades (e.g. BUY NO @99¢ where
        # the max win is 1¢ but the GT model error could easily swallow it).
        _ev_passes, _ev = _check_minimum_ev(
            signal.target_price, gt_prob_for_gap, signal.action
        )
        if not _ev_passes:
            logger.info(
                "ResolutionBot: SKIP %s – EV %.3f below minimum %.2f "
                "(action=%s price=%.3f gt_prob=%.3f)",
                mid, _ev, MIN_EV,
                signal.action, signal.target_price, gt_prob_for_gap,
            )
            return None
        logger.info(
            "ResolutionBot: EV check PASS %s — EV=%.3f "
            "(action=%s price=%.3f gt_prob=%.3f)",
            mid, _ev, signal.action, signal.target_price, gt_prob_for_gap,
        )

        # Size using fractional Kelly (time-to-resolution weighting applied inside)
        size_usd = self._compute_size(
            signal, score.source_confidence, score.resolution_clarity
        )
        if size_usd < 1.0:
            logger.info("ResolutionBot: SKIP %s – size too small ($%.2f)", mid, size_usd)
            return None

        # ── Per-series exposure cap ────────────────────────────────────────────
        # All markets sharing a root ticker (e.g. KXPAYROLLS) are driven by the
        # same underlying data point and are 100% correlated.  Cap total active
        # exposure per root-series at MAX_SERIES_EXPOSURE_FRACTION (15%).
        series_root = _series_root(mid)
        if series_root is not None:
            series_exposure = sum(
                r.size_usd for r_id, r in self._positions.items()
                if _series_root(r_id) == series_root
            )
            max_series    = self._bankroll.total_usd * MAX_SERIES_EXPOSURE_FRACTION
            existing_pct  = series_exposure / self._bankroll.total_usd * 100
            if series_exposure + size_usd > max_series:
                logger.info(
                    "ResolutionBot: SKIP %s — series exposure cap reached "
                    "(%.0f%% already in %s, max %d%%)",
                    mid, existing_pct, series_root,
                    round(MAX_SERIES_EXPOSURE_FRACTION * 100),
                )
                return None
        # ──────────────────────────────────────────────────────────────────────

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
                _set_orderbook_cooldown(mid, minutes=30)
                self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
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
                _set_orderbook_cooldown(mid, minutes=30)
                self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
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

    def approve_human_review(self, market_id: str) -> bool:
        """Approve a pending human-review signal and attempt to execute it.

        Called externally — e.g. from a Telegram command handler — when the
        operator has inspected a LARGE_DIVERGENCE signal and decided to trade.

        Clears the requires_human_review flag so _try_execute doesn't re-block
        it, then calls _try_execute directly.  Returns True if a pending signal
        was found, False if no signal was waiting for this market_id.

        Note: _try_execute may still skip the trade for other reasons (order
        book empty, fee too high, etc.) — those cases return True (signal was
        found and attempted) but no position is added.
        """
        signal = self._pending_human_review.pop(market_id, None)
        if signal is None:
            logger.info(
                "ResolutionBot: approve_human_review — no pending signal for %s",
                market_id,
            )
            return False

        logger.info(
            "ResolutionBot: human review APPROVED for %s — attempting execution",
            market_id,
        )

        # Clear the requires_human_review flag so the signal passes through
        # the live-mode block we just approved it past.
        if signal.ground_truth_result is not None:
            cleared_raw = {
                **signal.ground_truth_result.raw_data,
                "requires_human_review": False,
            }
            signal = dc_replace(
                signal,
                ground_truth_result=dc_replace(
                    signal.ground_truth_result, raw_data=cleared_raw
                ),
            )

        self._try_execute(signal)
        return True

    def _place_order(
        self, market: Market, signal: GapSignal, size_usd: float, fee: float,
        limit_price: Optional[float] = None,
    ) -> Optional[str]:
        # limit_price: the taker limit (live ask for YES, live bid for NO).
        # Falls back to signal.target_price only when not provided (dry-run log).
        order_price = limit_price if limit_price is not None else signal.target_price
        if self._dry_run:
            ghost_id = f"ghost_{market.market_id}_{int(time.time())}"
            logger.info(
                "ResolutionBot [GHOST TRADE]: %s %s @ %.4f size=$%.2f fee=%.4f "
                "(simulated — no real order placed, order_id=%s)",
                signal.action, market.market_id, order_price, size_usd, fee, ghost_id,
            )
            return ghost_id

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
                self._exit_position(
                    mid, decision.current_gain_usd,
                    current_price=decision.position.current_price,
                    capture=decision.capture_ratio,
                )
                exits += 1
        return exits

    def _exit_position(
        self,
        market_id: str,
        realized_pnl_usd: float,
        current_price: Optional[float] = None,
        capture: Optional[float] = None,
    ) -> None:
        rec = self._positions.pop(market_id, None)
        if not rec:
            return
        self._bankroll.release(market_id, realized_pnl_usd=realized_pnl_usd)

        # Record in resolved history (session-only; drives 'history' command).
        exit_price = current_price
        if exit_price is None and rec.size_usd > 0:
            offset = realized_pnl_usd / rec.size_usd
            raw = (rec.entry_price + offset) if rec.action == "buy_yes" else (rec.entry_price - offset)
            exit_price = max(0.0, min(1.0, raw))
        if capture is None:
            if rec.action == "buy_yes":
                theo = (rec.ground_truth_prob - rec.entry_price) * rec.size_usd
            else:
                theo = (rec.entry_price - rec.ground_truth_prob) * rec.size_usd
            capture = realized_pnl_usd / theo if theo > 1e-6 else 0.0
        src = "unknown"
        if rec.signal and rec.signal.ground_truth_result:
            src = rec.signal.ground_truth_result.source_name
        elif rec.signal and rec.signal.signal_type == "cross_platform":
            src = "cross-platform"
        self._resolved_positions.append(ResolvedPosition(
            market_id=market_id,
            action=rec.action,
            size_usd=rec.size_usd,
            entry_price=rec.entry_price,
            exit_price=exit_price or rec.entry_price,
            pnl=realized_pnl_usd,
            capture=capture,
            confidence=rec.source_confidence,
            source=src,
            resolved_at=datetime.utcnow(),
        ))

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

    def get_resolved_positions(self) -> List[ResolvedPosition]:
        """Return all trades resolved this session (exits from decay monitor)."""
        return list(self._resolved_positions)

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

    def get_near_miss_pairs(self, n: int = 10) -> tuple:
        """
        Return (results[:n], stats) for near-miss cross-platform pairs.

        Uses all markets currently in the tier registry (no new API calls).
        Run at least one scan cycle first so the registry is populated.
        """
        markets = self._registry.all_markets()
        return self._scanner.score_near_miss_pairs(markets, top_n=n)

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
        """Persist all open positions to the state store (called on every open/close).

        In ghost-trade (dry_run) mode, positions exist only for the current
        session — they are intentionally NOT written to disk so that each
        restart begins with a clean slate.
        """
        if self._dry_run or not self._state:
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
        """Reload open positions from the state store on startup.

        In ghost-trade (dry_run) mode, positions are session-scoped — we do
        NOT reload from disk so each run starts fresh with a clean ledger.
        """
        if self._dry_run or not self._state:
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
                if not self._dry_run and isinstance(order_id, str) and (
                    order_id.startswith("dry_") or order_id.startswith("ghost_")
                ):
                    logger.info(
                        "ResolutionBot: skipping phantom position %s "
                        "(saved from a ghost-trade/dry-run session, now running live)", mid,
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

    def _save_sticky_t1(self) -> None:
        """Persist sticky-T1 market IDs to the state store so they survive restarts.

        Always saved regardless of dry_run — sticky_t1 tracks signal history,
        not open positions, so the information is valuable across sessions.
        """
        if not self._state:
            return
        ids = self._registry.get_sticky_market_ids()
        self._state.set("sticky_t1_markets", ids)
        if ids:
            logger.debug("ResolutionBot: saved %d sticky-T1 market ID(s)", len(ids))

    def _restore_sticky_t1(self) -> None:
        """Re-apply sticky_t1=True for markets saved from the previous session."""
        if not self._state:
            return
        saved_ids = self._state.get("sticky_t1_markets", [])
        if saved_ids:
            self._registry.restore_sticky_t1(saved_ids)

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

    def _validate_open_positions(self) -> None:
        """
        On startup, re-run GT fetch for all open positions and flag any where the
        current ground truth probability has flipped more than 0.50 since entry.

        A flip > 0.50 (e.g. entry gt_prob=1.0 but now gt_prob=0.0) most often
        means a direction-parsing bug was present at entry time, or the underlying
        instrument moved dramatically between sessions.  The position is NOT
        auto-closed — it's marked requires_human_review=True and logged at ERROR
        so the operator sees it immediately on the next startup.
        """
        if not self._positions:
            return
        flagged = 0
        for mid, rec in self._positions.items():
            try:
                current_gt = self._ground_truth.fetch(rec.market)
                if current_gt is None or current_gt.ground_truth_prob is None:
                    continue
                delta = abs(current_gt.ground_truth_prob - rec.ground_truth_prob)
                if delta > 0.50:
                    rec.requires_human_review = True
                    logger.error(
                        "ResolutionBot [STARTUP VALIDATION]: %s — gt_prob FLIPPED "
                        "%.2f → %.2f (delta=%.2f > 0.50). "
                        "Position marked requires_human_review=True. "
                        "Verify direction and close manually if needed. "
                        "action=%s entry_price=%.3f",
                        mid,
                        rec.ground_truth_prob,
                        current_gt.ground_truth_prob,
                        delta,
                        rec.action,
                        rec.entry_price,
                    )
                    flagged += 1
            except Exception as exc:
                logger.warning(
                    "ResolutionBot [STARTUP VALIDATION]: GT fetch failed for %s: %s",
                    mid, exc,
                )
        if flagged:
            logger.error(
                "ResolutionBot [STARTUP VALIDATION]: %d position(s) require human "
                "review — gt_prob flipped >50%% since entry.  "
                "Run `positions` command to see details.",
                flagged,
            )
        else:
            logger.info(
                "ResolutionBot [STARTUP VALIDATION]: %d open position(s) checked — "
                "no large gt_prob flips detected",
                len(self._positions),
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
