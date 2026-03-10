"""
resolution/signal_stats.py – Per-signal evaluation counters for test mode.

Tracks how many markets each signal evaluated, how many were actionable,
how many were blocked by the confidence gate, etc.  In test mode the report
is printed every 10 cycles and persisted to data/signal_stats_{name}.json.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent.parent / "data"
_REPORT_EVERY_N_CYCLES = 10


# ── Per-signal counter bucket ─────────────────────────────────────────────────

@dataclass
class SignalCounters:
    """Accumulated counters for one signal source."""

    signal_name: str

    # Evaluation counts
    evaluated: int = 0          # total markets run through this signal's source
    no_source: int = 0          # source returned None / not applicable
    covered: int = 0            # source returned a result (gap computable)
    gap_too_small: int = 0      # covered but effective_gap < min_gap
    actionable: int = 0         # passed gap gate
    conf_blocked: int = 0       # passed gap gate but confidence below threshold
    ghost_trades: int = 0       # trades logged as ghost (test mode forced ghost)

    # Accumulators for averages (kept as running sums)
    _gap_sum: float = field(default=0.0, repr=False)
    _gap_count: int = field(default=0, repr=False)
    _conf_sum: float = field(default=0.0, repr=False)
    _conf_count: int = field(default=0, repr=False)

    # Session tracking
    cycles_seen: int = 0
    first_seen_ts: float = field(default_factory=time.time)
    last_updated_ts: float = field(default_factory=time.time)

    # ── helpers ───────────────────────────────────────────────────────────────

    def record_gap(self, gap: float) -> None:
        self._gap_sum   += gap
        self._gap_count += 1

    def record_confidence(self, conf: float) -> None:
        self._conf_sum   += conf
        self._conf_count += 1

    @property
    def avg_gap(self) -> Optional[float]:
        return self._gap_sum / self._gap_count if self._gap_count else None

    @property
    def avg_confidence(self) -> Optional[float]:
        return self._conf_sum / self._conf_count if self._conf_count else None

    def touch(self) -> None:
        self.last_updated_ts = time.time()

    # ── serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        d = asdict(self)
        # Replace private accumulators with computed averages
        for k in ("_gap_sum", "_gap_count", "_conf_sum", "_conf_count"):
            d.pop(k, None)
        d["avg_gap"]        = self.avg_gap
        d["avg_confidence"] = self.avg_confidence
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SignalCounters":
        name = d["signal_name"]
        obj  = cls(signal_name=name)
        for attr in (
            "evaluated", "no_source", "covered", "gap_too_small",
            "actionable", "conf_blocked", "ghost_trades",
            "cycles_seen", "first_seen_ts", "last_updated_ts",
        ):
            if attr in d:
                setattr(obj, attr, d[attr])
        # Restore running sums from avg × count (best-effort; precision loss OK)
        if d.get("avg_gap") is not None and d.get("covered", 0):
            obj._gap_count = d.get("covered", 0)
            obj._gap_sum   = d["avg_gap"] * obj._gap_count
        if d.get("avg_confidence") is not None and d.get("actionable", 0):
            obj._conf_count = d.get("actionable", 0)
            obj._conf_sum   = d["avg_confidence"] * obj._conf_count
        return obj


# ── Singleton stats store ─────────────────────────────────────────────────────

class SignalStats:
    """Session-scoped per-signal stats registry.

    Usage::

        stats = SignalStats.get()
        stats.record_evaluated("financial", market_id)
        stats.record_no_source("financial", market_id)
        stats.record_covered("financial", market_id, gap=0.08, confidence=0.85)
        stats.record_actionable("financial", market_id)
        stats.record_ghost_trade("financial", market_id)
        stats.end_cycle()          # call once at the end of every scan cycle
    """

    _instance: Optional["SignalStats"] = None

    def __init__(self) -> None:
        self._counters: Dict[str, SignalCounters] = {}
        self._cycle_count: int = 0
        self._active_signals: List[str] = []  # signals being tracked this session
        _DATA_DIR.mkdir(parents=True, exist_ok=True)

    # ── singleton access ──────────────────────────────────────────────────────

    @classmethod
    def get(cls) -> "SignalStats":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (used in tests / compare mode)."""
        cls._instance = None

    # ── session setup ─────────────────────────────────────────────────────────

    def set_active_signals(self, signal_names: List[str]) -> None:
        """Register the signals being tracked this session and load persisted data."""
        self._active_signals = list(signal_names)
        for name in signal_names:
            self._counters[name] = self._load_or_create(name)

    def _load_or_create(self, name: str) -> SignalCounters:
        path = _DATA_DIR / f"signal_stats_{name}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return SignalCounters.from_dict(json.load(f))
            except Exception as exc:
                logger.warning("Could not load signal stats for %s: %s", name, exc)
        return SignalCounters(signal_name=name)

    def _ensure(self, signal_name: str) -> SignalCounters:
        if signal_name not in self._counters:
            self._counters[signal_name] = self._load_or_create(signal_name)
        return self._counters[signal_name]

    # ── recording API ─────────────────────────────────────────────────────────

    def record_evaluated(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.evaluated += 1
        c.touch()

    def record_no_source(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.no_source += 1
        c.touch()

    def record_covered(
        self,
        signal_name: str,
        _market_id: str = "",
        gap: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        c = self._ensure(signal_name)
        c.covered += 1
        c.record_gap(abs(gap))
        c.record_confidence(confidence)
        c.touch()

    def record_gap_too_small(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.gap_too_small += 1
        c.touch()

    def record_actionable(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.actionable += 1
        c.touch()

    def record_conf_blocked(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.conf_blocked += 1
        c.touch()

    def record_ghost_trade(self, signal_name: str, _market_id: str = "") -> None:
        c = self._ensure(signal_name)
        c.ghost_trades += 1
        c.touch()

    # ── cycle end ─────────────────────────────────────────────────────────────

    def end_cycle(self, print_report: bool = True) -> None:
        """Call once per scan cycle.  Prints report every 10 cycles and persists."""
        self._cycle_count += 1
        for name in list(self._counters):
            self._counters[name].cycles_seen += 1

        if print_report and (self._cycle_count % _REPORT_EVERY_N_CYCLES == 0):
            self.print_report()

        self._persist_all()

    # ── reporting ─────────────────────────────────────────────────────────────

    def print_report(self, signal_names: Optional[List[str]] = None) -> None:
        """Print a formatted signal report to stdout."""
        names = signal_names or (self._active_signals or list(self._counters))
        sep = "=" * 46
        for name in names:
            c = self._counters.get(name)
            if c is None:
                continue
            gap_s  = f"{c.avg_gap * 100:.1f}%" if c.avg_gap is not None else "n/a"
            conf_s = f"{c.avg_confidence:.2f}"  if c.avg_confidence is not None else "n/a"
            print(f"\n{sep}")
            print(f"  SIGNAL REPORT   [{name}]   cycles={c.cycles_seen}")
            print(sep)
            print(f"  Evaluated       : {c.evaluated} markets")
            print(f"  no_source       : {c.no_source}")
            print(f"  covered         : {c.covered}")
            print(f"  gap_too_small   : {c.gap_too_small}")
            print(f"  actionable      : {c.actionable}")
            print(f"  conf_blocked    : {c.conf_blocked}")
            print(f"  ghost_trades    : {c.ghost_trades}")
            print(f"  avg_gap         : {gap_s}")
            print(f"  avg_confidence  : {conf_s}")
            print(sep)

    # ── persistence ───────────────────────────────────────────────────────────

    def _persist_all(self) -> None:
        for name, counters in self._counters.items():
            path = _DATA_DIR / f"signal_stats_{name}.json"
            try:
                with open(path, "w") as f:
                    json.dump(counters.to_dict(), f, indent=2)
            except Exception as exc:
                logger.warning("Could not persist signal stats for %s: %s", name, exc)

    def get_counters(self, signal_name: str) -> Optional[SignalCounters]:
        return self._counters.get(signal_name)
