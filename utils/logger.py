"""
utils.logger – structured logging setup.

Call setup_logging() once at startup to configure the root logger.

Console output: WARNING and above by default (keeps the terminal clean).
               Pass --info to also show INFO on the console.
               Pass --log-level DEBUG to show everything on the console.
File output   : INFO and above → logs/bot.log (rotating, 5 MB × 3 files).
                Pass --log-file to override the default file path.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python <3.9

_PST = ZoneInfo("America/Los_Angeles")

_DEFAULT_LOG_FILE = "logs/bot.log"
_MAX_BYTES = 150 * 1024 * 1024  # 150 MB
_BACKUP_COUNT = 20


def setup_logging(
    level: str = "WARNING",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure root logger.

    Parameters
    ----------
    level    : minimum level for BOTH the console and log file (default WARNING).
               Pass "INFO" (via --info) to also see INFO on the console.
               Pass "DEBUG" to see everything everywhere.
    log_file : path for the rotating log file (default: logs/bot.log).
               Pass an empty string "" to disable file logging entirely.

    The log file always captures at least INFO regardless of `level`, so
    detailed diagnostics are always available even in the default quiet mode.
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    class _PSTFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created, tz=_PST)
            return dt.strftime(datefmt) if datefmt else dt.isoformat(timespec="seconds")

    formatter = _PSTFormatter(fmt, datefmt=datefmt)

    numeric_level = getattr(logging, level.upper(), logging.WARNING)

    # ── Console handler: uses the requested level (WARNING by default) ────────
    # Ensure the stream uses UTF-8 so Unicode chars (→ ─ ≤ etc.) don't raise
    # UnicodeEncodeError on Windows cp1252 terminals.  reconfigure() is the
    # correct in-place API; it's a no-op if the stream is already UTF-8 or
    # doesn't support reconfigure (e.g. StringIO in tests).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)

    handlers: list = [console_handler]

    # ── File handler: always at least INFO, honours DEBUG if requested ────────
    file_path = log_file if log_file is not None else _DEFAULT_LOG_FILE
    if file_path:  # empty string disables file logging
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        file_level = min(numeric_level, logging.INFO)  # never coarser than INFO
        file_handler = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(file_level)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(
        level=logging.DEBUG,   # root captures everything; handlers filter
        handlers=handlers,
        force=True,
    )

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "requests", "websocket", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
