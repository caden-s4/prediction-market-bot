from __future__ import annotations

import importlib
import os

import pytest

import config as config_module
from config.signal_testing import SignalTestConfig


def _reload_config():
    return importlib.reload(config_module)


def test_app_config_loads_with_disabled_platforms(monkeypatch):
    config = _reload_config()
    monkeypatch.setenv("KALSHI_ENABLED", "false")
    monkeypatch.setenv("POLYMARKET_ENABLED", "false")
    monkeypatch.setenv("LIVE_TRADING", "false")
    monkeypatch.setenv("BANKROLL_USD", "1234")
    monkeypatch.setenv("RESOLUTION_WINDOW_HOURS", "48")
    app = config.AppConfig.load()

    assert app.kalshi.enabled is False
    assert app.polymarket.enabled is False
    assert app.bot.dry_run is True
    assert app.bot.bankroll_usd == 1234.0
    assert app.bot.resolution_window_hours == 48.0


def test_kalshi_config_requires_credentials_when_enabled(monkeypatch):
    config = _reload_config()
    monkeypatch.setenv("KALSHI_ENABLED", "true")
    monkeypatch.delenv("KALSHI_API_KEY", raising=False)
    monkeypatch.delenv("KALSHI_API_SECRET", raising=False)

    with pytest.raises(EnvironmentError):
        config.KalshiConfig.from_env()


def test_polymarket_public_mode_does_not_require_credentials(monkeypatch):
    config = _reload_config()
    monkeypatch.setenv("POLYMARKET_ENABLED", "true")
    monkeypatch.setenv("POLYMARKET_PUBLIC_MODE", "true")
    for key in [
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_FUNDER_ADDRESS",
    ]:
        monkeypatch.delenv(key, raising=False)

    poly = config.PolymarketConfig.from_env()

    assert poly.enabled is True
    assert poly.public_mode is True
    assert poly.api_key == ""


def test_signal_test_settings_helpers():
    st = config_module.SignalTestSettings.from_cli_args(
        active_signals=["fred"],
        suppress_signals=None,
        min_confidence=0.7,
        min_gap=0.03,
    )

    assert st.enabled is True
    assert st.is_signal_active("fred") is True
    assert st.is_signal_active("financial") is False
    assert st.effective_min_confidence(0.8) == 0.7
    assert st.effective_min_gap(0.04) == 0.03


def test_signal_test_config_rejects_unknown_signal():
    with pytest.raises(ValueError):
        SignalTestConfig.from_cli(
            test_signals=["unknown"],
            suppress_signals=None,
            min_confidence=None,
            min_gap=None,
        )
