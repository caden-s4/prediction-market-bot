"""
utils.logger – structured logging setup.

Call setup_logging() once at startup to configure the root logger.

Console output: WARNING and above only (keeps the terminal clean).
File output   : INFO and above → logs/bot.log (rotating, 5 MB × 3 files).
                Pass --log-file to override the default file path.
                Pass --log-level DEBUG/INFO to also show that level on console.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

_DEFAULT_LOG_FILE = "logs/bot.log"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_BACKUP_COUNT = 3


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure root logger.

    Parameters
    ----------
    level    : minimum level for the log FILE (default INFO).
               When DEBUG is requested the console handler also drops to DEBUG.
    log_file : path for the rotating log file (default: logs/bot.log).
               Pass an empty string "" to disable file logging entirely.
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    # ── Console handler: WARNING+ by default; drops to level if DEBUG requested ──
    console_level = min(numeric_level, logging.WARNING)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)

    handlers: list = [console_handler]

    # ── File handler: rotating, INFO+ (or DEBUG if requested) ────────────────
    file_path = log_file if log_file is not None else _DEFAULT_LOG_FILE
    if file_path:  # empty string disables file logging
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            file_path,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(
        level=min(numeric_level, logging.DEBUG),  # root captures everything
        handlers=handlers,
        force=True,
    )

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "requests", "websocket", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
