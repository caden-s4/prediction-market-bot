"""
main.py – entry point for the prediction market trading bot.

Two bots run simultaneously from this process:
  Bot 1 – Maker Rebate Harvester (Polymarket 5/15-min crypto markets)
  Bot 2 – Resolution Drift Arbitrage (non-crypto markets expiring within 24h)

Usage
-----
    python main.py                        # run both bots live (or dry-run)
    python main.py --once                 # single resolution bot cycle, then exit
    python main.py --maker-only           # run only the maker bot
    python main.py --resolution-only      # run only the resolution bot
    python main.py --log-level DEBUG      # verbose output

Environment
-----------
Copy .env.example → .env and fill in your API credentials.
See SETUP.txt for the complete credential setup guide.
Set DRY_RUN=true to simulate without placing real orders.
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
        description="Prediction market trading bot (maker rebates + resolution drift)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single resolution bot cycle and exit (for testing)",
    )
    parser.add_argument(
        "--maker-only",
        action="store_true",
        help="Run only the maker rebate bot (disables resolution bot for this session)",
    )
    parser.add_argument(
        "--resolution-only",
        action="store_true",
        help="Run only the resolution drift bot (disables maker bot for this session)",
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

    # Override bot toggles from CLI flags
    if args.maker_only:
        object.__setattr__(cfg.bot, "resolution_enabled", False)
        logger.info("CLI override: resolution bot DISABLED (--maker-only)")
    if args.resolution_only:
        object.__setattr__(cfg.bot, "maker_enabled", False)
        logger.info("CLI override: maker bot DISABLED (--resolution-only)")

    if cfg.bot.dry_run:
        logger.warning(
            "DRY RUN mode – no real orders will be placed. "
            "Set DRY_RUN=false in .env to trade live."
        )

    coordinator = BotCoordinator(config=cfg)

    if args.once:
        logger.info("Running single resolution cycle (--once mode)")
        result = coordinator.run_once()
        logger.info("Done. Result: %s", result)
    else:
        coordinator.run_forever()


if __name__ == "__main__":
    main()
