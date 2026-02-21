"""
main.py – entry point for the resolution drift arbitrage bot.

Scans Polymarket and Kalshi every 5 minutes for non-crypto markets expiring
within the configured window. Finds mispricings against hard data sources
(sports APIs, FRED, Federal Register) and fires taker orders on the lagging
platform.

Usage
-----
    python main.py                   # run continuously (dry-run by default)
    python main.py --once            # single scan cycle, then exit
    python main.py --log-level DEBUG # verbose output

Environment
-----------
Copy .env.example → .env and fill in credentials. See SETUP.txt for details.
Set DRY_RUN=true to simulate without placing real orders (default).

Demo vs Production note
-----------------------
Kalshi's demo environment only has long-dated markets (7-30+ days out).
Set KALSHI_ENV=prod and RESOLUTION_WINDOW_HOURS=24 for the full strategy.
While testing on demo, set RESOLUTION_WINDOW_HOURS=168 or higher.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig
from bot import BotCoordinator
from utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolution drift arbitrage bot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan cycle and exit (for testing)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional path to write logs to (in addition to stdout)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging(level=args.log_level, log_file=args.log_file)
    logger = logging.getLogger(__name__)

    try:
        cfg = AppConfig.load()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    if cfg.bot.dry_run:
        logger.warning(
            "DRY RUN mode – no real orders will be placed. "
            "Set DRY_RUN=false in .env to trade live."
        )

    coordinator = BotCoordinator(config=cfg)

    if args.once:
        logger.info("Running single scan cycle (--once mode)")
        result = coordinator.run_once()
        logger.info("Done. Result: %s", result)
    else:
        coordinator.run_forever()


if __name__ == "__main__":
    main()
