"""
bot.py – BotCoordinator: runs both bots from a single process.

Two bots, one process:
  Bot 1 (maker)      – async event loop, driven by WebSocket price feed
  Bot 2 (resolution) – sync polling loop, runs every 5 minutes

They share:
  - FeeCache       : per-market taker fee rates (refresh every 15 min)
  - ExclusionList  : markets neither bot may touch
  - Bankroll       : 60/40 capital split with daily halt logic

Architecture:
  - Bot 2 runs in a background thread
  - Bot 1 runs in the main asyncio event loop
  - Both write to the same event DB and monitoring layer
  - Ctrl-C or daily halt stops both cleanly
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from config import AppConfig
from data.markets.kalshi import KalshiClient
from data.markets.polymarket import PolymarketClient
from maker.quoter import MakerBot
from monitoring.alerts import AlertManager
from monitoring.event_db import EventDB
from resolution.executor import ResolutionBot
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache
from utils.storage import StateStore

logger = logging.getLogger(__name__)


class BotCoordinator:
    """
    Top-level coordinator that owns both bots and all shared infrastructure.

    Parameters
    ----------
    config : loaded AppConfig (credentials + settings)
    """

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        bc = config.bot

        # ── Platform clients ──────────────────────────────────────────────
        self._kalshi: Optional[KalshiClient] = None
        self._poly: Optional[PolymarketClient] = None

        if config.kalshi.enabled:
            self._kalshi = KalshiClient(
                api_key=config.kalshi.api_key,
                api_secret=config.kalshi.api_secret,
                base_url=config.kalshi.base_url,
            )
            logger.info("Kalshi client: ENABLED (%s)", config.kalshi.env)
        else:
            logger.info("Kalshi client: DISABLED")

        if config.polymarket.enabled:
            self._poly = PolymarketClient(
                api_key=config.polymarket.api_key,
                api_secret=config.polymarket.api_secret,
                api_passphrase=config.polymarket.api_passphrase,
                private_key=config.polymarket.private_key,
                funder_address=config.polymarket.funder_address,
            )
            logger.info("Polymarket client: ENABLED")
        else:
            logger.info("Polymarket client: DISABLED")

        if not self._kalshi and not self._poly:
            raise RuntimeError(
                "No platforms enabled. Set KALSHI_ENABLED=true and/or "
                "POLYMARKET_ENABLED=true in .env"
            )

        # ── Shared infrastructure ─────────────────────────────────────────
        self._fee_cache = FeeCache(ttl=bc.fee_cache_ttl_seconds)
        self._exclusions = ExclusionList()
        self._state = StateStore()

        starting_bankroll = self._state.get("bankroll", bc.bankroll_usd)
        self._bankroll = Bankroll(
            total_usd=starting_bankroll,
            maker_fraction=bc.maker_allocation_fraction,
            resolution_fraction=1.0 - bc.maker_allocation_fraction,
            max_daily_loss_usd=config.monitoring.max_daily_loss_usd,
        )

        # ── Monitoring ────────────────────────────────────────────────────
        self._event_db = EventDB()
        self._alerts = AlertManager(config.monitoring)

        # ── Bot 1: Maker (Polymarket only, async) ─────────────────────────
        self._maker: Optional[MakerBot] = None
        if self._poly and bc.maker_enabled:
            maker_markets = self._poly.get_markets(category="crypto", limit=200)
            self._maker = MakerBot(
                markets=maker_markets,
                poly_client=self._poly,
                fee_cache=self._fee_cache,
                bankroll=self._bankroll,
                exclusions=self._exclusions,
                half_spread=bc.maker_half_spread,
                cap_usd=bc.maker_cap_usd,
                dry_run=bc.dry_run,
            )
            logger.info(
                "MakerBot: ENABLED (half_spread=%.3f cap=$%.0f)",
                bc.maker_half_spread, bc.maker_cap_usd,
            )
        else:
            logger.info(
                "MakerBot: DISABLED (maker_enabled=%s poly=%s)",
                bc.maker_enabled, self._poly is not None,
            )

        # ── Bot 2: Resolution drift (both platforms, sync polling) ─────────
        self._resolution: Optional[ResolutionBot] = None
        if bc.resolution_enabled:
            self._resolution = ResolutionBot(
                kalshi_client=self._kalshi,
                poly_client=self._poly,
                fee_cache=self._fee_cache,
                bankroll=self._bankroll,
                exclusions=self._exclusions,
                dry_run=bc.dry_run,
            )
            logger.info("ResolutionBot: ENABLED")
        else:
            logger.info("ResolutionBot: DISABLED")

        logger.info(
            "BotCoordinator ready | dry_run=%s bankroll=$%.2f | "
            "maker_alloc=%.0f%% resolution_alloc=%.0f%%",
            bc.dry_run,
            starting_bankroll,
            bc.maker_allocation_fraction * 100,
            (1.0 - bc.maker_allocation_fraction) * 100,
        )

    # ── Entry points ──────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """
        Start both bots.
        Bot 2 (resolution) runs in a background daemon thread.
        Bot 1 (maker) runs in the main asyncio event loop.
        Ctrl-C stops both.
        """
        logger.info("BotCoordinator: starting both bots")

        # Launch Bot 2 in a background thread
        if self._resolution:
            t = threading.Thread(
                target=self._resolution_thread,
                name="resolution-bot",
                daemon=True,
            )
            t.start()
            logger.info("ResolutionBot: thread started")

        # Run Bot 1 in the main event loop (blocking)
        if self._maker:
            try:
                asyncio.run(self._maker.run())
            except KeyboardInterrupt:
                logger.info("BotCoordinator: interrupted by user")
            finally:
                self._maker.stop()
        else:
            # No maker bot – keep main thread alive for resolution bot
            logger.info("BotCoordinator: running resolution-only mode")
            try:
                while True:
                    time.sleep(10)
                    self._persist_state()
            except KeyboardInterrupt:
                logger.info("BotCoordinator: stopped by user")

        self._persist_state()
        logger.info("BotCoordinator: shutdown complete")

    def run_once(self) -> dict:
        """Run a single resolution bot cycle (for testing / --once mode)."""
        if self._resolution:
            return self._resolution.run_once()
        return {"error": "resolution bot disabled"}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _resolution_thread(self) -> None:
        """Target for the resolution bot background thread."""
        try:
            if self._resolution:
                self._resolution.run_forever()
        except Exception as exc:
            logger.exception("ResolutionBot: fatal error in thread: %s", exc)
            self._alerts.send_alert(
                f"CRITICAL: ResolutionBot thread died: {exc}", level="critical"
            )

    def _persist_state(self) -> None:
        summary = self._bankroll.summary()
        self._state.set("bankroll", summary["total_usd"])
        self._state.set("bankroll_summary", summary)
        logger.debug("BotCoordinator: state persisted %s", summary)
