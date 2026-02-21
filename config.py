"""
config.py – centralised settings loaded from environment / .env file.
All other modules import from here; nothing reads os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(_ENV_PATH, override=False)


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy .env.example → .env and fill in your credentials."
        )
    return value


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Kalshi ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KalshiConfig:
    enabled: bool
    api_key: str
    api_secret: str
    env: str            # "prod" | "demo"
    base_url: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "base_url",
            "https://api.elections.kalshi.com/trade-api/v2"
            if self.env == "prod"
            else "https://demo-api.kalshi.co/trade-api/v2",
        )

    @classmethod
    def from_env(cls) -> "KalshiConfig":
        enabled = _get("KALSHI_ENABLED", "true").lower() != "false"
        return cls(
            enabled=enabled,
            api_key=_require("KALSHI_API_KEY") if enabled else _get("KALSHI_API_KEY", ""),
            api_secret=_require("KALSHI_API_SECRET") if enabled else _get("KALSHI_API_SECRET", ""),
            env=_get("KALSHI_ENV", "demo"),
        )


# ── Polymarket ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolymarketConfig:
    enabled: bool
    api_key: str
    api_secret: str
    api_passphrase: str
    private_key: str
    funder_address: str
    host: str = "https://clob.polymarket.com"
    chain_id: int = 137

    @classmethod
    def from_env(cls) -> "PolymarketConfig":
        enabled = _get("POLYMARKET_ENABLED", "true").lower() != "false"
        return cls(
            enabled=enabled,
            api_key=_require("POLYMARKET_API_KEY") if enabled else _get("POLYMARKET_API_KEY", ""),
            api_secret=_require("POLYMARKET_API_SECRET") if enabled else _get("POLYMARKET_API_SECRET", ""),
            api_passphrase=_require("POLYMARKET_API_PASSPHRASE") if enabled else _get("POLYMARKET_API_PASSPHRASE", ""),
            private_key=_require("POLYMARKET_PRIVATE_KEY") if enabled else _get("POLYMARKET_PRIVATE_KEY", ""),
            funder_address=_require("POLYMARKET_FUNDER_ADDRESS") if enabled else _get("POLYMARKET_FUNDER_ADDRESS", ""),
        )


# ── Bot runtime settings ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotConfig:
    dry_run: bool                               # Never place real orders when True
    bankroll_usd: float                         # Total capital in USD

    # ── Shared infrastructure ──────────────────────────────────────────────
    fee_cache_ttl_seconds: int                  # Fee cache TTL (default 900 = 15 min)

    # ── Resolution Drift Arbitrage ─────────────────────────────────────────
    # Scan window: only trade markets expiring within this many hours.
    # Strategy spec is 24h for live trading. Set higher (e.g. 168) while testing
    # on Kalshi demo, which only has long-dated markets.
    resolution_window_hours: float
    resolution_min_gap: float                   # Min fee-adjusted gap to flag (default 4%)
    resolution_kelly_fraction: float            # Fractional Kelly (default 12%)
    resolution_max_position_fraction: float     # Hard cap per position (default 20%)
    resolution_scan_interval_seconds: int       # How often to poll (default 300 = 5 min)

    @classmethod
    def from_env(cls) -> "BotConfig":
        return cls(
            dry_run=_get("DRY_RUN", "true").lower() != "false",
            bankroll_usd=float(_get("BANKROLL_USD", "1000.0")),
            fee_cache_ttl_seconds=int(_get("FEE_CACHE_TTL_SECONDS", "900")),
            resolution_window_hours=float(_get("RESOLUTION_WINDOW_HOURS", "168.0")),
            resolution_min_gap=float(_get("RESOLUTION_MIN_GAP", "0.04")),
            resolution_kelly_fraction=float(_get("RESOLUTION_KELLY_FRACTION", "0.12")),
            resolution_max_position_fraction=float(_get("RESOLUTION_MAX_POSITION_FRACTION", "0.20")),
            resolution_scan_interval_seconds=int(_get("RESOLUTION_SCAN_INTERVAL_SECONDS", "300")),
        )


# ── Monitoring & alerts ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class MonitoringConfig:
    telegram_token: str
    telegram_chat_id: str
    discord_webhook_url: str
    daily_drawdown_alert_pct: float
    max_daily_loss_usd: float
    snapshot_interval_seconds: int

    @classmethod
    def from_env(cls) -> "MonitoringConfig":
        return cls(
            telegram_token=_get("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=_get("TELEGRAM_CHAT_ID", ""),
            discord_webhook_url=_get("DISCORD_WEBHOOK_URL", ""),
            daily_drawdown_alert_pct=float(_get("DAILY_DRAWDOWN_ALERT_PCT", "0.05")),
            max_daily_loss_usd=float(_get("MAX_DAILY_LOSS_USD", "0")),
            snapshot_interval_seconds=int(_get("SNAPSHOT_INTERVAL_SECONDS", "60")),
        )


# ── Aggregate config ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    kalshi: KalshiConfig
    polymarket: PolymarketConfig
    bot: BotConfig
    monitoring: MonitoringConfig

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            kalshi=KalshiConfig.from_env(),
            polymarket=PolymarketConfig.from_env(),
            bot=BotConfig.from_env(),
            monitoring=MonitoringConfig.from_env(),
        )
