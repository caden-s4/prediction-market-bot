"""
data.ground_truth.base – abstract base and shared types.

A DataSource fetches structured, factual data relevant to a prediction
market and returns a GroundTruthResult describing the likely true outcome.

Confidence scale:
  0.9-1.0  Hard: official API returns final result (score, data release)
  0.7-0.9  Strong: official government filing or regulatory document
  0.5-0.7  Moderate: multiple secondary structured sources agree
  0.0-0.5  Weak: inferred from indirect data (never triggers a trade)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from data.markets.base import Market


class SourceType(str, Enum):
    HARD = "hard"        # Live official API (scores, FRED, etc.)
    REGULATORY = "regulatory"  # Government filings (Federal Register, PACER)
    AGGREGATED = "aggregated"  # Multiple secondary structured sources
    SOFT = "soft"        # News / social – never used to trigger trades


# Confidence floor per source type
SOURCE_CONFIDENCE = {
    SourceType.HARD: 0.90,
    SourceType.REGULATORY: 0.80,
    SourceType.AGGREGATED: 0.55,
    SourceType.SOFT: 0.20,
}


@dataclass
class GroundTruthResult:
    """
    Structured output from a data source for a single market.

    ground_truth_prob : estimated true probability of YES (0-1).
                        0.0 or 1.0 when outcome is known.
                        None when data is insufficient.
    confidence        : how reliable this estimate is (0-1).
                        Only trade when confidence > 0.8.
    source_type       : classification of the data source.
    source_name       : name of the specific source used.
    source_url        : URL or identifier for the data, for auditing.
    raw_data          : the raw fetched data for debugging.
    reasoning         : human-readable explanation of how prob was derived.
    """
    ground_truth_prob: Optional[float]
    confidence: float
    source_type: SourceType
    source_name: str
    source_url: Optional[str] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
    # When the underlying data was published (e.g. FRED release timestamp,
    # game final whistle).  Used by ConfidenceScorer to apply a freshness
    # multiplier — stale data has likely already been priced in by other traders.
    # None means unknown; no freshness penalty is applied in that case.
    data_published_at: Optional[datetime] = None
    # Explicit directional assertion from the data source:
    #   "yes"      – data clearly supports YES resolution
    #   "no"       – data clearly supports NO resolution
    #   "ambiguous"– data is inconclusive about which side is correct (BLOCKS trade)
    #   None       – not assessed by this source (no directional block applied)
    directional_confidence: Optional[str] = None

    @property
    def is_tradeable(self) -> bool:
        """True if confidence meets the 0.8 threshold for trade execution."""
        return (
            self.confidence >= 0.8
            and self.ground_truth_prob is not None
            and self.source_type in (SourceType.HARD, SourceType.REGULATORY)
        )

    @property
    def outcome_known(self) -> bool:
        """True if the result is definitively known (prob is 0 or 1)."""
        if self.ground_truth_prob is None:
            return False
        return self.ground_truth_prob >= 0.99 or self.ground_truth_prob <= 0.01


class DataSource(ABC):
    """
    Abstract base class for all ground-truth data sources.

    Subclasses implement `fetch(market)` to return a GroundTruthResult.
    """

    @abstractmethod
    def can_handle(self, market: Market) -> bool:
        """
        Return True if this data source is relevant for the given market.
        Used by GroundTruthRouter to select the right source.
        """
        ...

    @abstractmethod
    def fetch(self, market: Market) -> Optional[GroundTruthResult]:
        """
        Fetch ground truth data for a market.

        Returns None if the source has no relevant data.
        Never raises – catch all exceptions internally and return None.
        """
        ...
