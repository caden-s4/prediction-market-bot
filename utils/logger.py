"""
utils.logger – structured logging setup.

Call setup_logging() once at startup to configure the root logger.
Uses structlog if available, falls back to stdlib logging.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> None:
    """
    Configure root logger.

    Parameters
    ----------
    level    : logging level string ("DEBUG", "INFO", "WARNING", "ERROR")
    log_file : optional path to write logs to (in addition to stdout)
    """
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="a"))

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt=datefmt,
        handlers=handlers,
    )

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "requests", "websocket", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
