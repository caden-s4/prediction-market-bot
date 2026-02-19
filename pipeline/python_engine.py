"""
pipeline.python_engine – concrete PythonReasoningEngine.

Wires Stages 1–5 into a single evaluate_market() call.
This is the default engine.  To add an LLM engine later:

    class LLMReasoningEngine(ReasoningEngine):
        def evaluate_market(self, market, order_book, ...):
            prompt = build_prompt(market, order_book, forecast)
            llm_output = call_llm(prompt)
            return parse_signal(llm_output)

Then swap it in:
    bot = Bot(reasoning_engine=LLMReasoningEngine(...), ...)
"""

from __future__ import annotations

import logging
from typing import Optional

from data.markets.base import Market, OrderBook, Side
from data.weather.aggregator import ConsensusForecast
from pipeline.reasoning_engine import ReasoningEngine, TradeAction, TradeSignal
from pipeline.stage1_probability import Stage1Probability
from pipeline.stage2_market import Stage2Market
from pipeline.stage3_edge import Stage3Edge
from pipeline.stage4_risk import Stage4Risk
from pipeline.stage5_timing import Stage5Timing

logger = logging.getLogger(__name__)


class PythonReasoningEngine(ReasoningEngine):
    """
    Deterministic Python-based reasoning engine.

    All decision logic lives in the five stage modules.
    Configuration is passed at construction time so each stage
    can be tuned independently.
    """

    def __init__(
        self,
        stage1: Optional[Stage1Probability] = None,
        stage2: Optional[Stage2Market] = None,
        stage3: Optional[Stage3Edge] = None,
        stage4: Optional[Stage4Risk] = None,
        stage5: Optional[Stage5Timing] = None,
    ) -> None:
        self._s1 = stage1 or Stage1Probability()
        self._s2 = stage2 or Stage2Market()
        self._s3 = stage3 or Stage3Edge()
        self._s4 = stage4 or Stage4Risk()
        self._s5 = stage5 or Stage5Timing()

    # ── ReasoningEngine interface ─────────────────────────────────────────────

    def evaluate_market(
        self,
        market: Market,
        order_book: OrderBook,
        consensus_forecast: Optional[ConsensusForecast],
        bankroll: float,
        current_exposure: float,
        correlated_positions: int = 0,
    ) -> TradeSignal:
        """
        Run market through Stages 1–5 and return a TradeSignal.

        If `consensus_forecast` is already provided (pre-fetched), Stage 1
        is skipped.  Otherwise Stage 1 is called here.
        """
        # ── Stage 1 ───────────────────────────────────────────────────────
        if consensus_forecast is None:
            consensus_forecast = self._s1.run(market)

        if consensus_forecast is None:
            return self._skip(market, "Stage1: no forecast available")

        our_prob = consensus_forecast.consensus_prob

        # ── Stage 2 ───────────────────────────────────────────────────────
        analysis = self._s2.run(
            market=market,
            order_book=order_book,
            our_prob=our_prob,
            target_size_usd=bankroll * 0.05,  # rough initial size for slippage
        )

        if analysis.implied_prob is None:
            return self._skip(market, "Stage2: no implied probability")

        # ── Stage 3 ───────────────────────────────────────────────────────
        edge_result = self._s3.run(
            forecast=consensus_forecast,
            analysis=analysis,
            platform=market.platform,
        )

        if edge_result is None or not edge_result.passes_threshold:
            reason = (
                f"Stage3: edge {edge_result.net_edge:.2%} below threshold"
                if edge_result else "Stage3: edge calculation failed"
            )
            return self._skip(market, reason)

        # ── Stage 4 ───────────────────────────────────────────────────────
        risk = self._s4.run(
            edge_result=edge_result,
            bankroll=bankroll,
            current_exposure_usd=current_exposure,
            correlated_positions=correlated_positions,
        )

        if not risk.passes_exposure_check or risk.position_size_usd <= 0:
            return self._skip(market, f"Stage4: {risk.reject_reason or 'position size zero'}")

        # ── Stage 5 ───────────────────────────────────────────────────────
        timing = self._s5.run(
            market=market,
            edge_result=edge_result,
        )

        if not timing.execute_now:
            return self._skip(market, f"Stage5: {timing.wait_reason}")

        # ── Build TradeSignal ─────────────────────────────────────────────
        action = TradeAction.BUY_YES if edge_result.direction == 1 else TradeAction.BUY_NO
        reasoning = (
            f"Our prob={our_prob:.1%} | market={analysis.implied_prob:.1%} | "
            f"net_edge={edge_result.net_edge:.2%} | "
            f"kelly={risk.fractional_kelly:.3f} | "
            f"size=${risk.position_size_usd:.2f} | "
            f"horizon={market.hours_to_resolution:.0f}h | "
            f"CI=[{consensus_forecast.prob_low:.1%},{consensus_forecast.prob_high:.1%}]"
        )

        logger.info("TRADE SIGNAL: market=%s action=%s %s", market.market_id, action, reasoning)

        return TradeSignal(
            market=market,
            action=action,
            edge=edge_result.net_edge,
            confidence=1.0 - consensus_forecast.confidence_width,
            position_size_usd=risk.position_size_usd,
            our_prob=our_prob,
            market_prob=analysis.implied_prob,
            reasoning=reasoning,
            metadata={
                "edge_result": edge_result,
                "risk_result": risk,
                "timing_result": timing,
                "forecast": consensus_forecast,
                "analysis": analysis,
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _skip(market: Market, reason: str) -> TradeSignal:
        logger.debug("SKIP market=%s reason=%s", market.market_id, reason)
        return TradeSignal(
            market=market,
            action=TradeAction.SKIP,
            edge=0.0,
            confidence=0.0,
            position_size_usd=0.0,
            our_prob=market.yes_price,
            market_prob=market.yes_price,
            reasoning=reason,
        )
