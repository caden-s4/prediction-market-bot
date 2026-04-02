"""
live_trading.py – async entry point for the WebSocket-based live trading loop.

This is a separate entry point from main.py (which runs the slower weather
pipeline polling bot).  Run both simultaneously or choose one:

    # WebSocket live loop (this file) – fast, event-driven
    python live_trading.py

    # Weather model polling loop (main.py) – slower, weather-model-driven
    python main.py

Architecture
------------
                  ┌─────────────────────────────────────┐
                  │           live_trading.py            │
                  │  ┌──────────┐    ┌────────────────┐  │
    Kalshi WS ───►│  │ Kalshi   │    │  Polymarket    │◄─── Polymarket WS
                  │  │ Adapter  │    │  Adapter       │  │
                  │  └────┬─────┘    └──────┬─────────┘  │
                  │       │ LiveOrderBook    │             │
                  │       └────────┬─────────┘            │
                  │            ▼ book updates              │
                  │       ┌────────────┐                   │
                  │       │  Signal    │                   │
                  │       │  Engine    │                   │
                  │       └─────┬──────┘                   │
                  │             │ Signal                    │
                  │       ┌─────▼──────┐                   │
                  │       │    Risk    │                   │
                  │       │  Manager   │                   │
                  │       └─────┬──────┘                   │
                  │             │ RiskDecision.approved     │
                  │       ┌─────▼──────┐                   │
                  │       │  Execution │─► Kalshi REST      │
                  │       │  Engine    │─► Polymarket REST  │
                  │       └─────┬──────┘                   │
                  │             │                           │
                  │       ┌─────▼──────┐                   │
                  │       │  Event DB  │  (SQLite)         │
                  │       │  Alerts    │  (Telegram/Discord)│
                  │       └────────────┘                   │
                  │       ┌────────────┐                   │
                  │       │  Recorder  │  (JSONL.gz)       │
                  │       └────────────┘                   │
                  └─────────────────────────────────────────┘

Usage
-----
    python live_trading.py
    python live_trading.py --log-level DEBUG
    python live_trading.py --dry-run
    python live_trading.py --scan-interval 10
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


# ── Execution engine (handles actual order placement) ─────────────────────────

class LiveExecutionEngine:
    """
    Places orders on Kalshi/Polymarket via REST in response to approved signals.
    Logs every order to the EventDB and sends alerts for large trades.
    """

    def __init__(
        self,
        kalshi_client,
        poly_client,
        event_db,
        alert_manager,
        dry_run: bool = True,
    ) -> None:
        self._kalshi = kalshi_client
        self._poly = poly_client
        self._db = event_db
        self._alerts = alert_manager
        self._dry_run = dry_run

    def execute(self, signal, decision) -> bool:
        """Place an order for an approved signal.  Returns True on success."""
        from data.markets.base import Order, Side

        platform = signal.platform
        market_id = signal.market_id
        side = Side.YES if signal.direction.value == "BUY_YES" else Side.NO
        price = signal.buy_price or signal.metadata.get("best_ask", 0.50)
        size = decision.position_size_usd

        # Log order intent
        order_row_id = self._db.log_order(
            platform=platform,
            market_id=market_id,
            side=side.value,
            expected_price=price,
            size_usd=size,
            status="SUBMITTING",
        )

        order = Order(
            market_id=market_id,
            platform=platform,
            side=side,
            price=price,
            size_usd=size,
            dry_run=self._dry_run,
        )

        try:
            client = self._kalshi if platform == "kalshi" else self._poly
            filled_order = client.place_order(order)

            fill_price = filled_order.filled_price or price
            self._db.update_order_status(order_row_id, filled_order.status.value,
                                          filled_order.order_id)
            self._db.log_fill(order_row_id, fill_price, filled_order.filled_size or size, price)

            self._alerts.alert_trade(
                action=signal.direction.value,
                platform=platform,
                market_id=market_id,
                size_usd=size,
                price=fill_price,
                dry_run=self._dry_run,
            )

            logger.info(
                "LiveExecution: placed %s %s:%s size=$%.2f price=%.4f fill=%.4f",
                signal.direction.value, platform, market_id,
                size, price, fill_price,
            )
            return True

        except Exception as exc:
            logger.error("LiveExecution: order failed for %s:%s – %s", platform, market_id, exc)
            self._db.update_order_status(order_row_id, "FAILED")
            self._alerts.alert_error("LiveExecutionEngine", str(exc))
            return False


# ── Signal callback ────────────────────────────────────────────────────────────

def make_signal_callback(risk_manager, execution_engine, event_db, alert_manager):
    """Return a callback that the SignalEngine calls whenever signals fire."""
    _halt_alerted = False

    def on_signals(signals):
        nonlocal _halt_alerted

        # Fire a one-time alert the first time the halt is detected.
        if risk_manager.is_halted and not _halt_alerted:
            _halt_alerted = True
            alert_manager.alert_daily_loss_limit(
                daily_loss_usd=-risk_manager.daily_pnl,
                limit_usd=risk_manager._max_daily_loss,
                open_positions=len(risk_manager._open_positions),
            )
        elif not risk_manager.is_halted:
            _halt_alerted = False  # reset after midnight roll

        for signal in signals:
            # Signal already recorded by SignalEngine._log_signal(); here we
            # only alert, risk-check, and execute (i.e. "acted upon" path).
            alert_manager.alert_signal(
                signal_type=signal.signal_type.value,
                market_id=signal.market_id,
                edge=signal.edge_estimate,
                platform=signal.platform,
            )

            # Risk check
            decision = risk_manager.check(signal)
            if not decision.approved:
                logger.info(
                    "Signal REJECTED: %s – %s",
                    signal.market_id, decision.reject_reason,
                )
                continue

            # Execute — log at INFO to distinguish "acted upon" from "detected"
            logger.info(
                "Signal APPROVED: %s %s edge=%.3f platform=%s",
                signal.signal_type.value, signal.market_id,
                signal.edge_estimate, signal.platform,
            )
            success = execution_engine.execute(signal, decision)
            if success:
                risk_manager.record_open(
                    signal.market_id, decision.position_size_usd, signal.platform
                )

    return on_signals


# ── Market pair discovery ─────────────────────────────────────────────────────

def discover_market_pairs(kalshi_client, poly_client, signal_engine, recorder, limit=50):
    """
    Scan both exchanges for weather markets, match arb pairs, and register
    them with the signal engine and recorder.

    Returns (kalshi_ids, poly_ids) – the market IDs to subscribe the WS adapters to.
    """
    from data.markets.scanner import build_default_scanner
    from adapters.base import LiveOrderBook

    scanner = build_default_scanner(
        polymarket_client=poly_client,
        kalshi_client=kalshi_client,
    )

    markets = scanner.scan(prioritise_weather=True)
    kalshi_ids, poly_ids = [], []
    arb_candidates = scanner.scan_arbitrage_candidates()

    for mkt in markets:
        if mkt.platform == "kalshi":
            kalshi_ids.append(mkt.market_id)
        elif mkt.platform == "polymarket":
            poly_ids.append(mkt.market_id)

    logger.info(
        "Discovered %d kalshi + %d polymarket weather markets, %d arb pairs",
        len(kalshi_ids), len(poly_ids), len(arb_candidates),
    )
    return kalshi_ids[:limit], poly_ids[:limit], arb_candidates


# ── Periodic snapshot task ────────────────────────────────────────────────────

async def position_reconciliation_loop(risk_manager, kalshi_client, interval: int = 60) -> None:
    """
    Periodically reconcile open positions tracked by RiskManager against live
    exchange state.  When a position no longer appears in the exchange response
    (i.e. it has settled), call record_close() so the stale entry is removed.

    This is most critical when the daily loss limit is active: the signal
    callback drops all incoming signals after a halt, which means record_close()
    would never be called for positions that resolve while the bot is halted.
    Without this loop those market IDs stay in _open_positions permanently,
    causing phantom exposure and blocking re-entry after the midnight reset.

    PnL is recorded as 0.0 because the live trading bot does not yet have a
    settlement-result endpoint; the bankroll reconciliation should be handled
    by a separate balance-sync job.
    """
    while True:
        await asyncio.sleep(interval)
        open_pos = risk_manager.open_positions
        if not open_pos:
            continue
        try:
            # Fetch current exchange positions (Kalshi only for now; Polymarket
            # does not expose a first-class positions endpoint).
            if kalshi_client is None:
                continue
            live = {p.market_id for p in kalshi_client.get_positions()}
            for market_id, (platform, size_usd) in list(open_pos.items()):
                if platform != "kalshi":
                    continue
                if market_id not in live:
                    logger.warning(
                        "PositionReconciler: %s no longer on exchange – "
                        "marking closed (pnl unknown, recorded as $0)",
                        market_id,
                    )
                    risk_manager.record_close(market_id, pnl_usd=0.0)
        except Exception as exc:
            logger.warning("PositionReconciler: exchange poll failed – %s", exc)


async def snapshot_loop(event_db, risk_manager, alert_manager, interval: int = 60):
    """Write a portfolio snapshot every `interval` seconds and check drawdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            daily_pnl = event_db.get_daily_pnl()
            bankroll = risk_manager.bankroll
            exposure = risk_manager.total_exposure_usd

            event_db.snapshot(
                bankroll=bankroll,
                total_exposure=exposure,
                open_positions=len(risk_manager._open_positions),
                daily_pnl=daily_pnl,
                total_pnl=daily_pnl,  # extend with total from DB if desired
            )
            alert_manager.check_drawdown(bankroll, daily_pnl)

            logger.info(
                "Snapshot: bankroll=$%.2f exposure=$%.2f daily_pnl=$%.2f",
                bankroll, exposure, daily_pnl,
            )
        except Exception as exc:
            logger.warning("Snapshot loop error: %s", exc)


