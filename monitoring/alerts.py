"""
monitoring.alerts – Telegram and Discord alert manager.

Sends alerts when:
  • Daily drawdown exceeds a threshold  (checked every portfolio snapshot)
  • A significant arb opportunity fires (edge > alert_edge_threshold)
  • The bot encounters a critical error

Telegram
--------
Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.
Uses the sendMessage Bot API endpoint (no library dependency – raw requests).

Discord
-------
Set DISCORD_WEBHOOK_URL in .env.
Uses Discord's Incoming Webhook API.

Both alert channels can be enabled simultaneously.  If neither is configured,
AlertManager silently logs instead of alerting.

Rate limiting
-------------
The manager enforces a minimum interval between repeated alerts of the same
type to prevent flooding (configurable, default 5 minutes).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Minimum seconds between alerts of the same category
_RATE_LIMIT_SECONDS = 300


class AlertManager:
    """
    Sends threshold-breach notifications to Telegram and/or Discord.

    Parameters
    ----------
    telegram_token      : Telegram Bot token (from @BotFather)
    telegram_chat_id    : Telegram chat / channel ID to send to
    discord_webhook_url : Discord incoming webhook URL
    daily_drawdown_pct  : alert when daily loss > this fraction of bankroll
                          (0.05 = 5%).  0 = disabled.
    alert_edge_threshold: alert when a signal fires with edge above this
                          (0 = always alert on fired signals).
    """

    def __init__(
        self,
        telegram_token: Optional[str] = None,
        telegram_chat_id: Optional[str] = None,
        discord_webhook_url: Optional[str] = None,
        daily_drawdown_pct: float = 0.05,
        alert_edge_threshold: float = 0.03,
    ) -> None:
        self._tg_token = telegram_token or ""
        self._tg_chat = telegram_chat_id or ""
        self._discord_url = discord_webhook_url or ""
        self._drawdown_pct = daily_drawdown_pct
        self._edge_threshold = alert_edge_threshold
        self._last_alert_ts: Dict[str, float] = {}   # category → last send time

        if not self._tg_token and not self._discord_url:
            logger.info(
                "AlertManager: no Telegram/Discord configured – alerts will log only"
            )

    # ── Public alert methods ───────────────────────────────────────────────────

    def check_drawdown(self, bankroll: float, daily_pnl: float) -> None:
        """
        Check if daily drawdown exceeds the threshold and send an alert.
        Call this on every portfolio snapshot.
        """
        if self._drawdown_pct <= 0 or bankroll <= 0:
            return
        drawdown_frac = -daily_pnl / bankroll
        if drawdown_frac >= self._drawdown_pct:
            msg = (
                f"⚠️ DAILY DRAWDOWN ALERT\n"
                f"Daily PnL: ${daily_pnl:.2f}\n"
                f"Bankroll: ${bankroll:.2f}\n"
                f"Drawdown: {drawdown_frac:.1%} (threshold: {self._drawdown_pct:.1%})\n"
                f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
            )
            self._send("drawdown_alert", msg)

    def alert_signal(self, signal_type: str, market_id: str, edge: float, platform: str) -> None:
        """Send an alert when a high-edge signal fires."""
        if edge < self._edge_threshold:
            return
        msg = (
            f"🎯 SIGNAL FIRED\n"
            f"Type: {signal_type}\n"
            f"Market: {platform}:{market_id}\n"
            f"Edge: {edge:.2%}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send(f"signal_{signal_type}", msg)

    def alert_error(self, component: str, error: str) -> None:
        """Send a critical error alert."""
        msg = (
            f"🚨 BOT ERROR\n"
            f"Component: {component}\n"
            f"Error: {error}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send("error", msg)

    def alert_trade(
        self,
        action: str,
        platform: str,
        market_id: str,
        size_usd: float,
        price: float,
        dry_run: bool = False,
    ) -> None:
        """Send a notification when a trade is executed."""
        prefix = "[DRY RUN] " if dry_run else ""
        msg = (
            f"{prefix}✅ TRADE EXECUTED\n"
            f"Action: {action}\n"
            f"Market: {platform}:{market_id}\n"
            f"Price: {price:.4f}  Size: ${size_usd:.2f}\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._send("trade", msg)

    # ── Delivery layer ─────────────────────────────────────────────────────────

    def _send(self, category: str, message: str) -> None:
        """Send to all configured channels, respecting rate limits."""
        now = time.monotonic()
        last = self._last_alert_ts.get(category, 0.0)
        if now - last < _RATE_LIMIT_SECONDS:
            logger.debug("Alert rate-limited: category=%s", category)
            return

        self._last_alert_ts[category] = now
        logger.info("ALERT [%s]: %s", category, message.replace("\n", " | "))

        if self._tg_token and self._tg_chat:
            self._send_telegram(message)

        if self._discord_url:
            self._send_discord(message)

    def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={"chat_id": self._tg_chat, "text": text, "parse_mode": ""},
                timeout=10,
            )
            if not resp.ok:
                logger.warning(
                    "Telegram alert failed: HTTP %d %s", resp.status_code, resp.text[:200]
                )
        except Exception as exc:
            logger.warning("Telegram alert error: %s", exc)

    def _send_discord(self, text: str) -> None:
        try:
            resp = requests.post(
                self._discord_url,
                json={"content": text},
                timeout=10,
            )
            if not resp.ok:
                logger.warning(
                    "Discord alert failed: HTTP %d %s", resp.status_code, resp.text[:200]
                )
        except Exception as exc:
            logger.warning("Discord alert error: %s", exc)
