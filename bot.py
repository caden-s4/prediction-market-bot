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
from datetime import datetime, timezone
from typing import List, Optional

import requests

from config import AppConfig
from data.ground_truth.economic_fred import FRED_SERIES
from data.markets.kalshi import KalshiClient
from data.markets.kalshi_ws import KalshiWebSocket
from data.markets.polymarket import PolymarketClient
from data.release_calendar import FREDReleaseCalendar
from monitoring.alerts import AlertManager
from monitoring.event_db import EventDB
from monitoring.tui_state import TUIStateSnapshot, _read_git_commit_short, write_snapshot
from resolution.executor import ResolutionBot
from resolution.gap_detector import SLIPPAGE_BUFFER
from shared.bankroll import Bankroll
from shared.exclusion_list import ExclusionList
from shared.fee_cache import FeeCache
from utils.storage import StateStore

logger = logging.getLogger(__name__)

_GHOST_DEFAULT_BANKROLL = 500.0


class BotCoordinator:
    """
    Owns all shared infrastructure and drives the resolution drift bot.

    Parameters
    ----------
    config : loaded AppConfig
    """

    def __init__(self, config: AppConfig, force_test: bool = False) -> None:
        self._cfg = config
        self._force_test = force_test
        bc = config.bot

        # ── Platform clients ──────────────────────────────────────────────
        self._kalshi: Optional[KalshiClient] = None
        self._kalshi_ws: Optional[KalshiWebSocket] = None
        self._poly: Optional[PolymarketClient] = None

        if config.kalshi.enabled:
            self._kalshi = KalshiClient(
                api_key=config.kalshi.api_key,
                api_secret=config.kalshi.api_secret,
                base_url=config.kalshi.base_url,
            )
            self._kalshi_ws = KalshiWebSocket(
                api_key=config.kalshi.api_key,
                api_secret=config.kalshi.api_secret,
            )
            self._kalshi_ws.start()
            logger.info("Kalshi client: ENABLED (%s) + WS orderbook", config.kalshi.env)
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
            logger.info(
                "Polymarket: DISABLED (POLYMARKET_ENABLED=false) "
                "— set to true and provide credentials to enable"
            )

        if not self._kalshi and not self._poly:
            raise RuntimeError(
                "No platforms enabled. Set KALSHI_ENABLED=true and/or "
                "POLYMARKET_ENABLED=true in .env"
            )

        logger.warning(
            "Financial bracket markets disabled — Yahoo Finance staleness issue. "
            "Re-enable after Twelve Data paid tier."
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

        # ── Ghost mode: restore persisted virtual bankroll ────────────────
        # In dry-run mode the live account balance is irrelevant — always
        # restore from ghost_state.json so P&L compounds across restarts.
        # If no file exists (first run or after ghost-reset) use the
        # configured default ($500).
        if bc.dry_run:
            persisted_br = self._load_ghost_state()
            ghost_start = persisted_br if persisted_br is not None else _GHOST_DEFAULT_BANKROLL
            self._bankroll.set_total(ghost_start)
            starting_bankroll = ghost_start   # keep sanity-check logic consistent

        # ── Startup sanity checks ─────────────────────────────────────────
        _MIN_VIABLE_BANKROLL = 50.0
        _RECOMMENDED_BANKROLL = 200.0
        if bc.dry_run and starting_bankroll < _MIN_VIABLE_BANKROLL:
            logger.warning(
                "GHOST TRADE: effective bankroll $%.2f is too small to generate any trades. "
                "With %.0f%% Kelly even a perfect signal produces a position of ~$%.2f "
                "(below the $1.00 minimum floor). "
                "Type 'bank <amount>' at the prompt to set a virtual bankroll "
                "(e.g. 'bank 500') and see realistic ghost-trade signal flow.",
                starting_bankroll,
                bc.resolution_kelly_fraction * 100,
                starting_bankroll * bc.resolution_kelly_fraction * 0.15,
            )
        elif not bc.dry_run:
            if starting_bankroll < _MIN_VIABLE_BANKROLL:
                logger.warning(
                    "BANKROLL TOO LOW TO TRADE: $%.2f is below the $%.0f minimum. "
                    "With %.0f%% Kelly a 10%%-gap signal produces a ~$%.2f position — "
                    "below the $1.00 floor. Fund to at least $%.0f before expecting trades.",
                    starting_bankroll,
                    _MIN_VIABLE_BANKROLL,
                    bc.resolution_kelly_fraction * 100,
                    starting_bankroll * bc.resolution_kelly_fraction * (0.10 / 0.90),
                    _RECOMMENDED_BANKROLL,
                )
            elif starting_bankroll < _RECOMMENDED_BANKROLL:
                logger.warning(
                    "Bankroll $%.2f is below the recommended $%.0f minimum. "
                    "Signal coverage will be limited — fund to $%.0f+ for reliable operation.",
                    starting_bankroll, _RECOMMENDED_BANKROLL, _RECOMMENDED_BANKROLL,
                )

        if config.monitoring.max_daily_loss_usd <= 0 and not bc.dry_run:
            logger.warning(
                "MAX_DAILY_LOSS_USD=0: daily loss circuit-breaker is DISABLED. "
                "Set MAX_DAILY_LOSS_USD in .env (e.g. %.0f for a $%.0f bankroll) "
                "to automatically halt trading on excessive daily losses.",
                round(starting_bankroll * 0.10),
                starting_bankroll,
            )

        # ── Monitoring ────────────────────────────────────────────────────
        self._event_db = EventDB()
        self._alerts = AlertManager(
            telegram_token=config.monitoring.telegram_token,
            telegram_chat_id=config.monitoring.telegram_chat_id,
            discord_webhook_url=config.monitoring.discord_webhook_url,
            daily_drawdown_pct=config.monitoring.daily_drawdown_alert_pct,
        )

        # ── FRED release calendar ─────────────────────────────────────────
        # Track upcoming BLS/BEA data releases and manage pre/hold/hunt windows.
        # Series list comes from FREDEconomicSource's own series registry so the
        # calendar always matches what the GT source is actually watching.
        self._calendar = FREDReleaseCalendar(
            series_ids=list(FRED_SERIES.keys()),
        )
        self._calendar.refresh_schedule()

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
            dynamic_exit_enabled=bc.dynamic_exit_enabled,
            calendar=self._calendar,
            force_test=force_test,
            min_confidence=bc.min_confidence_threshold,
            min_gap=config.signal_test.min_gap_override or SLIPPAGE_BUFFER,
            kalshi_ws=self._kalshi_ws,
        )

        self._cycle_count: int = 0

        # ── TUI snapshot infrastructure ────────────────────────────────────────
        self._uptime_start: datetime = datetime.now(timezone.utc)
        self._disabled_features: List[str] = self._compute_disabled_features()
        self._git_commit: Optional[str] = _read_git_commit_short()
        self._resolution._post_cycle_hook = self._write_tui_snapshot

        # Check for system clock drift at startup.  A VPS whose clock is more
        # than 2 seconds ahead of / behind real UTC will produce stale-looking
        # timestamps, cause order-book freshness checks to mis-fire, and can
        # trigger EIP-712 nonce rejections on Polymarket.
        self._check_clock_drift()

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

    def run_once(self, skip_stabilization: bool = False) -> dict:
        """Run a single scan cycle (for testing / --once mode)."""
        self._cycle_count += 1
        result = self._resolution.run_once(skip_stabilization=skip_stabilization)
        self._persist_state()
        result.update(self._bankroll.summary())
        result["platform_balances"] = self._platform_balances
        result["session_cycle"] = self._cycle_count
        return result

    def get_open_positions(self) -> list:
        """Return all open resolution-drift positions with live mark-to-market."""
        return self._resolution.get_open_positions()

    def get_last_signals(self) -> list:
        """Return gap signals detected in the most recent scan cycle."""
        return self._resolution.get_last_signals()

    def get_near_miss_pairs(self, n: int = 10) -> tuple:
        """Return (results, stats) for near-miss cross-platform pairs."""
        return self._resolution.get_near_miss_pairs(n)

    def clear_positions(self) -> int:
        """Wipe all tracked positions from memory and state (no exit orders placed)."""
        return self._resolution.clear_positions()

    def ghost_clear_positions(self) -> int:
        """Remove ghost positions from memory and persist the cleared state to SQLite."""
        return self._resolution.ghost_clear_positions()

    def get_resolved_positions(self) -> list:
        """Return all trades resolved this session (exits from the decay monitor)."""
        return self._resolution.get_resolved_positions()

    def get_paper_log(self):
        """Return the PaperTradeLog instance (dry-run only; None in live mode)."""
        return self._resolution.get_paper_log()

    def get_bankroll(self) -> float:
        """Return the current total bankroll (virtual or live)."""
        return self._bankroll.total_usd

    def set_virtual_bankroll(self, amount_usd: float) -> None:
        """
        Override the trading bankroll with a virtual amount.

        Only valid in dry-run mode.  Persists to SQLite (ghost_state table)
        so the value survives restarts.
        """
        if not self._cfg.bot.dry_run:
            raise RuntimeError(
                "set_virtual_bankroll() is only available in dry-run mode. "
                "Set LIVE_TRADING=false in .env to use virtual capital."
            )
        if amount_usd <= 0:
            raise ValueError(f"Virtual bankroll must be positive, got {amount_usd}")
        self._bankroll.set_total(amount_usd)
        self._platform_balances["virtual_usd"] = amount_usd
        self._save_ghost_state()
        logger.info(
            "BotCoordinator: virtual bankroll set to $%.2f (persisted to SQLite)",
            amount_usd,
        )

    def ghost_reset(self) -> int:
        """
        Full ghost-mode reset: clear all simulated positions and reset the
        virtual bankroll to the configured default ($500).

        Returns the number of positions cleared.  Persists the reset to
        SQLite (ghost_state table) so it survives the next restart.
        """
        if not self._cfg.bot.dry_run:
            raise RuntimeError("ghost_reset() is only available in dry-run mode.")
        n = self._resolution.ghost_clear_positions()
        default = _GHOST_DEFAULT_BANKROLL
        self._bankroll.reset_virtual(default)
        self._save_ghost_state()
        logger.info(
            "Ghost mode: reset complete — bankroll=$%.2f, %d position(s) cleared",
            default, n,
        )
        return n

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch_balances(self) -> dict:
        """Query live cash/USDC balances from each enabled platform."""
        kalshi_bal = self._kalshi.get_balance() if self._kalshi else None

        poly_bal = None
        if self._poly:
            poly_bal = self._poly.get_balance()
            if poly_bal is None:
                if getattr(self._poly, "_public_mode", False):
                    logger.warning(
                        "Polymarket balance: n/a — client is in PUBLIC MODE (read-only). "
                        "Set POLYMARKET_PUBLIC_MODE=false and provide credentials "
                        "to see your wallet balance."
                    )
                elif not getattr(self._poly, "_clob_client", None):
                    logger.warning(
                        "Polymarket balance: n/a — py-clob-client not available. "
                        "Run: pip install py-clob-client"
                    )
                else:
                    logger.warning(
                        "Polymarket balance: n/a — get_balance() returned None. "
                        "Verify POLYMARKET_PRIVATE_KEY, POLYMARKET_FUNDER_ADDRESS, "
                        "and POLYMARKET_API_KEY/SECRET/PASSPHRASE in .env. "
                        "Also confirm your wallet has USDC on Polygon mainnet (chain 137)."
                    )

        total = (kalshi_bal or 0.0) + (poly_bal or 0.0)
        return {
            "kalshi_usd": kalshi_bal,
            "polymarket_usd": poly_bal,
            "total_usd": total if (kalshi_bal is not None or poly_bal is not None) else None,
        }

    def _check_clock_drift(self, warn_seconds: float = 2.0) -> None:
        """
        Compare system UTC time against worldtimeapi.org and warn if the
        drift exceeds `warn_seconds`.  Failure to reach the NTP server is
        logged at DEBUG and does not block startup.
        """
        try:
            resp = requests.get(
                "http://worldtimeapi.org/api/timezone/Etc/UTC",
                timeout=5,
                proxies={},
            )
            resp.raise_for_status()
            server_dt_str = resp.json().get("datetime", "")
            # worldtimeapi returns ISO-8601 with offset, e.g. "2024-01-15T12:34:56.789+00:00"
            server_dt = datetime.fromisoformat(server_dt_str)
            local_dt = datetime.now(timezone.utc)
            drift_s = abs((local_dt - server_dt).total_seconds())
            if drift_s > warn_seconds:
                logger.warning(
                    "BotCoordinator: CLOCK DRIFT detected — system clock is "
                    "%.1fs off UTC (threshold=%.0fs). "
                    "Run 'sudo ntpdate -u pool.ntp.org' to resync. "
                    "Drift can cause order-book staleness false-positives and "
                    "EIP-712 nonce rejections.",
                    drift_s, warn_seconds,
                )
            else:
                logger.info(
                    "BotCoordinator: clock drift check OK (drift=%.2fs)", drift_s
                )
        except Exception as exc:
            logger.debug(
                "BotCoordinator: clock drift check skipped (NTP unreachable): %s", exc
            )

    def _persist_state(self) -> None:
        summary = self._bankroll.summary()
        if self._cfg.bot.dry_run:
            self._save_ghost_state()
        else:
            self._state.set("bankroll", summary["total_usd"])
            self._state.set("bankroll_summary", summary)
            if self._platform_balances:
                self._state.set("platform_balances", self._platform_balances)
        logger.debug("BotCoordinator: state persisted %s", summary)

    # ── ghost_state persistence ──────────────────────────────────────────

    def _save_ghost_state(self) -> None:
        """Write current virtual bankroll to the ghost_state row in SQLite.

        Failures are logged at WARNING.  Q1 crash-on-failure applies to the
        startup READ path only.
        """
        from data.runtime.sqlite_store import set_bankroll  # noqa: PLC0415
        try:
            summary = self._bankroll.summary()
            set_bankroll({
                "total_usd": summary["total_usd"],
                "realized_pnl_usd": summary["realized_pnl_usd"],
            })
        except Exception as exc:
            logger.warning(
                "BotCoordinator: failed to save ghost_state to SQLite: %s", exc,
            )

    def _load_ghost_state(self) -> Optional[float]:
        """Load virtual bankroll from the ghost_state row in SQLite.

        Returns total_usd or None if no row exists yet.  Per Q1, any
        exception during read propagates so the bot crashes at startup
        rather than degrading to an empty state.
        """
        from data.runtime.sqlite_store import get_bankroll, get_db_path  # noqa: PLC0415
        data = get_bankroll()
        if data is None:
            return None
        total = float(data["total_usd"])
        logger.info(
            "Ghost mode: restored bankroll $%.2f from %s (last_updated=%s)",
            total, get_db_path(), data.get("last_updated", "?"),
        )
        return total

    def _compute_disabled_features(self) -> List[str]:
        disabled: List[str] = [
            "yahoo_brackets",  # _FINANCIAL_BRACKET_PREFIXES = () in scanner.py
            "kxbrentd",        # wrong GT source (BZ=F vs CL=F), permanently removed
        ]
        if not self._cfg.polymarket.enabled:
            disabled.append("polymarket")
        return disabled

    def _write_tui_snapshot(self) -> None:
        """Build and atomically write data/runtime/tui_state.json."""
        from resolution.scanner import get_snipe_stats  # noqa: PLC0415
        res = self._resolution
        snipes_attempted, shadow_total = get_snipe_stats()
        now_utc = datetime.now(timezone.utc)
        snapshot = TUIStateSnapshot(
            mode="GHOST" if self._cfg.bot.dry_run else "LIVE",
            paused=False,
            cycle_start_ts=res._last_cycle_start_ts or now_utc.isoformat(),
            cycle_duration_s=res._last_cycle_duration_s,
            uptime_start_ts=self._uptime_start.isoformat(),
            wall_clock_utc=now_utc.isoformat(),
            disabled_features=self._disabled_features,
            current_pipeline_stage=res._pipeline_stage,
            signals_total=res._signals_total_cum,
            fills_total=res._fills_total_cum,
            snipes_attempted=snipes_attempted,
            snipes_placed=res._snipes_placed_cum,
            shadow_signals_total=shadow_total,
            schema_version=1,
            git_commit=self._git_commit,
        )
        write_snapshot(snapshot)
