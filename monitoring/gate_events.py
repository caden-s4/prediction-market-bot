"""monitoring/gate_events.py — Structured gate event logging.

Writes one JSON line per gate decision to data/runtime/gate_events.jsonl.
Thread-safe append; never raises for any input.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GATE_EVENTS_PATH = Path("data/runtime/gate_events.jsonl")
SCHEMA_VERSION = 1
MAX_BYTES = 150 * 1024 * 1024
BACKUP_COUNT = 20
_write_lock = threading.Lock()

GATE_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)


def _backup_path(index: int) -> Path:
    return GATE_EVENTS_PATH.with_name(GATE_EVENTS_PATH.name + f".{index}")


def _rotate_if_needed() -> None:
    """Rotate the events file once it exceeds MAX_BYTES. Caller must hold _write_lock."""
    try:
        if GATE_EVENTS_PATH.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = _backup_path(BACKUP_COUNT)
        if oldest.exists():
            oldest.unlink()
        for i in range(BACKUP_COUNT - 1, 0, -1):
            src = _backup_path(i)
            if src.exists():
                src.replace(_backup_path(i + 1))
        GATE_EVENTS_PATH.replace(_backup_path(1))
    except Exception as exc:
        logger.warning("gate_events: rotation failed: %s", exc)


def log_gate_event(
    *,
    ticker: str,
    gate: str,
    decision: str,
    reason: Optional[str] = None,
    cycle_id: Optional[int] = None,
    platform: str = "kalshi",
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one gate-decision event to GATE_EVENTS_PATH. Never raises."""
    _now = datetime.now(timezone.utc)
    ts = _now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_now.microsecond // 1000:03d}Z"

    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ts": ts,
        "ticker": ticker,
        "gate": gate,
        "decision": decision,
        "reason": reason,
        "cycle_id": cycle_id,
        "platform": platform,
        "extra": extra,
    }

    # Validate extra is JSON-serialisable; drop it if not.
    if extra is not None:
        try:
            json.dumps(extra)
        except (TypeError, ValueError):
            logger.warning(
                "gate_events: dropped non-serializable extra for gate=%s ticker=%s",
                gate, ticker,
            )
            record["extra"] = None

    try:
        line = json.dumps(record) + "\n"
        with _write_lock:
            _rotate_if_needed()
            with GATE_EVENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception as exc:
        logger.warning("gate_events: write failed (gate=%s ticker=%s): %s", gate, ticker, exc)


if __name__ == "__main__":
    log_gate_event(ticker="KXTEST-1", gate="smoke", decision="pass")
    log_gate_event(
        ticker="KXTEST-2",
        gate="smoke",
        decision="reject",
        reason="test_reject",
        extra={"foo": 42},
    )
    log_gate_event(
        ticker="KXTEST-3",
        gate="smoke",
        decision="reject",
        reason="test_unserializable",
        extra={"bad": object()},
    )
    print(f"Wrote 3 events to {GATE_EVENTS_PATH}")
