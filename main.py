"""
main.py – entry point for the prediction market weather trading bot.

Usage
-----
    python main.py                        # run live (or dry-run if DRY_RUN=true)
    python main.py --once                 # run a single pipeline cycle and exit
    python main.py --log-level DEBUG      # verbose output

Environment
-----------
Copy .env.example → .env and fill in your API credentials before running.
Set DRY_RUN=true to simulate trades without placing real orders.

Swapping the reasoning engine (e.g. to use an LLM)
-----------------------------------------------------
    from my_llm_engine import LLMReasoningEngine
    bot = Bot(config=cfg, reasoning_engine=LLMReasoningEngine(...))

Changing which markets are scanned
------------------------------------
Edit data/markets/scanner.py → build_default_scanner() and change `categories`
from ["weather"] to e.g. ["weather", "politics", "sports"].
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path so all modules resolve correctly
sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig
from bot import Bot
from utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prediction market weather trading bot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pipeline cycle and exit (useful for testing)",
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

    import logging
    logger = logging.getLogger(__name__)

    try:
        cfg = AppConfig.load()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    if cfg.bot.dry_run:
        logger.warning(
            "DRY RUN mode enabled – no real orders will be placed. "
            "Set DRY_RUN=false in .env to trade live."
        )

    # ── Instantiate the bot ───────────────────────────────────────────────────
    # To use a custom reasoning engine, pass reasoning_engine= here:
    #
    #   from pipeline.python_engine import PythonReasoningEngine
    #   engine = PythonReasoningEngine(...)
    #   bot = Bot(config=cfg, reasoning_engine=engine)
    #
    # Or for an LLM engine (future):
    #   from my_llm_engine import LLMReasoningEngine
    #   bot = Bot(config=cfg, reasoning_engine=LLMReasoningEngine(model="gpt-4o"))
    #
    bot = Bot(config=cfg)

    if args.once:
        logger.info("Running single cycle (--once mode)")
        result = bot.run_once()
        logger.info(
            "Done. Evaluated=%d Passed=%d Tradeable=%d",
            result.markets_evaluated,
            result.markets_passed,
            len(result.tradeable_signals),
        )
        for sig in result.top_signals(10):
            logger.info(
                "  SIGNAL: %s | %s | edge=%.2f%% | size=$%.2f | %s",
                sig.action,
                sig.market.market_id,
                sig.edge * 100,
                sig.position_size_usd,
                sig.reasoning,
            )
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
