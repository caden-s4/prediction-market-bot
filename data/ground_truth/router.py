"""
data.ground_truth.router – routes a flagged market to the correct data source.

The router tries ALL registered DataSources (not stopping at the first hit) and
returns the highest-confidence tradeable result.  This prevents a low-confidence
in-progress sports signal from shadowing a fresh FRED release that would trade.

Source priority (used only as tiebreaker when confidence is equal):
  1. SportsLive → ESPN live polling + ShockDetector (in-progress game signals)
  2. Sports     → ESPN API (live scores, final results)
  3. Economic   → FRED / BLS (data releases, rate decisions)
  4. Financial  → Twelve Data / Alpha Vantage / Yahoo Finance (prices)
  5. Congress   → Congress.gov (bill passage, signed/vetoed legislation)
  6. Regulatory → Federal Register / CourtListener (filings, rulings)

If no source can handle the market, returns None and logs per-source failure
reasons at DEBUG level so you can track which categories need new data sources.

A result validator runs as a post-step before returning any tradeable result:
  gap < 4%   → INFO  (small edge, likely already priced in)
  gap 4–40%  → proceed normally
  gap > 40%  → WARNING + requires_human_review=True flag on result;
               confidence is NOT capped so the signal can still pass the
               0.80 gate.  Executor fires a ghost trade (dry_run) or sends
               a Telegram alert and awaits approval (live mode).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import replace as dc_replace
from typing import List, Optional

from data.markets.base import Market
from .base import DataSource, GroundTruthResult
from .congress import CongressSource
from .economic import EconomicDataSource
from .economic_fred import FREDEconomicSource
from .eia import EIADataSource
from .federal_register import FederalRegisterSource
from .financial import FinancialDataSource
from .rotten_tomatoes import RottenTomatoesSource
from .sports import SportsDataSource
# SportsLiveSource is imported lazily inside _build_default_sources() to avoid
# a circular import: live_source → data.ground_truth (package __init__) → router
# → live_source.  The lazy import is safe because _build_default_sources() is
# called only once at GroundTruthRouter instantiation time, after all modules
# have finished loading.

logger = logging.getLogger(__name__)

# ── Per-category source toggles ───────────────────────────────────────────────
# Read once at import time from environment / .env file.
# Each GT_*_ENABLED variable accepts three values:
#   false  — disabled: category not scanned, no API calls, no signals
#   paper  — paper trade only: signals generated and logged but orders always simulated
#   true   — live: follows the global LIVE_TRADING setting (default)

def _gt_mode(name: str) -> str:
    """Return 'off', 'paper', or 'live' for a GT_*_ENABLED env var."""
    val = os.environ.get(name, "").strip().lower()
    if val in ("false", "0", "no", "off", "disabled", ""):
        return "off"
    if val in ("paper", "simulate", "test", "dry"):
        return "paper"
    # true / 1 / yes / on / live / real → live (follows global LIVE_TRADING)
    return "live"

_GT_SPORTS_MODE         = _gt_mode("GT_SPORTS_ENABLED")
_GT_ECONOMIC_MODE       = _gt_mode("GT_ECONOMIC_ENABLED")
_GT_FINANCIAL_MODE      = _gt_mode("GT_FINANCIAL_ENABLED")
_GT_CONGRESS_MODE       = _gt_mode("GT_CONGRESS_ENABLED")
_GT_REGULATORY_MODE     = _gt_mode("GT_REGULATORY_ENABLED")
_GT_ENTERTAINMENT_MODE  = _gt_mode("GT_ENTERTAINMENT_ENABLED")

# Source class names that are in paper-only mode — checked at order placement.
# Populated by _build_default_sources(); exported via is_paper_only().
_PAPER_ONLY_SOURCES: set[str] = set()

# Novelty / subjective prop-bet patterns that no data source can resolve.
# Markets matching these are logged as excluded_novelty and skipped entirely.
#
# Intentionally narrow — only match patterns that are UNAMBIGUOUSLY about
# tracking specific spoken words during a broadcast:
#   ✅ "What will the announcers say during Vera vs Martinez?"
#   ✅ "How many times will Joe say 'let's go' during the show?"
#   ✅ "What word will the host use to open the segment?"
#   ❌ "What will the Fed announce at the FOMC meeting?" (legitimate policy market)
#   ❌ "What will the court call the ruling?" (legitimate legal market)
#
# "announce", "call", "tweet", "post" are intentionally excluded because they
# produce false positives on FOMC/court markets.  "say" and "mention" are
# specific enough for broadcast prop bets while rarely appearing in policy markets.
# Ticker-prefix based novelty detection: these Kalshi series are always
# announcer-mention prop bets regardless of question text.
# Prefix matching is O(1) and more reliable than regex on free-form titles.
_NOVELTY_TICKER_PREFIXES = (
    "KXNBAMENTION",     # NBA announcer mention markets
    "KXNCAABMENTION",   # NCAAB announcer mention markets
    "KXNFLMENTION",     # NFL announcer mention markets
    "KXMLBMENTION",     # MLB announcer mention markets
)

_NOVELTY_RE = re.compile(
    # "What will [X] say/mention..." — spoken-word broadcast prop bets
    r"\bwhat\s+will\b.{0,80}\b(?:say|mention)\b"
    # "How many times will [X] say/do..." — explicit word-count prop bets
    r"|\bhow\s+many\s+times\s+will\b"
    # "What word/phrase will..." — word-choice prop bets
    r"|\bwhat\s+(?:word|phrase)\s+will\b",
    re.IGNORECASE | re.DOTALL,
)


def _register(
    sources: List[DataSource],
    mode: str,
    *instances: DataSource,
) -> None:
    """Append sources to the list and mark them paper-only if mode == 'paper'."""
    if mode == "off":
        return
    for src in instances:
        sources.append(src)
        if mode == "paper":
            _PAPER_ONLY_SOURCES.add(type(src).__name__)


def _build_default_sources() -> List[DataSource]:
    """Build the default source list, respecting GT_*_ENABLED toggles."""
    # Lazy import to avoid circular dependency:
    # live_source → data.ground_truth (package) → router → live_source
    from data.sports.live_source import SportsLiveSource  # noqa: PLC0415

    _PAPER_ONLY_SOURCES.clear()
    sources: List[DataSource] = []
    # SportsLiveSource is prepended before SportsDataSource so that shock
    # signals (high-confidence, sub-second latency) shadow the slower
    # final-only ESPN scoreboard results for in-progress game markets.
    _register(sources, _GT_SPORTS_MODE,       SportsLiveSource(), SportsDataSource())
    _register(sources, _GT_ECONOMIC_MODE,     EconomicDataSource(),
                                              EIADataSource(),        # active only when EIA_API_KEY is set
                                              FREDEconomicSource())   # active only when FRED_API_KEY is set
    _register(sources, _GT_FINANCIAL_MODE,    FinancialDataSource())
    _register(sources, _GT_CONGRESS_MODE,     CongressSource())
    _register(sources, _GT_REGULATORY_MODE,   FederalRegisterSource())
    _register(sources, _GT_ENTERTAINMENT_MODE, RottenTomatoesSource())
    return sources


def is_paper_only(source_name: str) -> bool:
    """Return True if the source's category is set to paper-trade mode."""
    return source_name in _PAPER_ONLY_SOURCES


