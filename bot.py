"""
bot.py – BotCoordinator: resolution drift arbitrage bot.

Scans Polymarket and Kalshi every 5 minutes for non-crypto markets expiring
within the configured window. Finds mispricings against hard data sources
and fires taker orders on the lagging platform.

Shared infrastructure:
  FeeCache      – per-market taker fee rates (refresh every 15 min)
  ExclusionList – markets the bot must never touch
  Bankroll      – capital management with daily halt logic
"""

from __future__ import annotations

import logging
from typing import Optional

from config import AppConfig
from data.markets.kalshi import KalshiClient
from data.markets.polymarket import PolymarketClient
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
    Owns all shared infrastructure and drives the resolution drift bot.

    Parameters
    ----------
    config : loaded AppConfig
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
            max_daily_loss_usd=config.monitoring.max_daily_loss_usd,
        )

        # ── Monitoring ────────────────────────────────────────────────────
        self._event_db = EventDB()
        self._alerts = AlertManager(config.monitoring)

        # ── Resolution drift bot ──────────────────────────────────────────
        self._resolution = ResolutionBot(
            kalshi_client=self._kalshi,
            poly_client=self._poly,
            fee_cache=self._fee_cache,
            bankroll=self._bankroll,
            exclusions=self._exclusions,
            dry_run=bc.dry_run,
            window_hours=bc.resolution_window_hours,
            scan_interval=bc.resolution_scan_interval_seconds,
        )

        logger.info(
            "BotCoordinator ready | dry_run=%s bankroll=$%.2f",
            bc.dry_run, starting_bankroll,
        )

    # ── Entry points ──────────────────────────────────────────────────────────

    def run_forever(self) -> None:
        """Run the resolution bot continuously until Ctrl-C."""
        logger.info("BotCoordinator: starting resolution drift bot")
        try:
            self._resolution.run_forever()
        except KeyboardInterrupt:
            logger.info("BotCoordinator: stopped by user")
        finally:
            self._persist_state()
            logger.info("BotCoordinator: shutdown complete")

    def run_once(self) -> dict:
        """Run a single scan cycle (for testing / --once mode)."""
        result = self._resolution.run_once()
        self._persist_state()
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _persist_state(self) -> None:
        summary = self._bankroll.summary()
        self._state.set("bankroll", summary["total_usd"])
        self._state.set("bankroll_summary", summary)
        logger.debug("BotCoordinator: state persisted %s", summary)
