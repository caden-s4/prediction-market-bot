"""
data.ground_truth – structured data sources for ground-truth fetching.

These sources are used to determine what has ALREADY effectively happened
but may not yet be priced into prediction markets.

Source hierarchy (confidence):
  1.0  Official API data (live scores, economic releases, court filings)
  0.8  Government/regulatory databases (Federal Register, PACER)
  0.5  Aggregated secondary sources
  0.0  News/social media (never used to trigger trades)
"""

from .base import GroundTruthResult, SourceType
from .router import GroundTruthRouter

__all__ = ["GroundTruthResult", "SourceType", "GroundTruthRouter"]