def _log_active_sources(sources: List[DataSource]) -> None:
    names = [type(s).__name__ for s in sources]
    logger.info("GroundTruthRouter: active sources — %s", ", ".join(names) or "none")


class GroundTruthRouter:
    """
    Tries each data source in order and returns the best result.

    Designed to be extended: add new DataSource subclasses to _sources.
    """

    def __init__(self, sources: Optional[List[DataSource]] = None) -> None:
        if sources is not None:
            self._sources = sources
        else:
            self._sources = _build_default_sources()
        _log_active_sources(self._sources)

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        """
        Fetch ground truth for a flagged market.

        Pre-filters to claimant sources only (those whose can_handle() returns
        True), then exhausts them rather than stopping at the first tradeable
        hit so we return the highest-confidence tradeable result.
        Never raises.
        """
        # Fast pre-check: bail early if no source claims this market at all.
        # All can_handle() calls are in-memory keyword checks — no I/O.
        if not any(s.can_handle(market) for s in self._sources):
            logger.debug("GroundTruthRouter: no source can handle %s", market.market_id)
            return None

        tradeable: List[GroundTruthResult] = []
        candidates: List[GroundTruthResult] = []
        none_reasons: List[str] = []

        for source in self._sources:
            source_name = type(source).__name__

            # Guard: re-check can_handle() immediately before fetch() so that a
            # source's foreign-market or category exclusion cannot be bypassed,
            # even if a higher-level cache or pre-filter list is stale.
            if not source.can_handle(market):
                logger.debug(
                    "Router: %s skipped — can_handle=False for %s",
                    source_name, market.market_id,
                )
                continue

            logger.debug(
                "GroundTruthRouter: trying %s for %s",
                source_name, market.market_id,
            )
            try:
                result = source.fetch(market)
            except Exception as exc:
                logger.warning(
                    "GroundTruthRouter: %s raised for %s: %s",
                    source_name, market.market_id, exc,
                )
                none_reasons.append(f"{source_name}: raised {type(exc).__name__}: {exc}")
                result = None

            if result is None:
                none_reasons.append(f"{source_name}: returned None (no relevant data found)")
                continue

            logger.info(
                "GroundTruthRouter: %s → confidence=%.2f prob=%s tradeable=%s for %s",
                source_name,
                result.confidence,
                f"{result.ground_truth_prob:.2f}" if result.ground_truth_prob is not None else "None",
                result.is_tradeable,
                market.market_id,
            )

            if result.is_tradeable:
                tradeable.append(result)
            else:
                candidates.append(result)

        # Return the highest-confidence tradeable result (passes through validator)
        if tradeable:
            best = max(tradeable, key=lambda r: r.confidence)
            return self._validate_result(best, market)

        # No tradeable result – return highest-confidence candidate for logging
        if candidates:
            best = max(candidates, key=lambda r: r.confidence)
            logger.info(
                "GroundTruthRouter: best non-tradeable result confidence=%.2f for %s",
                best.confidence, market.market_id,
            )
            return best

        logger.debug("GroundTruthRouter: no source could handle %s", market.market_id)
        if none_reasons:
            logger.debug(
                "GroundTruthRouter: per-source failures for %s — %s",
                market.market_id, "; ".join(none_reasons),
            )
        return None

    def validate_result(
        self, result: GroundTruthResult, market: Market
    ) -> GroundTruthResult:
        """Public wrapper for the result validator.

        Called by the executor when constructing synthetic GT results for
        bracket markets (e.g. KXAAAGASW series) so each bracket gets its own
        gap/confidence check even though they share a single underlying fetch.
        """
        return self._validate_result(result, market)

    def recompute_bracket_prob(
        self, raw_value: float, market: Market
    ) -> Optional[float]:
        """Re-derive YES probability for a bracket market from a cached raw value.

        Delegates to the first EconomicDataSource in the source list.
        Returns None if no EconomicDataSource is registered (e.g. custom test rigs).
        """
        for src in self._sources:
            if isinstance(src, EconomicDataSource):
                return src.compute_bracket_prob(raw_value, market)
        return None

    # ── Result validator ──────────────────────────────────────────────────────

    def _validate_result(
        self, result: GroundTruthResult, market: Market
    ) -> GroundTruthResult:
        """
        Sanity-check the ground truth probability against the current market price.

        A large divergence (ground truth vs market price) is either:
          (a) a genuine mispricing — a great opportunity, OR
          (b) a data error — we've misidentified the market question

        We can't distinguish these automatically, so we flag large gaps for
        human review rather than auto-trading them.

        gap < 4%   → small edge; log at INFO so the operator knows
        gap 4–40%  → normal tradeable range; return as-is
        gap > 40%  → suspicious; attach requires_human_review=True flag.
                     Confidence is left unchanged so the signal can still
                     pass the 0.80 gate.  The executor decides what to do
                     based on dry_run mode (ghost trade vs alert + pend).
        """
        if result.ground_truth_prob is None:
            return result

        gap = abs(result.ground_truth_prob - market.yes_price)

        if gap < 0.04:
            logger.info(
                "GroundTruthRouter: SMALL_GAP market=%s gap=%.1f%% "
                "ground_truth=%.2f market_price=%.2f — edge may not cover fees",
                market.market_id, gap * 100,
                result.ground_truth_prob, market.yes_price,
            )
            return result

        if gap > 0.40:
            # LARGE_DIVERGENCE: gap is suspicious — either a real edge or a data error.
            # Previously this capped confidence at 0.70 (below the 0.80 gate) which
            # created a catch-22: the signal was blocked precisely when the gap was
            # largest, i.e. when investigation was most warranted.
            #
            # New behaviour: preserve the source's original confidence so the trade
            # can still proceed through the confidence gate.  Attach
            # requires_human_review=True so the executor can:
            #   - In ghost/dry-run mode: fire a ghost trade for accuracy tracking.
            #   - In live mode: send a Telegram alert and await manual approval.
            logger.warning(
                "GroundTruthRouter: LARGE_DIVERGENCE market=%s gap=%.1f%% "
                "ground_truth=%.2f market_price=%.2f — flagging for human review "
                "(confidence NOT capped; executor will decide based on dry_run mode)",
                market.market_id, gap * 100,
                result.ground_truth_prob, market.yes_price,
            )
            return dc_replace(
                result,
                raw_data={
                    **result.raw_data,
                    "requires_human_review": True,
                    "validator_gap_pct": round(gap * 100, 1),
                },
                reasoning=(
                    result.reasoning
                    + f" [LARGE_DIVERGENCE: gap={gap*100:.1f}% — human review flagged]"
                ),
            )

        return result

    # ── Source management ─────────────────────────────────────────────────────

    def can_any_source_handle(self, market: Market) -> bool:
        """Return True if at least one registered source claims this market.

        All can_handle() implementations are in-memory keyword checks — no I/O.
        Use this as a fast pre-filter before calling fetch() to avoid the
        router's per-source logging overhead for markets that will inevitably
        return no_source.
        """
        return any(s.can_handle(market) for s in self._sources)

    @staticmethod
    def is_novelty_market(market: Market) -> bool:
        """Return True if this market is a subjective prop bet with no machine-readable
        resolution criterion (announcer dialogue, word counts, etc.).

        These markets should be logged as excluded_novelty and skipped before
        any source routing — no GT source can resolve them, and attempting to
        do so wastes cycle time on T1 markets that are guaranteed no_source.

        Ticker-prefix check runs first (O(1), most reliable) then falls back
        to the regex pattern for markets without a known novelty ticker prefix.
        """
        if market.market_id.upper().startswith(_NOVELTY_TICKER_PREFIXES):
            return True
        return bool(_NOVELTY_RE.search(market.question))

    def add_source(self, source: DataSource) -> None:
        """Add a custom data source at the end of the priority list."""
        self._sources.append(source)

    def prepend_source(self, source: DataSource) -> None:
        """Add a high-priority custom data source at the front of the list."""
        self._sources.insert(0, source)
