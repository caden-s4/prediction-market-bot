"""TUI state snapshot writer.

The bot writes data/runtime/tui_state.json at the end of every cycle.
A separate TUI process reads this file to render its panels.

Atomic write: write to tmp file then rename, so readers never see
partial JSON.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TUIStateSnapshot:
    # Status bar
    mode: str                          # "GHOST" or "LIVE"
    paused: bool
    cycle_start_ts: str                # ISO 8601 UTC of current/last cycle start
    cycle_duration_s: Optional[float]  # last completed cycle duration, None on first cycle
    uptime_start_ts: str               # ISO 8601 UTC of bot startup
    wall_clock_utc: str                # ISO 8601 UTC of snapshot moment

    # State context
    disabled_features: List[str]       # e.g. ["yahoo_brackets", "kxbrentd", "polymarket"]
    current_pipeline_stage: str        # "idle"|"scanning"|"gt_fetch"|"gap_detect"|"scoring"|"executing"|"decay_monitor"

    # Counters (cumulative since uptime_start_ts)
    signals_total: int
    fills_total: int
    snipes_attempted: int
    snipes_placed: int
    shadow_signals_total: int

    # Versioning
    schema_version: int = 1            # bump only on breaking changes; TUI checks this
    git_commit: Optional[str] = None   # short hash if available, None if not in a git repo


def _read_git_commit_short() -> Optional[str]:
    """Return the short git commit hash, or None if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except Exception:
        pass
    return None


def write_snapshot(
    snapshot: TUIStateSnapshot,
    path: str = "data/runtime/tui_state.json",
) -> None:
    """Atomically write the snapshot to disk.

    Writes to {path}.tmp then renames to {path}. Crash-safe — readers
    never observe a partial file.
    """
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(snapshot), f, indent=2)
        os.replace(tmp, path)
    except Exception as exc:
        logger.warning("tui_state write failed: %s", exc)
        try:
            os.remove(tmp)
        except Exception:
            pass
