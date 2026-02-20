"""
bot.py – main Bot orchestrator.

The Bot:
  1. Scans markets via MarketScanner
  2. Fetches order books for candidates
  3. Runs the full reasoning pipeline (Stages 1–5) via ReasoningEngine
  4. Executes trade signals via Executor
  5. Re-evaluates open positions (Stage 6) on every cycle
  6. Updates the meta-calibration layer
  7. Persists state between restarts

Run in a loop by main.py.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from config import AppConfig
from data.markets.base import BaseMarketClient, Market, OrderBook
from data.markets.kalshi import KalshiClient
from data.markets.polymarket import PolymarketClient
from data.markets.scanner import MarketScanner, build_default_scanner
from data.weather.aggregator import ConsensusForecast, WeatherAggregator
from execution.executor import Executor
from execution.portfolio import Portfolio
from meta.calibration import CalibrationTracker
from pipeline.python_engine import PythonReasoningEngine
from pipeline.reasoning_engine import PipelineResult, ReasoningEngine
from pipeline.stage1_probability import Stage1Probability
from pipeline.stage2_market import Stage2Market
from pipeline.stage3_edge import Stage3Edge
from pipeline.stage4_risk import Stage4Risk
from pipeline.stage5_timing import Stage5Timing
from pipeline.stage6_reevaluation import Stage6Reevaluation
from utils.storage import StateStore

logger = logging.getLogger(__name__)


class Bot:
    """
    Top-level prediction market trading bot.

    Inject any ReasoningEngine subclass to swap reasoning logic
    (e.g., switch from PythonReasoningEngine to LLMReasoningEngine).

    Parameters
    ----------
    config            : loaded AppConfig (credentials + bot settings)
    reasoning_engine  : optional custom ReasoningEngine; defaults to Python engine
    scanner           : optional custom MarketScanner; defaults to weather-first
    """

    def __init__(
        self,
        config: AppConfig,
        reasoning_engine: Optional[ReasoningEngine] = None,
        scanner: Optional[MarketScanner] = None,
    ) -> None:
        self._cfg = config
        self._bot_cfg = config.bot

        # ── Market clients ────────────────────────────────────────────────
        self._kalshi: Optional[KalshiClient] = None
        self._polymarket: Optional[PolymarketClient] = None
        self._clients: Dict[str, BaseMarketClient] = {}

        if config.kalshi.enabled:
            self._kalshi = KalshiClient(
                api_key=config.kalshi.api_key,
                api_secret=config.kalshi.api_secret,
                base_url=config.kalshi.base_url,
            )
            self._clients["kalshi"] = self._kalshi
        else:
            logger.info("Kalshi access DISABLED (KALSHI_ENABLED=false)")

        if config.polymarket.enabled:
            self._polymarket = PolymarketClient(
                api_key=config.polymarket.api_key,
                api_secret=config.polymarket.api_secret,
                api_passphrase=config.polymarket.api_passphrase,
                private_key=config.polymarket.private_key,
                funder_address=config.polymarket.funder_address,
            )
            self._clients["polymarket"] = self._polymarket
        else:
            logger.info("Polymarket access DISABLED (POLYMARKET_ENABLED=false)")

        if not self._clients:
            raise RuntimeError(
                "No platforms enabled. Set KALSHI_ENABLED=true and/or POLYMARKET_ENABLED=true in .env"
            )

        # ── Market scanner ────────────────────────────────────────────────
        self._scanner = scanner or build_default_scanner(
            polymarket_client=self._polymarket,
            kalshi_client=self._kalshi,
        )

        # ── Weather aggregator ────────────────────────────────────────────
        self._aggregator = WeatherAggregator()

        # ── Reasoning pipeline stages ─────────────────────────────────────
        s3 = Stage3Edge(min_edge_threshold=self._bot_cfg.min_edge_threshold)
        s4 = Stage4Risk(
            kelly_fraction=self._bot_cfg.kelly_fraction,
            max_position_fraction=self._bot_cfg.max_position_fraction,
            max_total_exposure=self._bot_cfg.max_total_exposure,
        )
        s5 = Stage5Timing(base_edge_threshold=self._bot_cfg.min_edge_threshold)

        self._engine: ReasoningEngine = reasoning_engine or PythonReasoningEngine(
            stage1=Stage1Probability(self._aggregator),
            stage2=Stage2Market(),
            stage3=s3,
            stage4=s4,
            stage5=s5,
        )
        self._stage6 = Stage6Reevaluation(
            min_edge_to_hold=0.0,
            rebalance_improvement=0.03,
        )

        # ── Portfolio + execution ─────────────────────────────────────────
        self._state = StateStore()
        bankroll = self._state.get("bankroll", self._bot_cfg.bankroll_usd)
        self._portfolio = Portfolio(starting_bankroll=bankroll)
        self._calibration = CalibrationTracker()
        self._executor = Executor(
            clients=self._clients,
            portfolio=self._portfolio,
            calibration=self._calibration,
            dry_run=self._bot_cfg.dry_run,
        )

        logger.info(
            "Bot initialised | dry_run=%s bankroll=$%.2f min_edge=%.1f%%",
            self._bot_cfg.dry_run,
            bankroll,
            self._bot_cfg.min_edge_threshold * 100,
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """Run the pipeline loop indefinitely. Ctrl-C to stop."""
        logger.info("Bot starting main loop (interval=%ds)", self._bot_cfg.poll_interval_seconds)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except Exception as exc:
                logger.exception("Bot loop error (will retry): %s", exc)

            # Persist bankroll between cycles
            self._state.set("bankroll", self._portfolio.bankroll)
            logger.info(
                "Cycle complete. Sleeping %ds | bankroll=$%.2f | open_positions=%d",
                self._bot_cfg.poll_interval_seconds,
                self._portfolio.bankroll,
                len(self._portfolio.open_positions),
            )
            time.sleep(self._bot_cfg.poll_interval_seconds)

    def run_once(self) -> PipelineResult:
        """
        Execute one full pipeline cycle.
        Returns a PipelineResult for inspection / testing.
        """
        logger.info("=== Pipeline cycle start ===")

        # ── Step 1: Scan markets ───────────────────────────────────────────
        markets: List[Market] = self._scanner.scan(prioritise_weather=True)
        logger.info("Scanned %d markets", len(markets))

        if not markets:
            logger.warning("No markets found – skipping cycle")
            return PipelineResult(signals=[], markets_evaluated=0, markets_passed=0)

        # ── Step 2: Re-evaluate open positions (Stage 6) first ────────────
        self._reevaluate_open_positions(markets)

        # ── Step 3: Fetch order books for candidate markets ────────────────
        order_books: Dict[str, OrderBook] = {}
        forecasts: Dict[str, Optional[ConsensusForecast]] = {}

        for market in markets:
            # Skip markets we're already in (handled by Stage 6)
            if self._portfolio.get_position(market.market_id):
                continue

            client = self._clients.get(market.platform)
            if not client:
                continue

            try:
                ob = client.get_order_book(market.market_id)
                order_books[market.market_id] = ob
            except Exception as exc:
                logger.warning("Failed to fetch order book for %s: %s", market.market_id, exc)
                continue

            # Pre-fetch forecasts for weather markets with location data
            if market.is_weather_market() and market.location:
                try:
                    fc = self._aggregator.get_consensus(
                        latitude=market.location["lat"],
                        longitude=market.location["lon"],
                        horizon_hours=min(market.hours_to_resolution, 240.0),
                    )
                    forecasts[market.market_id] = fc
                except Exception as exc:
                    logger.warning("Forecast failed for %s: %s", market.market_id, exc)
                    forecasts[market.market_id] = None
            else:
                forecasts[market.market_id] = None

        # ── Step 4: Run reasoning pipeline (Stages 1–5) ───────────────────
        result = self._engine.evaluate_markets(
            markets=[m for m in markets if m.market_id in order_books],
            order_books=order_books,
            forecasts=forecasts,
            bankroll=self._portfolio.bankroll,
            current_exposure=self._portfolio.total_exposure_usd,
        )

        logger.info(
            "Pipeline: %d evaluated, %d passed, %d tradeable",
            result.markets_evaluated,
            result.markets_passed,
            len(result.tradeable_signals),
        )

        # ── Step 5: Execute trade signals ─────────────────────────────────
        if result.tradeable_signals:
            logger.info("Top signals:")
            for sig in result.top_signals(5):
                logger.info(
                    "  %s %s edge=%.1f%% size=$%.2f",
                    sig.action, sig.market.market_id, sig.edge * 100, sig.position_size_usd,
                )
            self._executor.execute_signals(result.tradeable_signals)

        # ── Step 6: Check arbitrage opportunities ──────────────────────────
        arb_candidates = self._scanner.scan_arbitrage_candidates()
        if arb_candidates:
            logger.info(
                "Arbitrage: %d cross-platform opportunities found (best spread: %.3f)",
                len(arb_candidates),
                arb_candidates[0]["price_spread"],
            )

        # ── Step 7: Save calibration snapshot periodically ─────────────────
        self._calibration.snapshot()

        return result

    # ── Stage 6 helper ────────────────────────────────────────────────────────

    def _reevaluate_open_positions(self, fresh_markets: List[Market]) -> None:
        """Re-run Stage 6 on all open positions using fresh market data."""
        if not self._portfolio.open_positions:
            return

        market_map = {m.market_id: m for m in fresh_markets}
        open_orders = [p.order for p in self._portfolio.open_positions]
        original_edges = {
            p.market_id: p.entry_edge for p in self._portfolio.open_positions
        }

        # Fetch fresh forecasts + order books for open positions
        position_forecasts: Dict = {}
        fresh_order_books: Dict = {}

        for pos in self._portfolio.open_positions:
            mid = pos.market_id
            market = market_map.get(mid)
            if not market:
                continue
            client = self._clients.get(pos.platform)
            if client:
                try:
                    fresh_order_books[mid] = client.get_order_book(mid)
                except Exception:
                    pass
            if market.location:
                try:
                    position_forecasts[mid] = self._aggregator.get_consensus(
                        latitude=market.location["lat"],
                        longitude=market.location["lon"],
                        horizon_hours=min(market.hours_to_resolution, 240.0),
                    )
                except Exception:
                    position_forecasts[mid] = None

        # Determine best new opportunity edge for rebalance logic
        best_new_edge = 0.0

        decisions = self._stage6.run(
            open_positions=open_orders,
            position_markets={mid: market_map[mid] for mid in original_edges if mid in market_map},
            position_forecasts=position_forecasts,
            order_books=fresh_order_books,
            original_edges=original_edges,
            best_new_edge=best_new_edge,
            at_max_exposure=(
                self._portfolio.total_exposure_usd
                >= self._portfolio.bankroll * self._bot_cfg.max_total_exposure * 0.95
            ),
        )

        self._executor.execute_reevaluation(decisions)
