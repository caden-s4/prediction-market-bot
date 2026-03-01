"""
data.ground_truth.router – routes a flagged market to the correct data source.

The router tries ALL registered DataSources (not stopping at the first hit) and
returns the highest-confidence tradeable result.  This prevents a low-confidence
in-progress sports signal from shadowing a fresh FRED release that would trade.

Source priority (used only as tiebreaker when confidence is equal):
  1. Sports     → ESPN API (live scores, final results)
  2. Economic   → FRED / BLS (data releases, rate decisions)
  3. Financial  → Twelve Data / Alpha Vantage / Yahoo Finance (prices)
  4. Congress   → Congress.gov (bill passage, signed/vetoed legislation)
  5. Regulatory → Federal Register / CourtListener (filings, rulings)

If no source can handle the market, returns None and logs per-source failure
reasons at DEBUG level so you can track which categories need new data sources.

A result validator runs as a post-step before returning any tradeable result:
  gap < 4%   → INFO  (small edge, likely already priced in)
  gap 4–40%  → proceed normally
  gap > 40%  → WARNING + confidence capped at 0.70 (human review required)
"""

from __future__ import annotations

import logging
from dataclasses import replace as dc_replace
from typing import List, Optional

from data.markets.base import Market
from .base import DataSource, GroundTruthResult
from .congress import CongressSource
from .economic import EconomicDataSource
from .federal_register import FederalRegisterSource
from .financial import FinancialDataSource
from .sports import SportsDataSource

logger = logging.getLogger(__name__)


class GroundTruthRouter:
    """
    Tries each data source in order and returns the best result.

    Designed to be extended: add new DataSource subclasses to _sources.
    """

    def __init__(self, sources: Optional[List[DataSource]] = None) -> None:
        self._sources: List[DataSource] = sources or [
            SportsDataSource(),
            EconomicDataSource(),
            FinancialDataSource(),
            CongressSource(),        # bill passage status (specific, definitive outcomes)
            FederalRegisterSource(), # regulatory filings (broad political/legal coverage)
        ]

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        """
        Fetch ground truth for a flagged market.

        Exhausts all sources rather than stopping at the first tradeable hit.
        Returns the highest-confidence tradeable result, or the
        highest-confidence non-tradeable candidate for logging if none trade.
        Never raises.
        """
        tradeable: List[GroundTruthResult] = []
        candidates: List[GroundTruthResult] = []
        none_reasons: List[str] = []

        for source in self._sources:
            source_name = type(source).__name__
            if not source.can_handle(market):
                none_reasons.append(f"{source_name}: cannot handle this market category")
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
        gap > 40%  → suspicious; cap confidence at 0.70 (below 0.80 gate)
                     so the trade requires a human to override
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
            logger.warning(
                "GroundTruthRouter: LARGE_DIVERGENCE market=%s gap=%.1f%% "
                "ground_truth=%.2f market_price=%.2f — blocking auto-trade, human review needed",
                market.market_id, gap * 100,
                result.ground_truth_prob, market.yes_price,
            )
            return dc_replace(
                result,
                confidence=min(result.confidence, 0.70),
                raw_data={
                    **result.raw_data,
                    "requires_human_review": True,
                    "validator_gap_pct": round(gap * 100, 1),
                },
                reasoning=(
                    result.reasoning
                    + f" [AUTO-TRADE BLOCKED: gap={gap*100:.1f}% requires human review]"
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

    def add_source(self, source: DataSource) -> None:
        """Add a custom data source at the end of the priority list."""
        self._sources.append(source)

    def prepend_source(self, source: DataSource) -> None:
        """Add a high-priority custom data source at the front of the list."""
        self._sources.insert(0, source)
