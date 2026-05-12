"""
Shared loader for `ghost_trades.jsonl`.

Pairs entries with exits (FIFO within market_id, chronological), applies the
standard filter set from the Phase D-Fidelity audit
(`docs/diagnostics/trade_log_fidelity_audit.md`), and returns a clean trade
list plus a structured shrinkage report.

Public API:
    load_clean_trades(path: Path = DEFAULT_PATH) -> tuple[list[Trade], ShrinkageReport]
    Trade        — dataclass, one closed paired trade
    ShrinkageReport — dataclass, filter accounting
    iter_raw_entries(path: Path = DEFAULT_PATH) -> Iterator[dict]
        Raw entry-event records, dedup-by-first-seen-market_id. Provided so
        scripts/phase0_accuracy.py can swap its load_entries() to this loader
        in a follow-up phase ("phase0_accuracy swap to _ghost_loader").

Filter order (applied in `load_clean_trades`, stable):
    1. Pair entries with exits (orphans dropped; split into open vs
       log-missing-exit using ghost_positions.json).
    2. Sign-inverted exclusion: sign(implied_pnl) != sign(recorded_pnl),
       both non-zero. Audit found 3 known cases (KXBRENTD 2026-03-31).
    3. Clamped exclusion: for non-zero pnl, back-computed implied_size
       differs from recorded size_usd by > 10%. Also captures the
       cannot-back-compute case (xp == ep and pnl != 0).
    4. Pre-2026-04-15 sports exclusion: sports-tagged source/market and
       exit ts < 2026-04-15T00:00:00Z.

pnl == 0 trades are kept (audit's "unverifiable" tag was about back-computing
size, not analytical validity — they are valid flat exits).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).parent.parent
DEFAULT_PATH = ROOT / "data" / "runtime" / "ghost_trades.jsonl"
OPEN_POSITIONS_PATH = ROOT / "data" / "runtime" / "ghost_positions.json"

PRE_FIX_SPORTS_CUTOFF = datetime(2026, 4, 15, tzinfo=timezone.utc)

_SPORTS_SOURCE_PREFIXES = (
    "ESPN/",
    "SportsLiveSource",
    "SportsDataSource",
    "ShockDetector",
)
_SPORTS_SOURCES_EXACT = {"ResolutionDetector/ConfirmedFinal"}
_SPORTS_MARKET_PREFIXES = (
    "KXNBAGAME",
    "KXNCAAMBGAME",
    "KXNFLGAME",
    "KXNCAAWBGAME",
    "KXMLB",
    "KXNHL",
)

_CLAMP_RATIO_THRESHOLD = 0.10


@dataclass
class Trade:
    market_id: str
    source: str
    signal_class: str
    action: str
    event_entry_ts: datetime
    event_exit_ts: datetime
    entry_price: float
    exit_price: float
    size_usd: float
    pnl: float
    pnl_pct: float
    is_open: bool = False


@dataclass
class ShrinkageReport:
    raw_entries: int = 0
    raw_exits: int = 0
    paired: int = 0
    sign_inverted_excluded: int = 0
    clamped_excluded: int = 0
    pre_fix_sports_excluded: int = 0
    orphans_open: int = 0
    orphans_log_missing_exit: int = 0
    clean_count: int = 0

    @property
    def orphans_excluded(self) -> int:
        return self.orphans_open + self.orphans_log_missing_exit


def _parse_ts(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _is_sports(source: str, market_id: str) -> bool:
    if source in _SPORTS_SOURCES_EXACT:
        return True
    for pfx in _SPORTS_SOURCE_PREFIXES:
        if source.startswith(pfx):
            return True
    for pfx in _SPORTS_MARKET_PREFIXES:
        if market_id.startswith(pfx):
            return True
    return False


def _implied_pnl(action: str, ep: float, xp: float, size_usd: float) -> Optional[float]:
    """P&L predicted by recorded entry_price, exit_price, size_usd."""
    if action == "buy_yes":
        if ep <= 0:
            return None
        contracts = size_usd / ep
        return contracts * (xp - ep)
    if action == "buy_no":
        if (1 - ep) <= 0:
            return None
        contracts = size_usd / (1 - ep)
        return contracts * (ep - xp)
    return None


def _implied_size(action: str, ep: float, xp: float, pnl: float) -> Optional[float]:
    """Size back-computed from recorded pnl. None if undefined."""
    if action == "buy_yes":
        denom = xp - ep
        if denom == 0:
            return None
        return pnl * ep / denom
    if action == "buy_no":
        denom = ep - xp
        if denom == 0:
            return None
        return pnl * (1 - ep) / denom
    return None


def _load_open_position_ids(path: Path = OPEN_POSITIONS_PATH) -> set[str]:
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return set()
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    return set(positions.keys())


def iter_raw_entries(path: Path = DEFAULT_PATH) -> Iterator[dict]:
    """Yield entry records, dedup-by-first-seen-market_id.

    Mirrors scripts/phase0_accuracy.py load_entries() semantics so that
    script can later swap to a single-line import.
    """
    seen: set[str] = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "entry":
                continue
            mid = rec.get("market_id", "")
            if mid in seen:
                continue
            seen.add(mid)
            yield rec


def _load_raw_records(path: Path) -> tuple[list[dict], list[dict]]:
    entries: list[dict] = []
    exits: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ev = rec.get("event")
            if ev == "entry":
                entries.append(rec)
            elif ev == "exit":
                exits.append(rec)
    return entries, exits


def _pair_records(
    entries: list[dict], exits: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """FIFO pair within market_id by chronological order.

    Returns (paired list of (entry, exit), unpaired entry list).
    Orphan exits (no matching entry) are silently dropped — they cannot
    influence per-(source, signal_class) P&L.
    """
    from collections import defaultdict, deque

    events_by_mid: dict[str, list[tuple[datetime, str, dict]]] = defaultdict(list)
    for rec in entries:
        ts = _parse_ts(rec.get("ts", ""))
        if ts is None:
            continue
        events_by_mid[rec.get("market_id", "")].append((ts, "entry", rec))
    for rec in exits:
        ts = _parse_ts(rec.get("ts", ""))
        if ts is None:
            continue
        events_by_mid[rec.get("market_id", "")].append((ts, "exit", rec))

    paired: list[tuple[dict, dict]] = []
    unpaired_entries: list[dict] = []

    for events in events_by_mid.values():
        events.sort(key=lambda t: t[0])
        open_q: deque[dict] = deque()
        for _, kind, rec in events:
            if kind == "entry":
                open_q.append(rec)
            else:
                if open_q:
                    paired.append((open_q.popleft(), rec))
        unpaired_entries.extend(open_q)

    return paired, unpaired_entries


def load_clean_trades(
    path: Path = DEFAULT_PATH,
    positions_path: Path = OPEN_POSITIONS_PATH,
) -> tuple[list[Trade], ShrinkageReport]:
    raw_entries, raw_exits = _load_raw_records(path)
    paired, unpaired = _pair_records(raw_entries, raw_exits)

    open_ids = _load_open_position_ids(positions_path)
    orphans_open = sum(1 for e in unpaired if e.get("market_id", "") in open_ids)

    report = ShrinkageReport(
        raw_entries=len(raw_entries),
        raw_exits=len(raw_exits),
        paired=len(paired),
        orphans_open=orphans_open,
        orphans_log_missing_exit=len(unpaired) - orphans_open,
    )

    clean: list[Trade] = []
    for entry, exit_rec in paired:
        action = entry.get("action", "")
        ep = float(entry.get("entry_price", 0) or 0)
        xp = float(exit_rec.get("exit_price", 0) or 0)
        size_usd = float(entry.get("size_usd", 0) or 0)
        pnl = float(exit_rec.get("pnl", 0) or 0)
        pnl_pct = float(exit_rec.get("pnl_pct", 0) or 0)

        # Filter 2: sign-inverted.
        if pnl != 0:
            implied_pnl = _implied_pnl(action, ep, xp, size_usd)
            if implied_pnl is not None and implied_pnl != 0:
                if (implied_pnl > 0) != (pnl > 0):
                    report.sign_inverted_excluded += 1
                    continue

        # Filter 3: clamped.
        if pnl != 0:
            isize = _implied_size(action, ep, xp, pnl)
            if isize is None:
                # xp == ep and pnl != 0 — can't back-compute. Anomalous.
                report.clamped_excluded += 1
                continue
            if size_usd > 0 and abs(isize - size_usd) / size_usd > _CLAMP_RATIO_THRESHOLD:
                report.clamped_excluded += 1
                continue

        # Filter 4: pre-2026-04-15 sports.
        source = entry.get("source", "unknown") or "unknown"
        market_id = entry.get("market_id", "")
        exit_ts = _parse_ts(exit_rec.get("ts", ""))
        if exit_ts is not None and exit_ts < PRE_FIX_SPORTS_CUTOFF:
            if _is_sports(source, market_id):
                report.pre_fix_sports_excluded += 1
                continue

        entry_ts = _parse_ts(entry.get("ts", ""))
        if entry_ts is None or exit_ts is None:
            # Missing timestamp — drop quietly (cannot be sliced by window).
            continue

        signal_class = entry.get("signal_class") or "unknown"

        clean.append(Trade(
            market_id=market_id,
            source=source,
            signal_class=signal_class,
            action=action,
            event_entry_ts=entry_ts,
            event_exit_ts=exit_ts,
            entry_price=ep,
            exit_price=xp,
            size_usd=size_usd,
            pnl=pnl,
            pnl_pct=pnl_pct,
            is_open=False,
        ))

    report.clean_count = len(clean)
    return clean, report


def open_positions_by_source_class(
    path: Path = OPEN_POSITIONS_PATH,
) -> dict[tuple[str, str], int]:
    """Group currently-open ghost positions by (source, signal_class)."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    positions = data.get("positions", {}) if isinstance(data, dict) else {}
    out: dict[tuple[str, str], int] = {}
    for pos in positions.values():
        src = pos.get("source") or "unknown"
        sc = pos.get("signal_class") or "unknown"
        out[(src, sc)] = out.get((src, sc), 0) + 1
    return out
