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
import os
import random
import re
import time

import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo as _ZoneInfo  # type: ignore[no-redef]

from data.markets.kalshi_ws import KalshiWebSocket
from data.ground_truth.base import GT_FRESHNESS_SECONDS, GroundTruthResult, SourceType
from data.ground_truth.economic_fred import FREDEconomicSource
from data.ground_truth.financial import FinancialDataSource, _PRICE_CACHE as _FINANCIAL_PRICE_CACHE
from data.ground_truth.router import GroundTruthRouter, is_paper_only
from data.release_calendar import FREDReleaseCalendar
from data.sports.live_game_monitor import refresh_if_stale as refresh_sports_cache
from data.sports.shock_detector import run_shock_detection
from data.sports import resolution_detector as _res_det
from data.sports.resolution_detector import ResolutionSignal, drain_ghost_exits as _drain_ghost_exits
from data.markets.base import BaseMarketClient, FillResult, Market, Order, OrderBook, Side
from data.markets.kalshi import _is_game_market, _is_financial_bracket_market
from data.markets.polymarket_ws import PolymarketWSManager
from monitoring import gate_names as gn
from monitoring.alerts import AlertManager
from monitoring.gate_events import log_gate_event
from resolution.confidence import ConfidenceScorer
from resolution.decay_monitor import (
    DecayAction, DecayMonitor, OpenResolutionPosition,
)
from resolution.gap_detector import GapDetector, GapSignal, SLIPPAGE_BUFFER
from resolution.priority import PriorityScorer, _SERIES_TO_SYMBOL, _parse_strike
from resolution.scanner import ResolutionScanner
from resolution.tier_registry import TierRegistry
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache
from shared.paper_log import PaperTradeLog
from strategies.weather_snipe import SnipeSignal
from utils.storage import StateStore

logger = logging.getLogger(__name__)

KELLY_FRACTION = 0.12             # 12% fractional Kelly (conservative for thin books)
MAX_POSITION_FRACTION = 0.20      # hard cap: 20% of total bankroll per position

# Ghost-mode sizing cap: Kelly sizing uses this as the effective bankroll ceiling
# regardless of how much simulated P&L has accumulated.  The actual bankroll
# total_usd continues to grow for tracking purposes — this cap only constrains
# what feeds into position sizing, preventing compounding from inflating positions.
GHOST_SIZING_BANKROLL_CAP = 500.0

# Re-entry cooldown after any exit (market-level and series-level).
# Prevents the bot from immediately re-entering the same market or same series
# bracket after an exit, which was causing bankroll-compounding spiral trades.
_EXIT_COOLDOWN_SECONDS = 1800     # 30 minutes

# Hard stop for financial-source positions (Yahoo Finance / Twelve Data / Alpha Vantage).
# If the market price moves this many points against our entry, exit immediately —
# before the decay monitor even runs.  Overridable via FINANCIAL_HARD_STOP_THRESHOLD env var.
FINANCIAL_HARD_STOP_THRESHOLD = 0.20

# Spread gate for _get_current_price().
# When both YES ask and YES bid are present but the spread is wider than this
# threshold, the mid-price is meaningless (e.g. bid=0.006, ask=0.997 → mid=0.50).
# Returning None causes all downstream consumers (hard stop, decay monitor,
# stale drift check) to skip gracefully rather than act on a garbage price.
# 0.85 is the widest spread where the mid still carries directional signal:
# bid=0.10 / ask=0.95 → spread=0.85, mid=0.525 (marginal but usable).
ILLIQUID_SPREAD_THRESHOLD = 0.85

# Source-name prefixes that identify a financial ground-truth source.
_FINANCIAL_SOURCE_PREFIXES = ("Yahoo Finance/", "Twelve Data/", "Alpha Vantage/")

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

# Hard per-cycle batch caps.  Regardless of how the batch-size math shakes
# out (e.g. scan_interval > TIER_N_INTERVAL collapsing cycles_per_interval
# to 1), no single cycle will attempt to refresh more than this many markets
# in a given tier.  Keeps a misconfigured interval from blowing up cycle time.
TIER_1_MAX_BATCH_SIZE = 50
TIER_2_MAX_BATCH_SIZE = 150
TIER_3_MAX_BATCH_SIZE = 30

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

# How many consecutive cycles a market must return no_source (slow path) before it
# is promoted to the permanent-skip list and treated like no_source_fast_skip (0 ms).
# At 5 cycles × ~0.1s × 500 markets = 250s of wasted fetch time eliminated.
_NO_SOURCE_STREAK_THRESHOLD = 5

# How many consecutive cycles a market must fail can_any_source_handle() before
# it is permanently skipped (bypasses the router entirely, not just fetch()).
# 3 consecutive "no source exists" signals is a hard miss — no source will ever
# handle this market until new GT sources are added.
FAST_SKIP_PERM_THRESHOLD = 3

# Stale-price guard: relative + absolute-floor approach.
# threshold = max(scanner_price × RELATIVE, FLOOR)
# A 25¢ market allows ~4¢ drift (floor); a 90¢ market allows 13.5¢ drift.
# This catches the dangerous case where a cheap market moves 48% but only 12¢.
STALE_PRICE_RELATIVE_THRESHOLD = 0.15   # 15% of scanner price
STALE_PRICE_ABSOLUTE_FLOOR     = 0.04   # minimum 4¢ absolute floor

# Maximum distance (as a fraction of underlying price) between a financial
# bracket market's strike and the current underlying before we skip GT
# evaluation entirely.  Prevents trading "NQ above 22,600" when NQ is 24,000.
_BRACKET_PROXIMITY_MAX = 0.05      # 5% deep-OTM gate

# Ghost mode: cancel pending ghost orders that haven't been filled within this
# many minutes.  Only applies to positions entered with an empty order book
# (bracket bypass).
_GHOST_FILL_TIMEOUT_MINUTES = 10

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
# Mirrors MIN_CONFIDENCE_THRESHOLD env var. Used in log messages only —
# actual gate is set via ConfidenceScorer(threshold=...) in __init__.
CONFIDENCE_GATE_LIVE                = 0.65
# LOOSENED for diagnostic visibility (was 0.50) — let cross-platform ghosts
# fire at low confidence so executor mechanics can be verified end-to-end.
CONFIDENCE_GATE_GHOST_CROSS_PLATFORM = 0.30

# Per-series exposure cap: all markets sharing a root ticker (e.g. KXPAYROLLS)
# are driven by the same underlying data point and are 100% correlated.  Without
# a cap a single bad FRED observation can commit the entire bankroll across all
# strike levels and expiry dates.  Cap at 15% of bankroll across the root series.
MAX_SERIES_EXPOSURE_FRACTION = 0.15
# Ghost mode uses a looser cap so paper-trade validation can accumulate enough
# positions per series to be statistically meaningful.  The cap still prevents
# 100% concentration in one series; 50% is the limit.
MAX_SERIES_EXPOSURE_FRACTION_GHOST = 0.50

# ── Liquidity realism guards ─────────────────────────────────────────────────
# These prevent ghost-mode PnL inflation from fills that would never execute
# on a real exchange.

# Minimum cost per contract.  When buying YES at $0.01 for $500, you'd get
# 50,000 contracts — but no real book has that depth at a penny.  Skip any
# trade where the effective cost per contract is below this floor.
# For BUY YES: cost = entry_price.  For BUY NO: cost = 1 - entry_price.
MIN_EFFECTIVE_ENTRY_PRICE = 0.05   # 5 cents

# Hard floor on YES price for entry, regardless of side. Defense-in-depth
# against extreme-price entries where:
#   - Orderbook depth in ghost mode is unrealistic
#   - Kelly contract counts explode at extreme prices
#   - Stale GT data has the largest impact in absolute terms
# This is a backstop, not a substitute for correct EV / fee modeling.
# LOOSENED for ghost-mode edge discovery (was 0.02) — revert if losing
MIN_ENTRY_YES_PRICE = 0.01
# LOOSENED for ghost-mode edge discovery (was 0.98) — revert if losing
MAX_ENTRY_YES_PRICE = 0.99

# Market-conviction filter for LARGE_DIVERGENCE signals.
# When the market has converged to extreme certainty, trust the market over GT.
EXTREME_CONVICTION_LOW  = 0.05   # market prices near-certain NO
EXTREME_CONVICTION_HIGH = 0.95   # market prices near-certain YES


def _check_minimum_ev(
    market_price: float, gt_prob: float, action: str
) -> "tuple[bool, float]":
    """
    Compute absolute expected value per contract and check against MIN_EV.

    market_price is always the YES price (0–1).

    BUY YES: buying YES at yes_price p per contract.
        YES wins with probability g; payout = 1.0 per contract.
        EV = g * 1.0 - p  =  g - p
        ⟹  ev = gt_prob - market_price

    BUY NO: buying NO at cost (1 - yes_price) per contract.
        NO wins with probability (1 - g); payout = 1.0 per contract.
        EV = (1-g) * 1.0 - (1 - p)  =  p - g
        ⟹  ev = market_price - gt_prob
        NOTE: market_price here is the YES price, NOT the cost of NO.
              The old formula used (1-g) - p which treated the YES price
              as if it were the NO cost — that was wrong and let every
              cheap-YES / large-NO-edge trade through with fake +EV.

    EV is dollars of edge per contract, independent of position size.

    Returns (passes_gate, ev).

    Validation examples:
      BUY YES  gt=0.99, yes=0.50   → EV = 0.99-0.50  = +0.49  → PASS  ✓
      BUY YES  gt=0.99, yes=0.9991 → EV = 0.99-0.9991 = -0.009 → BLOCK ✓
      BUY NO   gt=0.05, yes=0.0002 → EV = 0.0002-0.05 = -0.050 → BLOCK ✓
      BUY NO   gt=0.05, yes=0.50   → EV = 0.50-0.05   = +0.45  → PASS  ✓
      BUY NO   gt=0.95, yes=0.99   → EV = 0.99-0.95   = +0.04  → PASS  ✓
    """
    if action == "buy_yes":
        # Buying YES at yes_price; wins with probability gt_prob.
        ev = gt_prob - market_price
    else:  # buy_no
        # Buying NO at cost (1 - yes_price); wins with probability (1 - gt_prob).
        # EV = (1 - gt_prob) * 1.0 - (1 - market_price)  =  market_price - gt_prob
        ev = market_price - gt_prob
    return ev >= MIN_EV, ev


# ── Price refresh TTL cache ────────────────────────────────────────────────────
# Tracks the last time each market_id had a get_market() call for price refresh.
# Markets that are genuinely at 0.50 live would otherwise be refreshed every
# cycle.  Skip if refreshed within the last 120 seconds.
_price_refresh_cache: Dict[str, Tuple[float, float]] = {}   # market_id → (monotonic_ts, refreshed_price)
_PRICE_REFRESH_TTL_S: float = 120.0


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


def _is_us_market_hours() -> bool:
    """Return True if the current wall-clock time is within regular US equity
    market hours (09:30–16:00 Eastern, Monday–Friday)."""
    from datetime import time as _time
    _now = datetime.now(_ZoneInfo("America/New_York"))
    if _now.weekday() >= 5:       # Saturday=5, Sunday=6
        return False
    return _time(9, 30) <= _now.time() <= _time(16, 0)


# Map trader-initiated exit reasons to fill_cap path tags. Resolution exits
# (game_final) and order cancels (unfilled_timeout) are exempt — they do not
# consume book depth and never reach the fill cap.
_FILL_CAP_EXIT_PATHS = {
    "early_exit":   "exit_early",
    "stop_loss":    "exit_stoploss",
    "resolution":   "exit_approach",
    "hard_stop":    "exit_hardstop",
}


def _book_top3_snapshot(ob: OrderBook, side: Side) -> List[dict]:
    """Top 3 levels of the consumed book side as JSON-serializable dicts.
    Used as `book_snapshot` in fill_cap gate event payloads."""
    levels = ob.yes_asks if side == Side.YES else ob.yes_bids
    return [
        {"price": round(lvl.price, 4), "size": round(lvl.size, 2)}
        for lvl in levels[:3]
    ]


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
    # Distance of the underlying price from the market's resolution threshold at
    # entry time (raw_data["margin_pct"]).  Stored here so the decay monitor can
    # reference it on every cycle without touching the signal object.
    distance_pct: float = 0.0
    entry_time: float = field(default_factory=time.time)
    order_id: Optional[str] = None
    requires_human_review: bool = False   # set on startup if gt_prob flipped >50% since entry
    fill_status: str = "filled"           # "filled" | "pending" | "cancelled"
    limit_price_used: Optional[float] = None  # limit at placement (pending-fill tracking)


@dataclass
class ResolvedPosition:
    """A closed trade — appended to _resolved_positions for session-level history."""
    market_id: str
    action: str          # "buy_yes" | "buy_no"
    size_usd: float      # dollars wagered (cost basis)
    entry_price: float   # YES price at entry
    exit_price: float    # YES price at exit (always live market price; see exit_was_decisive_gt for settlement semantics)
    num_contracts: float # contracts held = size_usd / entry_price (YES) or / (1-entry_price) (NO)
    pnl: float           # realized P&L in dollars
    capture: float       # pnl / theoretical_max  (positive = with the signal)
    confidence: float    # source_confidence at entry
    source: str          # e.g. "FRED/PAYEMS", "cross-platform"
    resolved_at: datetime
    entered_at: Optional[datetime] = None      # when the trade was opened
    dynamic_exit_threshold: Optional[float] = None  # capture threshold that triggered exit
    exit_was_decisive_gt: bool = False  # True when GT was both decisive (≤0.02 or ≥0.98) AND fresh at exit


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


