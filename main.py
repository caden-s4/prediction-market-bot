"""
main.py – entry point for the resolution drift arbitrage bot.

Scans Polymarket and Kalshi every 5 minutes for non-crypto markets expiring
within the configured window. Finds mispricings against hard data sources
(sports APIs, FRED, Federal Register) and fires taker orders on the lagging
platform.

Usage
-----
    python main.py                              # run continuously (dry-run by default)
    python main.py --once                       # single scan cycle, then exit
    python main.py --log-level DEBUG            # verbose output

    # Signal testing / isolation
    python main.py --test-signal financial      # run only the financial signal
    python main.py --test-signal sports_shock   # run only sports shock signal
    python main.py --test-signal fred --min-confidence 0.7
    python main.py --test-signal sports_resolution --min-gap 0.05
    python main.py --suppress-signal fred --suppress-signal cross_platform
    python main.py --compare financial fred     # side-by-side comparison
    python main.py --replay logs/bot_log.1      # re-run from saved verbose log

Environment
-----------
Copy .env.example → .env and fill in credentials. See SETUP.txt for details.
Set LIVE_TRADING=false (default) to simulate without placing real orders.

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

from config import AppConfig, SignalTestSettings
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
    mode = "GHOST TRADE" if cfg.bot.dry_run else "LIVE"

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

    # Annotation tags — use post-gate signal count so "would execute" is only
    # shown when signals actually passed the confidence gate.
    confidence_blocked = result.get("confidence_blocked", 0)
    passed_gate = len(result.get("signals_detail", []))
    if trades:
        signal_tag = ""  # trade_tag covers this
    elif passed_gate and not trades:
        signal_tag = "  <-- would execute!"
    elif confidence_blocked and not passed_gate:
        signal_tag = f"  <-- {confidence_blocked} blocked by confidence gate"
    else:
        signal_tag = ""
    trade_tag  = "  <-- trades executed!" if trades else ""

    # Per-platform balance strings (always 10 chars wide, aligned)
    k_s = f"${kalshi_bal:>9,.2f}" if kalshi_bal is not None else "       n/a"
    p_s = f"${poly_bal:>9,.2f}"   if poly_bal  is not None else "       n/a"

    registry  = result.get("registry", {})
    reg_t1    = registry.get("t1", 0)
    reg_t2    = registry.get("t2", 0)
    reg_t3    = registry.get("t3", 0)
    reg_total = registry.get("total", 0)
    t1_scanned = result.get("t1_scanned", 0)
    t2_scanned = result.get("t2_scanned", 0)
    t2_total   = result.get("t2_total", reg_t2)

    # "Markets scanned" with per-tier breakdown of what was evaluated this cycle.
    if reg_total:
        tier_detail = f"T1={t1_scanned}/{reg_t1}  T2 batch={t2_scanned}/{t2_total}  T3={reg_t3} (watch-only)"
    else:
        tier_detail = "T1 all + T2 rotating batch"

    # Registry discovery line — only shown once the registry is populated.
    reg_str = (
        f"  Registry total           T1={reg_t1}  T2={reg_t2}  T3={reg_t3}  ({reg_total} markets)\n"
        if reg_total else ""
    )

    print(f"\n{sep}")
    print(f"  SCAN COMPLETE   {now}   {mode}   {platform_str}{halt_s}{cycle_s}")
    print(sep)
    print(f"  Markets scanned          {scanned:>5}   {tier_detail}")
    print(f"{reg_str}", end="")
    print(f"  Cross-platform pairs     {pairs:>5}")
    print(f"  Gap signals detected     {signals:>5}{signal_tag}")
    ghost = cfg.bot.dry_run
    trades_label    = "Ghost trades fired" if ghost else "Trades fired"
    positions_label = "Ghost positions    " if ghost else "Open positions     "
    pnl_label       = "Ghost P&L today" if ghost else "P&L today      "
    print(f"  {trades_label:<24} {trades:>5}{trade_tag}")
    print(f"  {positions_label:<24} {positions:>5}")
    print(f"  Exits triggered          {exits:>5}")
    print(thin)
    print(f"  Kalshi    {k_s}   |   Polymarket  {p_s}")
    print(f"  Total     ${bankroll:>9,.2f}   |   {pnl_label}  {pnl_s:>8}   |   {elapsed_s:.1f}s")
    print(sep)

    if show_names and trade_details:
        label = "Ghost trades this cycle (SIMULATED)" if cfg.bot.dry_run else "Trades this cycle"
        print(f"\n  {label}:")
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

    # In dry-run mode show the flagged signals (potential trades) so you can
    # see what the bot is considering even when 0 orders are placed.
    dry_run = cfg.bot.dry_run
    signal_details = result.get("signals_detail", [])
    confidence_blocked = result.get("confidence_blocked", 0)
    if dry_run and confidence_blocked and not signal_details and not trade_details:
        # Signals were detected but all blocked by the confidence gate –
        # show a clear summary instead of silently printing nothing.
        total = result.get("signals_flagged", confidence_blocked)
        print(
            f"\n  {total} gap signal(s) detected, all blocked by confidence gate"
            f" (source confidence below 0.80 — check log for reason)."
        )
        print()
    if dry_run and signal_details and not trade_details:
        print(f"\n  Ghost trades that passed all gates (simulated, session-only):")
        print(f"  {'─' * (_SEP_W - 2)}")
        for d in signal_details:
            action  = d["action"].replace("_", " ").upper()
            gap_pct = d["effective_gap"] * 100
            hrs     = d.get("hours_left", 0)
            src     = d.get("source", "")
            stype   = "cross" if d["signal_type"] == "cross_platform" else "info"
            held    = "  [already held]" if d.get("already_held") else ""
            print(
                f"  {action:<10}  @{d['price']:.2f}  gap={gap_pct:.1f}%"
                f"  [{hrs:.1f}h]  [{stype}:{src}]{held}"
            )
            q = d["question"]
            print(f"    {q[:80]}")
            if len(q) > 80:
                print(f"    {q[80:]}")
        print()
    elif show_names and not trade_details and not (dry_run and signal_details) and not (dry_run and confidence_blocked):
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


def _print_signals(coordinator: BotCoordinator) -> None:
    """Print the gap signals detected in the most recent scan cycle."""
    signals = coordinator.get_last_signals()
    sep  = "=" * _SEP_W
    thin = "-" * _SEP_W
    now  = datetime.now().strftime("%H:%M:%S")

    print(f"\n{sep}")
    print(f"  LAST SIGNALS   {now}   ({len(signals)} total)")
    print(sep)

    if not signals:
        print("  No signals from the last cycle (run a scan first).")
    else:
        for s in signals:
            action   = s["action"].replace("_", " ").upper()
            gap_pct  = s["effective_gap"] * 100
            hrs      = s.get("hours_left", 0)
            src      = s.get("source", "")
            stype    = "cross" if s["signal_type"] == "cross_platform" else "info"
            held_tag = "  [already held]" if s.get("already_held") else ""
            q        = s.get("question", "")

            print(f"  {action:<10}  @{s['price']:.2f}  gap={gap_pct:.1f}%  "
                  f"[{hrs:.1f}h]  [{stype}:{src}]{held_tag}")
            print(f"    {q[:_SEP_W - 4]}")
            print(f"  {thin}")

    print(sep)
    print()


def _print_near_miss_pairs(coordinator: BotCoordinator, top_n: int = 10) -> None:
    """
    Print the top-N near-miss cross-platform pairs ranked by word overlap.

    Only pairs within the 6h time window are shown (the time gate is now a
    hard pre-filter).  A near-miss is a within-window pair that failed the
    word-count (>=3) or entity-match requirement.
    """
    pairs, stats = coordinator.get_near_miss_pairs(top_n)
    sep  = "=" * _SEP_W
    thin = "-" * _SEP_W
    now  = datetime.now().strftime("%H:%M:%S")

    n_poly   = stats.get("poly_count", 0)
    n_kalshi = stats.get("kalshi_count", 0)
    n_window = stats.get("within_window", 0)
    n_word   = stats.get("with_word_overlap", 0)

    print(f"\n{sep}")
    print(f"  NEAR-MISS PAIRS   {now}   (top {top_n}, within-6h window only)")
    print(f"  {n_poly} poly × {n_kalshi} kalshi  →  {n_window} within 6h  →  {n_word} with word overlap")
    print(sep)

    if not pairs:
        if n_poly == 0 or n_kalshi == 0:
            print("  Registry has no markets from one or both platforms.")
            print("  Run a scan first ('s') to populate the registry.")
        elif n_window == 0:
            print(f"  All {n_poly * n_kalshi:,} pairs fall outside the 6h resolution-date window.")
            print(f"  ({n_poly} Poly markets expire >24h out; {n_kalshi} Kalshi markets expire 2–24h out.)")
            print("  Near-miss analysis only surfaces same-day markets — this is correct.")
        elif n_word == 0:
            print(f"  {n_window} within-window pair(s) found, but none share any significant words")
            print("  after filtering stopwords (months, years, aux verbs).")
        else:
            print(f"  {n_word} within-window pair(s) with word overlap all qualified as full matches.")
        print(f"{sep}\n")
        return

    for i, p in enumerate(pairs, 1):
        n_words    = p["overlap_count"]
        dt_h       = p["time_delta_hours"]
        words_ok   = p["would_match_on_words"]
        entity_ok  = p.get("would_match_on_entity", p.get("has_entity_overlap", False))
        overlap_s  = ", ".join(p["overlap_words"])
        entity_s   = ", ".join(p.get("entity_words", [])) or "none"

        blockers = []
        if not words_ok:
            blockers.append(f"needs {3 - n_words} more word(s)")
        if not entity_ok:
            blockers.append("no entity overlap")
        blocker_s = "BLOCKED: " + " + ".join(blockers) if blockers else "BLOCKED: (unknown)"

        gap_s = f"  Δprice={p['price_gap']:.3f}" if p["price_gap"] else ""

        print(f"  #{i:02d}  overlap={n_words}  Δt={dt_h:.1f}h{gap_s}")
        print(f"       words:   [{overlap_s}]")
        print(f"       entity:  [{entity_s}]")
        print(f"       {blocker_s}")

        pq = p["poly_question"]
        print(f"  POLY   [{p['poly_hours_left']:.0f}h]  YES={p['poly_yes_price']:.2f}  {pq[:_SEP_W - 24]}")
        if len(pq) > _SEP_W - 24:
            print(f"         {pq[_SEP_W - 24:2 * (_SEP_W - 24)]}")

        kq = p["kalshi_question"]
        print(f"  KALSHI [{p['kalshi_hours_left']:.0f}h]  YES={p['kalshi_yes_price']:.2f}  {kq[:_SEP_W - 24]}")
        if len(kq) > _SEP_W - 24:
            print(f"         {kq[_SEP_W - 24:2 * (_SEP_W - 24)]}")

        print(f"  {thin}")

    print(sep)
    print()


def _series_root_hist(market_id: str) -> str:
    idx = market_id.find("-")
    return market_id[:idx] if idx != -1 else market_id


def _fmt_pnl(v: float) -> str:
    return f"+${v:.2f}" if v >= 0 else f"-${abs(v):.2f}"


def _print_history(coordinator: BotCoordinator) -> None:
    """Print all trades resolved this session with summary stats and P&L breakdown."""
    resolved = coordinator.get_resolved_positions()
    sep  = "=" * _SEP_W
    thin = "-" * _SEP_W
    now  = datetime.now().strftime("%H:%M:%S")

    print(f"\n{sep}")
    print(f"  TRADE HISTORY   {now}   ({len(resolved)} resolved this session)")
    print(sep)

    if not resolved:
        print("  No trades resolved this session.")
        print(sep)
        print()
        return

    # --- aggregate stats ---
    total_pnl    = sum(r.pnl for r in resolved)
    total_invest = sum(r.size_usd for r in resolved)
    roi          = total_pnl / total_invest if total_invest else 0.0

    wins      = [r for r in resolved if r.pnl > 0.005]
    losses    = [r for r in resolved if r.pnl < -0.005]
    scratches = [r for r in resolved if abs(r.pnl) <= 0.005]
    win_rate  = len(wins) / len(resolved) if resolved else 0.0

    captures = [r.capture for r in resolved if r.capture is not None]
    avg_cap  = sum(captures) / len(captures) if captures else None

    best  = max(resolved, key=lambda r: r.pnl)
    worst = min(resolved, key=lambda r: r.pnl)

    print(f"  P&L      : {_fmt_pnl(total_pnl)}   ROI: {roi:+.1%}   invested: ${total_invest:.0f}")
    print(f"  Results  : {len(wins)}W / {len(losses)}L / {len(scratches)}S   "
          f"win rate: {win_rate:.0%}")
    cap_s = f"{avg_cap:.0%}" if avg_cap is not None else "n/a"
    print(f"  Avg cap  : {cap_s}")
    print(f"  Best     : {_fmt_pnl(best.pnl)}  [{best.market_id}]")
    print(f"  Worst    : {_fmt_pnl(worst.pnl)}  [{worst.market_id}]")

    # --- per-series breakdown ---
    series_map: dict = {}
    for r in resolved:
        root = _series_root_hist(r.market_id)
        if root not in series_map:
            series_map[root] = {"count": 0, "pnl": 0.0}
        series_map[root]["count"] += 1
        series_map[root]["pnl"]   += r.pnl

    if len(series_map) > 1:
        print(f"\n  {thin}")
        print("  BY SERIES")
        print(f"  {thin}")
        for root, agg in sorted(series_map.items(), key=lambda x: -abs(x[1]["pnl"])):
            print(f"  {root:<30}  {agg['count']:>2} trade(s)   {_fmt_pnl(agg['pnl'])}")

    # --- individual trades ---
    print(f"\n  {thin}")
    print("  TRADES")
    print(f"  {thin}")
    for r in resolved:
        if r.pnl > 0.005:
            result = "WIN    "
        elif r.pnl < -0.005:
            result = "LOSS   "
        else:
            result = "SCRATCH"
        cap_s     = f"{r.capture:.0%}" if r.capture is not None else "n/a"
        entered_s = r.entered_at.strftime("%m-%d %H:%M") if r.entered_at else "?"
        closed_s  = r.resolved_at.strftime("%m-%d %H:%M") if r.resolved_at else "?"
        n_contracts = r.num_contracts
        direction = "YES" if r.action == "buy_yes" else "NO "
        print(f"  {result}  {_fmt_pnl(r.pnl):<10}  cap={cap_s:<5}  "
              f"conf={r.confidence:.2f}  {direction}  "
              f"entry={r.entry_price:.3f}  exit={r.exit_price:.3f}  "
              f"${r.size_usd:.0f} ({n_contracts:.0f}¢)")
        print(f"    src={r.source}  opened={entered_s}  closed={closed_s}  [{r.market_id}]")

    print(f"\n  {thin}")
    print(f"  Session P&L: {_fmt_pnl(total_pnl)} across {len(resolved)} trade(s)")
    print(sep)
    print()


def _print_test_mode_banner(st: SignalTestSettings) -> None:
    """Print a prominent banner when signal test mode is active."""
    W = 44
    box_w = W + 2

    def _pad(s: str) -> str:
        return f"║  {s:<{W}}║"

    lines = []
    if st.active_signals:
        signals_str = ", ".join(st.active_signals)
        lines.append(f"SIGNAL TEST MODE — {signals_str} only")
        lines.append("All other signals suppressed")
    else:
        suppressed_str = ", ".join(st.suppress_signals)
        lines.append("SIGNAL TEST MODE — suppress mode")
        lines.append(f"Suppressed: {suppressed_str}")

    lines.append("Ghost mode forced ON")

    if st.min_confidence_override is not None:
        lines.append(f"Min confidence: {st.min_confidence_override:.2f} (override)")
    else:
        lines.append("Min confidence: 0.80 (default)")

    if st.min_gap_override is not None:
        lines.append(f"Min gap: {st.min_gap_override:.2f} (override)")

    print(f"\n╔{'═' * box_w}╗")
    for line in lines:
        print(_pad(line[:W]))
    print(f"╚{'═' * box_w}╝\n")


def _run_compare_mode(
    args: argparse.Namespace,
    cfg: AppConfig,
    signals_a: list,
    signals_b: list,
) -> None:
    """Run two signal configs side-by-side and log differences."""
    logger = logging.getLogger(__name__)

    st_a = SignalTestSettings.from_cli_args(signals_a, None, args.min_confidence, args.min_gap)
    st_b = SignalTestSettings.from_cli_args(signals_b, None, args.min_confidence, args.min_gap)

    cfg_a = cfg.with_signal_test(st_a)
    cfg_b = cfg.with_signal_test(st_b)

    _print_test_mode_banner(st_a)
    print(f"  COMPARE MODE: {signals_a}  vs  {signals_b}\n")

    coord_a = BotCoordinator(config=cfg_a)
    coord_b = BotCoordinator(config=cfg_b)

    result_a = coord_a.run_once(skip_stabilization=True)
    result_b = coord_b.run_once(skip_stabilization=True)

    sep = "=" * 54
    print(f"\n{sep}")
    print(f"  COMPARE RESULTS")
    print(sep)

    sigs_a = result_a.get("signals_detail", [])
    sigs_b = result_b.get("signals_detail", [])

    ids_a = {s.get("market_id", s.get("question", "")) for s in sigs_a}
    ids_b = {s.get("market_id", s.get("question", "")) for s in sigs_b}

    only_a = ids_a - ids_b
    only_b = ids_b - ids_a
    both   = ids_a & ids_b

    sig_a_str = ", ".join(signals_a)
    sig_b_str = ", ".join(signals_b)

    print(f"  [{sig_a_str}] only  : {len(only_a)} signal(s)")
    print(f"  [{sig_b_str}] only  : {len(only_b)} signal(s)")
    print(f"  Both agree (convergence): {len(both)} signal(s)")

    if both:
        print(f"\n  Convergence signals (both fired):")
        for mid in sorted(both):
            print(f"    {mid}")

    if only_a:
        print(f"\n  Only [{sig_a_str}]:")
        for mid in sorted(only_a):
            print(f"    {mid}")

    if only_b:
        print(f"\n  Only [{sig_b_str}]:")
        for mid in sorted(only_b):
            print(f"    {mid}")

    print(sep)


def _run_replay_mode(log_path: str) -> None:
    """Re-run signal evaluation by parsing a verbose log file (no live API calls)."""
    import re as _re

    logger = logging.getLogger(__name__)
    path = Path(log_path)
    if not path.exists():
        logger.error("Replay: log file not found: %s", log_path)
        return

    sep = "=" * 54
    print(f"\n{sep}")
    print(f"  REPLAY MODE — {path.name}")
    print(sep)

    # Parse verbose log entries emitted by _verbose_log() in router.py
    # Format: "[SourceName] MARKET_ID\n  source=... value=... ..."
    entry_re  = _re.compile(r"^\[(\w+)\] (\S+)$")
    verdict_re = _re.compile(r"verdict:\s+(\w+)")

    entries: list[dict] = []
    current: dict | None = None

    try:
        text = path.read_text(errors="replace")
    except Exception as exc:
        logger.error("Replay: could not read %s: %s", log_path, exc)
        return

    for raw_line in text.splitlines():
        line = raw_line.strip()
        m = entry_re.match(line)
        if m:
            if current:
                entries.append(current)
            current = {"source": m.group(1), "market_id": m.group(2), "verdict": "unknown", "lines": [line]}
        elif current is not None:
            current["lines"].append(line)
            vm = verdict_re.search(line)
            if vm:
                current["verdict"] = vm.group(1)

    if current:
        entries.append(current)

    if not entries:
        print("  No verbose signal entries found in log file.")
        print("  (Run with --test-signal <name> to generate verbose logs.)")
        print(sep)
        return

    # Aggregate
    from collections import Counter
    verdict_counts: Counter = Counter(e["verdict"] for e in entries)
    source_counts: Counter  = Counter(e["source"] for e in entries)

    print(f"  Total entries parsed: {len(entries)}")
    print(f"  Sources: {dict(source_counts)}")
    print(f"\n  Verdict breakdown:")
    for verdict, count in sorted(verdict_counts.items()):
        print(f"    {verdict:<20} : {count}")

    actionable = [e for e in entries if e["verdict"] == "actionable"]
    if actionable:
        print(f"\n  Actionable signals ({len(actionable)}):")
        for e in actionable[:20]:
            print(f"    [{e['source']}] {e['market_id']}")
        if len(actionable) > 20:
            print(f"    ... and {len(actionable) - 20} more")

    print(sep)


def _print_paper(coordinator: "BotCoordinator", days: int = 1) -> None:
    """Print paper-trade log summary for the last N days."""
    from datetime import datetime, timedelta, timezone
    from resolution.executor import _print_paper_summary

    paper_log = coordinator.get_paper_log()
    if paper_log is None:
        print("  Paper log is only available in dry-run mode.")
        return

    now = datetime.now(timezone.utc)
    printed = 0
    for d in range(days - 1, -1, -1):
        target_day = now - timedelta(days=d)
        summary = paper_log.get_daily_summary(date=target_day)
        if summary["total_entries"] > 0 or summary["exits"] > 0:
            _print_paper_summary(summary)
            printed += 1

    if printed == 0:
        sep = "-" * 52
        print(f"\n{sep}")
        print(f"  GHOST TRADE LOG — no trades in the last {days} day(s).")
        print(sep)
        print()


def _print_help() -> None:
    sep = "=" * _SEP_W
    print(f"\n{sep}")
    print("  LIVE COMMANDS")
    print(sep)
    print("  p  /  positions   Show all open positions (live mark-to-market)")
    print("  sig / signals     Show gap signals from the last scan cycle")
    print("  pairs [N]         Near-miss cross-platform pairs ranked by overlap")
    print("  history / hist    Show all trades resolved this session with P&L")
    print("  paper [N]         Ghost-trade daily summary (default today; N=days back)")
    print("  s  /  scan        Run a scan cycle right now")
    print("  bank <amount>     Set virtual bankroll for this session (dry-run only)")
    print("  clear             Wipe all tracked positions (no exit orders placed)")
    print("  ghost-clear       Remove ghost positions and delete ghost_positions.json")
    print("  h  /  help        Show this help")
    print("  Ctrl-C            Stop the bot")
    print(sep)
    print()


def _start_command_listener(
    coordinator: BotCoordinator,
    scan_event: threading.Event,
    cfg: AppConfig,
) -> None:
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
                elif cmd in ("sig", "signals"):
                    _print_signals(coordinator)
                elif cmd.startswith("pairs"):
                    parts = cmd.split()
                    top_n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                    _print_near_miss_pairs(coordinator, top_n=top_n)
                elif cmd in ("s", "scan"):
                    print("  Triggering scan now...")
                    scan_event.set()
                elif cmd == "clear":
                    n = coordinator.clear_positions()
                    print(f"  Cleared {n} position(s) from state.")
                elif cmd == "ghost-clear":
                    n = coordinator.ghost_clear_positions()
                    print(f"  Cleared {n} ghost position(s) and deleted ghost_positions.json.")
                elif cmd.startswith("bank"):
                    parts = cmd.split()
                    if not cfg.bot.dry_run:
                        print("  'bank' command is only available in dry-run mode.")
                    elif len(parts) < 2:
                        cur = coordinator.get_bankroll()
                        print(f"  Current bankroll: ${cur:,.2f}  (usage: bank <amount>)")
                    else:
                        try:
                            amount = float(parts[1].replace(",", ""))
                            coordinator.set_virtual_bankroll(amount)
                            print(f"  Virtual bankroll set to ${amount:,.2f}  "
                                  f"(session only, not saved to .env)")
                        except ValueError:
                            print(f"  Usage: bank <amount>   e.g.  bank 500")
                        except RuntimeError as e:
                            print(f"  Error: {e}")
                elif cmd in ("history", "hist"):
                    _print_history(coordinator)
                elif cmd.startswith("paper"):
                    parts = cmd.split()
                    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                    _print_paper(coordinator, days=days)
                elif cmd in ("h", "help", "?"):
                    _print_help()
                elif cmd:
                    print(f"  Unknown command '{cmd}'. Type 'help' for commands.")
            except EOFError:
                break
            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  Command error: {exc}")

    t = threading.Thread(target=_listen, daemon=True, name="cmd-listener")
    t.start()
    print("  Type 'p' positions · 's' scan now · 'pairs' near-miss · 'history' resolved trades · 'paper [N]' ghost-trade log · 'bank <amount>' virtual bankroll · 'ghost-clear' reset ghost positions · 'help'\n")


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
        help="Show INFO-level log lines on the console (now the default; kept for compatibility)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity for the console (default: INFO). "
             "Use WARNING to suppress routine status messages.",
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

    # ── Signal testing / isolation ─────────────────────────────────────────────
    _sig_group = parser.add_argument_group("signal testing")
    _sig_group.add_argument(
        "--test-signal",
        dest="test_signals",
        metavar="SIGNAL",
        action="append",
        default=[],
        help=(
            "Run only this signal source; all others are suppressed. "
            "Can be specified multiple times. "
            "Valid: financial, fred, sports_shock, sports_staleness, "
            "sports_panic, sports_resolution, cross_platform"
        ),
    )
    _sig_group.add_argument(
        "--suppress-signal",
        dest="suppress_signals",
        metavar="SIGNAL",
        action="append",
        default=[],
        help=(
            "Suppress this signal source even if it would normally fire. "
            "Can be specified multiple times. "
            "Ignored when --test-signal is also specified."
        ),
    )
    _sig_group.add_argument(
        "--min-confidence",
        dest="min_confidence",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Override the minimum confidence gate (0.0–1.0) for test mode.",
    )
    _sig_group.add_argument(
        "--min-gap",
        dest="min_gap",
        type=float,
        default=None,
        metavar="FLOAT",
        help="Override the minimum effective-gap threshold for test mode.",
    )
    _sig_group.add_argument(
        "--compare",
        dest="compare",
        nargs=2,
        metavar=("SIGNAL_A", "SIGNAL_B"),
        default=None,
        help=(
            "Run two signal configs side-by-side and log differences. "
            "Example: --compare financial fred"
        ),
    )
    _sig_group.add_argument(
        "--replay",
        dest="replay",
        metavar="LOG_FILE",
        default=None,
        help=(
            "Re-run signal evaluation by parsing a saved verbose log file "
            "(no live API calls). "
            "Example: --replay logs/bot_log.1"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    # --info is a shortcut for --log-level INFO; explicit --log-level takes priority
    # if both are set (e.g. --info --log-level DEBUG the user gets DEBUG).
    log_level = args.log_level
    if args.info:
        log_level = "INFO"
    setup_logging(level=log_level, log_file=args.log_file)
    logger = logging.getLogger(__name__)

    # ── Replay mode — no live API calls needed ─────────────────────────────────
    if args.replay:
        _run_replay_mode(args.replay)
        return

    try:
        cfg = AppConfig.load()
    except EnvironmentError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    # ── Compare mode ───────────────────────────────────────────────────────────
    if args.compare:
        _run_compare_mode(args, cfg, [args.compare[0]], [args.compare[1]])
        return

    # ── Build signal test config from CLI args ─────────────────────────────────
    signal_test = SignalTestSettings.disabled()
    if args.test_signals or args.suppress_signals:
        from config.signal_testing import VALID_SIGNALS  # noqa: PLC0415
        for name in (args.test_signals or []) + (args.suppress_signals or []):
            if name not in VALID_SIGNALS:
                valid = ", ".join(sorted(VALID_SIGNALS))
                logger.error("Unknown signal '%s'. Valid signals: %s", name, valid)
                sys.exit(1)
        signal_test = SignalTestSettings.from_cli_args(
            active_signals=args.test_signals or None,
            suppress_signals=args.suppress_signals or None,
            min_confidence=args.min_confidence,
            min_gap=args.min_gap,
        )
        cfg = cfg.with_signal_test(signal_test)

        # Register active signals with SignalStats for per-cycle reporting
        from resolution.signal_stats import SignalStats  # noqa: PLC0415
        tracked = list(signal_test.active_signals or signal_test.suppress_signals)
        if tracked:
            SignalStats.get().set_active_signals(tracked)

    # ── Startup banners ────────────────────────────────────────────────────────
    if signal_test.enabled:
        _print_test_mode_banner(signal_test)

    if cfg.bot.dry_run:
        logger.warning(
            "GHOST TRADE mode – simulated trades will be tracked this session "
            "but NO real orders will be placed and positions reset on restart. "
            "Set LIVE_TRADING=true in .env to trade live."
        )

    coordinator = BotCoordinator(config=cfg)

    # In dry-run mode always show trade details so it's obvious what would have
    # fired.  --names also forces it in live mode (or overrides are additive).
    show_names = args.names or cfg.bot.dry_run

    if args.once:
        logger.info("Running single scan cycle (--once mode)")
        result = coordinator.run_once(skip_stabilization=True)
        _print_summary(result, cfg, show_names=show_names)
        if signal_test.enabled:
            from resolution.signal_stats import SignalStats  # noqa: PLC0415
            SignalStats.get().end_cycle(print_report=True)
    else:
        interval = cfg.bot.resolution_scan_interval_seconds
        logger.info("Starting continuous scan (interval=%ds)", interval)
        scan_event = threading.Event()
        _start_command_listener(coordinator, scan_event, cfg)
        _inhibit_sleep()
        try:
            cycle_count = 0
            while True:
                try:
                    # Skip the startup stabilization guard in continuous mode.
                    # The guard was designed for run_forever() where cycles fire
                    # every 15s — in that context "wait 60s" prevented trading
                    # on data that hadn't been fetched yet.  Here the scheduler
                    # sleeps `interval` seconds (≥ 60s by default) between
                    # cycles, so the gap is already at least one full interval.
                    # More importantly, the discovery scan runs *inside* the
                    # first call, so fresh prices are fetched before any gap
                    # evaluation; there is nothing stale to guard against.
                    result = coordinator.run_once(skip_stabilization=True)
                    _print_summary(result, cfg, show_names=show_names)
                    cycle_count += 1
                    if signal_test.enabled:
                        from resolution.signal_stats import SignalStats  # noqa: PLC0415
                        SignalStats.get().end_cycle(print_report=True)
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
