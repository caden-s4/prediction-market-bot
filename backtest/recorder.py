"""
backtest.recorder – WebSocket book-state recorder.

Attaches to live order books via add_global_callback() and writes every
book snapshot to a compressed JSONL file for later replay.

Each line of the file is a JSON object:
    {
        "ts":        "2024-12-24T18:30:00.123456+00:00",
        "platform":  "kalshi",
        "market_id": "KXWEATHER-SEA-24DEC-T50",
        "bids":      [[0.45, 150.0], [0.44, 300.0], ...],
        "asks":      [[0.46, 200.0], [0.47, 100.0], ...]
    }

Storage
-------
Files are written to data/recordings/<YYYY-MM-DD>/<platform>_<market_id>.jsonl
(one file per market per day).  The recorder appends to the file so restarts
don't lose data.  Files rotate automatically at UTC midnight.

Usage
-----
recorder = BookRecorder()
poly_adapter.add_global_callback(recorder.on_book_update)
kalshi_adapter.add_global_callback(recorder.on_book_update)
# Now every book update is automatically written to disk.

To stop recording:
    recorder.close()
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, IO, Optional

from adapters.base import LiveOrderBook

logger = logging.getLogger(__name__)

_RECORDINGS_DIR = Path(__file__).parent.parent / "data" / "recordings"


def _safe_filename(s: str) -> str:
    """Replace characters not safe for filenames."""
    return s.replace("/", "-").replace(":", "-").replace(" ", "_")


class BookRecorder:
    """
    Thread-safe WebSocket book-state recorder.

    Writes every book update to a per-market JSONL file compressed with gzip.
    Files rotate at UTC midnight.
    """

    def __init__(
        self,
        recordings_dir: Optional[Path] = None,
        compress: bool = True,
        min_interval_ms: float = 100.0,   # deduplicate updates faster than this
    ) -> None:
        self._dir = recordings_dir or _RECORDINGS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._compress = compress
        self._min_interval_ms = min_interval_ms

        self._handles: Dict[str, IO] = {}          # key → open file handle
        self._last_write: Dict[str, float] = {}    # key → monotonic timestamp
        self._lock = threading.Lock()
        self._closed = False

    # ── Callback (attach to adapter) ───────────────────────────────────────────

    def on_book_update(self, book: LiveOrderBook) -> None:
        """Called by LiveOrderBook on every update.  Thread-safe."""
        if self._closed:
            return

        import time
        now_mono = time.monotonic()
        key = f"{book.platform}:{book.market_id}"

        # Deduplicate rapid-fire updates
        with self._lock:
            last = self._last_write.get(key, 0.0)
            if (now_mono - last) * 1000 < self._min_interval_ms:
                return
            self._last_write[key] = now_mono

        ts = datetime.now(timezone.utc)
        record = {
            "ts": ts.isoformat(),
            "platform": book.platform,
            "market_id": book.market_id,
            "bids": book.snapshot()["bids"],
            "asks": book.snapshot()["asks"],
        }

        self._write(key, book.platform, book.market_id, ts, record)

    # ── File management ────────────────────────────────────────────────────────

    def _write(
        self,
        key: str,
        platform: str,
        market_id: str,
        ts: datetime,
        record: dict,
    ) -> None:
        with self._lock:
            fh = self._get_handle(key, platform, market_id, ts)
            try:
                line = json.dumps(record) + "\n"
                if self._compress:
                    fh.write(line.encode("utf-8"))
                else:
                    fh.write(line)
                fh.flush()
            except Exception as exc:
                logger.warning("BookRecorder write error for %s: %s", key, exc)
                # Close and reopen on next write
                self._handles.pop(key, None)

    def _get_handle(
        self,
        key: str,
        platform: str,
        market_id: str,
        ts: datetime,
    ) -> IO:
        """Return an open file handle, creating/rotating as needed."""
        date_str = ts.strftime("%Y-%m-%d")
        day_dir = self._dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)

        ext = ".jsonl.gz" if self._compress else ".jsonl"
        filename = f"{platform}_{_safe_filename(market_id)}{ext}"
        expected_path = day_dir / filename

        existing = self._handles.get(key)
        # Rotate if a new day started (path changed)
        if existing is not None:
            existing_path = getattr(existing, "name", None)
            if existing_path and str(expected_path) != existing_path:
                try:
                    existing.close()
                except Exception:
                    pass
                del self._handles[key]
                existing = None

        if existing is None:
            if self._compress:
                fh = gzip.open(str(expected_path), "ab")
            else:
                fh = open(str(expected_path), "a", encoding="utf-8")
            self._handles[key] = fh
            logger.info("BookRecorder: opened %s", expected_path)

        return self._handles[key]

    def close(self) -> None:
        """Flush and close all open file handles."""
        self._closed = True
        with self._lock:
            for fh in self._handles.values():
                try:
                    fh.close()
                except Exception:
                    pass
            self._handles.clear()
        logger.info("BookRecorder: all handles closed")

    # ── Listing recordings ─────────────────────────────────────────────────────

    @staticmethod
    def list_recordings(
        recordings_dir: Optional[Path] = None,
    ) -> list[Path]:
        """Return all recorded files sorted by date/name."""
        d = recordings_dir or _RECORDINGS_DIR
        ext_patterns = ["*.jsonl.gz", "*.jsonl"]
        files: list[Path] = []
        for pattern in ext_patterns:
            files.extend(sorted(d.rglob(pattern)))
        return sorted(files)
