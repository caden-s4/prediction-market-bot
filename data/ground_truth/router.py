"""
data.ground_truth.router – routes a flagged market to the correct data source.

The router tries each registered DataSource in priority order and returns the
first result with confidence > 0 (or the highest-confidence result overall).

Source priority:
  1. Sports     → ESPN API (live scores, final results)
  2. Economic   → FRED / BLS (data releases, rate decisions)
  3. Regulatory → Federal Register / CourtListener (filings, rulings)

If no source can handle the market, returns None.

The caller (resolution bot) then checks result.is_tradeable before acting.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from data.markets.base import Market
from .base import DataSource, GroundTruthResult
from .economic import EconomicDataSource
from .federal_register import FederalRegisterSource
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
            FederalRegisterSource(),
        ]

    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        """
        Fetch ground truth for a flagged market.

        Returns the best GroundTruthResult found, or None if no source
        can provide data. Never raises.
        """
        candidates: List[GroundTruthResult] = []

        for source in self._sources:
            if not source.can_handle(market):
                continue

            logger.debug(
                "GroundTruthRouter: trying %s for %s",
                type(source).__name__, market.market_id,
            )
            try:
                result = source.fetch(market)
            except Exception as exc:
                logger.warning(
                    "GroundTruthRouter: %s raised for %s: %s",
                    type(source).__name__, market.market_id, exc,
                )
                result = None

            if result is None:
                continue

            logger.info(
                "GroundTruthRouter: %s → confidence=%.2f prob=%s tradeable=%s for %s",
                type(source).__name__,
                result.confidence,
                f"{result.ground_truth_prob:.2f}" if result.ground_truth_prob is not None else "None",
                result.is_tradeable,
                market.market_id,
            )

            if result.is_tradeable:
                return result  # First tradeable result wins

            candidates.append(result)

        # No tradeable result – return the highest-confidence candidate for logging
        if candidates:
            best = max(candidates, key=lambda r: r.confidence)
            logger.info(
                "GroundTruthRouter: best non-tradeable result confidence=%.2f for %s",
                best.confidence, market.market_id,
            )
            return best

        logger.debug("GroundTruthRouter: no data source for %s", market.market_id)
        return None

    def add_source(self, source: DataSource) -> None:
        """Add a custom data source at the end of the priority list."""
        self._sources.append(source)

    def prepend_source(self, source: DataSource) -> None:
        """Add a high-priority custom data source at the front of the list."""
        self._sources.insert(0, source)
