"""
pipeline.reasoning_engine – abstract reasoning interface.

Designed for easy swapping between:
  - PythonReasoningEngine (current): deterministic statistical logic
  - LLMReasoningEngine (future):    delegates decisions to an LLM

All pipeline stages produce intermediate results; the ReasoningEngine
orchestrates them and produces a final TradeSignal per market.

To add an LLM backend later:
  1. Subclass ReasoningEngine
  2. Override `_score_edge`, `_override_position_size`, or `evaluate_market`
  3. Pass your subclass to the Bot constructor
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from data.markets.base import Market, OrderBook, Side
from data.weather.aggregator import ConsensusForecast


class TradeAction(str, Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    SKIP = "SKIP"
    EXIT = "EXIT"       # signal to close an existing position


@dataclass
class TradeSignal:
    """
    Final output of the reasoning pipeline for a single market.

    market          : the Market being evaluated
    action          : what to do (BUY_YES, BUY_NO, SKIP, EXIT)
    edge            : estimated edge as a decimal [0–1] (e.g. 0.08 = 8%)
    confidence      : pipeline confidence in the edge estimate [0–1]
    position_size_usd : recommended USD notional (after Kelly & caps)
    our_prob        : bot's probability estimate for YES outcome
    market_prob     : implied probability from the market price
    reasoning       : human-readable explanation (always populated)
    metadata        : arbitrary extra data for debugging / logging
    """

    market: Market
    action: TradeAction
    edge: float
    confidence: float
    position_size_usd: float
    our_prob: float
    market_prob: float
    reasoning: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_tradeable(self) -> bool:
        return self.action in (TradeAction.BUY_YES, TradeAction.BUY_NO)

    def to_side(self) -> Optional[Side]:
        if self.action == TradeAction.BUY_YES:
            return Side.YES
        if self.action == TradeAction.BUY_NO:
            return Side.NO
        return None


@dataclass
class PipelineResult:
    """
    Aggregated output of one full pipeline run across all scanned markets.
    """
    signals: List[TradeSignal]
    markets_evaluated: int
    markets_passed: int
    run_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def tradeable_signals(self) -> List[TradeSignal]:
        return [s for s in self.signals if s.is_tradeable]

    def top_signals(self, n: int = 5) -> List[TradeSignal]:
        """Return top-N signals by edge, tradeable only."""
        return sorted(self.tradeable_signals, key=lambda s: -s.edge)[:n]


class ReasoningEngine(ABC):
    """
    Abstract base class for the reasoning engine.

    Subclasses implement `evaluate_market` which runs a single Market
    through the full pipeline and returns a TradeSignal.

    The default `evaluate_markets` method runs evaluate_market on each
    Market in a list and aggregates results into a PipelineResult.
    """

    @abstractmethod
    def evaluate_market(
        self,
        market: Market,
        order_book: OrderBook,
        consensus_forecast: Optional[ConsensusForecast],
        bankroll: float,
        current_exposure: float,
        correlated_positions: int,
    ) -> TradeSignal:
        """
        Evaluate a single market and return a TradeSignal.

        Parameters
        ----------
        market              : the Market to evaluate
        order_book          : current order book snapshot
        consensus_forecast  : weather model consensus (None for non-weather)
        bankroll            : total available bankroll in USD
        current_exposure    : total USD currently deployed
        correlated_positions: number of open positions correlated with this one
        """
        ...

    def evaluate_markets(
        self,
        markets: List[Market],
        order_books: Dict[str, OrderBook],
        forecasts: Dict[str, Optional[ConsensusForecast]],
        bankroll: float,
        current_exposure: float,
    ) -> PipelineResult:
        """
        Evaluate a list of markets and return a PipelineResult.
        Default implementation calls evaluate_market serially.
        """
        signals: List[TradeSignal] = []
        for market in markets:
            ob = order_books.get(market.market_id)
            fc = forecasts.get(market.market_id)
            if ob is None:
                continue
            # Count correlated positions: same location bucket
            correlated = self._count_correlated(market, [s.market for s in signals if s.is_tradeable])
            try:
                signal = self.evaluate_market(
                    market=market,
                    order_book=ob,
                    consensus_forecast=fc,
                    bankroll=bankroll,
                    current_exposure=current_exposure,
                    correlated_positions=correlated,
                )
                signals.append(signal)
            except Exception as exc:
                import logging
                logging.getLogger(__name__).warning(
                    "ReasoningEngine: evaluate_market failed for %s: %s",
                    market.market_id, exc,
                )

        tradeable = [s for s in signals if s.is_tradeable]
        return PipelineResult(
            signals=signals,
            markets_evaluated=len(markets),
            markets_passed=len(tradeable),
        )

    @staticmethod
    def _count_correlated(market: Market, open_markets: List[Market]) -> int:
        """Count how many open positions share the same approximate location."""
        if not market.location:
            return 0
        count = 0
        for m in open_markets:
            if not m.location:
                continue
            lat_close = abs(market.location.get("lat", 0) - m.location.get("lat", 0)) < 2.0
            lon_close = abs(market.location.get("lon", 0) - m.location.get("lon", 0)) < 2.0
            if lat_close and lon_close:
                count += 1
        return count
