"""
execution.executor – translates TradeSignals into actual orders.

Handles:
  - Routing orders to the correct platform client
  - Dry-run mode (simulates fills, no real API calls)
  - Recording entries in CalibrationTracker
  - Stage-6 re-evaluation exit execution
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from data.markets.base import BaseMarketClient, Order, OrderStatus
from execution.portfolio import Portfolio, Position
from meta.calibration import CalibrationTracker, TradeRecord
from pipeline.reasoning_engine import TradeAction, TradeSignal
from pipeline.stage6_reevaluation import ReevalAction, ReevaluationDecision

logger = logging.getLogger(__name__)


class Executor:
    """
    Converts TradeSignals into order placements and manages the portfolio.
    """

    def __init__(
        self,
        clients: Dict[str, BaseMarketClient],   # {"polymarket": ..., "kalshi": ...}
        portfolio: Portfolio,
        calibration: CalibrationTracker,
        dry_run: bool = True,
    ) -> None:
        self._clients = clients
        self._portfolio = portfolio
        self._calibration = calibration
        self._dry_run = dry_run

    # ── Entry ─────────────────────────────────────────────────────────────────

    def execute_signals(self, signals: List[TradeSignal]) -> List[Order]:
        """
        Process a list of TradeSignals; place orders for tradeable ones.
        Returns the list of placed orders.
        """
        placed: List[Order] = []
        for signal in signals:
            if not signal.is_tradeable:
                continue
            order = self._execute_one(signal)
            if order:
                placed.append(order)
        return placed

    def _execute_one(self, signal: TradeSignal) -> Optional[Order]:
        market = signal.market
        client = self._clients.get(market.platform)
        if not client:
            logger.error(
                "No client registered for platform=%s (market=%s)",
                market.platform, market.market_id,
            )
            return None

        side = signal.to_side()
        if side is None:
            return None

        # Price: use the best ask/bid for the chosen side
        price = signal.market_prob   # fallback; ideally from order book
        if signal.action == TradeAction.BUY_YES:
            price = market.yes_price
        else:
            price = market.no_price

        order = Order(
            market_id=market.market_id,
            platform=market.platform,
            side=side,
            price=price,
            size_usd=signal.position_size_usd,
            dry_run=self._dry_run,
        )

        order = client.place_order(order)

        if order.status in (OrderStatus.FILLED, OrderStatus.OPEN):
            # Record in calibration DB
            mtype = self._infer_market_type(market.question)
            trade_record = TradeRecord(
                market_id=market.market_id,
                platform=market.platform,
                market_type=mtype,
                our_prob=signal.our_prob,
                market_prob=signal.market_prob,
                edge=signal.edge,
                position_size_usd=signal.position_size_usd,
                direction=1 if signal.action == TradeAction.BUY_YES else -1,
            )
            trade_id = self._calibration.record_entry(trade_record)

            # Add to portfolio
            pos = Position(
                order=order,
                market_id=market.market_id,
                platform=market.platform,
                entry_edge=signal.edge,
                entry_prob=signal.our_prob,
                trade_id=trade_id,
            )
            self._portfolio.add_position(pos)
            logger.info(
                "Executed: %s %s on %s size=$%.2f edge=%.1f%%",
                signal.action, market.market_id, market.platform,
                signal.position_size_usd, signal.edge * 100,
            )

        return order

    # ── Exit / rebalance (Stage 6) ────────────────────────────────────────────

    def execute_reevaluation(
        self,
        decisions: List[ReevaluationDecision],
    ) -> None:
        """Process Stage-6 HOLD/EXIT/REBALANCE decisions."""
        for decision in decisions:
            if decision.action == ReevalAction.HOLD:
                continue

            mid = decision.market.market_id
            platform = decision.market.platform
            client = self._clients.get(platform)
            pos = self._portfolio.get_position(mid)

            if not pos:
                continue

            if decision.action in (ReevalAction.EXIT, ReevalAction.REBALANCE):
                # Cancel the open order if still open
                if pos.order.order_id and client:
                    success = client.cancel_order(pos.order.order_id, mid)
                    logger.info(
                        "Stage6 %s: cancel order %s → %s",
                        decision.action, pos.order.order_id, success,
                    )
                self._portfolio.remove_position(mid)
                logger.info(
                    "Stage6 %s: exited position in %s | reason: %s",
                    decision.action, mid, decision.reason,
                )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _infer_market_type(question: str) -> str:
        q = question.lower()
        if any(w in q for w in ("rain", "precip", "snow", "flood")):
            return "precipitation"
        if any(w in q for w in ("temp", "celsius", "fahrenheit", "heat", "cold")):
            return "temperature"
        if any(w in q for w in ("wind", "gust", "hurricane", "tornado")):
            return "wind"
        return "weather_other"