def _print_paper_summary(summary: dict) -> None:
    """Print a ghost-trade daily summary block to stdout."""
    sep = "-" * 52
    date_str  = summary.get("date_str", "today")
    entries     = summary.get("total_entries", 0)
    exits       = summary.get("exits", 0)
    unfilled    = summary.get("unfilled_orders", 0)
    cap_blocked = summary.get("cap_blocked", 0)
    wins        = summary.get("wins", 0)
    losses    = summary.get("losses", 0)
    drawdowns = summary.get("drawdowns", 0)
    win_rate  = summary.get("win_rate", 0.0)
    total_pnl = summary.get("total_pnl", 0.0)
    avg_pnl   = summary.get("avg_pnl_per_trade", 0.0)
    avg_gap   = summary.get("avg_gap_at_entry", 0.0)
    avg_hold  = summary.get("avg_hold_minutes", 0.0)
    best      = summary.get("best_trade")
    worst     = summary.get("worst_trade")
    by_src    = summary.get("by_source", {})

    print(f"\n{sep}")
    print(f"  GHOST TRADE DAILY SUMMARY  [{date_str}]")
    print(sep)
    cap_str = f"   Cap-blocked: {cap_blocked}" if cap_blocked else ""
    unfilled_str = f"   Unfilled: {unfilled}" if unfilled else ""
    print(f"  Entries: {entries}   Exits: {exits}{unfilled_str}{cap_str}")
    if exits > 0:
        drawdown_str = f"  Drawdowns: {drawdowns}" if drawdowns else ""
        print(f"  Wins: {wins}  Losses: {losses}{f'  ' + drawdown_str if drawdown_str else ''}")
        print(f"  Win rate: {win_rate:.1%}")
        print(f"  Total P&L:  ${total_pnl:+.2f}   Avg/trade: ${avg_pnl:+.2f}")
        print(f"  Avg gap at entry: {avg_gap:.1%}   Avg hold: {avg_hold:.0f}min")
        if best:
            print(f"  Best:  {best['market_id']}  ${best['pnl']:+.2f}  ({best['exit_reason']})")
        if worst:
            print(f"  Worst: {worst['market_id']}  ${worst['pnl']:+.2f}  ({worst['exit_reason']})")
        if by_src:
            print(f"  By source:")
            for src, stats in sorted(by_src.items()):
                n   = stats["trades"]
                pnl = stats["pnl"]
                w   = stats["wins"]
                print(f"    {src:<35} {n:>3} trades  ${pnl:>+7.2f}  {w}/{n} wins")
    print(sep)
    print()


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
        dynamic_exit_enabled: bool = True,
        calendar: Optional[FREDReleaseCalendar] = None,
        force_test: bool = False,
        min_confidence: float = 0.45,
        min_gap: float = SLIPPAGE_BUFFER,
        kalshi_ws: Optional[KalshiWebSocket] = None,
    ) -> None:
        self._kalshi = kalshi_client
        self._kalshi_ws = kalshi_ws
        self._poly = poly_client
        self._fee_cache = fee_cache
        self._bankroll = bankroll
        self._exclusions = exclusions
        self._dry_run = dry_run
        self._force_test = force_test
        self._scan_interval = scan_interval
        self._state = state_store
        self._alert_manager: Optional[AlertManager] = alert_manager
        self._calendar: Optional[FREDReleaseCalendar] = calendar

        # Append-only ghost-trade journal (dry-run only).
        self._paper_log: Optional[PaperTradeLog] = (
            PaperTradeLog() if dry_run else None
        )

        self._ground_truth = GroundTruthRouter()

        # Grab the FREDEconomicSource from the router for release-window lookups.
        # Falls back to None when FRED_API_KEY is absent (source not registered).
        self._fred_src: Optional[FREDEconomicSource] = next(
            (s for s in self._ground_truth._sources if isinstance(s, FREDEconomicSource)),
            None,
        )

        # Priority scorer: reuse the FinancialDataSource already registered in
        # the router so the module-level price cache is shared and no extra API
        # calls are made.  Falls back to None (bracket scoring disabled) if no
        # FinancialDataSource is present in the router's source list.
        _fin_src = next(
            (s for s in self._ground_truth._sources if isinstance(s, FinancialDataSource)),
            None,
        )
        _priority_scorer = PriorityScorer(_fin_src)

        self._scanner = ResolutionScanner(
            kalshi_client, poly_client, exclusions,
            window_hours=window_hours,
            kalshi_window_hours=kalshi_window_hours,
            poly_window_hours=poly_window_hours,
            priority_scorer=_priority_scorer,
            snipe_callback=self.place_snipe_trade,
            kalshi_ws=kalshi_ws,
        )
        self._gap_detector = GapDetector(fee_cache, force_test=force_test, slippage_buffer=min_gap)
        self._confidence = ConfidenceScorer(threshold=min_confidence)
        self._decay = DecayMonitor(dynamic_exit_enabled=dynamic_exit_enabled)

        # ── Tiered market registry ─────────────────────────────────────────────
        # Tracks every known market and which scan tier it belongs to.
        # Populated on the first discovery cycle and kept current thereafter.
        self._registry = TierRegistry()

        # ── Sports live pipeline: inject Kalshi callbacks into ResolutionDetector ──
        # The detector needs two callables to dispatch post-game resolution checks:
        #   market_id_resolver – looks up a Kalshi market_id given team names/sport
        #   market_price_fetcher – re-fetches yes_price for a specific market_id
        # Both are thin lambdas over the Kalshi client; the detector calls them only
        # in background threads, so None-safety guards are essential.
        def _sports_market_id_resolver(
            home_team: str, away_team: str, sport: str
        ) -> Optional[str]:
            """Walk the registry to find a Kalshi game market for this matchup."""
            from data.sports.market_matcher import match_market  # noqa: PLC0415
            ht, at = home_team.lower(), away_team.lower()
            for m in self._registry.all_markets():
                if m.platform != "kalshi":
                    continue
                result = match_market(m.market_id, m.question, sport)
                if result is None:
                    continue
                rh = result["home_team"].lower()
                ra = result["away_team"].lower()
                # match_market encodes YES team as "home_team" by convention,
                # which may not match ESPN's actual home/away designation.
                # Check both orderings so NCAAW/NCAAB neutral-site games resolve.
                if (
                    (ht in rh or rh in ht) and (at in ra or ra in at)
                ) or (
                    (ht in ra or ra in ht) and (at in rh or rh in at)
                ):
                    return m.market_id
            return None

        def _sports_price_fetcher(market_id: str) -> Optional[float]:
            """Return the current Kalshi YES ask price, or None on failure."""
            if kalshi_client is None:
                return None
            try:
                fresh = kalshi_client.get_market(market_id)
                return fresh.yes_price if fresh is not None else None
            except Exception:
                return None

        _res_det.initialize(_sports_market_id_resolver, _sports_price_fetcher)
        logger.info("ResolutionBot: sports live pipeline initialized")

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
        self._last_gt_coverage: dict = {}
        # Consecutive no_source cycle counter per market_id.
        # Incremented each cycle a market passes can_any_source_handle() but
        # fetch() still returns None; reset to 0 when fetch() returns data.
        # Once the count hits _NO_SOURCE_STREAK_THRESHOLD the market is promoted
        # to permanent-skip (treated identically to no_source_fast_skip).
        self._no_source_streak: Dict[str, int] = {}
        # Consecutive fast-skip counter: incremented each cycle a market fails
        # can_any_source_handle().  After FAST_SKIP_PERM_THRESHOLD consecutive
        # failures the market graduates to _no_source_perm_skipped and is never
        # passed to the router again — saving the can_any_source_handle() call.
        self._no_source_fast_skip_streak: Dict[str, int] = {}
        # Markets whose fast-skip streak has crossed the threshold.  Checked
        # before any router call; these markets skip the GT loop entirely.
        self._no_source_perm_skipped: set = set()
        # Consecutive unfilled-timeout counter per market_id (ghost mode only).
        # After 3 timeouts the market is added to _unfilled_timeout_perm_skipped
        # so the bot stops placing dead orders on illiquid markets this session.
        self._unfilled_timeout_count: Dict[str, int] = {}
        self._unfilled_timeout_perm_skipped: set = set()
        # Consecutive EV-failure counter per market_id.  After 3 consecutive EV
        # failures the market is added to the same perm-skip set so the GT loop
        # stops wasting time on structurally-negative-EV positions.
        self._ev_fail_count: Dict[str, int] = {}
        # Consecutive confidence-failure counter per market_id.  After 3 consecutive
        # confidence-gate failures the market is added to _unfilled_timeout_perm_skipped
        # to stop evaluating markets that can never trade (e.g. pre-game sports with
        # source_confidence=0.65 < gate=0.80).
        self._confidence_fail_count: Dict[str, int] = {}
        # Consecutive financial hard-stop counter per market_id.  After 3 hard stops
        # the market is perm-skipped this session.  A 30-minute orderbook cooldown is
        # also set on each hard stop so the same market cannot be re-entered immediately.
        self._hard_stop_count: Dict[str, int] = {}
        # Polymarket token IDs that returned 404 from the CLOB orderbook API.
        # These tokens are stale or invalid — no cooldown retry, permanent for
        # the session.  Checked in _try_execute() before calling _get_live_book().
        self._orderbook_perm_skipped: set = set()
        self._skip_empty_book_ghost_count: int = 0
        # Post-exit re-entry cooldowns.  Set in _exit_position() for both the
        # specific market and its series prefix.  Checked in _try_execute() before
        # sizing to prevent compounding spirals where repeated wins on the same
        # market inflate the ghost bankroll then immediately feed back into sizing.
        self._exit_cooldowns: Dict[str, float] = {}        # market_id → expiry (time.time())
        self._series_exit_cooldowns: Dict[str, float] = {} # series_prefix → expiry
        # Previous cycle's perm-skip count — used to detect when the population
        # changes so we can log a single informational line instead of per-market spam.
        self._prev_perm_skip_count: int = 0
        # Timestamp of when this instance was created — used by the startup
        # stabilization guard to delay new trade entry until the first full
        # scan cycle has completed and in-memory state is populated.
        self._startup_time: float = time.time()

        # ── TUI snapshot state ─────────────────────────────────────────────────
        # Written to data/runtime/tui_state.json at end of every cycle by
        # BotCoordinator via _post_cycle_hook.  Never crash the bot if unset.
        self._pipeline_stage: str = "idle"
        self._last_cycle_start_ts: Optional[str] = None
        self._last_cycle_duration_s: Optional[float] = None
        self._signals_total_cum: int = 0
        self._fills_total_cum: int = 0
        self._snipes_placed_cum: int = 0
        self._post_cycle_hook: Optional[callable] = None

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

        # Fix 6: re-check LIVE_TRADING every cycle in case .env was edited while
        # the bot was running.  force_test must never coexist with live trading.
        if self._force_test and os.environ.get("LIVE_TRADING", "false").lower() == "true":
            logger.critical(
                "ResolutionBot: LIVE_TRADING=true detected at cycle start — "
                "disabling force_test immediately"
            )
            self._force_test = False

        self._kalshi_backend_down = False   # reset circuit breaker each cycle
        cycle_start = time.monotonic()
        self._cycle_start_monotonic = cycle_start  # stash for sub-methods
        self._last_cycle_start_ts = datetime.now(timezone.utc).isoformat()
        self._pipeline_stage = "scanning"
        logger.info("[CYCLE_TIMING] phase=cycle_total event=start elapsed_since_cycle_start=0.0s")
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
            summary["positions_monitored"] = len(self._positions)
            elapsed = (time.monotonic() - cycle_start) * 1000
            summary["cycle_ms"] = round(elapsed)
            self._pipeline_stage = "idle"
            self._call_post_cycle_hook()
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
            summary["positions_monitored"] = len(self._positions)
            elapsed = (time.monotonic() - cycle_start) * 1000
            summary["cycle_ms"] = round(elapsed)
            self._pipeline_stage = "idle"
            self._call_post_cycle_hook()
            return summary

        # ── Calendar: once-daily schedule refresh ──────────────────────────
        if self._calendar is not None:
            self._calendar.refresh_schedule()

        # ── Calendar: imminent release check (within next 6 minutes) ───────
        # If a FRED data release is about to happen, force a full discovery
        # rescan so fresh market listings are visible, then promote all affected
        # markets to Tier 1 so they get evaluated on every cycle during the
        # pre-release and hunt windows.
        if self._calendar is not None:
            imminent = self._calendar.get_upcoming_releases(within_hours=0.1)
            for rel in imminent:
                series_str = ", ".join(rel["series_ids"])
                release_time_str = rel["release_time"].strftime("%H:%M UTC")
                logger.info(
                    "[CALENDAR] FRED release imminent: %s (%s) at %s "
                    "— forcing discovery rescan",
                    rel["release_name"], series_str, release_time_str,
                )
                # Force a discovery rescan by resetting the last-run timestamp.
                self._last_discovery_at = float("-inf")
                # Promote all registry markets whose GT series is in this release.
                if self._fred_src is not None:
                    for entry in self._registry.all_markets():
                        sid = self._fred_src.get_series_for_market(entry)
                        if sid in rel["series_ids"]:
                            self._registry.mark_urgent(entry.market_id)

        # ── Discovery scan (every DISCOVERY_INTERVAL) ──────────────────────
        # Full platform fetch: finds new markets and seeds/refreshes the registry.
        if now_mono - self._last_discovery_at >= DISCOVERY_INTERVAL:
            logger.info("[CYCLE_TIMING] phase=discovery_scan event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
            self._run_discovery()
            self._last_discovery_at = time.monotonic()
            logger.info("[CYCLE_TIMING] phase=discovery_scan event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        else:
            logger.info("[CYCLE_TIMING] phase=discovery_scan event=skipped elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        # Promote markets that have crossed tier boundaries since last cycle.
        logger.info("[CYCLE_TIMING] phase=tier_promotions event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        self._registry.evict_expired()
        promoted = self._registry.promote_due()
        if promoted:
            logger.info("TierRegistry: %d market(s) promoted to a higher tier", promoted)
        logger.info("[CYCLE_TIMING] phase=tier_promotions event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        # Prune no-source skip dicts against current registry to prevent
        # unbounded growth from resolved/evicted markets accumulating over long sessions.
        live_ids = self._registry.known_ids()
        for stale_id in list(self._no_source_fast_skip_streak):
            if stale_id not in live_ids:
                del self._no_source_fast_skip_streak[stale_id]
        self._no_source_perm_skipped &= live_ids

        # Sync Polymarket WebSocket subscriptions with current Tier 1 set.
        logger.info("[CYCLE_TIMING] phase=ws_subscribe_drain event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        t1_ids = {e.market_id for e in self._registry.get_tier(1)}
        self._ws.sync_subscriptions(t1_ids)
        logger.info("[CYCLE_TIMING] phase=ws_subscribe_drain event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        # Sync Kalshi WebSocket subscriptions with current T1+T2 set so the
        # WS orderbook fast path in scanner.refresh_markets() can actually hit.
        if self._kalshi_ws is not None:
            kalshi_t1_t2 = [
                e.market_id
                for tier in (1, 2)
                for e in self._registry.get_tier(tier)
                if e.platform == "kalshi"
            ]
            logger.info(
                "[CYCLE_TIMING] phase=kalshi_ws_sync event=start "
                "elapsed_since_cycle_start=%.1fs target_count=%d",
                time.monotonic() - cycle_start, len(kalshi_t1_t2),
            )
            self._kalshi_ws.sync_subscriptions(kalshi_t1_t2)
            logger.info(
                "[CYCLE_TIMING] phase=kalshi_ws_sync event=end "
                "elapsed_since_cycle_start=%.1fs",
                time.monotonic() - cycle_start,
            )

        # ── Tier 1: refresh all active-watch markets (every cycle) ─────────
        # Sort by priority_score so the highest-priority markets are evaluated
        # first within the cycle.  Scores are preserved through the refresh so
        # the ordering persists until the next discovery re-scores the batch.
        logger.info("[CYCLE_TIMING] phase=tier_ingestion event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        t1_entries = sorted(
            self._registry.get_tier(1),
            key=lambda e: getattr(e.market, "priority_score", 0.0),
            reverse=True,
        )
        # Hard cap (safety): T1 should always fit under this, but if some
        # mis-tiering ever promoted a huge pool to T1 we don't want a single
        # cycle to try refreshing all of it.  Highest-priority entries win.
        if len(t1_entries) > TIER_1_MAX_BATCH_SIZE:
            logger.warning(
                "ResolutionBot: T1 pool %d exceeds TIER_1_MAX_BATCH_SIZE=%d; "
                "capping this cycle's T1 refresh",
                len(t1_entries), TIER_1_MAX_BATCH_SIZE,
            )
            t1_entries = t1_entries[:TIER_1_MAX_BATCH_SIZE]
        if t1_entries:
            # Snapshot priority scores before refresh (refresh returns new Market
            # objects that don't carry the dynamic attribute).
            t1_priority = {
                e.market_id: getattr(e.market, "priority_score", 0.0)
                for e in t1_entries
            }
            # Pull any order-book updates from the WebSocket first (free, fast).
            ws_prices = self._ws.get_pending_updates()
            t1_raw = [e.market for e in t1_entries]
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t1_refresh event=start elapsed_since_cycle_start=%.1fs t1_count=%d", time.monotonic() - cycle_start, len(t1_raw))
            t1_markets = self._scanner.refresh_markets(t1_raw)
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t1_refresh event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
            for m in t1_markets:
                # Restore priority score so sort order persists across refreshes.
                if not hasattr(m, "priority_score"):
                    m.priority_score = t1_priority.get(m.market_id, 0.0)
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
            # Preserve priority scores through refresh (same reason as T1).
            t2_priority = {
                m.market_id: getattr(m, "priority_score", 0.0) for m in t2_batch_raw
            }
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t2_refresh event=start elapsed_since_cycle_start=%.1fs t2_count=%d", time.monotonic() - cycle_start, len(t2_batch_raw))
            t2_markets = self._scanner.refresh_markets(t2_batch_raw)
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t2_refresh event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
            for m in t2_markets:
                if not hasattr(m, "priority_score"):
                    m.priority_score = t2_priority.get(m.market_id, 0.0)
                self._registry.ingest(m)
            self._stagger()
        else:
            t2_markets = []

        # ── Tier 3: rotating batch (refresh + promote; no GT eval) ──────────
        t3_batch_raw = self._next_tier_batch(3)
        if t3_batch_raw:
            t3_priority = {
                m.market_id: getattr(m, "priority_score", 0.0) for m in t3_batch_raw
            }
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t3_refresh event=start elapsed_since_cycle_start=%.1fs t3_count=%d", time.monotonic() - cycle_start, len(t3_batch_raw))
            t3_markets = self._scanner.refresh_markets(t3_batch_raw)
            logger.info("[CYCLE_TIMING] phase=tier_ingestion_t3_refresh event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
            for m in t3_markets:
                if not hasattr(m, "priority_score"):
                    m.priority_score = t3_priority.get(m.market_id, 0.0)
                self._registry.ingest(m)   # ingest recomputes tier → may promote

        # Active markets for this cycle = Tier 1 (all) + Tier 2 batch + Tier 3 batch.
        # T3 markets are included so that long-dated markets receive GT evaluation
        # every cycle instead of only during discovery.  The T3 batch is already
        # a small rotating slice (TIER_3_INTERVAL=1800s ÷ cycle), so the added
        # API load per cycle is modest.
        logger.info("[CYCLE_TIMING] phase=tier_ingestion event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
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

        # ── Sports live data refresh ────────────────────────────────────────
        # refresh_sports_cache() is the single entry point that:
        #   1. Fetches ESPN scoreboards (NBA/NFL/NCAAB) if the 15s TTL has elapsed
        #   2. Updates _game_snapshots so SportsLiveSource.fetch() finds live games
        #   3. Resets the newly-confirmed finals list for this cycle
        # run_shock_detection() then diffs current vs previous game state to cache
        # any actionable probability shocks for SportsLiveSource to read.
        # check_for_resolution_lags() dispatches background threads for games that
        # just went final, checking whether Kalshi price has caught up yet.
        # All three must run before GT evaluation; failure must never crash the cycle.
        _sports_games_active = 0
        shock_signals: list = []
        resolution_signals: list = []
        ghost_exits: list = []
        try:
            refresh_sports_cache()
            shock_signals = run_shock_detection()
            if shock_signals:
                logger.info(
                    "ResolutionBot: shock detector — %d actionable signal(s) cached",
                    len(shock_signals),
                )
            resolution_signals = _res_det.check_for_resolution_lags()
            ghost_exits = _drain_ghost_exits()
            if resolution_signals:
                logger.info(
                    "ResolutionBot: resolution detector — %d market(s) newly final",
                    len(resolution_signals),
                )
            from data.sports.live_game_monitor import get_active_snapshots  # noqa: PLC0415
            _sports_games_active = len(get_active_snapshots())
            if _sports_games_active:
                logger.info(
                    "ResolutionBot: sports live — %d game(s) in progress, "
                    "%d shock(s), %d resolution signal(s)",
                    _sports_games_active, len(shock_signals), len(resolution_signals),
                )
        except Exception as _sports_exc:
            logger.warning(
                "ResolutionBot: sports pipeline refresh failed (ESPN may be down): %s",
                _sports_exc,
            )

        # ── Ghost position exits for confirmed finals ───────────────────────
        # When a game goes CONFIRMED FINAL, exit any open ghost positions on
        # that market at the settlement value (1.0 or 0.0).  This runs before
        # GT evaluation so the position is gone before it would be re-evaluated.
        if ghost_exits:
            try:
                _n_ghost_exited = self._exit_ghost_positions_for_finals(ghost_exits)
                if _n_ghost_exited:
                    logger.info(
                        "ResolutionBot: ghost final exit — %d position(s) closed at settlement",
                        _n_ghost_exited,
                    )
            except Exception as _gex_exc:
                logger.warning("ResolutionBot: ghost final exit error: %s", _gex_exc)

        # ── Promote live game markets to T1 ────────────────────────────────
        # When games are in progress, game markets need T1 treatment (every
        # cycle) rather than slow T2/T3 rotation.  For each non-T1 game market
        # in the registry, check whether it matches an active ESPN snapshot; if
        # so, mark it urgent so the tier registry moves it to T1 this cycle.
        if _sports_games_active > 0:
            from data.sports.market_matcher import match_market  # noqa: PLC0415
            for tier in (2, 3):
                for entry in self._registry.get_tier(tier):
                    m = entry.market
                    if _is_game_market(m.market_id):
                        result = match_market(m.market_id, m.question)
                        if result:
                            self._registry.mark_urgent(m.market_id)
                            logger.debug(
                                "ResolutionBot: promoted %s to T1 (game may be live)",
                                m.market_id,
                            )

        # ── Cross-platform gap detection (Tier 1 + all Tier 2) ─────────────
        self._pipeline_stage = "gap_detect"
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

        # ── Prioritize just-finalized game markets ──────────────────────────
        # When a game goes final this cycle, the corresponding Kalshi market is
        # mispriced for only a short window before other bots reprice it.
        # Sorting those markets to the front of active_markets ensures GT
        # evaluation + order placement happens in the next few seconds rather
        # than after 100+ other markets are processed.
        try:
            from data.sports.live_game_monitor import get_newly_confirmed_finals  # noqa: PLC0415
            from data.sports.market_matcher import match_market as _match_market  # noqa: PLC0415
            _newly_final = get_newly_confirmed_finals()
            if _newly_final:
                _final_teams: set = set()
                for _cg in _newly_final:
                    _final_teams.add(_cg.home_team.lower())
                    _final_teams.add(_cg.away_team.lower())

                def _is_final_game_market(m: "Market") -> bool:
                    if not _is_game_market(m.market_id):
                        return False
                    _r = _match_market(m.market_id, m.question)
                    if _r is None:
                        return False
                    return (
                        _r["home_team"].lower() in _final_teams
                        or _r["away_team"].lower() in _final_teams
                    )

                _hot, _rest = [], []
                for _m in active_markets:
                    (_hot if _is_final_game_market(_m) else _rest).append(_m)
                if _hot:
                    logger.info(
                        "ResolutionBot: prioritizing %d just-finalized game market(s) "
                        "for immediate GT evaluation: %s",
                        len(_hot),
                        ", ".join(_m.market_id for _m in _hot),
                    )
                    active_markets = _hot + _rest
        except Exception as _sort_exc:
            logger.warning(
                "ResolutionBot: failed to prioritize final game markets: %s", _sort_exc
            )

        # ── Information signals (GT fetch for active markets) ───────────────
        self._pipeline_stage = "gt_fetch"
        logger.info("[CYCLE_TIMING] phase=gt_fetch_loop event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        try:
            info_signals = self._fetch_info_signals(active_markets)
        except Exception:
            logger.exception(
                "ResolutionBot: _fetch_info_signals failed — "
                "returning empty signals for this cycle"
            )
            info_signals = []
        logger.info("[CYCLE_TIMING] phase=gt_fetch_loop event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        # Urgent-promote markets with detected info gaps.
        for sig in info_signals:
            self._registry.mark_urgent(sig.market_to_buy.market_id)

        # Persist sticky-T1 promotions so they survive a restart.
        self._save_sticky_t1()

        # ── Resolution lag signals (confirmed finals, still mispriced) ───────
        # Convert ResolutionSignals → GapSignals so they run through _try_execute.
        # These are prepended to info_signals so they execute first — resolution
        # lag windows are seconds wide and shouldn't wait behind slower GT signals.
        resolution_gap_signals: List[GapSignal] = []
        if resolution_signals:
            market_by_id = {m.market_id: m for m in self._registry.all_markets()}
            for rsig in resolution_signals:
                market = market_by_id.get(rsig.market_id)
                if market is None:
                    logger.warning(
                        "ResolutionBot: resolution signal for %s — not in registry, skipping",
                        rsig.market_id,
                    )
                    continue
                gsig = self._resolution_signal_to_gap(rsig, market)
                resolution_gap_signals.append(gsig)
                self._registry.mark_urgent(market.market_id)
                logger.info(
                    "ResolutionBot: [RESOLUTION LAG] %s | correct=%.2f market=%.3f "
                    "gap=%.1f%% lag=%.0fms",
                    rsig.market_id, rsig.correct_prob, rsig.market_price,
                    rsig.gap * 100, rsig.resolution_lag_ms,
                )

        all_signals = cross_signals + fuzzy_signals + resolution_gap_signals + info_signals
        self._last_signals = all_signals
        summary["signals_flagged"] = len(all_signals)
        logger.info(
            "ResolutionBot: %d exact + %d fuzzy cross-platform + %d resolution lag + %d info signals = %d total "
            "(T1=%d T2_batch=%d/%d total_t2=%d)",
            len(cross_signals), len(fuzzy_signals), len(resolution_gap_signals),
            len(info_signals), len(all_signals),
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
        self._pipeline_stage = "scoring"
        logger.info("[CYCLE_TIMING] phase=signal_eval event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        display_signals: List[GapSignal] = []
        confidence_blocked = 0
        cooldown_blocked = 0
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
                if _is_in_orderbook_cooldown(mid) and not self._force_test:
                    cooldown_blocked += 1
                else:
                    display_signals.append(signal)
            else:
                confidence_blocked += 1

        summary["confidence_blocked"] = confidence_blocked
        summary["cooldown_blocked"] = cooldown_blocked
        logger.info(
            "ResolutionBot: confidence gate: %d pass, %d confidence-blocked, %d cooldown-blocked",
            len(display_signals), confidence_blocked, cooldown_blocked,
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
        self._pipeline_stage = "executing"
        for signal in all_signals:
            detail = self._try_execute(signal)
            if detail is not None:
                summary["trades_fired"] += 1
                summary["trade_details"].append(detail)
        logger.info("[CYCLE_TIMING] phase=signal_eval event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        # ── Monitor open positions ───────────────────────────────────────────
        self._pipeline_stage = "decay_monitor"
        logger.info("[CYCLE_TIMING] phase=cycle_summary_write event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        exits = self._monitor_positions()
        summary["exits_triggered"] = exits

        # Refresh registry stats at end so cycle #1 reflects post-discovery state.
        summary["registry"] = self._registry.stats()
        summary["sports_games_active"] = _sports_games_active
        summary["gt_coverage"] = self._last_gt_coverage
        # Refresh post-cycle so ghost-reset, sports-final exits, and decay
        # exits that ran during the cycle are reflected in the printed count.
        summary["positions_monitored"] = len(self._positions)

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

        # ── Ghost-trade daily summary ────────────────────────────────────────
        if self._dry_run and self._paper_log is not None:
            _day = self._paper_log.get_daily_summary()
            if _day["total_entries"] > 0:
                _print_paper_summary(_day)
        logger.info("[CYCLE_TIMING] phase=cycle_summary_write event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)
        logger.info("[CYCLE_TIMING] phase=cycle_total event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - cycle_start)

        self._last_cycle_duration_s = elapsed / 1000
        self._signals_total_cum += summary.get("signals_flagged", 0)
        self._fills_total_cum += summary.get("trades_fired", 0)
        self._pipeline_stage = "idle"
        self._call_post_cycle_hook()

        return summary

    def _call_post_cycle_hook(self) -> None:
        if self._post_cycle_hook is not None:
            try:
                self._post_cycle_hook()
            except Exception as _h:
                logger.warning("ResolutionBot: post_cycle_hook failed: %s", _h)

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
        _cs = getattr(self, "_cycle_start_monotonic", time.monotonic())
        try:
            logger.info("[CYCLE_TIMING] phase=polymarket_scan event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
            markets = self._scanner.scan()
            logger.info("[CYCLE_TIMING] phase=polymarket_scan event=end elapsed_since_cycle_start=%.1fs total_markets=%d", time.monotonic() - _cs, len(markets))

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
                    # Let needs_rebuild() decide — discovery already runs on its
                    # own 900 s interval which is shorter than _PAIR_CACHE_TTL
                    # (30 min), so a rebuild will fire here on the first
                    # discovery cycle after TTL expiry without force-clearing.
                    # Explicitly nulling _last_built would bypass the TTL guard
                    # and defeat the fix in needs_rebuild().
                    logger.info("[CYCLE_TIMING] phase=cross_platform_rebuild event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
                    self._gap_detector.run_cross_platform_scan(
                        kalshi_markets, poly_markets
                    )
                    logger.info("[CYCLE_TIMING] phase=cross_platform_rebuild event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
                else:
                    logger.info("[CYCLE_TIMING] phase=cross_platform_rebuild event=skipped elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
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
        # Sort by priority_score descending so the highest-priority markets are
        # processed first in each rotation.  Falls back to 0.0 for markets whose
        # priority_score was not yet set (e.g. on the first cycle before discovery).
        entries = sorted(
            self._registry.get_tier(tier),
            key=lambda e: getattr(e.market, "priority_score", 0.0),
            reverse=True,
        )
        if not entries:
            return []

        interval = TIER_2_INTERVAL if tier == 2 else TIER_3_INTERVAL
        max_batch = TIER_2_MAX_BATCH_SIZE if tier == 2 else TIER_3_MAX_BATCH_SIZE
        # How many markets to process per cycle to cover the full tier within
        # one tier interval.  Use the actual configured scan interval (e.g. 60s
        # from main.py) rather than TIER_1_INTERVAL (15s); if we used 15s here
        # but cycles only fire every 60s, we'd cover 4× fewer markets per cycle
        # and need 4× as long to sweep the full T2/T3 pool.
        #
        # Previously `interval // cycle_s` with an integer floor of 1 had the
        # effect of forcing cycles_per_interval = 1 whenever scan_interval >
        # interval (e.g. main.py scan_interval=300 vs TIER_2_INTERVAL=150),
        # which made batch_size = len(entries) and caused a full sweep every
        # cycle.  Float division is cosmetic given the max(1.0, ...) floor —
        # the real fix is the hard `max_batch` cap below, which guarantees no
        # single cycle refreshes more than TIER_N_MAX_BATCH_SIZE markets
        # regardless of config.
        cycle_s = max(1.0, float(self._scan_interval))
        cycles_per_interval = max(1.0, interval / cycle_s)
        batch_size = max(1, math.ceil(len(entries) / cycles_per_interval))
        batch_size = min(batch_size, max_batch)

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

    def _resolution_signal_to_gap(
        self, sig: ResolutionSignal, market: Market
    ) -> GapSignal:
        """
        Convert a ResolutionSignal (confirmed ESPN final, live Kalshi price
        already re-fetched by the background thread) into a GapSignal so it
        can flow through _try_execute unchanged.

        Confidence is pre-verified at 0.99; gap is pre-checked against a live
        Kalshi price fetch (not the stale bulk-API price).  The signal bypasses
        the GT router — outcome is known with certainty.
        """
        fee = self._fee_cache.get_taker_fee(
            market.platform, market.market_id, price=market.yes_price
        )
        effective_gap = max(0.0, sig.gap - fee * 2)

        gt = GroundTruthResult(
            ground_truth_prob=sig.correct_prob,
            confidence=sig.confidence,          # always 0.99
            source_type=SourceType.HARD,
            source_name="ResolutionDetector/ConfirmedFinal",
            source_url="https://site.api.espn.com/apis/site/v2/sports",
            raw_data={
                "game_id": sig.game_id,
                "sport": sig.sport,
                "home_team": sig.home_team,
                "away_team": sig.away_team,
                "winner": sig.winner,
                "resolution_lag_ms": sig.resolution_lag_ms,
                "market_price_at_detection": sig.market_price,
            },
            reasoning=(
                f"Confirmed final: {sig.sport.upper()} {sig.home_team} vs "
                f"{sig.away_team} — winner={sig.winner} | "
                f"correct={sig.correct_prob:.2f} market={sig.market_price:.3f} "
                f"({sig.resolution_lag_ms:.0f}ms lag)"
            ),
            data_published_at=datetime.now(timezone.utc),
            # Outcome is known — never ambiguous
            directional_confidence="yes" if sig.correct_prob >= 0.99 else "no",
        )

        return GapSignal(
            signal_type="information",
            market_to_buy=market,
            market_reference=None,
            target_price=sig.market_price,
            reference_price=sig.correct_prob,
            ground_truth_prob=sig.correct_prob,
            raw_gap=sig.gap,
            effective_gap=effective_gap,
            taker_fee=fee,
            ground_truth_result=gt,
            reasoning=gt.reasoning,
        )

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

        if not candidates:
            logger.info(
                "ResolutionBot: no candidate markets this cycle — "
                "skipping GT evaluation and prefetch"
            )
            return []

        # ── Subscribe WS orderbook for Kalshi candidates ─────────────────
        _cs = getattr(self, "_cycle_start_monotonic", time.monotonic())
        logger.info("[CYCLE_TIMING] phase=ws_subscribe_drain event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
        if self._kalshi_ws is not None:
            kalshi_tickers = [
                m.market_id for m in candidates if m.platform == "kalshi"
            ]
            if kalshi_tickers:
                self._kalshi_ws.subscribe(kalshi_tickers)
        logger.info("[CYCLE_TIMING] phase=ws_subscribe_drain event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)

        # Coverage diagnostic counters — reset to 0 on every call; these are
        # per-cycle (per-batch) counts, never cumulative across cycles.
        n_novelty: int = 0              # novelty prop-bets filtered at ingest; always 0 here
        n_no_source_fast_skip: int = 0  # failed can_any_source_handle(), not yet permanent
        n_no_source_perm_skip: int = 0  # in _no_source_perm_skipped — no router call at all
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

        # FRED markets currently in the hunt window — promoted to T1 eagerly so
        # the bot surveys them every Tier-1 cycle (15 s) rather than T2 (150 s).
        # Collected during the main loop; logged in a single line at the end.
        _hunt_window_fred_markets: List[str] = []

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

        # ── Pre-GT price refresh (BEFORE illiquidity check) ──────────────────
        # Game and financial bracket markets have real-time external price sources
        # and are most likely to have stale discovery prices (default 0.50¢).
        # Refresh these prices from the live order book BEFORE any filtering,
        # so the illiquidity check uses current prices, not stale scanner data.
        #
        # Performance: use the module-level TTL cache to skip markets refreshed
        # recently, then fan out remaining calls across 8 threads to avoid the
        # 200+ serial HTTP calls that previously took 60–90s per cycle.
        logger.info("[CYCLE_TIMING] phase=pre_gt_price_refresh event=start elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
        _now_mono = time.monotonic()
        _markets_by_id: Dict[str, object] = {m.market_id: m for m in candidates}
        _to_refresh: List[str] = []

        # Mark all non-bracket/non-game markets as price-OK (scanner price is
        # acceptable for gap detection on these).  Game and financial bracket
        # markets must be explicitly refreshed before gap detection — their
        # scanner prices are often stale 0.50 defaults or wide-spread mids.
        for m in candidates:
            _needs_refresh = _is_game_market(m.market_id) or _is_financial_bracket_market(m.market_id)
            m.price_refresh_success = not _needs_refresh

        for m in candidates:
            if not (_is_game_market(m.market_id) or _is_financial_bracket_market(m.market_id)):
                continue
            # Financial bracket markets: only refresh when near 0.50 (stale Kalshi default).
            # Game markets: ALWAYS refresh — live game prices can be anywhere (0.01–0.99)
            # and a stale scanner price (e.g. 0.0099 from earlier in the game) will produce
            # phantom 100x leverage on the contract count and fake P&L.
            if _is_financial_bracket_market(m.market_id) and not (abs(m.yes_price - 0.50) <= 0.02):
                # Price is not near the stale 0.50 default — accept it as valid.
                m.price_refresh_success = True
                continue
            if self._kalshi is None:
                continue
            # Skip if refreshed within TTL — financial brackets during market hours
            # use a shorter 30s TTL because NQ/ES can move 50+ points in 2 minutes.
            _market_ttl = (
                30.0
                if _is_financial_bracket_market(m.market_id) and _is_us_market_hours()
                else _PRICE_REFRESH_TTL_S
            )
            _cached_entry = _price_refresh_cache.get(m.market_id)
            if _cached_entry is not None and (_now_mono - _cached_entry[0]) < _market_ttl:
                # Recently refreshed within TTL — apply the cached price to this Market object
                # (scanner may have created a fresh Market with yes_price=0.50 this cycle).
                m.yes_price = _cached_entry[1]
                m.no_price = round(1.0 - _cached_entry[1], 4)
                m.price_refresh_success = True
                continue
            _to_refresh.append(m.market_id)

        if _to_refresh:
            logger.debug(
                "ResolutionBot: pre-GT price refresh — %d markets need refresh (base TTL=%.0fs, skipped=%d)",
                len(_to_refresh),
                _PRICE_REFRESH_TTL_S,
                sum(1 for m in candidates
                    if (_is_game_market(m.market_id) or _is_financial_bracket_market(m.market_id))
                    and m.market_id not in _to_refresh),
            )

            def _fetch_price(mid: str):
                try:
                    return mid, self._kalshi.get_market(mid)
                except Exception as _e:
                    logger.debug("ResolutionBot: pre-GT price refresh failed %s: %s", mid, _e)
                    return mid, None

            _t_refresh_start = time.monotonic()
            _refresh_ok = 0
            _refresh_fail = 0
            with ThreadPoolExecutor(max_workers=8) as _pool:
                _futures = {_pool.submit(_fetch_price, mid): mid for mid in _to_refresh}
                for _fut in as_completed(_futures):
                    mid, fresh = _fut.result()
                    m = _markets_by_id.get(mid)
                    if fresh is not None:
                        if m is not None:
                            new_price = getattr(fresh, "yes_price", None)
                            if new_price is not None:
                                # Only cache successful refreshes with the actual price — failed
                                # refreshes or missing prices must NOT update the cache, otherwise
                                # subsequent cycles skip the market with a stale 0.50 price still
                                # stored in the object.
                                _price_refresh_cache[mid] = (time.monotonic(), new_price)
                                old_price = m.yes_price
                                m.yes_price = new_price
                                m.no_price = round(1.0 - new_price, 4)
                                m.price_refresh_success = True
                                _refresh_ok += 1
                                if abs(old_price - new_price) > 0.02:
                                    logger.info(
                                        "ResolutionBot: pre-GT price refresh %s: %.3f → %.3f",
                                        mid, old_price, new_price,
                                    )
                    else:
                        _refresh_fail += 1
                        if m is not None:
                            logger.info(
                                "ResolutionBot: pre-GT price refresh FAILED %s — "
                                "market will be blocked from gap detection (stale price %.3f)",
                                mid, m.yes_price,
                            )
            logger.debug(
                "ResolutionBot: pre-GT price refresh done — %d ok, %d failed, %.1fs",
                _refresh_ok, _refresh_fail, time.monotonic() - _t_refresh_start,
            )
        logger.info("[CYCLE_TIMING] phase=pre_gt_price_refresh event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
        # ─────────────────────────────────────────────────────────────────────

        # ── Uniform-50 illiquid filter (NOW uses refreshed prices) ───────────
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
        # NOTE: This check now runs AFTER price refresh, so it uses live prices.
        _illiquid_prefixes: set = set()
        _prefix_prices: Dict[str, list] = {}
        for m in candidates:
            pfx = _bracket_prefix(m.market_id)
            if pfx is not None:
                _prefix_prices.setdefault(pfx, []).append(m.yes_price)
        for pfx, prices in _prefix_prices.items():
            near_50 = sum(1 for p in prices if abs(p - 0.50) <= 0.02)
            pct_near_50 = near_50 / len(prices) if prices else 0.0

            # Illiquidity gate: proportional + absolute minimum
            # Flag only if BOTH:
            #   (1) At least 4 brackets at 0.50 (minimum count to avoid false positives)
            #   (2) More than 50% of the series at 0.50 (indicates genuinely dead series)
            # Example: 4/28 = 14% → PASS, 35/40 = 87% → FLAG
            is_illiquid = near_50 > 3 and pct_near_50 > 0.50

            if is_illiquid:
                if self._force_test:
                    logger.info(
                        "ResolutionBot: [FORCE-TEST] bypassing series illiquidity filter for %s "
                        "(%d/%d brackets = %.0f%% within 2¢ of 0.50)",
                        pfx, near_50, len(prices), pct_near_50 * 100,
                    )
                else:
                    _illiquid_prefixes.add(pfx)
                    logger.info(
                        "ResolutionBot: series %s flagged illiquid — "
                        "%d/%d brackets = %.0f%% priced within 2¢ of 0.50 "
                        "(uniform_50_pricing; skipping whole series)",
                        pfx, near_50, len(prices), pct_near_50 * 100,
                    )
        # ─────────────────────────────────────────────────────────────────────

        # ── Parallel financial price prefetch ─────────────────────────────────
        # Fetch all needed financial symbols in parallel before the GT loop.
        # This pre-warms the _PRICE_CACHE so individual _fetch_price() calls
        # return instantly instead of blocking on HTTP.  Reduces cycle time from
        # ~28s (sequential) to ~7s (parallel: 4 symbols × 7s → 1 × 7s + overhead).
        _all_financial_symbols = [s for s in _SERIES_TO_SYMBOL.values() if s is not None]
        logger.info("[CYCLE_TIMING] phase=financial_prefetch event=start elapsed_since_cycle_start=%.1fs symbols=%d", time.monotonic() - _cs, len(_all_financial_symbols))
        if _all_financial_symbols:
            self._ground_truth.prefetch_financial_prices(_all_financial_symbols)
        logger.info("[CYCLE_TIMING] phase=financial_prefetch event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)
        # ─────────────────────────────────────────────────────────────────────

        logger.info("[CYCLE_TIMING] phase=gt_fetch_loop_inner event=start elapsed_since_cycle_start=%.1fs candidates=%d", time.monotonic() - _cs, total)
        for i, market in enumerate(candidates, 1):
            if i % 25 == 0 or i == total:
                logger.info(
                    "ResolutionBot: ground truth progress %d/%d (signals so far: %d)",
                    i, total, len(signals),
                )

            # ── Pre-GT price refresh (BEFORE GT fetch) ────────────────────────
            # Game and financial bracket markets have real-time external price
            # sources and are most likely to have stale discovery prices.
            # Scoped to these two categories to avoid per-cycle get_market()
            # calls for politics/entertainment/etc. that don't move frequently.
            _now_mono = time.monotonic()
            _cached_gt_entry = _price_refresh_cache.get(market.market_id)
            _last_refresh = _cached_gt_entry[0] if _cached_gt_entry is not None else 0.0
            # Financial brackets use a shorter TTL during live market hours
            # because NQ/ES can move 50+ points in 2 minutes.
            _refresh_ttl = (
                30.0
                if _is_financial_bracket_market(market.market_id) and _is_us_market_hours()
                else _PRICE_REFRESH_TTL_S
            )
            if (
                (_is_game_market(market.market_id) or _is_financial_bracket_market(market.market_id))
                and abs(market.yes_price - 0.50) <= 0.02
                and self._kalshi is not None
                and (_now_mono - _last_refresh) >= _refresh_ttl
            ):
                try:
                    fresh = self._kalshi.get_market(market.market_id)
                    if fresh is not None:
                        new_price = getattr(fresh, "yes_price", None)
                        if new_price is not None:
                            # Store (timestamp, price) so cache hits next cycle can apply
                            # the refreshed price to newly-constructed Market objects.
                            _price_refresh_cache[market.market_id] = (time.monotonic(), new_price)
                            old_price = market.yes_price
                            market.yes_price = new_price
                            market.no_price = round(1.0 - new_price, 4)
                            if abs(old_price - new_price) > 0.02:
                                logger.info(
                                    "ResolutionBot: price refresh %s: %.3f → %.3f",
                                    market.market_id, old_price, new_price,
                                )
                            else:
                                logger.debug(
                                    "ResolutionBot: price refresh %s: Kalshi also at %.3f",
                                    market.market_id, new_price,
                                )
                except Exception as _e:
                    logger.debug(
                        "ResolutionBot: price refresh failed %s: %s",
                        market.market_id, _e,
                    )
            # ─────────────────────────────────────────────────────────────────

            # ── Game market default illiquid price floor ──────────────────────
            # Skip game markets at the Kalshi default illiquid price (0.255 ±0.01).
            # This price never shows real counterparty volume in logs.
            if _is_game_market(market.market_id):
                if abs(market.yes_price - 0.255) <= 0.01 or abs(market.no_price - 0.255) <= 0.01:
                    logger.debug(
                        "ResolutionBot: skipping %s — game market at default illiquid price "
                        "(yes=%.3f no=%.3f)",
                        market.market_id, market.yes_price, market.no_price,
                    )
                    n_no_source += 1
                    continue
            # ─────────────────────────────────────────────────────────────────

            # ── Permanent fast-skip check ─────────────────────────────────────
            # Bypasses the router entirely — no can_any_source_handle() call.
            # Markets graduate here after FAST_SKIP_PERM_THRESHOLD consecutive
            # fast-skip cycles.  Keyed by market_id so it survives discovery.
            if market.market_id in self._no_source_perm_skipped:
                n_no_source_perm_skip += 1
                continue

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

            # ── Deep OTM bracket gate ─────────────────────────────────────────
            # Skip financial bracket markets where the strike is more than
            # _BRACKET_PROXIMITY_MAX (5%) from the current underlying price.
            # Reuses the price already fetched by the priority scorer — no new
            # API call.  Only applied when the price cache has a fresh entry.
            if prefix is not None and _is_financial_bracket_market(market.market_id):
                _otm_series_root = prefix.split("-")[0]
                _otm_symbol = _SERIES_TO_SYMBOL.get(_otm_series_root)
                if _otm_symbol is not None:
                    _otm_cache_entry = _FINANCIAL_PRICE_CACHE.get(_otm_symbol)
                    if _otm_cache_entry is not None:
                        _otm_current_price = _otm_cache_entry[1]  # (fetched_at, price, source, quote_ts)
                        if _otm_current_price is not None and _otm_current_price > 0:
                            _otm_strike = _parse_strike(market.market_id)
                            if _otm_strike is not None:
                                _otm_distance = abs(_otm_current_price - _otm_strike) / _otm_current_price
                                if _otm_distance > _BRACKET_PROXIMITY_MAX:
                                    logger.debug(
                                        "ResolutionBot: skipping %s — strike %.2f is %.1f%% "
                                        "from current %.2f (deep OTM > %d%%)",
                                        market.market_id, _otm_strike,
                                        _otm_distance * 100, _otm_current_price,
                                        int(_BRACKET_PROXIMITY_MAX * 100),
                                    )
                                    n_no_source += 1
                                    continue
            # ─────────────────────────────────────────────────────────────────

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
                    # Track consecutive fast-skip cycles; graduate to permanent
                    # skip after FAST_SKIP_PERM_THRESHOLD consecutive misses.
                    streak = self._no_source_fast_skip_streak.get(market.market_id, 0) + 1
                    self._no_source_fast_skip_streak[market.market_id] = streak
                    if streak >= FAST_SKIP_PERM_THRESHOLD:
                        self._no_source_perm_skipped.add(market.market_id)
                    continue

                # A source now claims this market — clear any fast-skip state so
                # the market is no longer bypassing the router.
                self._no_source_fast_skip_streak.pop(market.market_id, None)
                self._no_source_perm_skipped.discard(market.market_id)

                # Permanent-skip: market has a source that claims it but fetch()
                # has returned None for _NO_SOURCE_STREAK_THRESHOLD consecutive
                # cycles.  Stop calling fetch() until the streak resets (i.e. until
                # the market is refreshed or a source begins returning data).
                #
                # Game markets are exempt — they are expected to have no source
                # until the game starts (pre-game state). Skipping them permanently
                # would cause the bot to miss live signals when the game begins.
                if (
                    not market.market_id.startswith(("KXNBAGAME", "KXNCAAMBGAME", "KXNFLGAME", "KXNCAAWBGAME"))
                    and self._no_source_streak.get(market.market_id, 0)
                    >= _NO_SOURCE_STREAK_THRESHOLD
                ):
                    n_no_source_perm_skip += 1
                    _source_timing["no_source_perm_skip"] = _source_timing.get(
                        "no_source_perm_skip", 0.0
                    )  # 0.0 — no fetch issued
                    _source_fetch_count["no_source_perm_skip"] = (
                        _source_fetch_count.get("no_source_perm_skip", 0) + 1
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
                    # Increment streak — if this market never yields data it will
                    # be promoted to permanent-skip after _NO_SOURCE_STREAK_THRESHOLD cycles.
                    self._no_source_streak[market.market_id] = (
                        self._no_source_streak.get(market.market_id, 0) + 1
                    )
                    n_no_source += 1
                    continue
                # fetch() returned data — reset streak so the market stays live.
                self._no_source_streak.pop(market.market_id, None)
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
                if self._force_test:
                    logger.info(
                        "ResolutionBot: [FORCE-TEST] bypassing illiquidity filter for %s "
                        "(yes_price=%.3f gt_prob=%.3f)",
                        market.market_id, market.yes_price, gt.ground_truth_prob,
                    )
                    # Fall through to gap detection
                else:
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

            # ── FRED hunt window: relaxed min_gap + T1 promotion ─────────────
            # Compute the release window for this market so gap_detector can
            # apply a 30% min_gap reduction during the hunt window.  Also collect
            # hunt-window markets for a single bulk T1 promotion + log below.
            _gap_release_window: Optional[str] = None
            if self._calendar is not None and self._fred_src is not None:
                _gap_fred_series = self._fred_src.get_series_for_market(market)
                if _gap_fred_series is not None:
                    _gap_release_window = self._calendar.get_active_window(_gap_fred_series)
                    if _gap_release_window == "hunt":
                        _hunt_window_fred_markets.append(market.market_id)
                        self._registry.mark_urgent(market.market_id)

            signal = self._gap_detector.detect_information_signal(
                market, gt.ground_truth_prob, release_window=_gap_release_window
            )
            if signal:
                signal.ground_truth_prob = gt.ground_truth_prob
                signal.ground_truth_result = gt  # preserve real source confidence

                # ── Release window check ──────────────────────────────────────
                # If this market's GT source is a FRED series that is currently
                # in a release window, gate trading accordingly.
                #   hold        → never trade; log the held signal
                #   pre_release → never trade; log the scan-only signal
                #   hunt        → allow normal trading (debug log only)
                #   None (idle) → no change
                _release_window: Optional[str] = None
                _series_id_for_window: Optional[str] = None
                _release_time_str: str = "unknown"
                if self._calendar is not None and self._fred_src is not None:
                    _series_id_for_window = self._fred_src.get_series_for_market(market)
                    if _series_id_for_window is not None:
                        _release_window = self._calendar.get_active_window(
                            _series_id_for_window
                        )
                        _rt = self._calendar._schedule.get(_series_id_for_window)
                        _release_time_str = (
                            _rt.strftime("%H:%M UTC") if _rt else "unknown"
                        )

                if _release_window == "hold":
                    logger.info(
                        "[CALENDAR] Holding signal for %s — FRED series %s in hold window "
                        "(release at %s, waiting for FRED update) | "
                        "would_have: gap=%.3f gt_prob=%.3f price=%.3f action=%s",
                        market.market_id, _series_id_for_window, _release_time_str,
                        signal.effective_gap, gt.ground_truth_prob,
                        market.yes_price, signal.action,
                    )
                    gt_evaluated.add(market.market_id)
                elif _release_window == "pre_release":
                    logger.info(
                        "[CALENDAR] %s in pre-release window — scan only, no trades "
                        "| gap=%.3f gt_prob=%.3f price=%.3f action=%s",
                        market.market_id, signal.effective_gap,
                        gt.ground_truth_prob, market.yes_price, signal.action,
                    )
                    gt_evaluated.add(market.market_id)
                else:
                    if _release_window == "hunt":
                        logger.debug(
                            "[CALENDAR] %s in hunt window for %s — FRED confirmed "
                            "updated, trading enabled",
                            market.market_id, _series_id_for_window,
                        )
                    signals.append(signal)
                    gt_evaluated.add(market.market_id)
            else:
                n_gap_too_small += 1
                gt_evaluated.add(market.market_id)

        logger.info("[CYCLE_TIMING] phase=gt_fetch_loop_inner event=end elapsed_since_cycle_start=%.1fs", time.monotonic() - _cs)

        # Emit a single summary line so operators can distinguish
        # "no data sources cover these markets" from "markets are efficiently priced".
        sources_str = (
            ", ".join(f"{src}={cnt}" for src, cnt in sorted(coverage_sources.items()))
            if coverage_sources else "none"
        )
        logger.info(
            "ResolutionBot: GT coverage summary — "
            "excluded_novelty=%d no_source_fast_skip=%d no_source_perm_skip=%d "
            "no_source=%d no_prob=%d covered=%d (sources: %s) "
            "gap_too_small=%d actionable=%d skip_empty_book_ghost=%d",
            n_novelty, n_no_source_fast_skip, n_no_source_perm_skip,
            n_no_source, n_no_prob,
            n_covered, sources_str, n_gap_too_small, len(signals),
            self._skip_empty_book_ghost_count,
        )
        self._last_gt_coverage = {
            "covered": n_covered,
            "no_source": n_no_source + n_no_source_fast_skip + n_no_source_perm_skip,
            "no_prob": n_no_prob,
            "gap_too_small": n_gap_too_small,
            "sources": sources_str,
        }

        # Log a single line when the permanent-skip population changes so
        # operators can see the graduation plateau without per-market spam.
        perm_total = len(self._no_source_perm_skipped)
        if perm_total != self._prev_perm_skip_count:
            logger.info(
                "ResolutionBot: permanent no-source skip set updated: %d markets "
                "(was %d) — these markets bypass the router entirely each cycle",
                perm_total, self._prev_perm_skip_count,
            )
            self._prev_perm_skip_count = perm_total

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

            # Append total ESPN sports fetch time so it's visible alongside GT timings.
            try:
                from data.sports.live_game_monitor import get_cycle_fetch_ms  # noqa: PLC0415
                sports_fetch_ms = get_cycle_fetch_ms()
                if sports_fetch_ms > 0:
                    timing_str += f"  espn_fetch:{sports_fetch_ms:.0f}ms"
            except Exception:
                pass

            logger.info("ResolutionBot: GT source timing — %s", timing_str)

        # Persist evaluated set so run_once can guard the clear_urgent logic.
        self._last_gt_evaluated_ids = frozenset(gt_evaluated)

        # Log hunt window T1 promotions as a single line so operators can see
        # which markets are under active surveillance during the data release.
        if _hunt_window_fred_markets:
            _unique_hunt = sorted(set(_hunt_window_fred_markets))
            logger.info(
                "[CALENDAR] FRED hunt window active — %d market(s) promoted to T1: %s",
                len(_unique_hunt), ", ".join(_unique_hunt[:10])
                + (" …" if len(_unique_hunt) > 10 else ""),
            )

        # ── Pass 1: game-level deduplication ─────────────────────────────────
        # For game markets (KXNBAGAME/KXNCAAMBGAME/etc.), two markets can
        # express opposite sides of the same outcome:
        #   KXNCAAMBGAME-26MAR22IOWAFL-IOWA  → BUY YES (Iowa wins, gt=0.98)
        #   KXNCAAMBGAME-26MAR22IOWAFL-FL    → BUY NO  (Florida loses, gt=0.02)
        # These are correlated bets.  Keep only the single highest-gap signal
        # per game (everything before the last '-' in a game market ID).
        def _game_prefix(market_id: str) -> Optional[str]:
            if not _is_game_market(market_id):
                return None
            parts = market_id.rsplit("-", 1)
            return parts[0] if len(parts) == 2 else None

        game_buckets: dict = defaultdict(list)
        non_game_signals: List[GapSignal] = []
        for sig in signals:
            gp = _game_prefix(sig.market_to_buy.market_id)
            if gp:
                game_buckets[gp].append(sig)
            else:
                non_game_signals.append(sig)

        game_deduped: List[GapSignal] = []
        for gp, gp_sigs in game_buckets.items():
            # Keep only the best signal per game: prefer mid-range price over
            # extreme, then highest gap.
            gp_sigs.sort(key=lambda s: (
                0 if 0.15 <= s.target_price <= 0.85 else 1,
                -s.effective_gap,
            ))
            kept = gp_sigs[0]
            game_deduped.append(kept)
            if len(gp_sigs) > 1:
                dropped = [s.market_to_buy.market_id for s in gp_sigs[1:]]
                logger.info(
                    "ResolutionBot: game dedup %s — kept %s (gap=%.3f), "
                    "dropped opposite side(s): %s",
                    gp, kept.market_to_buy.market_id, kept.effective_gap, dropped,
                )

        signals = game_deduped + non_game_signals

        # ── Pass 2: source+direction deduplication ────────────────────────────
        # Cap at MAX_SIGNALS_PER_SOURCE_ACTION per (source, direction) bucket
        # so a single instrument (e.g. Nasdaq) can't consume the whole bankroll
        # with 40 correlated contracts expressing the same directional bet.
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
        # Skip markets that have timed out unfilled 3+ times this session.
        base_mid = mid.rsplit("-", 1)[0] if mid.count("-") > 2 else mid
        if base_mid in self._unfilled_timeout_perm_skipped:
            return None
        # Skip Polymarket tokens whose orderbook returned 404 (stale/invalid).
        # No retry — perm-skip is set by _get_live_book on first 404 encounter.
        if mid in self._orderbook_perm_skipped:
            return None
        if _is_in_orderbook_cooldown(mid):
            if self._force_test:
                logger.info(
                    "ResolutionBot: [FORCE-TEST] bypassing order-book cooldown for %s "
                    "(ghost trade doesn't need fill)", mid
                )
            else:
                logger.info(
                    "ResolutionBot: SKIP %s – order-book cooldown active "
                    "(signal passed confidence, blocked at execution)", mid
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

        # Diagnostic logging for financial bracket markets (next 3 cycles)
        if _is_financial_bracket_market(mid):
            if ob_live is None:
                logger.info(
                    "ResolutionBot: orderbook state for %s: "
                    "ask=None bid=None ask_size=None bid_size=None mid=None raw_response=None",
                    mid
                )
            else:
                logger.info(
                    "ResolutionBot: orderbook state for %s: "
                    "ask=%.4f bid=%.4f ask_size=%s bid_size=%s mid=%.4f raw_response=OrderBook",
                    mid,
                    ob_live.best_yes_ask or 0.0,
                    ob_live.best_yes_bid or 0.0,
                    getattr(ob_live, 'yes_ask_size', 'unknown'),
                    getattr(ob_live, 'yes_bid_size', 'unknown'),
                    ob_live.mid_price or 0.0
                )

        if ob_live is None:
            if self._force_test:
                logger.info(
                    "ResolutionBot: [FORCE-TEST] bypassing order book check for %s "
                    "(book empty but ghost trade doesn't need fill)", mid
                )
                # ob_live stays None; depth_ratio stays None (scorer skips penalty);
                # limit_price will fall back to signal.target_price below.
            elif _is_game_market(mid) or _is_financial_bracket_market(mid):
                if self._dry_run:
                    # Ghost mode: placing into an empty book produces unfillable
                    # PENDING orders that distort the ghost dataset.  Skip entirely.
                    log_gate_event(
                        ticker=mid,
                        gate=gn.GATE_EXECUTOR_PRETRADE,
                        decision="skip",
                        reason=gn.REASON_EMPTY_BOOK_GHOST,
                        platform=market.platform,
                    )
                    logger.info(
                        "ResolutionBot: SKIP_EMPTY_BOOK_GHOST %s — "
                        "game/bracket book empty, not placing ghost trade "
                        "(would be unfillable)",
                        mid,
                    )
                    self._skip_empty_book_ghost_count += 1
                    return None
                # Live mode: intermittent book data between trades.  The book
                # appearing empty does NOT mean the market is dead.  Fall through
                # with ob_live=None; limit_price falls back to signal.target_price
                # below.  2-min cooldown at limit-price step keeps retries tight.
                logger.info(
                    "ResolutionBot: %s order book empty — "
                    "proceeding with signal.target_price=%.3f as limit (bracket bypass)",
                    mid, signal.target_price,
                )
            else:
                # Empty order book — scanner price is unverifiable. Placing an order
                # based on stale bulk-API data (e.g. Kalshi's default yes_bid=99)
                # creates unfillable limit orders. Skip entirely and suppress.
                # During FRED hunt window, shorten to 2 min so the signal retries
                # quickly while the market reprices after the data release.
                _empty_cooldown_min = 30
                if self._fred_src is not None and self._calendar is not None:
                    _fs = self._fred_src.get_series_for_market(market)
                    if _fs is not None and self._calendar.get_active_window(_fs) == "hunt":
                        _empty_cooldown_min = 2
                logger.info(
                    "ResolutionBot: SKIP %s – order book empty, cannot verify scanner "
                    "price %.3f (would create unfillable limit order)",
                    mid, signal.target_price,
                )
                _set_orderbook_cooldown(mid, minutes=_empty_cooldown_min)
                self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
                return None

        # For cross-platform signals, populate depth_ratio now so the confidence
        # scorer can apply the liquidity penalty before deciding to pass/skip.
        # Skip if ob_live is None (force-test empty-book bypass); depth_ratio
        # stays None and the scorer skips the liquidity penalty by design.
        if signal.signal_type == "cross_platform" and ob_live is not None:
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
                # Track consecutive confidence failures for permanent skip
                _conf_count = self._confidence_fail_count.get(mid, 0) + 1
                self._confidence_fail_count[mid] = _conf_count
                if _conf_count >= 3:
                    self._unfilled_timeout_perm_skipped.add(mid)
                    log_gate_event(
                        ticker=mid,
                        gate=gn.GATE_EXECUTOR_PRETRADE,
                        decision="skip",
                        reason=gn.REASON_PERM_SKIP_CONFIDENCE_FAILURES,
                        platform=market.platform,
                        extra={
                            "source_confidence": score.source_confidence,
                            "confidence_gate": CONFIDENCE_GATE_LIVE,
                            "consecutive_failures": _conf_count,
                        },
                    )
                    logger.info(
                        "ResolutionBot: perm-skipping %s after %d consecutive "
                        "confidence failures (source=%.2f clarity=%.2f vs gate=%.2f)",
                        mid, _conf_count,
                        score.source_confidence, score.resolution_clarity,
                        CONFIDENCE_GATE_LIVE,
                    )
                else:
                    logger.info(
                        "ResolutionBot: SKIP %s – %s", mid, score.skip_reason
                    )
                return None
        else:
            # Confidence passed — reset failure counter if any
            self._confidence_fail_count.pop(mid, None)
            self._unfilled_timeout_perm_skipped.discard(mid)

        # ── GT re-fetch (pre-freshness) ────────────────────────────────────────
        # GT is fetched once at cycle start. Rate-limited cycles can stretch
        # 30-60+ seconds, aging out the data before execution. Yahoo cache
        # TTL = 60s = GT_FRESHNESS_SECONDS, so a re-fetch at >60s triggers a
        # cache miss and gets fresh data automatically — no cache invalidation
        # needed. If re-fetch fails, original stale GT falls through to the
        # freshness gate below and is blocked as before.
        if not gt.is_fresh(GT_FRESHNESS_SECONDS):
            _gt_old_age = gt.gt_age_seconds()
            _refreshed_gt = self._ground_truth.fetch(market)
            if _refreshed_gt is not None and _refreshed_gt.ground_truth_prob is not None:
                gt = _refreshed_gt
                signal.ground_truth_result = gt
                logger.info(
                    "ResolutionBot: GT re-fetched for %s — old_age=%.0fs new_age=%.0fs",
                    market.market_id,
                    _gt_old_age if _gt_old_age is not None else -1,
                    gt.gt_age_seconds() if gt.gt_age_seconds() is not None else -1,
                )

        # ── GT freshness gate ──────────────────────────────────────────────────
        # Hard gate: refuse to enter on stale Yahoo/financial data.
        # is_fresh() returns True for sources with data_published_at=None
        # (e.g. FRED) so this only fires for financial signals with a real
        # timestamp that is older than GT_FRESHNESS_SECONDS.
        if not gt.is_fresh(GT_FRESHNESS_SECONDS):
            _gt_age = gt.gt_age_seconds()
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_EXECUTOR_PRETRADE,
                decision="skip",
                reason=gn.REASON_GT_STALE_AT_ENTRY,
                platform=market.platform,
                extra={
                    "gt_age_seconds": _gt_age,
                    "threshold_seconds": GT_FRESHNESS_SECONDS,
                    "gt_source": gt.source_name,
                },
            )
            logger.info(
                "ResolutionBot: SKIP %s gt_stale_at_entry "
                "gt_age=%.0fs threshold=%ds "
                "gt_source=%s gt_prob=%.4f market_price=%.4f",
                mid, _gt_age, GT_FRESHNESS_SECONDS,
                gt.source_name,
                gt.ground_truth_prob if gt.ground_truth_prob is not None else 0.0,
                market.yes_price,
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
        _min_gap = self._gap_detector._slippage_buffer

        # ── Hunt window aggressiveness: 30% buffer reduction ──────────────
        # During the hunt window (T+45min to T+3h after FRED update), the
        # market is often still stale while FRED has the latest data.  This
        # creates a higher-quality signal window where smaller gaps are still
        # profitable — so we reduce the slippage buffer by 30%.
        _hunt_window: Optional[str] = None
        if self._calendar is not None and self._fred_src is not None:
            _fred_series = self._fred_src.get_series_for_market(market)
            if _fred_series is not None:
                _hunt_window = self._calendar.get_active_window(_fred_series)
                if _hunt_window == "hunt":
                    _min_gap *= 0.70  # 30% reduction
                    logger.debug(
                        "GapDetector: %s in hunt window for %s, buffer reduced to %.3f",
                        market.market_id, _fred_series, _min_gap,
                    )

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

            # ── Market-conviction filter ────────────────────────────────────
            # When the market has converged to near-certainty (yes_price <=
            # EXTREME_CONVICTION_LOW or >= EXTREME_CONVICTION_HIGH), the
            # LARGE_DIVERGENCE almost certainly reflects a stale or misrouted
            # GT signal rather than a real edge.  Block in both ghost and live
            # modes — ghost-trade accuracy tracking of these patterns has
            # empirically yielded 0 correct calls and pollutes the track record.
            if (
                market.yes_price <= EXTREME_CONVICTION_LOW
                or market.yes_price >= EXTREME_CONVICTION_HIGH
            ):
                log_gate_event(
                    ticker=mid,
                    gate=gn.GATE_EXECUTOR_PRETRADE,
                    decision="skip",
                    reason=gn.REASON_LARGE_DIVERGENCE_EXTREME,
                    platform=market.platform,
                    extra={
                        "gap_pct": gap_pct,
                        "market_price": market.yes_price,
                        "gt_prob": gt.ground_truth_prob if gt else None,
                    },
                )
                logger.info(
                    "ResolutionBot: SKIP %s large_divergence_extreme_market "
                    "yes_price=%.4f gt_prob=%.4f gt_source=%s "
                    "gt_confidence=%.2f gap_pct=%.1f%% "
                    "— market conviction too high to trust GT",
                    mid,
                    market.yes_price,
                    gt.ground_truth_prob if gt else float("nan"),
                    gt.source_name if gt else "unknown",
                    gt.confidence if gt else 0.0,
                    gap_pct,
                )
                return None

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
        # Skip entirely in force-test mode when ob_live is None (no live price
        # available; ghost trades don't place real orders so drift doesn't matter).
        live_price = ob_live.mid_price if ob_live is not None else signal.target_price
        drift = abs(live_price - signal.target_price)
        # Relative threshold: 15% of scanner price, floor 4¢.
        # Catches cheap markets that move 48% but only 12¢ in absolute terms.
        _stale_threshold = max(
            signal.target_price * STALE_PRICE_RELATIVE_THRESHOLD,
            STALE_PRICE_ABSOLUTE_FLOOR,
        )
        if drift > _stale_threshold:
            gt_prob = (
                gt.ground_truth_prob
                if gt and gt.ground_truth_prob is not None
                else signal.reference_price
            )
            live_gap = abs(live_price - gt_prob)
            live_effective_gap = live_gap - signal.taker_fee * 2
            if live_effective_gap < self._gap_detector._slippage_buffer:
                logger.info(
                    "ResolutionBot: SKIP %s – stale scanner %.3f → live %.3f "
                    "(drift=%.3f > threshold=%.3f), recalc gap %.3f below min",
                    mid, signal.target_price, live_price,
                    drift, _stale_threshold, live_effective_gap,
                )
                return None
            logger.info(
                "ResolutionBot: STALE corrected %s – scanner %.3f → live %.3f "
                "(drift=%.3f > threshold=%.3f), live effective_gap=%.3f – proceeding",
                mid, signal.target_price, live_price,
                drift, _stale_threshold, live_effective_gap,
            )
            signal.target_price = live_price
            signal.effective_gap = live_effective_gap
            # EV recheck: now that the price has been updated to the live price,
            # re-run the MIN_EV gate.  The original EV check (below) ran against
            # the stale scanner price — it may have passed only because the stale
            # price created a phantom gap.  This catches e.g. BUY YES entries
            # that looked cheap at the scanner price but are now at 0.99 live.
            _stale_gt_prob = (
                gt.ground_truth_prob
                if gt and gt.ground_truth_prob is not None
                else signal.reference_price
            )
            _stale_ev_passes, _stale_ev = _check_minimum_ev(
                live_price, _stale_gt_prob, signal.action
            )
            if not _stale_ev_passes:
                logger.info(
                    "ResolutionBot: SKIP %s stale_price_ev_recheck_failed "
                    "ev=%.4f threshold=%.2f "
                    "(action=%s live_price=%.4f gt_prob=%.4f)",
                    mid, _stale_ev, MIN_EV,
                    signal.action, live_price, _stale_gt_prob,
                )
                return None

        # Fee check: recompute with live price (gap_detector may have used scanner price).
        # Recompute fresh from current target price so we use one consistent fee.
        # Kalshi fee is deterministic from price — no API call, always current.
        fee = self._fee_cache.get_taker_fee(
            market.platform, mid, price=signal.target_price, force_refresh=True
        )
        gt_prob_for_gap = (
            gt.ground_truth_prob
            if gt and gt.ground_truth_prob is not None
            else signal.reference_price
        )
        live_effective_gap = abs(signal.target_price - gt_prob_for_gap) - fee * 2
        if live_effective_gap < self._gap_detector._slippage_buffer:
            logger.info(
                "ResolutionBot: SKIP %s – live effective_gap=%.3f below "
                "slippage buffer %.3f (one_way_fee=%.4f target=%.3f gt=%.3f)",
                mid, live_effective_gap, self._gap_detector._slippage_buffer,
                fee, signal.target_price, gt_prob_for_gap,
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
            _ev_count = self._ev_fail_count.get(mid, 0) + 1
            self._ev_fail_count[mid] = _ev_count
            if _ev_count >= 3:
                self._unfilled_timeout_perm_skipped.add(mid)
                logger.warning(
                    "ResolutionBot: perm-skipping %s after %d consecutive EV failures",
                    mid, _ev_count,
                )
            return None
        logger.info(
            "ResolutionBot: EV check PASS %s — EV=%.3f "
            "(action=%s price=%.3f gt_prob=%.3f)",
            mid, _ev, signal.action, signal.target_price, gt_prob_for_gap,
        )

        # ── Re-entry cooldown check ────────────────────────────────────────────
        # Block re-entry into a market (or any bracket in the same date-series)
        # within 30 min of an exit.  Without this, the bot immediately re-enters
        # resolved markets, compounds ghost P&L into the next size, and produces
        # a bankroll-compounding spiral (e.g. $553 → $607 → $5,229 on KXBRENTD).
        _now_ts = time.time()
        # Purge expired cooldowns lazily to keep the dict from growing unbounded.
        self._exit_cooldowns = {k: v for k, v in self._exit_cooldowns.items() if v > _now_ts}
        self._series_exit_cooldowns = {k: v for k, v in self._series_exit_cooldowns.items() if v > _now_ts}
        if mid in self._exit_cooldowns:
            _remaining = int(self._exit_cooldowns[mid] - _now_ts) // 60
            logger.info(
                "ResolutionBot: SKIP %s – exit cooldown active (%d min remaining)",
                mid, _remaining,
            )
            return None
        _series_pfx = _bracket_prefix(mid)
        if _series_pfx is not None and _series_pfx in self._series_exit_cooldowns:
            _remaining = int(self._series_exit_cooldowns[_series_pfx] - _now_ts) // 60
            logger.info(
                "ResolutionBot: SKIP %s – series exit cooldown active for %s (%d min remaining)",
                mid, _series_pfx, _remaining,
            )
            return None

        # Size using fractional Kelly (time-to-resolution weighting applied inside)
        size_usd = self._compute_size(
            signal, score.source_confidence, score.resolution_clarity
        )
        if size_usd < 1.0:
            logger.info("ResolutionBot: SKIP %s – size too small ($%.2f)", mid, size_usd)
            return None

        # Rollover risk: futures contract is within its quarterly changeover window.
        # The continuous-contract price can gap as the front month rolls, which may
        # create spurious signals.  Trade is still allowed — the margin/confidence
        # gates already filtered out weak setups — but position size is cut to 25%
        # so the bot is not heavily exposed if the price gaps at changeover.
        _gt_raw = (signal.ground_truth_result.raw_data or {}) if signal.ground_truth_result else {}
        if _gt_raw.get("rollover_risk"):
            _full_size = size_usd
            size_usd = round(size_usd * 0.25, 2)
            logger.warning(
                "ResolutionBot: ROLLOVER_RISK — %s (%s) is in the quarterly futures "
                "rollover window; sizing down $%.2f → $%.2f (25%% of normal)",
                mid, _gt_raw.get("symbol", "?"), _full_size, size_usd,
            )
            if size_usd < 1.0:
                logger.info(
                    "ResolutionBot: SKIP %s – post-rollover size too small ($%.2f)",
                    mid, size_usd,
                )
                return None

        # ── Per-series exposure cap ────────────────────────────────────────────
        # All markets sharing a root ticker (e.g. KXPAYROLLS) are driven by the
        # same underlying data point and are 100% correlated.  Cap total active
        # exposure per root-series at MAX_SERIES_EXPOSURE_FRACTION (15% live,
        # 50% ghost so validation can accumulate enough positions per series).
        series_root = _series_root(mid)
        if series_root is not None:
            series_exposure = sum(
                r.size_usd for r_id, r in self._positions.items()
                if _series_root(r_id) == series_root
            )
            _cap_fraction = (
                MAX_SERIES_EXPOSURE_FRACTION_GHOST
                if self._dry_run
                else MAX_SERIES_EXPOSURE_FRACTION
            )
            max_series    = self._bankroll.total_usd * _cap_fraction
            existing_pct  = series_exposure / self._bankroll.total_usd * 100
            if series_exposure + size_usd > max_series:
                log_gate_event(
                    ticker=mid,
                    gate=gn.GATE_EXECUTOR_PRETRADE,
                    decision="skip",
                    reason=gn.REASON_SERIES_CAP,
                    platform=market.platform,
                    extra={
                        "series_root": series_root,
                        "existing_pct": existing_pct,
                        "cap_pct": round(_cap_fraction * 100),
                    },
                )
                logger.info(
                    "ResolutionBot: SKIP %s — series exposure cap reached "
                    "(%.0f%% already in %s, max %d%%)",
                    mid, existing_pct, series_root,
                    round(_cap_fraction * 100),
                )
                if self._dry_run and self._paper_log is not None:
                    self._paper_log.log_cap_blocked(
                        market_id=mid,
                        action=signal.action,
                        entry_price=signal.target_price,
                        size_usd=size_usd,
                        gt_prob=gt_prob_for_gap,
                        gap=signal.effective_gap,
                        series_root=series_root,
                        series_exposure=series_exposure,
                        max_series_exposure=max_series,
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
        if _is_game_market(mid):
            _ob_cooldown_minutes = 2          # NBA quarters are 12 min; 30 min kills 2.5
        elif _is_financial_bracket_market(mid) and _is_us_market_hours():
            _ob_cooldown_minutes = 5          # futures can revive within minutes during hours
        else:
            _ob_cooldown_minutes = 30
        # During FRED hunt window, shorten to 2 min for FRED-linked markets so
        # empty-book retries are fast while the market reprices after release.
        if _ob_cooldown_minutes > 2 and self._fred_src is not None and self._calendar is not None:
            _fred_series_id = self._fred_src.get_series_for_market(market)
            if _fred_series_id is not None and self._calendar.get_active_window(_fred_series_id) == "hunt":
                _ob_cooldown_minutes = 2
        if signal.action == "buy_yes":
            limit_price = ob_live.best_yes_ask if ob_live is not None else None
            if limit_price is None:
                if self._force_test:
                    logger.info(
                        "ResolutionBot: [FORCE-TEST] bypassing YES ask check for %s "
                        "(book empty, using signal.target_price for ghost trade)", mid
                    )
                    limit_price = signal.target_price
                elif _is_game_market(mid) or _is_financial_bracket_market(mid):
                    # Game / financial bracket bypass: use signal.target_price as
                    # limit.  The scanner price is the last-traded mid; placing at
                    # this price creates a resting limit order that fills on the
                    # next trade.
                    logger.info(
                        "ResolutionBot: %s no YES ask — "
                        "using signal.target_price=%.3f as limit (bracket bypass)", mid, signal.target_price
                    )
                    limit_price = signal.target_price
                else:
                    logger.info(
                        "ResolutionBot: SKIP %s – no YES ask in order book "
                        "(no sellers to fill against)", mid
                    )
                    _set_orderbook_cooldown(mid, minutes=_ob_cooldown_minutes)
                    self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
                    return None
        else:
            # BUY_NO: order.price is the YES price field for Kalshi.
            # Using best_yes_bid places our NO bid at (1 - best_yes_bid),
            # which exactly matches the current NO ask → taker fill.
            limit_price = ob_live.best_yes_bid if ob_live is not None else None
            if limit_price is None:
                if self._force_test:
                    logger.info(
                        "ResolutionBot: [FORCE-TEST] bypassing YES bid check for %s "
                        "(book empty, using signal.target_price for ghost trade)", mid
                    )
                    limit_price = signal.target_price
                elif _is_game_market(mid) or _is_financial_bracket_market(mid):
                    # Game / financial bracket bypass: use signal.target_price as limit.
                    logger.info(
                        "ResolutionBot: %s no YES bid — "
                        "using signal.target_price=%.3f as limit (bracket bypass)", mid, signal.target_price
                    )
                    limit_price = signal.target_price
                else:
                    logger.info(
                        "ResolutionBot: SKIP %s – no YES bid in order book "
                        "(no NO sellers to fill against)", mid
                    )
                    _set_orderbook_cooldown(mid, minutes=_ob_cooldown_minutes)
                    self._registry.clear_urgent(mid)   # demote T1→T2 if signal_urgent
                    return None
        logger.info(
            "ResolutionBot: limit_price=%.4f (action=%s ask=%.4f bid=%.4f) for %s",
            limit_price, signal.action,
            (ob_live.best_yes_ask or 0.0) if ob_live is not None else 0.0,
            (ob_live.best_yes_bid or 0.0) if ob_live is not None else 0.0,
            mid,
        )

        # ── Walk-with-clamp: cap fill to live book depth ──────────────────────
        # Replaces the best-level read with a level-by-level walk and clamps
        # size_usd to what the book can actually absorb. Vaccine against the
        # ghost-mode phantom-P&L class (e.g. 763-contract fills at 0.06¢ on
        # KXAAAGASD where the book held a small fraction of that depth).
        _fill = self._walk_entry_book(
            ob_live, signal.action, size_usd, limit_price, mid, market, "entry_main",
        )
        if _fill is None:
            return None
        size_usd = _fill[0]
        limit_price = _fill[1]

        # ── Extreme-price guard ───────���──────────────────────────────��───────
        # When the cost per contract is below MIN_EFFECTIVE_ENTRY_PRICE the
        # resulting contract count (size_usd / cost) explodes, creating
        # unrealistic leverage.  At $0.01/contract, a $500 position buys
        # 50,000 contracts — no real book supports that fill.
        _eff_cost = limit_price if signal.action == "buy_yes" else (1.0 - limit_price)
        if _eff_cost < MIN_EFFECTIVE_ENTRY_PRICE:
            logger.info(
                "ResolutionBot: SKIP %s — effective cost per contract $%.4f "
                "below minimum $%.2f (action=%s limit=%.4f) — unrealistic "
                "leverage at extreme price",
                mid, _eff_cost, MIN_EFFECTIVE_ENTRY_PRICE,
                signal.action, limit_price,
            )
            return None

        # ── Hard YES-price floor / ceiling (defense-in-depth) ────────────────
        # Blocks entries at extreme YES prices regardless of side.  The
        # MIN_EFFECTIVE_ENTRY_PRICE check above only covers the cheap-side cost
        # floor (BUY YES cheap or BUY NO expensive).  This gate covers the
        # opposite extreme: BUY YES at 0.99, BUY NO at 0.01.
        # Applies to limit_price (live YES price after orderbook fetch).
        if limit_price < MIN_ENTRY_YES_PRICE or limit_price > MAX_ENTRY_YES_PRICE:
            logger.info(
                "ResolutionBot: SKIP %s extreme_entry_price "
                "limit_price=%.4f action=%s "
                "floor=[%.2f,%.2f]",
                mid, limit_price, signal.action,
                MIN_ENTRY_YES_PRICE, MAX_ENTRY_YES_PRICE,
            )
            return None

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

        _gt_raw_entry = (signal.ground_truth_result.raw_data or {}) if signal.ground_truth_result else {}
        # Ghost orders placed with an empty order book are marked "pending":
        # they are resting limit orders that may or may not fill.  The
        # _check_pending_ghost_fills() monitor auto-cancels them after
        # _GHOST_FILL_TIMEOUT_MINUTES if the price never reaches the limit.
        _ghost_pending = (
            self._dry_run
            and order_id is not None
            and isinstance(order_id, str)
            and order_id.startswith("ghost_")
            and ob_live is None
        )
        self._positions[mid] = TradeRecord(
            market_id=mid,
            platform=market.platform,
            market=market,
            signal=signal,
            action=signal.action,
            entry_price=limit_price,
            size_usd=size_usd,
            ground_truth_prob=gt_prob,
            source_confidence=score.source_confidence,
            distance_pct=float(_gt_raw_entry.get("margin_pct", 0.0)),
            order_id=order_id,
            fill_status="pending" if _ghost_pending else "filled",
            limit_price_used=limit_price if _ghost_pending else None,
        )
        if _ghost_pending:
            logger.info(
                "ResolutionBot: ghost order %s marked PENDING (empty book at placement, "
                "limit=%.3f) — will auto-cancel after %d min if unfilled",
                mid, limit_price, _GHOST_FILL_TIMEOUT_MINUTES,
            )
        if self._paper_log is not None:
            _tier = getattr(self._registry._entries.get(mid), "tier", 0)
            _src_name = gt.source_name if gt else "cross-platform"
            self._paper_log.log_entry(
                market_id=mid,
                platform=market.platform,
                action=signal.action,
                entry_price=limit_price,
                size_usd=size_usd,
                gt_prob=gt_prob,
                gap=signal.effective_gap,
                confidence=score.source_confidence,
                source=_src_name,
                tier=_tier,
                question=market.question,
                entry_time=self._positions[mid].entry_time,
                signal_class=signal.signal_type,
            )
        self._save_positions()
        _entry = limit_price
        if signal.action == "buy_yes":
            _entry_nc = size_usd / _entry if _entry > 1e-9 else 0.0
        else:
            _no_e = 1.0 - _entry
            _entry_nc = size_usd / _no_e if _no_e > 1e-9 else 0.0
        logger.info(
            "ResolutionBot: TRADE %s %s @ %.4f size=$%.2f contracts=%.2f (order=%s)\n  → \"%s\"",
            signal.action, mid, _entry, size_usd, _entry_nc, order_id,
            market.question,
        )
        return {
            "action": signal.action,
            "question": market.question,
            "market_id": mid,
            "platform": market.platform,
            "price": _entry,
            "size_usd": size_usd,
            "num_contracts": round(_entry_nc, 4),
            "hours_left": round(market.hours_to_resolution, 1),
            "source": gt.source_name if gt else "unknown",
        }

    def place_snipe_trade(
        self, market: Market, signal: SnipeSignal,
    ) -> Optional[str]:
        """Place a trade emitted by a sniping strategy (e.g. WeatherSnipe).

        Bypasses the standard GT router / gap detector / scorer pipeline —
        the strategy module is responsible for its own gap/edge decision.
        Still applies the executor's safety gates: exclusions, position
        dedup, series exposure cap, bankroll reserve. Honors ghost mode.

        Empty-book guard differs from `_try_execute`: an empty book in the
        final-hour snipe window is itself a signal that something is wrong,
        so we log SKIP_EMPTY_BOOK_SNIPE and refuse to place a PENDING ghost
        rather than creating an unfillable resting order.

        Returns the order ID (or ghost trade ID), or None when a gate blocked.

        TODO(pre-live): Snipe positions store TradeRecord.signal=None because
        SnipeSignal isn't a GapSignal. Audited 2026-04-30 — all `.signal.`
        dereferences in this module are None-guarded so no AttributeError,
        but downstream visibility is reduced: the financial hard-stop loop
        skips snipe positions (they are not financial sources), the decay
        monitor sees `_gt_published_at=None` (no freshness reference), and
        exit records show source="unknown". Before flipping snipes to live
        mode, either populate a synthetic ground_truth_result on the wrapped
        GapSignal or add a TradeRecord.source attribute and migrate the
        attribution paths off of `signal.ground_truth_result.source_name`.
        """
        mid = market.market_id
        # Signal-class metadata — threaded into all gate_events so the
        # gate_funnel can categorize per-strategy. Defaults to "weather_snipe"
        # for legacy SnipeSignal objects which don't carry this attribute.
        signal_class = getattr(signal, "signal_class", "weather_snipe")
        logger.info(
            "ResolutionBot: SNIPE_ENTRY %s action=%s target_price=%.4f edge=%.4f class=%s",
            mid, signal.action, signal.target_price, signal.edge, signal_class,
        )

        # Phase 14b ghost-only guard (defense in depth — strategy module also
        # gates on its own dry_run flag). Refuses live placement of the new
        # weather_peak_snipe class until the per-signal-class PnL gate is in
        # place.
        if signal_class == "weather_peak_snipe" and not self._dry_run:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="skip",
                reason="ghost_only",
                platform=market.platform,
                extra={"signal_class": signal_class},
            )
            logger.info(
                "ResolutionBot: SKIP_SNIPE %s — weather_peak_snipe is "
                "ghost-only in Phase 14b v1", mid,
            )
            return None

        if self._exclusions.is_excluded(market.platform, mid):
            logger.info("ResolutionBot: SKIP_SNIPE %s — on exclusion list", mid)
            return None
        if mid in self._positions:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="skip",
                reason=gn.REASON_DEDUP,
                platform=market.platform,
                extra={"signal_class": signal_class},
            )
            logger.info(
                "ResolutionBot: SKIP_SNIPE %s — already holding position", mid,
            )
            return None
        if signal.action not in ("buy_yes", "buy_no"):
            logger.warning(
                "ResolutionBot: SKIP_SNIPE %s — invalid action %r",
                mid, signal.action,
            )
            return None

        # Empty-book guard (snipe variant: skip rather than place PENDING).
        ob_live = self._get_live_book(market)
        if ob_live is None:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="skip",
                reason=gn.REASON_EMPTY_BOOK_SNIPE,
                platform=market.platform,
                extra={"signal_class": signal_class},
            )
            logger.info(
                "ResolutionBot: SKIP_EMPTY_BOOK_SNIPE %s — book empty in "
                "final-hour window; refusing to place PENDING (would be "
                "unfillable, and an empty book here is itself suspect)", mid,
            )
            return None
        if signal.action == "buy_yes":
            limit_price = ob_live.best_yes_ask
            side_label = "YES ask"
        else:
            limit_price = ob_live.best_yes_bid
            side_label = "YES bid"
        if limit_price is None:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="skip",
                reason=gn.REASON_EMPTY_BOOK_SNIPE,
                platform=market.platform,
                extra={"signal_class": signal_class},
            )
            logger.info(
                "ResolutionBot: SKIP_EMPTY_BOOK_SNIPE %s — no %s in book",
                mid, side_label,
            )
            return None

        # Wrap the SnipeSignal in a minimal GapSignal so we can reuse the
        # existing _compute_size and _place_order paths without forking the
        # Kelly logic. ground_truth_prob and reference_price are set so the
        # GapSignal.action property derives the correct side, and Kelly sees
        # a decisive 0.99 probability on the bought side.
        if signal.action == "buy_yes":
            gt_prob_for_kelly = 0.99
            ref_price = 0.99
        else:
            gt_prob_for_kelly = 0.01
            ref_price = 0.01
        snipe_gap = GapSignal(
            signal_type="weather_snipe",
            market_to_buy=market,
            market_reference=None,
            target_price=limit_price,
            reference_price=ref_price,
            ground_truth_prob=gt_prob_for_kelly,
            effective_gap=signal.edge,
            taker_fee=0.0,
            ground_truth_result=None,
            reasoning=signal.rationale,
        )

        size_usd = self._compute_size(snipe_gap, signal.confidence)
        # Strategy-supplied per-bracket risk ceiling (Phase 14b weather peak
        # snipe sets max_risk_usd=$5). Legacy SnipeSignal objects don't carry
        # this attribute; getattr default of None disables the ceiling.
        max_risk_usd = getattr(signal, "max_risk_usd", None)
        if max_risk_usd is not None and size_usd > max_risk_usd:
            logger.info(
                "ResolutionBot: SNIPE_SIZE %s clamped $%.2f → $%.2f "
                "(strategy max_risk_usd, class=%s)",
                mid, size_usd, max_risk_usd, signal_class,
            )
            size_usd = float(max_risk_usd)
        logger.info(
            "ResolutionBot: SNIPE_SIZE %s computed=$%.2f (confidence=%.3f bankroll=$%.2f class=%s)",
            mid, size_usd, signal.confidence, self._bankroll.total_usd, signal_class,
        )
        if size_usd < 1.0:
            logger.info(
                "ResolutionBot: SKIP_SNIPE %s — size too small ($%.2f)",
                mid, size_usd,
            )
            return None

        # ── Walk-with-clamp: cap fill to live book depth ──────────────────────
        # Snipe path reaches this only when ob_live is non-None and limit_price
        # is set (empty-book SKIP_SNIPE branches at the top return early).
        _fill = self._walk_entry_book(
            ob_live, signal.action, size_usd, limit_price, mid, market, "entry_snipe",
        )
        if _fill is None:
            return None
        size_usd = _fill[0]
        limit_price = _fill[1]

        # Per-series exposure cap (same logic as _try_execute).
        series_root = _series_root(mid)
        if series_root is not None:
            series_exposure = sum(
                r.size_usd for r_id, r in self._positions.items()
                if _series_root(r_id) == series_root
            )
            cap_fraction = (
                MAX_SERIES_EXPOSURE_FRACTION_GHOST
                if self._dry_run
                else MAX_SERIES_EXPOSURE_FRACTION
            )
            max_series = self._bankroll.total_usd * cap_fraction
            if series_exposure + size_usd > max_series:
                log_gate_event(
                    ticker=mid,
                    gate=gn.GATE_SNIPE,
                    decision="skip",
                    reason=gn.REASON_SERIES_CAP,
                    platform=market.platform,
                    extra={"series_root": series_root, "signal_class": signal_class},
                )
                logger.info(
                    "ResolutionBot: SKIP_SNIPE %s — series exposure cap "
                    "($%.2f in %s, max $%.2f)",
                    mid, series_exposure, series_root, max_series,
                )
                return None

        if not self._bankroll.reserve(mid, size_usd):
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_SNIPE,
                decision="skip",
                reason=gn.REASON_BANKROLL,
                platform=market.platform,
                extra={"signal_class": signal_class},
            )
            logger.info(
                "ResolutionBot: SKIP_SNIPE %s — bankroll reserve failed", mid,
            )
            return None

        order_id = self._place_order(
            market, snipe_gap, size_usd, fee=0.0, limit_price=limit_price,
        )
        if order_id is None and not self._dry_run:
            logger.warning(
                "ResolutionBot: SNIPE order placement failed for %s — "
                "no position recorded", mid,
            )
            return None

        self._positions[mid] = TradeRecord(
            market_id=mid,
            platform=market.platform,
            market=market,
            signal=None,
            action=signal.action,
            entry_price=limit_price,
            size_usd=size_usd,
            ground_truth_prob=gt_prob_for_kelly,
            source_confidence=signal.confidence,
            distance_pct=0.0,
            order_id=order_id,
            fill_status="filled",
            limit_price_used=None,
        )

        if self._paper_log is not None:
            _tier = getattr(self._registry._entries.get(mid), "tier", 0)
            _source = (
                "WeatherPeakSnipe" if signal_class == "weather_peak_snipe"
                else "WeatherSnipe"
            )
            self._paper_log.log_entry(
                market_id=mid,
                platform=market.platform,
                action=signal.action,
                entry_price=limit_price,
                size_usd=size_usd,
                gt_prob=gt_prob_for_kelly,
                gap=signal.edge,
                confidence=signal.confidence,
                source=_source,
                tier=_tier,
                question=market.question,
                entry_time=self._positions[mid].entry_time,
                signal_class=signal_class,
            )

        self._snipes_placed_cum += 1
        self._save_positions()
        logger.info(
            "ResolutionBot: SNIPE %s %s @ %.4f size=$%.2f (order=%s) — %s",
            signal.action, mid, limit_price, size_usd, order_id,
            signal.rationale,
        )
        return order_id

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

    # ── Fill-cap (walk-with-clamp) ────────────────────────────────────────────

    def _walk_entry_book(
        self,
        ob_live: Optional[OrderBook],
        action: str,
        size_usd: float,
        requested_price: float,
        mid: str,
        market: Market,
        path: str,
    ) -> Optional[Tuple[float, float]]:
        """Walk the live book for an entry; clamp size_usd to fillable depth.

        Returns (clamped_size_usd, vwap_price) on success, or None when the
        clamp drops the size below the $1 minimum (caller should abort).

        Emits a fill_cap gate event in every case.  When ob_live is None
        (force-test or game/bracket live-mode bypass), the call is a no-op:
        the event records `no_book=True` and the requested size/price pass
        through unchanged.
        """
        walk_side = Side.YES if action == "buy_yes" else Side.NO

        if ob_live is None:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_FILL_CAP,
                decision="full",
                platform=market.platform,
                extra={
                    "path": path,
                    "action": action,
                    "requested_size_usd": round(size_usd, 2),
                    "filled_size_usd": round(size_usd, 2),
                    "requested_price": round(requested_price, 4),
                    "filled_vwap": round(requested_price, 4),
                    "levels_consumed": 0,
                    "clamped": False,
                    "no_book": True,
                    "book_snapshot": [],
                },
            )
            return size_usd, requested_price

        fill = ob_live.slippage_adjusted_price(walk_side, size_usd)
        log_gate_event(
            ticker=mid,
            gate=gn.GATE_FILL_CAP,
            decision="clamp" if fill.clamped else "full",
            platform=market.platform,
            extra={
                "path": path,
                "action": action,
                "requested_size_usd": round(size_usd, 2),
                "filled_size_usd": round(fill.filled_size_usd, 2),
                "requested_price": round(requested_price, 4),
                "filled_vwap": round(fill.vwap, 4),
                "levels_consumed": fill.levels_consumed,
                "clamped": fill.clamped,
                "book_snapshot": _book_top3_snapshot(ob_live, walk_side),
            },
        )

        if fill.filled_size_usd < 1.0:
            logger.info(
                "ResolutionBot: SKIP %s — fill_cap clamp $%.2f → $%.2f below "
                "$1 minimum (action=%s book thin)",
                mid, size_usd, fill.filled_size_usd, action,
            )
            return None

        clamped_size = round(fill.filled_size_usd, 2)
        if fill.clamped:
            logger.info(
                "ResolutionBot: FILL_CAP_CLAMP %s %s requested=$%.2f filled=$%.2f "
                "vwap=%.4f levels=%d (was limit=%.4f)",
                mid, action, size_usd, clamped_size, fill.vwap,
                fill.levels_consumed, requested_price,
            )
        return clamped_size, fill.vwap

    def _apply_fill_cap_exit(
        self,
        rec: "TradeRecord",
        exit_reason: str,
        capture: Optional[float] = None,
        dynamic_exit_threshold: Optional[float] = None,
        exit_was_decisive_gt: bool = False,
    ) -> int:
        """Walk the opposite-side book and dispatch full / partial / block.

        Returns 1 when the position fully closed, 0 when it stayed open
        (partial fill or book-insufficient block).  Block events use the
        executor_exit gate; partial and full fills emit fill_cap events.
        """
        mid = rec.market_id
        exit_path = _FILL_CAP_EXIT_PATHS.get(exit_reason, "exit_unknown")
        # Exit walks the opposite side to entry.
        exit_side = Side.NO if rec.action == "buy_yes" else Side.YES
        ob_live = self._get_live_book(rec.market)

        if ob_live is None:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_EXECUTOR_EXIT,
                decision="skip",
                reason=gn.REASON_BOOK_INSUFFICIENT_ON_EXIT,
                platform=rec.platform,
                extra={
                    "exit_reason": exit_reason,
                    "position_size_usd": round(rec.size_usd, 2),
                    "action": rec.action,
                    "no_book": True,
                },
            )
            logger.info(
                "ResolutionBot: BLOCK_EXIT %s — book unavailable; position "
                "held (size=$%.2f reason=%s)",
                mid, rec.size_usd, exit_reason,
            )
            return 0

        fill = ob_live.slippage_adjusted_price(exit_side, rec.size_usd)

        if fill.filled_size_usd <= 0:
            log_gate_event(
                ticker=mid,
                gate=gn.GATE_EXECUTOR_EXIT,
                decision="skip",
                reason=gn.REASON_BOOK_INSUFFICIENT_ON_EXIT,
                platform=rec.platform,
                extra={
                    "exit_reason": exit_reason,
                    "position_size_usd": round(rec.size_usd, 2),
                    "action": rec.action,
                    "book_snapshot": _book_top3_snapshot(ob_live, exit_side),
                },
            )
            logger.info(
                "ResolutionBot: BLOCK_EXIT %s — no depth on %s side; position "
                "held (size=$%.2f reason=%s)",
                mid, exit_side.value, rec.size_usd, exit_reason,
            )
            return 0

        # Emit fill_cap for every reachable exit (clamped or not).
        log_gate_event(
            ticker=mid,
            gate=gn.GATE_FILL_CAP,
            decision="clamp" if fill.clamped else "full",
            platform=rec.platform,
            extra={
                "path": exit_path,
                "action": rec.action,
                "exit_reason": exit_reason,
                "requested_size_usd": round(rec.size_usd, 2),
                "filled_size_usd": round(fill.filled_size_usd, 2),
                "filled_vwap": round(fill.vwap, 4),
                "levels_consumed": fill.levels_consumed,
                "clamped": fill.clamped,
                "remaining_size_usd": round(
                    max(0.0, rec.size_usd - fill.filled_size_usd), 2,
                ),
                "book_snapshot": _book_top3_snapshot(ob_live, exit_side),
            },
        )

        if not fill.clamped:
            # Full exit — compute pnl at the realistic vwap, then dispatch.
            if rec.action == "buy_yes":
                nc = rec.size_usd / rec.entry_price if rec.entry_price > 1e-9 else 0.0
                pnl = (fill.vwap - rec.entry_price) * nc
            else:
                no_entry = 1.0 - rec.entry_price
                nc = rec.size_usd / no_entry if no_entry > 1e-9 else 0.0
                pnl = (rec.entry_price - fill.vwap) * nc
            self._exit_position(
                mid, pnl, current_price=fill.vwap,
                capture=capture,
                dynamic_exit_threshold=dynamic_exit_threshold,
                exit_reason=exit_reason,
                exit_was_decisive_gt=exit_was_decisive_gt,
            )
            return 1

        # Partial exit — bank the filled portion, keep remainder open.
        self._partial_exit(rec, fill.filled_size_usd, fill.vwap, exit_reason)
        return 0

    def _partial_exit(
        self,
        rec: "TradeRecord",
        filled_size_usd: float,
        exit_vwap: float,
        exit_reason: str,
    ) -> None:
        """Exit the filled portion of a position at exit_vwap; keep the
        remainder open at the original entry_price.  Bankroll is released
        with the partial realized pnl and re-reserved for the remainder.
        """
        mid = rec.market_id
        if filled_size_usd <= 0 or filled_size_usd >= rec.size_usd:
            return  # caller routed wrong; full or zero exit belongs elsewhere

        if rec.action == "buy_yes":
            nc_total = rec.size_usd / rec.entry_price if rec.entry_price > 1e-9 else 0.0
        else:
            no_entry = 1.0 - rec.entry_price
            nc_total = rec.size_usd / no_entry if no_entry > 1e-9 else 0.0
        filled_fraction = filled_size_usd / rec.size_usd
        nc_filled = nc_total * filled_fraction
        if rec.action == "buy_yes":
            pnl = (exit_vwap - rec.entry_price) * nc_filled
        else:
            pnl = (rec.entry_price - exit_vwap) * nc_filled

        remaining_size = round(rec.size_usd - filled_size_usd, 2)

        # Bankroll: release full reservation with partial pnl, re-reserve the
        # remainder. release() also adjusts _total_usd by realized pnl, which
        # is the right semantics — the filled portion's P&L is realized.
        self._bankroll.release(mid, realized_pnl_usd=pnl)
        residual_flushed = False
        if remaining_size >= 1.0 and self._bankroll.reserve(mid, remaining_size):
            rec.size_usd = remaining_size
            self._save_positions()
        else:
            # Remainder too small (or bankroll re-reserve failed); flush it.
            self._positions.pop(mid, None)
            self._save_positions()
            residual_flushed = True

        if self._paper_log is not None:
            hold_min = (time.time() - rec.entry_time) / 60.0
            pnl_pct = pnl / filled_size_usd if filled_size_usd > 0 else 0.0
            self._paper_log.log_exit(
                market_id=mid,
                exit_price=exit_vwap,
                pnl=pnl,
                pnl_pct=pnl_pct,
                exit_reason=f"{exit_reason}_partial",
                hold_duration_minutes=hold_min,
            )

        logger.info(
            "ResolutionBot: PARTIAL_EXIT %s reason=%s filled=$%.2f at vwap=%.4f "
            "pnl=$%.2f remaining=$%.2f%s",
            mid, exit_reason, filled_size_usd, exit_vwap, pnl, remaining_size,
            " (residual flushed)" if residual_flushed else "",
        )

    def _place_order(
        self, market: Market, signal: GapSignal, size_usd: float, fee: float,
        limit_price: Optional[float] = None,
    ) -> Optional[str]:
        # limit_price: the taker limit (live ask for YES, live bid for NO).
        # Falls back to signal.target_price only when not provided (dry-run log).
        order_price = limit_price if limit_price is not None else signal.target_price
        _src_name = (
            signal.ground_truth_result.source_name
            if signal.ground_truth_result
            else ""
        )
        _paper = self._dry_run or is_paper_only(_src_name)
        if _paper:
            ghost_id = f"ghost_{market.market_id}_{int(time.time())}"
            _reason = "global dry-run" if self._dry_run else f"category paper-only ({_src_name})"
            logger.info(
                "ResolutionBot [GHOST TRADE]: %s %s @ %.4f size=$%.2f fee=%.4f "
                "(simulated — %s, order_id=%s)",
                signal.action, market.market_id, order_price, size_usd, fee, _reason, ghost_id,
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

        # Ghost-mode sizing cap: use a fixed bankroll ceiling regardless of how
        # much simulated P&L has accumulated.  The real bankroll.total_usd keeps
        # growing for tracking purposes; we only cap what feeds into sizing so
        # a compounding win streak cannot inflate positions into the hundreds.
        sizing_bankroll = (
            min(self._bankroll.total_usd, GHOST_SIZING_BANKROLL_CAP)
            if self._dry_run
            else self._bankroll.total_usd
        )
        size = kelly * frac * sizing_bankroll
        max_size = sizing_bankroll * MAX_POSITION_FRACTION
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

    @staticmethod
    def _is_financial_source(rec: "TradeRecord") -> bool:
        """Return True if this position was opened from a financial price source."""
        if not rec.signal or not rec.signal.ground_truth_result:
            return False
        src = rec.signal.ground_truth_result.source_name or ""
        return src.startswith(_FINANCIAL_SOURCE_PREFIXES)

    def _check_pending_ghost_fills(self) -> int:
        """Cancel ghost orders that were placed with an empty order book but
        haven't filled within _GHOST_FILL_TIMEOUT_MINUTES.

        Only runs in dry_run (ghost) mode.  After the timeout we check whether
        the market price has moved near the limit price (within 5¢).  If so,
        the order is considered filled and the position stays open.  Otherwise
        the position is cancelled and capital is returned.

        Returns the number of positions cancelled.
        """
        _FILL_TOLERANCE = 0.05   # price within 5¢ of limit → treat as filled
        cancelled = 0
        now = time.time()
        for mid, rec in list(self._positions.items()):
            if rec.fill_status != "pending":
                continue
            elapsed_min = (now - rec.entry_time) / 60.0
            if elapsed_min < _GHOST_FILL_TIMEOUT_MINUTES:
                continue
            # Timeout reached — check if market price moved to the limit range.
            current_price = self._get_current_price(rec.market)
            limit = rec.limit_price_used if rec.limit_price_used is not None else rec.entry_price
            if current_price is not None and abs(current_price - limit) <= _FILL_TOLERANCE:
                rec.fill_status = "filled"
                logger.info(
                    "ResolutionBot: ghost pending fill CONFIRMED %s — "
                    "price=%.3f within %.2f of limit=%.3f after %.0f min",
                    mid, current_price, _FILL_TOLERANCE, limit, elapsed_min,
                )
                # Book came alive — reset churn-prevention counters so the
                # market isn't banned if it signals again next session.
                _base = mid.rsplit("-", 1)[0] if mid.count("-") > 2 else mid
                self._unfilled_timeout_count.pop(_base, None)
                self._unfilled_timeout_perm_skipped.discard(_base)
                self._ev_fail_count.pop(mid, None)
                self._hard_stop_count.pop(mid, None)
            else:
                logger.info(
                    "ResolutionBot: cancelling unfilled ghost order %s after %.0f min "
                    "(current=%s, limit=%.3f)",
                    mid, elapsed_min,
                    f"{current_price:.3f}" if current_price is not None else "unknown",
                    limit,
                )
                self._exit_position(
                    mid, realized_pnl_usd=0.0,
                    current_price=current_price if current_price is not None else rec.entry_price,
                    exit_reason="unfilled_timeout",
                )
                cancelled += 1
                # Track consecutive unfilled timeouts per market; perm-skip after 3.
                base_mid = mid.rsplit("-", 1)[0] if mid.count("-") > 2 else mid
                count = self._unfilled_timeout_count.get(base_mid, 0) + 1
                self._unfilled_timeout_count[base_mid] = count
                if count >= 3:
                    self._unfilled_timeout_perm_skipped.add(base_mid)
                    logger.warning(
                        "ResolutionBot: perm-skipping %s after 3 unfilled timeouts",
                        base_mid,
                    )
        return cancelled

    def _monitor_positions(self) -> int:
        """Run hard-stop check then decay monitor on all open positions.

        Financial hard stop fires first: if a financial-source position has
        moved FINANCIAL_HARD_STOP_THRESHOLD points against entry, exit
        immediately and skip the decay monitor for that position.
        The decay monitor handles everything else.

        Returns the total number of exits triggered.
        """
        if not self._positions:
            return 0

        exits = 0

        # ── Pass 0: pending ghost fill timeout ───────────────────────────────
        if self._dry_run:
            exits += self._check_pending_ghost_fills()

        # ── Pass 1: financial hard stop ───────────────────────────────────────
        # Iterate over a snapshot so we can safely pop from self._positions.
        for mid, rec in list(self._positions.items()):
            if not self._is_financial_source(rec):
                continue
            current_price = self._get_current_price(rec.market)
            if current_price is None:
                continue

            # Direction-aware adverse move (positive = moved against our position).
            # entry_price = YES BID at order placement (Kalshi stores NO orders
            # using the YES price field).  current_price = YES mid from order book.
            if rec.action == "buy_yes":
                # Adverse move for buy_yes = YES price fell below entry.
                # BUT: for deep-ITM YES entries (entry_YES ≈ 0.997), the YES mid
                # on an illiquid book (bid=0.003, ask=0.997) collapses to 0.50 —
                # a mid-price artefact that is NOT a real adverse move.
                # Guard: skip the hard-stop check when entry is deep ITM AND
                # current mid is near 0.50 (the illiquid sentinel value).
                # (The spread gate in _get_current_price should prevent this from
                # ever firing with a spread >0.85, but keep the guard as defence-
                # in-depth for narrower illiquid spreads that still produce a
                # misleading mid.)
                if rec.entry_price > 0.90 and 0.40 <= current_price <= 0.60:
                    self._hard_stop_count.pop(mid, None)
                    continue
                move = rec.entry_price - current_price
            else:
                # Adverse move for buy_no = YES price rose above entry.
                # BUT: for deep-OTM NO entries (entry_YES ≈ 0.006), the YES mid
                # on an illiquid book (bid=0.006, ask=0.994) collapses to 0.50 —
                # a mid-price artefact that is NOT a real adverse move.
                # Guard: skip the hard-stop check when entry is deep OTM AND
                # current mid is near 0.50 (the illiquid sentinel value).
                if rec.entry_price < 0.10 and 0.40 <= current_price <= 0.60:
                    self._hard_stop_count.pop(mid, None)
                    continue
                move = current_price - rec.entry_price

            if move < FINANCIAL_HARD_STOP_THRESHOLD:
                continue

            src_name = rec.signal.ground_truth_result.source_name  # guaranteed non-None here
            logger.warning(
                "ResolutionBot: FINANCIAL_HARD_STOP %s [%s] — "
                "entry=%.4f current=%.4f move=%.4f >= threshold=%.4f size=$%.2f",
                mid, src_name,
                rec.entry_price, current_price, move, FINANCIAL_HARD_STOP_THRESHOLD,
                rec.size_usd,
            )
            # Walk-with-clamp dispatch: full exit at vwap, partial-fill keeps
            # remainder open, book-insufficient blocks the exit entirely.
            fully_exited = self._apply_fill_cap_exit(rec, exit_reason="hard_stop")
            exits += fully_exited
            if fully_exited:
                # Prevent immediate re-entry: 30-min cooldown on this exact market.
                _set_orderbook_cooldown(mid, minutes=30)
            # Track consecutive hard-stop triggers regardless of fill outcome.
            # A partial fill is still a hard-stop event; three in a row → perm-skip.
            _hs_count = self._hard_stop_count.get(mid, 0) + 1
            self._hard_stop_count[mid] = _hs_count
            if _hs_count >= 3:
                self._unfilled_timeout_perm_skipped.add(mid)
                logger.warning(
                    "ResolutionBot: perm-skipping %s after %d consecutive hard stops",
                    mid, _hs_count,
                )

        if not self._positions:
            return exits

        # ── Pass 2: decay monitor (remaining positions only) ─────────────────
        open_positions = []
        for rec in self._positions.values():
            current_price = self._get_current_price(rec.market)
            if current_price is None:
                continue
            _gt_published_at = None
            if rec.signal and rec.signal.ground_truth_result:
                _gt_published_at = rec.signal.ground_truth_result.data_published_at
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
                distance_pct=rec.distance_pct,
                ground_truth_published_at=_gt_published_at,
            ))

        decisions = self._decay.evaluate(open_positions)
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
                _decay_reason = {
                    DecayAction.EARLY_EXIT:    "early_exit",
                    DecayAction.STOP_LOSS:     "stop_loss",
                    DecayAction.APPROACH_EXIT: "resolution",
                }.get(decision.action, "unknown")
                # Walk-with-clamp dispatch: full exit fills at the live-book
                # VWAP, partial fills bank the filled portion and keep the rest
                # open for the next cycle, and a fully-empty exit side blocks
                # the exit entirely (executor_exit:book_insufficient_on_exit).
                rec = self._positions.get(mid)
                if rec is None:
                    continue
                fully_exited = self._apply_fill_cap_exit(
                    rec,
                    exit_reason=_decay_reason,
                    capture=decision.capture_ratio,
                    dynamic_exit_threshold=decision.dynamic_exit_threshold,
                    exit_was_decisive_gt=decision.decisive_gt,
                )
                exits += fully_exited
        return exits

    def _exit_ghost_positions_for_finals(self, ghost_exits: list) -> int:
        """
        Close open ghost positions whose game just reached CONFIRMED FINAL.

        ghost_exits: list of (market_id, winner_team) from drain_ghost_exits().
          winner_team is the canonical name (lowercased) of the winning team,
          or "" for a tie.  correct_prob = 1.0 if the YES team for this specific
          market won; YES team is determined by parsing the market_id suffix via
          get_yes_team(), which correctly handles both home-YES and away-YES
          contracts.  If YES team cannot be determined the market is skipped and
          the position is left open rather than settled on the wrong side.

        Exits at the settlement value so P&L is accurate.  Skips any live
        (non-ghost) positions — those need real order handling.
        """
        from data.sports.team_resolver import get_yes_team  # noqa: PLC0415
        exits = 0
        for market_id, winner_team in ghost_exits:
            yes_team = get_yes_team(market_id)
            if yes_team is None:
                logger.warning(
                    "ResolutionBot: cannot determine YES team for %s — "
                    "ghost exit skipped (position left open)", market_id,
                )
                continue
            if not winner_team:
                # Tie: no team won, YES contract resolves NO
                correct_prob = 0.0
            else:
                wt = winner_team.lower()
                correct_prob = 1.0 if (wt in yes_team or yes_team in wt) else 0.0
            rec = self._positions.get(market_id)
            if rec is None:
                continue
            if not rec.order_id or not rec.order_id.startswith("ghost_"):
                logger.debug(
                    "ResolutionBot: ghost final exit skipped %s — live position, "
                    "needs real order", market_id,
                )
                continue

            if rec.action == "buy_yes":
                nc = rec.size_usd / rec.entry_price if rec.entry_price > 1e-9 else 0.0
                pnl = (correct_prob - rec.entry_price) * nc
            else:
                no_cost = 1.0 - rec.entry_price
                nc = rec.size_usd / no_cost if no_cost > 1e-9 else 0.0
                pnl = (rec.entry_price - correct_prob) * nc

            outcome = "WIN" if pnl >= 0 else "LOSS"
            logger.info(
                "ResolutionBot: [GHOST EXIT] %s %s entry=%.3f settled=%.1f "
                "contracts=%.2f pnl=$%.2f [%s]",
                rec.action, market_id, rec.entry_price, correct_prob, nc, pnl, outcome,
            )
            # Debug: verify settlement value is binary (1.0 or 0.0)
            if correct_prob not in (0.0, 1.0):
                logger.error(
                    "ResolutionBot: CRITICAL — game_final exit for %s has non-binary settlement! "
                    "winner_prob=%.2f (expected 1.00 or 0.00). position_side=%s entry=%.3f",
                    market_id, correct_prob, rec.action, rec.entry_price,
                )
            logger.info(
                "ResolutionBot: game_final exit for %s: position_side=%s settlement_value=%.1f "
                "entry=%.3f pnl=$%.2f exit_reason=game_final",
                market_id, rec.action, correct_prob, rec.entry_price, pnl,
            )
            self._exit_position(
                market_id,
                realized_pnl_usd=pnl,
                current_price=correct_prob,
                exit_reason="game_final",
            )
            exits += 1
        return exits

    def _exit_position(
        self,
        market_id: str,
        realized_pnl_usd: float,
        current_price: Optional[float] = None,
        capture: Optional[float] = None,
        dynamic_exit_threshold: Optional[float] = None,
        exit_reason: str = "unknown",
        exit_was_decisive_gt: bool = False,
    ) -> None:
        rec = self._positions.pop(market_id, None)
        if not rec:
            return
        self._bankroll.release(market_id, realized_pnl_usd=realized_pnl_usd)

        # Set re-entry cooldowns so the bot cannot immediately re-enter this
        # market or any bracket in the same date-series.  This prevents the
        # compounding spiral where repeated exits feed P&L back into sizing.
        _cooldown_expiry = time.time() + _EXIT_COOLDOWN_SECONDS
        self._exit_cooldowns[market_id] = _cooldown_expiry
        _series_pfx = _bracket_prefix(market_id)
        if _series_pfx is not None:
            self._series_exit_cooldowns[_series_pfx] = _cooldown_expiry
            logger.info(
                "ResolutionBot: exit cooldown set — %s and series %s locked for %d min",
                market_id, _series_pfx, _EXIT_COOLDOWN_SECONDS // 60,
            )
        else:
            logger.info(
                "ResolutionBot: exit cooldown set — %s locked for %d min",
                market_id, _EXIT_COOLDOWN_SECONDS // 60,
            )

        # Clean up no-source tracking for this market.
        self._no_source_streak.pop(market_id, None)
        self._no_source_fast_skip_streak.pop(market_id, None)
        self._no_source_perm_skipped.discard(market_id)

        # Record in resolved history (session-only; drives 'history' command).
        # Kalshi contract economics: cost per contract = entry_price (YES) or 1-entry_price (NO).
        if rec.action == "buy_yes":
            _nc = rec.size_usd / rec.entry_price if rec.entry_price > 1e-9 else 0.0
        else:
            _no_entry = 1.0 - rec.entry_price
            _nc = rec.size_usd / _no_entry if _no_entry > 1e-9 else 0.0

        exit_price = current_price
        if exit_price is None and _nc > 0:
            # Back-calculate exit price from realized P&L: pnl = (exit - entry) * num_contracts
            offset = realized_pnl_usd / _nc
            raw = (rec.entry_price + offset) if rec.action == "buy_yes" else (rec.entry_price - offset)
            exit_price = max(0.0, min(1.0, raw))
            logger.debug(
                "ResolutionBot: back-calculated exit_price for %s: pnl=$%.2f nc=%.2f offset=%.4f "
                "raw=%.4f → clamped=%.4f", market_id, realized_pnl_usd, _nc, offset, raw, exit_price,
            )
        # Trace game_final exits to ensure they use settlement values
        if exit_reason == "game_final":
            logger.info(
                "ResolutionBot: _exit_position finalize game_final: market=%s exit_price=%.4f "
                "pnl=$%.2f entry=%.3f side=%s",
                market_id, exit_price or rec.entry_price, realized_pnl_usd, rec.entry_price, rec.action,
            )
        if capture is None:
            if rec.action == "buy_yes":
                theo = (rec.ground_truth_prob - rec.entry_price) * _nc
            else:
                theo = (rec.entry_price - rec.ground_truth_prob) * _nc
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
            exit_price=exit_price if exit_price is not None else rec.entry_price,
            num_contracts=_nc,
            pnl=realized_pnl_usd,
            capture=capture,
            confidence=rec.source_confidence,
            source=src,
            resolved_at=datetime.utcnow(),
            entered_at=datetime.utcfromtimestamp(rec.entry_time),
            dynamic_exit_threshold=dynamic_exit_threshold,
            exit_was_decisive_gt=exit_was_decisive_gt,
        ))

        if self._paper_log is not None:
            _hold_min = (time.time() - rec.entry_time) / 60.0
            _exit_p   = exit_price if exit_price is not None else rec.entry_price
            _pnl_pct  = realized_pnl_usd / rec.size_usd if rec.size_usd > 0 else 0.0
            self._paper_log.log_exit(
                market_id=market_id,
                exit_price=_exit_p,
                pnl=realized_pnl_usd,
                pnl_pct=_pnl_pct,
                exit_reason=exit_reason,
                hold_duration_minutes=_hold_min,
                exit_was_decisive_gt=exit_was_decisive_gt,
            )

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
        _thresh_str = f" exit_thresh={dynamic_exit_threshold:.0%}" if dynamic_exit_threshold is not None else ""
        logger.info(
            "ResolutionBot: EXITED %s entry=%.4f exit=%.4f contracts=%.2f pnl=$%.2f%s",
            market_id, rec.entry_price, exit_price if exit_price is not None else rec.entry_price, _nc, realized_pnl_usd,
            _thresh_str,
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

    def ghost_clear_positions(self) -> int:
        """
        Remove all ghost positions from memory and delete data/runtime/ghost_positions.json.

        Only removes positions whose order_id starts with "ghost_" or "dry_"
        (i.e. positions that were never real orders).  Live positions are untouched.
        Returns the number of ghost positions cleared.
        """
        import os as _os
        ghost_ids = [
            mid for mid, rec in self._positions.items()
            if rec.order_id is not None and (
                rec.order_id.startswith("ghost_") or rec.order_id.startswith("dry_")
            )
        ]
        for mid in ghost_ids:
            self._bankroll.release(mid, realized_pnl_usd=0.0)
            del self._positions[mid]
        # Delete the on-disk ghost file so it can't reload on next startup.
        gf = self._GHOST_POSITIONS_FILE
        if _os.path.exists(gf):
            try:
                _os.remove(gf)
                logger.info("ResolutionBot: deleted %s", gf)
            except OSError as exc:
                logger.warning("ResolutionBot: failed to delete %s: %s", gf, exc)
        if ghost_ids:
            self._save_positions()
        logger.info("ResolutionBot: ghost-clear removed %d ghost position(s)", len(ghost_ids))
        return len(ghost_ids)

    def get_resolved_positions(self) -> List[ResolvedPosition]:
        """Return all trades resolved this session (exits from decay monitor)."""
        return list(self._resolved_positions)

    def get_paper_log(self) -> Optional[PaperTradeLog]:
        """Return the PaperTradeLog instance (dry-run only; None in live mode)."""
        return self._paper_log

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
                    ((1.0 - current_price) - rec.entry_price) * rec.size_usd
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

        Checks the WS orderbook cache first for Kalshi markets (< 30s staleness).
        Falls back to REST if the WS book is missing or stale.

        Polymarket 404: the token is stale or delisted.  The market_id is added
        to _orderbook_perm_skipped so _try_execute() short-circuits on all
        subsequent cycles without issuing another API call.
        """
        # ── WS cache fast path (Kalshi only) ─────────────────────────────
        if self._kalshi_ws is not None and market.platform == "kalshi":
            ws_book = self._kalshi_ws.get_book(market.market_id)
            if ws_book is not None and ws_book.mid_price is not None:
                age = self._kalshi_ws.get_book_age(market.market_id)
                if age is not None and age < 30.0:
                    logger.debug(
                        "WS book hit for %s (age=%.1fs)", market.market_id, age
                    )
                    return ws_book
                else:
                    logger.debug(
                        "WS book stale for %s (age=%.1fs), falling back to REST",
                        market.market_id, age or -1,
                    )

        # ── REST fallback ─────────────────────────────────────────────────
        try:
            client = self._poly if market.platform == "polymarket" else self._kalshi
            if not client:
                return None
            ob = client.get_order_book(market.market_id)
            if ob.mid_price is not None:
                return ob
            # API returned an OrderBook but it's empty (no orders on either side)
            logger.info(
                "ResolutionBot: _get_live_book %s → API returned OK but empty book "
                "(best_ask=%s best_bid=%s yes_asks=%d yes_bids=%d)",
                market.market_id,
                ob.best_yes_ask, ob.best_yes_bid,
                len(ob.yes_asks) if ob.yes_asks else 0,
                len(ob.yes_bids) if ob.yes_bids else 0,
            )
            return None
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                self._orderbook_perm_skipped.add(market.market_id)
                logger.warning(
                    "ResolutionBot: perm-skipping %s — orderbook 404 (stale/invalid token)",
                    market.market_id,
                )
                return None
            logger.info(
                "ResolutionBot: _get_live_book %s → HTTP error: %s",
                market.market_id, exc,
            )
            return None
        except Exception as exc:
            logger.info(
                "ResolutionBot: _get_live_book %s → EXCEPTION: %s",
                market.market_id, exc,
            )
            return None

    def _get_current_price(self, market: Market) -> Optional[float]:
        # ── WS cache fast path (Kalshi only) ─────────────────────────────
        if self._kalshi_ws is not None and market.platform == "kalshi":
            ws_book = self._kalshi_ws.get_book(market.market_id)
            if ws_book is not None and ws_book.mid_price is not None:
                age = self._kalshi_ws.get_book_age(market.market_id)
                if age is not None and age < 30.0:
                    # Apply the same spread gate as the REST path.
                    ask = ws_book.best_yes_ask
                    bid = ws_book.best_yes_bid
                    if ask is not None and bid is not None:
                        spread = ask - bid
                        if spread > ILLIQUID_SPREAD_THRESHOLD:
                            logger.debug(
                                "ResolutionBot: _get_current_price %s — WS illiquid spread %.3f "
                                "(bid=%.3f ask=%.3f) > threshold %.2f, suppressing",
                                market.market_id, spread, bid, ask,
                                ILLIQUID_SPREAD_THRESHOLD,
                            )
                            return None
                    return ws_book.mid_price

        # ── REST fallback ─────────────────────────────────────────────────
        try:
            client = self._poly if market.platform == "polymarket" else self._kalshi
            if not client:
                return None
            ob = client.get_order_book(market.market_id)
            # Spread gate: when both sides are present but the book is illiquid
            # (e.g. bid=0.006, ask=0.997), the arithmetic mid (~0.50) is a noise
            # artefact, not a real price signal.  Return None so all downstream
            # consumers (hard stop, decay monitor, stale drift) skip gracefully.
            ask = ob.best_yes_ask
            bid = ob.best_yes_bid
            if ask is not None and bid is not None:
                spread = ask - bid
                if spread > ILLIQUID_SPREAD_THRESHOLD:
                    logger.debug(
                        "ResolutionBot: _get_current_price %s — illiquid spread %.3f "
                        "(bid=%.3f ask=%.3f) > threshold %.2f, suppressing mid_price",
                        market.market_id, spread, bid, ask, ILLIQUID_SPREAD_THRESHOLD,
                    )
                    return None
            return ob.mid_price
        except Exception:
            return None

    # ── Position persistence ───────────────────────────────────────────────────

    _GHOST_POSITIONS_FILE = "data/runtime/ghost_positions.json"

    def _save_positions(self) -> None:
        """Persist all open positions to disk.

        Live mode  → state store ("open_positions" key).
        Ghost mode → data/runtime/ghost_positions.json
        """
        import json as _json
        from pathlib import Path as _Path

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
                "fill_status": rec.fill_status,
                "limit_price_used": rec.limit_price_used,
            }

        if self._dry_run:
            payload = {
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "positions": data,
            }
            try:
                _Path(self._GHOST_POSITIONS_FILE).write_text(
                    _json.dumps(payload, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                logger.warning("ResolutionBot: failed to save ghost positions: %s", exc)
            return

        if not self._state:
            return
        self._state.set("open_positions", data)

    def _load_positions(self) -> None:
        """Reload open positions from disk on startup.

        Ghost mode → load from data/runtime/ghost_positions.json (separate from live).
        Live mode  → load from state store ("open_positions" key).
        """
        import json as _json
        from pathlib import Path as _Path

        if self._dry_run:
            gf = _Path(self._GHOST_POSITIONS_FILE)
            if not gf.exists():
                return
            try:
                payload = _json.loads(gf.read_text(encoding="utf-8"))
                data = payload.get("positions", {})
                saved_at = payload.get("saved_at", "unknown")
                logger.info(
                    "ResolutionBot: loading ghost positions from %s (saved_at=%s)",
                    self._GHOST_POSITIONS_FILE, saved_at,
                )
            except Exception as exc:
                logger.warning(
                    "ResolutionBot: failed to load ghost positions: %s", exc
                )
                return
        elif not self._state:
            return
        else:
            data: dict = self._state.get("open_positions", {})
        if not data:
            return

        loaded = 0
        skipped = 0
        expired_count = 0
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
                    if self._dry_run:
                        expired_count += 1
                    else:
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
                    fill_status=saved.get("fill_status", "filled"),
                    limit_price_used=saved.get("limit_price_used"),
                )
                loaded += 1

            except Exception as exc:
                logger.warning(
                    "ResolutionBot: failed to restore position %s: %s", mid, exc,
                )
                skipped += 1

        if expired_count:
            logger.info(
                "ResolutionBot: expired %d stale ghost position(s) on load "
                "(resolution date already passed)", expired_count,
            )
            skipped += expired_count
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
