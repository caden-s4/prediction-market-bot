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
                public_mode=config.polymarket.public_mode,
            )
            mode_label = "public/read-only" if config.polymarket.public_mode else "authenticated"
            logger.info("Polymarket client: ENABLED (%s)", mode_label)
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

        # Fetch live account balances; use their sum as the starting bankroll
        # so the bot always reflects real capital rather than a config value.
        self._platform_balances: dict = self._fetch_balances()
        live_total = self._platform_balances.get("total_usd")
        if live_total:
            starting_bankroll = live_total
            logger.info(
                "Live balances – Kalshi: %s  Polymarket: %s  Total: $%.2f",
                f"${self._platform_balances['kalshi_usd']:.2f}"
                if self._platform_balances["kalshi_usd"] is not None else "n/a",
                f"${self._platform_balances['polymarket_usd']:.2f}"
                if self._platform_balances["polymarket_usd"] is not None else "n/a",
                live_total,
            )
        else:
            starting_bankroll = self._state.get("bankroll", bc.bankroll_usd)
            saved_balances = self._state.get("platform_balances", {})
            if saved_balances:
                self._platform_balances = saved_balances
                logger.info(
                    "Using saved platform balances (live fetch unavailable): %s",
                    saved_balances,
                )
            else:
                logger.info(
                    "Using configured starting bankroll $%.2f "
                    "(live balance fetch unavailable)", starting_bankroll,
                )

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
            kalshi_window_hours=bc.kalshi_resolution_window_hours,
            poly_window_hours=bc.polymarket_resolution_window_hours,
            scan_interval=bc.resolution_scan_interval_seconds,
            state_store=self._state,
        )

        self._cycle_count: int = 0

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
        self._cycle_count += 1
        result = self._resolution.run_once()
        self._persist_state()
        result.update(self._bankroll.summary())
        result["platform_balances"] = self._platform_balances
        result["session_cycle"] = self._cycle_count
        return result

    def get_open_positions(self) -> list:
        """Return all open resolution-drift positions with live mark-to-market."""
        return self._resolution.get_open_positions()

    def clear_positions(self) -> int:
        """Wipe all tracked positions from memory and state (no exit orders placed)."""
        return self._resolution.clear_positions()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_balances(self) -> dict:
        """Query live cash/USDC balances from each enabled platform."""
        kalshi_bal = self._kalshi.get_balance() if self._kalshi else None
        poly_bal = self._poly.get_balance() if self._poly else None
        total = (kalshi_bal or 0.0) + (poly_bal or 0.0)
        return {
            "kalshi_usd": kalshi_bal,
            "polymarket_usd": poly_bal,
            "total_usd": total if (kalshi_bal is not None or poly_bal is not None) else None,
        }

    def _persist_state(self) -> None:
        summary = self._bankroll.summary()
        self._state.set("bankroll", summary["total_usd"])
        self._state.set("bankroll_summary", summary)
        if self._platform_balances:
            self._state.set("platform_balances", self._platform_balances)
        logger.debug("BotCoordinator: state persisted %s", summary)
