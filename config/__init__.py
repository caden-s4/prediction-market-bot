"""
config – centralised settings loaded from environment / .env file.
All other modules import from here; nothing reads os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH, override=True)


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
    public_mode: bool       # True = unauthenticated GET only; no credentials needed
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
        public_mode = _get("POLYMARKET_PUBLIC_MODE", "false").lower() != "false"
        need_creds = enabled and not public_mode
        return cls(
            enabled=enabled,
            public_mode=public_mode,
            api_key=_require("POLYMARKET_API_KEY") if need_creds else _get("POLYMARKET_API_KEY", ""),
            api_secret=_require("POLYMARKET_API_SECRET") if need_creds else _get("POLYMARKET_API_SECRET", ""),
            api_passphrase=_require("POLYMARKET_API_PASSPHRASE") if need_creds else _get("POLYMARKET_API_PASSPHRASE", ""),
            private_key=_require("POLYMARKET_PRIVATE_KEY") if need_creds else _get("POLYMARKET_PRIVATE_KEY", ""),
            funder_address=_require("POLYMARKET_FUNDER_ADDRESS") if need_creds else _get("POLYMARKET_FUNDER_ADDRESS", ""),
        )


# ── Bot runtime settings ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class BotConfig:
    dry_run: bool                               # Never place real orders when True
    bankroll_usd: float                         # Total capital in USD

    # ── Shared infrastructure ──────────────────────────────────────────────
    fee_cache_ttl_seconds: int                  # Fee cache TTL (default 900 = 15 min)

    # ── Resolution Drift Arbitrage ─────────────────────────────────────────
    # Shared fallback window. Override per-platform with
    # KALSHI_RESOLUTION_WINDOW_HOURS / POLYMARKET_RESOLUTION_WINDOW_HOURS.
    # Kalshi demo markets are 14-30 days out — set 720 for demo testing.
    # Polymarket markets rarely resolve within 24h — use 720 for most scans.
    resolution_window_hours: float              # shared fallback
    kalshi_resolution_window_hours: float       # kalshi-specific (falls back to shared)
    polymarket_resolution_window_hours: float   # polymarket-specific (falls back to shared)
    resolution_min_gap: float                   # Base gap floor (default 4%); actual min_gap = base + hours×1.5%
    resolution_kelly_fraction: float            # Fractional Kelly (default 12%)
    resolution_max_position_fraction: float     # Hard cap per position (default 20%)
    resolution_scan_interval_seconds: int       # How often to poll (default 300 = 5 min)

    # ── Risk controls ──────────────────────────────────────────────────────
    # If a position opened from a financial source (Yahoo Finance / Twelve Data /
    # Alpha Vantage) moves this many probability points against entry, it is
    # exited immediately regardless of capture percentage or time remaining.
    # Set to 1.0 to effectively disable.  Env var: FINANCIAL_HARD_STOP_THRESHOLD.
    financial_hard_stop_threshold: float        # default 0.20 (20 points)

    # When True, the decay monitor's early-exit capture threshold is determined
    # by a confidence × distance-from-threshold lookup table instead of the fixed
    # 80% floor.  Set DYNAMIC_EXIT_ENABLED=false to instantly revert to 80%.
    dynamic_exit_enabled: bool                  # default True
    min_confidence_threshold: float             # Both confidence dimensions must meet this (default 0.80)

    @classmethod
    def from_env(cls) -> "BotConfig":
        shared_window = float(_get("RESOLUTION_WINDOW_HOURS", "720.0"))
        return cls(
            dry_run=_get("LIVE_TRADING", "false").lower() != "true",
            bankroll_usd=float(_get("BANKROLL_USD", "1000.0")),
            fee_cache_ttl_seconds=int(_get("FEE_CACHE_TTL_SECONDS", "900")),
            resolution_window_hours=shared_window,
            kalshi_resolution_window_hours=float(_get("KALSHI_RESOLUTION_WINDOW_HOURS", str(shared_window))),
            polymarket_resolution_window_hours=float(_get("POLYMARKET_RESOLUTION_WINDOW_HOURS", "4320.0")),
            resolution_min_gap=float(_get("RESOLUTION_MIN_GAP", "0.04")),
            resolution_kelly_fraction=float(_get("RESOLUTION_KELLY_FRACTION", "0.12")),
            resolution_max_position_fraction=float(_get("RESOLUTION_MAX_POSITION_FRACTION", "0.20")),
            resolution_scan_interval_seconds=int(float(_get("RESOLUTION_SCAN_INTERVAL_SECONDS", "300"))),
            financial_hard_stop_threshold=float(_get("FINANCIAL_HARD_STOP_THRESHOLD", "0.20")),
            dynamic_exit_enabled=_get("DYNAMIC_EXIT_ENABLED", "true").lower() != "false",
            min_confidence_threshold=float(_get("MIN_CONFIDENCE_THRESHOLD", "0.80")),
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


# ── Signal testing ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SignalTestSettings:
    """Frozen signal-testing config stored on AppConfig.

    All modules read test-mode flags via ``cfg.signal_test``.
    Built from CLI args by main.py via :meth:`from_cli_args`.
    """

    enabled: bool = False
    active_signals: tuple = ()       # tuple for hashability (frozen dataclass)
    suppress_signals: tuple = ()
    force_ghost: bool = True
    verbose: bool = True
    min_confidence_override: Optional[float] = None
    min_gap_override: Optional[float] = None

    def is_signal_active(self, signal_name: str) -> bool:
        if not self.enabled:
            return True
        if self.active_signals:
            return signal_name in self.active_signals
        return signal_name not in self.suppress_signals

    def effective_min_confidence(self, default: float) -> float:
        return self.min_confidence_override if self.min_confidence_override is not None else default

    def effective_min_gap(self, default: float) -> float:
        return self.min_gap_override if self.min_gap_override is not None else default

    @classmethod
    def disabled(cls) -> "SignalTestSettings":
        return cls(enabled=False)

    @classmethod
    def from_cli_args(
        cls,
        active_signals: Optional[List[str]],
        suppress_signals: Optional[List[str]],
        min_confidence: Optional[float],
        min_gap: Optional[float],
    ) -> "SignalTestSettings":
        active   = tuple(active_signals or [])
        suppress = tuple(suppress_signals or [])
        if not active and not suppress:
            return cls.disabled()
        return cls(
            enabled=True,
            active_signals=active,
            suppress_signals=suppress,
            force_ghost=True,
            verbose=True,
            min_confidence_override=min_confidence,
            min_gap_override=min_gap,
        )


# ── Aggregate config ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AppConfig:
    kalshi: KalshiConfig
    polymarket: PolymarketConfig
    bot: BotConfig
    monitoring: MonitoringConfig
    signal_test: SignalTestSettings = SignalTestSettings()

    @classmethod
    def load(cls) -> "AppConfig":
        return cls(
            kalshi=KalshiConfig.from_env(),
            polymarket=PolymarketConfig.from_env(),
            bot=BotConfig.from_env(),
            monitoring=MonitoringConfig.from_env(),
        )

    def with_signal_test(self, st: SignalTestSettings) -> "AppConfig":
        """Return a new AppConfig with the given signal_test settings applied."""
        return AppConfig(
            kalshi=self.kalshi,
            polymarket=self.polymarket,
            bot=self.bot,
            monitoring=self.monitoring,
            signal_test=st,
        )