# ── Main async loop ────────────────────────────────────────────────────────────

async def run_live(cfg: AppConfig, dry_run: bool, scan_interval: int) -> None:
    from data.markets.kalshi import KalshiClient
    from data.markets.polymarket import PolymarketClient
    from adapters.kalshi_ws import KalshiWSAdapter
    from adapters.polymarket_ws import PolymarketWSAdapter
    from signals.cross_exchange import CrossExchangeSignal
    from signals.book_imbalance import BookImbalanceSignal
    from signals.engine import SignalEngine
    from risk.manager import RiskManager
    from monitoring.event_db import EventDB
    from monitoring.alerts import AlertManager
    from backtest.recorder import BookRecorder

    # ── Clients (REST) ─────────────────────────────────────────────────────
    kalshi_rest = None
    poly_rest = None

    if cfg.kalshi.enabled:
        kalshi_rest = KalshiClient(
            api_key=cfg.kalshi.api_key,
            api_secret=cfg.kalshi.api_secret,
            base_url=cfg.kalshi.base_url,
        )
    else:
        logger.info("Kalshi DISABLED – skipping Kalshi REST client and WS adapter")

    if cfg.polymarket.enabled:
        poly_rest = PolymarketClient(
            api_key=cfg.polymarket.api_key,
            api_secret=cfg.polymarket.api_secret,
            api_passphrase=cfg.polymarket.api_passphrase,
            private_key=cfg.polymarket.private_key,
            funder_address=cfg.polymarket.funder_address,
        )
    else:
        logger.info("Polymarket DISABLED – skipping Polymarket REST client and WS adapter")

    if kalshi_rest is None and poly_rest is None:
        logger.error("No platforms enabled. Set KALSHI_ENABLED and/or POLYMARKET_ENABLED to true.")
        return

    # ── Monitoring ─────────────────────────────────────────────────────────
    event_db = EventDB()
    alert_manager = AlertManager(
        telegram_token=cfg.monitoring.telegram_token,
        telegram_chat_id=cfg.monitoring.telegram_chat_id,
        discord_webhook_url=cfg.monitoring.discord_webhook_url,
        daily_drawdown_pct=cfg.monitoring.daily_drawdown_alert_pct,
    )

    # ── Recorder ───────────────────────────────────────────────────────────
    recorder = BookRecorder()

    # ── Risk manager ───────────────────────────────────────────────────────
    risk_manager = RiskManager(
        bankroll_usd=cfg.bot.bankroll_usd,
        kelly_fraction=cfg.bot.resolution_kelly_fraction,
        max_position_fraction=cfg.bot.resolution_max_position_fraction,
        min_edge_threshold=cfg.bot.resolution_min_gap,
        max_daily_loss_usd=cfg.monitoring.max_daily_loss_usd,
    )

    # ── Signal engine ──────────────────────────────────────────────────────
    signal_engine = SignalEngine(
        cross_exchange=CrossExchangeSignal(
            min_spread=cfg.bot.resolution_min_gap,
        ),
        book_imbalance=BookImbalanceSignal(
            bullish_threshold=0.65,
            bearish_threshold=0.35,
        ),
        event_logger=event_db,
    )

    # ── Execution engine ───────────────────────────────────────────────────
    exec_engine = LiveExecutionEngine(
        kalshi_client=kalshi_rest,
        poly_client=poly_rest,
        event_db=event_db,
        alert_manager=alert_manager,
        dry_run=dry_run,
    )

    # ── Wire signal callback ────────────────────────────────────────────────
    signal_engine.add_callback(
        make_signal_callback(risk_manager, exec_engine, event_db, alert_manager)
    )

    # ── Market discovery ───────────────────────────────────────────────────
    logger.info("Scanning markets for WebSocket subscriptions...")
    try:
        kalshi_ids, poly_ids, arb_pairs = discover_market_pairs(
            kalshi_rest, poly_rest, signal_engine, recorder
        )
    except Exception as exc:
        logger.error("Market discovery failed: %s", exc)
        kalshi_ids, poly_ids, arb_pairs = [], [], []

    # ── WebSocket adapters ─────────────────────────────────────────────────
    kalshi_adapter = None
    poly_adapter = None

    if cfg.kalshi.enabled:
        kalshi_adapter = KalshiWSAdapter(
            api_key=cfg.kalshi.api_key,
            api_secret=cfg.kalshi.api_secret,
            env=cfg.kalshi.env,
        )
        kalshi_adapter.add_global_callback(recorder.on_book_update)

    if cfg.polymarket.enabled:
        poly_adapter = PolymarketWSAdapter()
        poly_adapter.add_global_callback(recorder.on_book_update)

    # Register signal engine books and arb pairs
    if kalshi_adapter and kalshi_ids:
        kalshi_adapter.subscribe(kalshi_ids)
        for mid in kalshi_ids:
            book = kalshi_adapter.get_book(mid)
            if book:
                signal_engine.register_book(book)

    if poly_adapter and poly_ids:
        poly_adapter.subscribe(poly_ids)
        for mid in poly_ids:
            book = poly_adapter.get_book(mid)
            if book:
                signal_engine.register_book(book)

    # Register cross-exchange arb pairs (only when both adapters active)
    if kalshi_adapter and poly_adapter:
        for pair in arb_pairs:
            poly_book = poly_adapter.get_book(pair["poly_market"].market_id)
            kalshi_book = kalshi_adapter.get_book(pair["kalshi_market"].market_id)
            if poly_book and kalshi_book:
                signal_engine.register_arb_pair(
                    poly_book=poly_book,
                    kalshi_book=kalshi_book,
                    poly_market_id=pair["poly_market"].market_id,
                    kalshi_market_id=pair["kalshi_market"].market_id,
                )

    logger.info(
        "Live trading starting | dry_run=%s kalshi=%s kalshi_mkts=%d poly=%s poly_mkts=%d arb_pairs=%d",
        dry_run,
        cfg.kalshi.enabled, len(kalshi_ids),
        cfg.polymarket.enabled, len(poly_ids),
        len(arb_pairs),
    )

    if dry_run:
        logger.warning("DRY RUN mode – no real orders will be placed")

    # ── Start all tasks ────────────────────────────────────────────────────
    tasks = [
        asyncio.create_task(
            snapshot_loop(event_db, risk_manager, alert_manager, interval=60),
            name="snapshot_loop",
        ),
        asyncio.create_task(
            position_reconciliation_loop(risk_manager, kalshi_rest, interval=60),
            name="position_reconciliation",
        ),
    ]
    if kalshi_adapter:
        tasks.append(asyncio.create_task(kalshi_adapter.run(), name="kalshi_ws"))
    if poly_adapter:
        tasks.append(asyncio.create_task(poly_adapter.run(), name="poly_ws"))

    # ── Periodic signal evaluation loop ──────────────────────────────────
    async def signal_eval_loop(engine: "SignalEngine", interval: float = 5.0):
        """Evaluate all signals on a fixed interval.

        Simpler and safer than event-driven evaluation — debouncing is
        automatic via the interval.  A single evaluation failure is logged
        but never kills the loop.
        """
        cycle = 0
        while True:
            await asyncio.sleep(interval)
            cycle += 1
            try:
                fired = engine.evaluate_all()
                if fired:
                    logger.info(
                        "Signal eval cycle %d: %d signal(s) fired",
                        cycle, len(fired),
                    )
                else:
                    logger.debug("Signal eval cycle %d: no signals", cycle)
            except Exception:
                logger.exception("Signal eval cycle %d failed", cycle)

    tasks.append(
        asyncio.create_task(
            signal_eval_loop(signal_engine), name="signal_eval",
        )
    )

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down live trading...")
    finally:
        if kalshi_adapter:
            kalshi_adapter.stop()
        if poly_adapter:
            poly_adapter.stop()
        recorder.close()
        for task in tasks:
            task.cancel()
        logger.info("Live trading stopped")


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="WebSocket-based live prediction market trader")
    p.add_argument("--dry-run", action="store_true", help="Paper trade only (no real orders)")
    p.add_argument("--log-level", default="INFO",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--log-file", default=None, help="Path to write logs to")
    p.add_argument("--scan-interval", type=int, default=3600,
                   help="Seconds between re-scanning for new markets (default 3600)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(level=args.log_level, log_file=args.log_file)

    try:
        cfg = AppConfig.load()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    dry_run = args.dry_run or cfg.bot.dry_run

    asyncio.run(run_live(cfg, dry_run=dry_run, scan_interval=args.scan_interval))


if __name__ == "__main__":
    main()
