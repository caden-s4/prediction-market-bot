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
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig
from bot import BotCoordinator
from utils.logger import setup_logging

_SEP_W = 54  # width of separator lines


def _print_summary(result: dict, cfg: AppConfig, show_names: bool = False) -> None:
    """Print a clean, human-readable cycle summary to stdout."""
    now = datetime.now().strftime("%H:%M:%S")
    mode = "DRY RUN" if cfg.bot.dry_run else "LIVE"

    platforms = []
    if cfg.kalshi.enabled:
        platforms.append(f"kalshi:{cfg.kalshi.env}")
    if cfg.polymarket.enabled:
        platforms.append("polymarket")
    platform_str = " + ".join(platforms) if platforms else "no platform"

    elapsed_s = result.get("cycle_ms", 0) / 1000
    bankroll   = result.get("total_usd", 0.0)
    daily_pnl  = result.get("daily_pnl_usd", 0.0)
    halted     = result.get("halted", False)

    scanned   = result.get("markets_scanned", 0)
    pairs     = result.get("pairs_found", 0)
    signals   = result.get("signals_flagged", 0)
    trades    = result.get("trades_fired", 0)
    positions = result.get("positions_monitored", 0)
    exits     = result.get("exits_triggered", 0)
    trade_details = result.get("trade_details", [])

    sep   = "=" * _SEP_W
    thin  = "-" * _SEP_W
    pnl_s = f"+${daily_pnl:.2f}" if daily_pnl >= 0 else f"-${abs(daily_pnl):.2f}"
    halt_s = "  [HALTED]" if halted else ""

    # Annotation tags
    signal_tag = "  <-- potential trades" if signals and not trades else ""
    trade_tag  = "  <-- trades executed!" if trades else ""

    print(f"\n{sep}")
    print(f"  SCAN COMPLETE   {now}   {mode}   {platform_str}{halt_s}")
    print(sep)
    print(f"  Markets scanned          {scanned:>5}")
    print(f"  Cross-platform pairs     {pairs:>5}")
    print(f"  Signals found            {signals:>5}{signal_tag}")
    print(f"  Trades fired             {trades:>5}{trade_tag}")
    print(f"  Open positions           {positions:>5}")
    print(f"  Exits triggered          {exits:>5}")
    print(thin)
    print(f"  Bankroll  ${bankroll:>10,.2f}   |   P&L today  {pnl_s:>8}   |   {elapsed_s:.1f}s")
    print(sep)

    if show_names and trade_details:
        print(f"\n  Trades this cycle:")
        print(f"  {'─' * (_SEP_W - 2)}")
        for d in trade_details:
            action = d["action"].replace("_", " ").upper()
            src    = d.get("source", "")
            hrs    = d.get("hours_left", 0)
            print(
                f"  {action:<10}  ${d['size_usd']:<7.0f}  @{d['price']:.2f}"
                f"  [{hrs:.1f}h]  [{src}]"
            )
            q = d["question"]
            print(f"    {q[:80]}")
            if len(q) > 80:
                print(f"    {q[80:]}")
        print()
    elif show_names and not trade_details:
        sample = result.get("scanned_sample", [])
        if sample:
            print(f"\n  No trades – first {len(sample)} markets scanned:")
            print(f"  {'─' * (_SEP_W - 2)}")
            for m in sample:
                cat  = m.get("category", "?")
                hrs  = m.get("hours_left", 0)
                yes  = m.get("yes_price", 0)
                q    = m.get("question", "")
                print(f"  [{cat:<11}]  {hrs:>5.1f}h  YES={yes:.2f}   {q[:55]}")
            print()


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
    parser.add_argument(
        "--names",
        action="store_true",
        help="Print the full market name and details for each trade fired",
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
        _print_summary(result, cfg, show_names=args.names)
    else:
        interval = cfg.bot.resolution_scan_interval_seconds
        logger.info("Starting continuous scan (interval=%ds)", interval)
        while True:
            try:
                result = coordinator.run_once()
                _print_summary(result, cfg, show_names=args.names)
            except KeyboardInterrupt:
                logger.info("Stopped by user")
                break
            except Exception as exc:
                logger.exception("Cycle error: %s", exc)
            time.sleep(interval)


if __name__ == "__main__":
    main()
