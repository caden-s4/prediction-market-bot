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
import ctypes
import logging
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # Python <3.9

_PST = ZoneInfo("America/Los_Angeles")

sys.path.insert(0, str(Path(__file__).parent))

from config import AppConfig
from bot import BotCoordinator
from utils.logger import setup_logging

_SEP_W = 54  # width of separator lines

# Windows SetThreadExecutionState flags
_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _inhibit_sleep() -> None:
    """Tell the OS not to sleep while the bot is running (Windows only)."""
    if platform.system() == "Windows":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            )
        except Exception:
            pass


def _restore_sleep() -> None:
    """Restore normal OS sleep behaviour after the bot exits (Windows only)."""
    if platform.system() == "Windows":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:
            pass


def _print_summary(result: dict, cfg: AppConfig, show_names: bool = False) -> None:
    """Print a clean, human-readable cycle summary to stdout."""
    now = datetime.now(tz=_PST).strftime("%H:%M:%S")
    mode = "DRY RUN" if cfg.bot.dry_run else "LIVE"

    platforms = []
    if cfg.kalshi.enabled:
        platforms.append(f"kalshi:{cfg.kalshi.env}")
    if cfg.polymarket.enabled:
        platforms.append("polymarket")
    platform_str = " + ".join(platforms) if platforms else "no platform"

    elapsed_s    = result.get("cycle_ms", 0) / 1000
    bankroll     = result.get("total_usd", 0.0)
    daily_pnl    = result.get("daily_pnl_usd", 0.0)
    halted       = result.get("halted", False)
    cycle_num    = result.get("session_cycle", 0)

    platform_bals = result.get("platform_balances", {})
    kalshi_bal    = platform_bals.get("kalshi_usd")
    poly_bal      = platform_bals.get("polymarket_usd")

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
    halt_s  = "  [HALTED]" if halted else ""
    cycle_s = f"   cycle #{cycle_num}" if cycle_num else ""

    # Annotation tags
    signal_tag = "  <-- potential trades" if signals and not trades else ""
    trade_tag  = "  <-- trades executed!" if trades else ""

    # Per-platform balance strings (always 10 chars wide, aligned)
    k_s = f"${kalshi_bal:>9,.2f}" if kalshi_bal is not None else "       n/a"
    p_s = f"${poly_bal:>9,.2f}"   if poly_bal  is not None else "       n/a"

    print(f"\n{sep}")
    print(f"  SCAN COMPLETE   {now}   {mode}   {platform_str}{halt_s}{cycle_s}")
    print(sep)
    print(f"  Markets scanned          {scanned:>5}")
    print(f"  Cross-platform pairs     {pairs:>5}")
    print(f"  Signals found            {signals:>5}{signal_tag}")
    print(f"  Trades fired             {trades:>5}{trade_tag}")
    print(f"  Open positions           {positions:>5}")
    print(f"  Exits triggered          {exits:>5}")
    print(thin)
    print(f"  Kalshi    {k_s}   |   Polymarket  {p_s}")
    print(f"  Total     ${bankroll:>9,.2f}   |   P&L today  {pnl_s:>8}   |   {elapsed_s:.1f}s")
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


def _print_positions(coordinator: BotCoordinator) -> None:
    """Print a live mark-to-market view of all open positions."""
    positions = coordinator.get_open_positions()
    sep  = "=" * _SEP_W
    thin = "-" * _SEP_W
    now  = datetime.now().strftime("%H:%M:%S")

    print(f"\n{sep}")
    print(f"  OPEN POSITIONS   {now}   ({len(positions)} total)")
    print(sep)

    if not positions:
        print("  No open positions.")
    else:
        for p in positions:
            action   = p["action"].replace("_", " ").upper()
            hrs      = p.get("hours_left", 0)
            gain     = p.get("current_gain_usd", 0.0)
            gain_s   = f"+${gain:.2f}" if gain >= 0 else f"-${abs(gain):.2f}"
            cap      = p.get("capture_ratio", 0.0)
            cprice   = p.get("current_price")
            cp_s     = f"{cprice:.3f}" if cprice is not None else "n/a"
            conf     = p.get("source_confidence", 0.0)
            gt       = p.get("ground_truth_prob", 0.0)
            q        = p.get("question", "")

            print(f"  {action:<10}  ${p['size_usd']:<6.0f}  "
                  f"entry={p['entry_price']:.3f}  live={cp_s}  gt={gt:.3f}  "
                  f"[{hrs:.1f}h left]")
            print(f"    gain={gain_s}  capture={cap:.0%}  conf={conf:.2f}  "
                  f"[{p['platform']}]")
            print(f"    {q[:_SEP_W - 4]}")
            print(f"  {thin}")

    print(sep)
    print()


def _print_help() -> None:
    sep = "=" * _SEP_W
    print(f"\n{sep}")
    print("  LIVE COMMANDS")
    print(sep)
    print("  p  /  positions   Show all open positions (live mark-to-market)")
    print("  s  /  scan        Run a scan cycle right now")
    print("  clear             Wipe all tracked positions (no exit orders placed)")
    print("  h  /  help        Show this help")
    print("  Ctrl-C            Stop the bot")
    print(sep)
    print()


def _start_command_listener(coordinator: BotCoordinator, scan_event: threading.Event) -> None:
    """Spawn a daemon thread that reads commands from stdin while the bot runs."""
    if not sys.stdin.isatty():
        return  # Skip in non-interactive mode (piped input, cron, etc.)

    def _listen() -> None:
        while True:
            try:
                line = input()
                cmd  = line.strip().lower()
                if cmd in ("p", "positions"):
                    _print_positions(coordinator)
                elif cmd in ("s", "scan"):
                    print("  Triggering scan now...")
                    scan_event.set()
                elif cmd == "clear":
                    n = coordinator.clear_positions()
                    print(f"  Cleared {n} position(s) from state.")
                elif cmd in ("h", "help", "?"):
                    _print_help()
                elif cmd:
                    print(f"  Unknown command '{cmd}'. Type 'help' for commands.")
            except EOFError:
                break
            except KeyboardInterrupt:
                break

    t = threading.Thread(target=_listen, daemon=True, name="cmd-listener")
    t.start()
    print("  Type 'p' for positions, 's' to scan now, 'help' for commands.\n")


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
        "--info",
        action="store_true",
        help="Show INFO-level log lines on the console (default: WARNING+ only)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity for the console (default: WARNING). "
             "--info is a shortcut for --log-level INFO.",
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
    # --info is a shortcut for --log-level INFO; explicit --log-level takes priority
    # if both are set (e.g. --info --log-level DEBUG the user gets DEBUG).
    log_level = args.log_level
    if args.info and args.log_level == "WARNING":
        log_level = "INFO"
    setup_logging(level=log_level, log_file=args.log_file)
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
        scan_event = threading.Event()
        _start_command_listener(coordinator, scan_event)
        _inhibit_sleep()
        try:
            while True:
                try:
                    result = coordinator.run_once()
                    _print_summary(result, cfg, show_names=args.names)
                except KeyboardInterrupt:
                    logger.info("Stopped by user")
                    break
                except Exception as exc:
                    logger.exception("Cycle error: %s", exc)
                scan_event.wait(timeout=interval)
                scan_event.clear()
        finally:
            _restore_sleep()


if __name__ == "__main__":
    main()
