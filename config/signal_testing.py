"""
config/signal_testing.py – Signal isolation & testing framework configuration.

Controls which signal sources are active, suppressed, or run in ghost-only mode
during a test session.  Normally loaded from CLI args via main.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, List, Optional


# ── Valid signal names ─────────────────────────────────────────────────────────

VALID_SIGNALS: FrozenSet[str] = frozenset({
    "financial",
    "fred",
    "sports_shock",
    "sports_staleness",
    "sports_panic",
    "sports_resolution",
    "cross_platform",
})

# Maps signal name → the source class name(s) that implement it.
# Used by router.py to filter sources and by signal_stats.py to tag counters.
SIGNAL_SOURCE_MAP: dict[str, list[str]] = {
    "financial":        ["FinancialDataSource"],
    "fred":             ["FredDataSource"],
    "sports_shock":     ["SportsLiveSource"],      # shock path inside SportsLiveSource
    "sports_staleness": ["SportsLiveSource"],      # staleness path
    "sports_panic":     ["SportsLiveSource"],      # panic path
    "sports_resolution":["SportsLiveSource"],      # resolution-lag path
    "cross_platform":   ["CrossPlatformSource"],
}

# Sports sub-signal tag injected into GroundTruthResult.source_label so that
# the router can distinguish the four sports signal types all served by the
# same SportsLiveSource class.
SPORTS_SUB_SIGNALS: FrozenSet[str] = frozenset({
    "sports_shock",
    "sports_staleness",
    "sports_panic",
    "sports_resolution",
})


# ── Config dataclass ──────────────────────────────────────────────────────────

@dataclass
class SignalTestConfig:
    """Runtime signal-testing configuration.

    Attributes
    ----------
    enabled:
        Master toggle.  When False all other fields are ignored and the bot
        runs normally.
    active_signals:
        If non-empty, *only* these signals are evaluated; all others are
        suppressed.  Takes priority over suppress_signals.
    suppress_signals:
        Signals to suppress even if they would normally fire.  Ignored when
        active_signals is non-empty.
    force_ghost:
        When True all trades from active signals are logged as ghost trades
        regardless of LIVE_TRADING setting.  Defaults to True in test mode.
    verbose:
        Emit per-market decision-chain log lines (verdict / gap / conf / etc.).
    min_confidence_override:
        If set, replaces the per-source minimum confidence gate for every
        active signal during the test session.
    min_gap_override:
        If set, replaces the minimum effective-gap threshold for every active
        signal during the test session.
    """

    enabled: bool = False
    active_signals: List[str] = field(default_factory=list)
    suppress_signals: List[str] = field(default_factory=list)
    force_ghost: bool = True
    verbose: bool = True
    min_confidence_override: Optional[float] = None
    min_gap_override: Optional[float] = None

    # ── factory ───────────────────────────────────────────────────────────────

    @classmethod
    def disabled(cls) -> "SignalTestConfig":
        """Return the default no-op config (test mode off)."""
        return cls(enabled=False)

    @classmethod
    def from_cli(
        cls,
        test_signals: Optional[List[str]],
        suppress_signals: Optional[List[str]],
        min_confidence: Optional[float],
        min_gap: Optional[float],
    ) -> "SignalTestConfig":
        """Build a SignalTestConfig from parsed CLI arguments.

        At least one of test_signals or suppress_signals must be non-empty for
        enabled to be True; otherwise the disabled no-op config is returned.
        """
        active   = list(test_signals or [])
        suppress = list(suppress_signals or [])

        if not active and not suppress:
            return cls.disabled()

        # Validate signal names
        for name in active + suppress:
            if name not in VALID_SIGNALS:
                valid = ", ".join(sorted(VALID_SIGNALS))
                raise ValueError(
                    f"Unknown signal '{name}'. Valid signals: {valid}"
                )

        return cls(
            enabled=True,
            active_signals=active,
            suppress_signals=suppress,
            force_ghost=True,
            verbose=True,
            min_confidence_override=min_confidence,
            min_gap_override=min_gap,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def is_signal_active(self, signal_name: str) -> bool:
        """Return True if this signal should be evaluated this session."""
        if not self.enabled:
            return True  # test mode off → everything runs normally
        if self.active_signals:
            return signal_name in self.active_signals
        return signal_name not in self.suppress_signals

    def effective_min_confidence(self, default: float) -> float:
        return self.min_confidence_override if self.min_confidence_override is not None else default

    def effective_min_gap(self, default: float) -> float:
        return self.min_gap_override if self.min_gap_override is not None else default
