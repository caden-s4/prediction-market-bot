"""
shared.exclusion_list – dynamic market exclusion list shared by both bots.

Markets are auto-excluded when:
  1. An unexpected fee is detected on a previously free market
  2. The market has had an oracle dispute resolved in the last 30 days
  3. Order book depth is below the minimum viable trade size
  4. The market is manually added via add()

Both bots check this before scanning or quoting. If a market is excluded,
it is skipped entirely.

Persistence: exclusions are stored in data/exclusions.json so they survive
restarts. Temporary exclusions (e.g. depth-based) can have a TTL.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import RLock
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).parent.parent / "data" / "exclusions.json"
_ORACLE_DISPUTE_TTL = 30 * 24 * 3600   # 30 days
_DEPTH_EXCLUSION_TTL = 3600             # 1 hour (re-check after market recovers)


class ExclusionList:
    """
    Thread-safe dynamic exclusion list for markets both bots must never touch.

    Key: "{platform}:{market_id}"
    Value: { "reason": str, "expires_at": float|None, "added_at": float }
    """

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path = path
        self._lock = RLock()
        self._entries: Dict[str, dict] = {}
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_excluded(self, platform: str, market_id: str) -> bool:
        key = f"{platform}:{market_id}"
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False
            expires_at = entry.get("expires_at")
            if expires_at is not None and time.time() > expires_at:
                del self._entries[key]
                return False
            return True

    def add(
        self,
        platform: str,
        market_id: str,
        reason: str,
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """
        Add a market to the exclusion list.

        Parameters
        ----------
        platform   : "polymarket" | "kalshi"
        market_id  : platform market id
        reason     : human-readable reason
        ttl_seconds: if set, exclusion expires after this many seconds
        """
        key = f"{platform}:{market_id}"
        entry = {
            "reason": reason,
            "added_at": time.time(),
            "expires_at": (time.time() + ttl_seconds) if ttl_seconds else None,
        }
        with self._lock:
            self._entries[key] = entry
            logger.warning("ExclusionList: excluded %s – %s", key, reason)
        self._save()

    def add_fee_surprise(self, platform: str, market_id: str) -> None:
        self.add(platform, market_id, reason="Unexpected fee detected")

    def add_oracle_dispute(self, platform: str, market_id: str) -> None:
        self.add(
            platform, market_id,
            reason="Oracle dispute in last 30 days",
            ttl_seconds=_ORACLE_DISPUTE_TTL,
        )

    def add_low_depth(self, platform: str, market_id: str) -> None:
        self.add(
            platform, market_id,
            reason="Order book depth below minimum",
            ttl_seconds=_DEPTH_EXCLUSION_TTL,
        )

    def remove(self, platform: str, market_id: str) -> None:
        key = f"{platform}:{market_id}"
        with self._lock:
            self._entries.pop(key, None)
        self._save()

    def all_excluded(self) -> Dict[str, dict]:
        """Return a snapshot of all current exclusions (for logging/monitoring)."""
        with self._lock:
            now = time.time()
            return {
                k: v for k, v in self._entries.items()
                if v.get("expires_at") is None or v["expires_at"] > now
            }

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path) as f:
                self._entries = json.load(f)
            logger.info("ExclusionList: loaded %d entries", len(self._entries))
        except Exception as exc:
            logger.warning("ExclusionList: failed to load %s: %s", self._path, exc)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = dict(self._entries)
            with open(self._path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.warning("ExclusionList: failed to save: %s", exc)
