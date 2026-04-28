"""
generate_report.py — Bot diagnostic report generator.

Usage:
    python diagnostics/generate_report.py START END
    python diagnostics/generate_report.py auto END

START / END formats: YYYY-MM-DD  or  YYYY-MM-DD_HH-MM
If START is "auto", resolves to the most recent git commit timestamp.
"""

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from glob import glob
from pathlib import Path
from statistics import median

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
ET = timezone(timedelta(hours=-4))  # EDT (UTC-4); adjust to -5 for EST if needed
SCRIPT_ERRORS = []   # accumulated non-fatal parse errors


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_arg_ts(raw: str) -> datetime:
    """Parse YYYY-MM-DD or YYYY-MM-DD_HH-MM into an ET-aware datetime."""
    raw = raw.strip()
    for fmt in ("%Y-%m-%d_%H-%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=ET)
        except ValueError:
            pass
    raise ValueError(f"Cannot parse timestamp argument: {raw!r}")


def _resolve_auto_start() -> datetime:
    """Return the most recent git commit timestamp as an ET datetime."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ai"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        line = result.stdout.strip()  # e.g. "2026-03-28 15:49:27 -0700"
        if not line:
            raise RuntimeError("git log returned empty output")
        dt = datetime.strptime(line[:25], "%Y-%m-%d %H:%M:%S %z")
        return dt.astimezone(ET)
    except Exception as exc:
        print(f"ERROR: Could not resolve auto START via git log: {exc}", file=sys.stderr)
        sys.exit(1)


def _ts_to_et(ts_str: str) -> datetime | None:
    """Parse an ISO timestamp string to ET. Returns None on failure."""
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ET)
    except Exception:
        return None


def _log_ts(line: str) -> datetime | None:
    """Extract the leading 'YYYY-MM-DD HH:MM:SS' from a log line as ET (naive logs assumed ET)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
    except ValueError:
        return None


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "N/A"
    return dt.strftime("%Y-%m-%d %H:%M:%S ET")


def _fmt_usd(v) -> str:
    try:
        return f"${v:,.2f}"
    except Exception:
        return str(v)


def _safe_median(lst):
    if not lst:
        return 0
    return median(lst)


# ---------------------------------------------------------------------------
# 1 — Log file discovery + filtering
# ---------------------------------------------------------------------------

def load_log_lines(start: datetime, end: datetime):
    """
    Discover all logs/bot.log* files.  Read rotated files (bot.log.N) in
    descending numeric order first, then bot.log last.  Return:
        file_summaries  list of (filename, first_ts, last_ts, line_count)
        lines_in_range  list of raw log lines within [start, end]
        all_gaps        list of (gap_start, gap_end, minutes) for >10min gaps
    """
    log_dir = REPO_ROOT / "logs"
    if not log_dir.exists():
        return [], [], []

    # Discover and sort: rotated files highest-number first, then bot.log
    rotated = sorted(
        [p for p in log_dir.glob("bot.log.*") if re.search(r"bot\.log\.\d+$", p.name)],
        key=lambda p: int(re.search(r"(\d+)$", p.name).group(1)),
        reverse=True,
    )
    base = log_dir / "bot.log"
    ordered = rotated + ([base] if base.exists() else [])

    file_summaries = []
    lines_in_range = []
    prev_ts = None
    all_gaps = []

    for path in ordered:
        first_ts = last_ts = None
        count_in_range = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.rstrip("\n")
                    ts = _log_ts(line)
                    if ts is None:
                        continue
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                    if ts < start or ts > end:
                        continue
                    # Gap detection across the full ordered sequence
                    if prev_ts is not None:
                        gap_min = (ts - prev_ts).total_seconds() / 60
                        if gap_min > 10:
                            all_gaps.append((prev_ts, ts, gap_min))
                    prev_ts = ts
                    lines_in_range.append(line)
                    count_in_range += 1
        except OSError as exc:
            SCRIPT_ERRORS.append(f"Could not read {path.name}: {exc}")
            continue

        file_summaries.append((path.name, first_ts, last_ts, count_in_range))

    return file_summaries, lines_in_range, all_gaps


# ---------------------------------------------------------------------------
# 2 — Cycle stats
# ---------------------------------------------------------------------------

def parse_cycles(lines):
    """
    Return dict with: total, actionable_list, blocked_list, zero_actionable_count
    from [SIGNAL] Cycle summary lines.
    """
    actionable_list = []
    blocked_list = []
    pattern = re.compile(r"\[SIGNAL\] Cycle summary: (\d+) actionable, (\d+) blocked")
    for line in lines:
        m = pattern.search(line)
        if m:
            actionable_list.append(int(m.group(1)))
            blocked_list.append(int(m.group(2)))
    zero = sum(1 for a in actionable_list if a == 0)
    return {
        "total": len(actionable_list),
        "actionable": actionable_list,
        "blocked": blocked_list,
        "zero_actionable": zero,
    }


# ---------------------------------------------------------------------------
# 3 — Trades
# ---------------------------------------------------------------------------

def load_trades(start: datetime, end: datetime):
    """
    Load ghost_trades.jsonl, filter to [start, end].
    Return list of all records in range.
    """
    path = REPO_ROOT / "data" / "runtime" / "ghost_trades.jsonl"
    if not path.exists():
        return []
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, raw in enumerate(fh, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                r = json.loads(raw)
                ts = _ts_to_et(r.get("ts", ""))
                if ts is None:
                    SCRIPT_ERRORS.append(f"ghost_trades.jsonl line {i}: unparseable ts")
                    continue
                if ts < start or ts > end:
                    continue
                r["_ts_et"] = ts
                records.append(r)
            except json.JSONDecodeError as exc:
                SCRIPT_ERRORS.append(f"ghost_trades.jsonl line {i}: JSON error — {exc}")
    return records


def analyse_trades(records):
    """
    Split into entries/exits, match pairs, compute stats.
    Returns a dict of metrics plus the matched trade rows.
    """
    entries = [r for r in records if r.get("event") == "entry"]
    exits   = [r for r in records if r.get("event") == "exit"]

    # Build entry queue per market_id (FIFO)
    entry_queue: dict[str, list] = defaultdict(list)
    for e in sorted(entries, key=lambda x: x["_ts_et"]):
        entry_queue[e["market_id"]].append(e)

    rows = []
    wins = losses = unfilled = 0
    gross_pnl = 0.0

    for x in sorted(exits, key=lambda x: x["_ts_et"]):
        mid = x["market_id"]
        pnl = x.get("pnl", 0.0) or 0.0
        reason = x.get("exit_reason", "unknown")
        gross_pnl += pnl

        matched_entry = None
        if entry_queue[mid]:
            matched_entry = entry_queue[mid].pop(0)

        entry_price = matched_entry["entry_price"] if matched_entry else None
        entry_time  = matched_entry["_ts_et"].strftime("%m-%d %H:%M") if matched_entry else ""
        side        = matched_entry.get("action", "?") if matched_entry else "?"

        if reason == "unfilled_timeout":
            unfilled += 1
        elif pnl > 0:
            wins += 1
        else:
            losses += 1

        rows.append({
            "market_id":    mid,
            "side":         side,
            "entry_price":  entry_price,
            "exit_price":   x.get("exit_price"),
            "pnl":          pnl,
            "exit_reason":  reason,
            "hold_min":     x.get("hold_duration_minutes", 0.0),
            "entry_time":   entry_time,
            "exit_ts":      x["_ts_et"],
        })

    resolved = wins + losses
    win_rate = wins / resolved * 100 if resolved else 0.0

    return {
        "entries":    len(entries),
        "exits":      len(exits),
        "wins":       wins,
        "losses":     losses,
        "unfilled":   unfilled,
        "gross_pnl":  gross_pnl,
        "win_rate":   win_rate,
        "resolved":   resolved,
        "rows":       rows,
    }


def load_bankroll():
    path = REPO_ROOT / "data" / "runtime" / "ghost_state.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        SCRIPT_ERRORS.append(f"ghost_state.json: {exc}")
        return None


# ---------------------------------------------------------------------------
# 4 — Open positions
# ---------------------------------------------------------------------------

def load_positions():
    path = REPO_ROOT / "data" / "runtime" / "ghost_positions.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        SCRIPT_ERRORS.append(f"ghost_positions.json: {exc}")
        return None


# ---------------------------------------------------------------------------
# 5 — Signal funnel
# ---------------------------------------------------------------------------

def parse_signal_funnel(lines):
    actionable = 0
    blocked = 0
    block_reasons: Counter = Counter()
    no_source = no_prob = perm_skip = illiq = deep_otm = extreme_entry_price = stale_ev_recheck = large_div_extreme = gt_stale_at_entry = 0

    blocked_re = re.compile(r"\[SIGNAL\] BLOCKED")
    # Try to extract a reason token after "reason=" or classify from content
    reason_key_re = re.compile(r"reason=(\w+)")

    for line in lines:
        if "[SIGNAL] ACTIONABLE" in line:
            actionable += 1
        elif "[SIGNAL] BLOCKED" in line:
            blocked += 1
            m = reason_key_re.search(line)
            if m:
                block_reasons[m.group(1)] += 1
            elif "insufficient_edge" in line or "min_gap=" in line:
                block_reasons["insufficient_edge"] += 1
            elif "illiquid" in line.lower():
                block_reasons["illiquid_series"] += 1
            elif "deep_otm" in line.lower() or "deep otm" in line.lower():
                block_reasons["deep_otm"] += 1
            elif "no_source" in line.lower():
                block_reasons["no_source"] += 1
            elif "no_prob" in line.lower():
                block_reasons["no_prob"] += 1
            elif "confidence" in line.lower():
                block_reasons["confidence_below_threshold"] += 1
            else:
                block_reasons["other"] += 1

        ll = line.lower()
        if "no_source" in ll or "no gt source" in ll:
            no_source += 1
        if "no_prob" in ll or "no gt prob" in ll:
            no_prob += 1
        if "perm_skip" in ll or "permanent skip" in ll or "permanently skip" in ll:
            perm_skip += 1
        if "illiquid_series" in ll or "illiquid series" in ll:
            illiq += 1
        if "deep_otm" in ll or "deep otm" in ll:
            deep_otm += 1
        if "extreme_entry_price" in ll:
            extreme_entry_price += 1
        if "stale_price_ev_recheck_failed" in ll:
            stale_ev_recheck += 1
        if "large_divergence_extreme_market" in ll:
            large_div_extreme += 1
        if "gt_stale_at_entry" in ll:
            gt_stale_at_entry += 1

    return {
        "actionable":               actionable,
        "blocked":                  blocked,
        "block_reasons":            block_reasons,
        "no_source":                no_source,
        "no_prob":                  no_prob,
        "perm_skip":                perm_skip,
        "illiq":                    illiq,
        "deep_otm":                 deep_otm,
        "extreme_entry_price":      extreme_entry_price,
        "stale_ev_recheck":         stale_ev_recheck,
        "large_div_extreme":        large_div_extreme,
        "gt_stale_at_entry":        gt_stale_at_entry,
    }


# ---------------------------------------------------------------------------
# 6 — GT coverage
# ---------------------------------------------------------------------------

def parse_gt_coverage(lines):
    """Count GT source hits from log lines."""
    source_hits: Counter = Counter()
    no_source = no_prob = 0

    # Patterns to extract source name
    source_re = re.compile(
        r"(Yahoo Finance/[^\s,|]+|FRED|SportsLiveSource[^\s,|]*|SportsDataSource[^\s,|]*|ESPN[^\s,|]*)"
    )

    for line in lines:
        ll = line.lower()
        # Count explicit source routing/hits
        m = source_re.search(line)
        if m and ("routing" in ll or "gt_source" in ll or "ACTIONABLE" in line
                  or "source=" in ll or "gt source" in ll):
            source_hits[m.group(1)] += 1

        if "no_source" in ll or "no gt source" in ll:
            no_source += 1
        if "no_prob" in ll or "no gt prob" in ll:
            no_prob += 1

    return {
        "source_hits": source_hits,
        "no_source":   no_source,
        "no_prob":     no_prob,
    }


# ---------------------------------------------------------------------------
# 7 — Errors / warnings
# ---------------------------------------------------------------------------

def parse_errors(lines):
    """
    Collect WARNING and ERROR lines, normalize dynamic tokens, count per pattern.
    Returns Counter of normalized message -> count, top 20.
    """
    patterns: Counter = Counter()
    level_re = re.compile(r"\| (WARNING|ERROR)\s+\|")
    msg_re = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| \w+\s+\| [\w.]+ \| (.+)")

    for line in lines:
        if not level_re.search(line):
            continue
        m = msg_re.match(line)
        if not m:
            continue
        msg = m.group(1)
        # Normalise dynamic parts
        msg = re.sub(r"0x[0-9a-fA-F]{8,}", "0x<hash>", msg)
        msg = re.sub(r"KX[A-Z0-9]+-\d{2}[A-Z]{3}\d{2,4}[A-Z0-9\-\.]*", "KX<market>", msg)
        msg = re.sub(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}[^\s]*)?", "<date>", msg)
        msg = re.sub(r"\b\d+\.\d+\b", "<N>", msg)
        msg = re.sub(r"\b\d{3,}\b", "<N>", msg)
        msg = msg[:120].strip()
        patterns[msg] += 1

    return patterns.most_common(20)


# ---------------------------------------------------------------------------
# 8 — Anomalies
# ---------------------------------------------------------------------------

def detect_anomalies(trade_records, start, end):
    """
    Inspect parsed trade records for known anomaly signatures.
    Returns list of description strings.
    """
    findings = []

    entries = [r for r in trade_records if r.get("event") == "entry"]
    exits   = [r for r in trade_records if r.get("event") == "exit"]

    # gt_prob at exactly 0.0 or 1.0
    bad_prob = [e for e in entries if e.get("gt_prob") in (0.0, 1.0)]
    if bad_prob:
        counts = Counter(e.get("gt_prob") for e in bad_prob)
        sample = bad_prob[0]["market_id"]
        findings.append(
            f"gt_prob at exactly 0.0 or 1.0 (should be clamped to 0.02/0.98): "
            f"{len(bad_prob)} entries — {dict(counts)} — e.g. {sample}"
        )

    # hard_stop exits with hold < 0.1 min
    instant_stops = [
        x for x in exits
        if x.get("exit_reason") == "hard_stop"
        and (x.get("hold_duration_minutes") or 0.0) < 0.1
    ]
    if instant_stops:
        sample = instant_stops[0]["market_id"]
        findings.append(
            f"hard_stop exits with hold_duration_minutes < 0.1: "
            f"{len(instant_stops)} exits — e.g. {sample}"
        )

    # Same market_id entered >3 times
    entry_counts: Counter = Counter(e["market_id"] for e in entries)
    repeat_markets = {mid: cnt for mid, cnt in entry_counts.items() if cnt > 3}
    if repeat_markets:
        worst = max(repeat_markets, key=repeat_markets.get)
        finding_parts = [f"{mid}:{cnt}" for mid, cnt in
                         sorted(repeat_markets.items(), key=lambda x: -x[1])[:5]]
        findings.append(
            f"Market(s) entered >3 times in range ({len(repeat_markets)} markets): "
            + ", ".join(finding_parts)
        )

    # Inverted P&L bug: action=buy_no, pnl_pct exactly -0.4999 or -0.5000
    inverted = [
        x for x in exits
        if abs((x.get("pnl_pct") or 0.0) - (-0.5)) < 0.001
    ]
    if inverted:
        sample = inverted[0]["market_id"]
        findings.append(
            f"Possible inverted P&L (pnl_pct ≈ -0.50): "
            f"{len(inverted)} exits — e.g. {sample}"
        )

    return findings


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(start: datetime, end: datetime, auto_commit_msg: str | None) -> str:
    lines_out = []
    w = lines_out.append

    start_str = start.strftime("%Y-%m-%d %H:%M:%S ET")
    end_str   = end.strftime("%Y-%m-%d %H:%M:%S ET")

    w(f"# BOT DIAGNOSTIC: {start_str} to {end_str}")
    w("")
    if auto_commit_msg:
        w(f"Auto-resolved START from git: {start_str}")
        w(f"Commit: {auto_commit_msg}")
        w("")

    # ------------------------------------------------------------------ 1
    w("## 1. LOG COVERAGE")
    w("")
    file_summaries, log_lines, all_gaps = load_log_lines(start, end)
    if not file_summaries:
        w("No data found — logs/ directory missing or empty.")
    else:
        w(f"{'Filename':<20}  {'First timestamp':<26}  {'Last timestamp':<26}  {'Lines in range':>14}")
        w("-" * 95)
        for fname, first_ts, last_ts, count in file_summaries:
            w(f"{fname:<20}  {_fmt_dt(first_ts):<26}  {_fmt_dt(last_ts):<26}  {count:>14,}")
        w("")
        w(f"Total lines in range: {len(log_lines):,}")
        w("")
        if all_gaps:
            w(f"Gaps >10 min ({len(all_gaps)} total):")
            for gs, ge, gm in all_gaps:
                w(f"  {_fmt_dt(gs)}  →  {_fmt_dt(ge)}  ({gm:.0f} min)")
        else:
            w("No gaps >10 min detected.")
    w("")

    # ------------------------------------------------------------------ 2
    w("## 2. CYCLES")
    w("")
    cycle_data = parse_cycles(log_lines)
    total_cycles = cycle_data["total"]
    act_list = cycle_data["actionable"]
    if total_cycles == 0:
        w("0 completed cycles found in log range.")
    else:
        def _pct(v): return f"{v:.1f}"
        act_sorted = sorted(act_list)
        w(f"Total cycles: {total_cycles}")
        w(f"Actionable signals per cycle:")
        w(f"  Min:    {min(act_list)}")
        w(f"  Max:    {max(act_list)}")
        w(f"  Avg:    {_pct(sum(act_list)/len(act_list))}")
        w(f"  Median: {_pct(_safe_median(act_list))}")
        w(f"Cycles with 0 actionable: {cycle_data['zero_actionable']} "
          f"({cycle_data['zero_actionable']/total_cycles*100:.1f}%)")
    w("")

    # ------------------------------------------------------------------ 3
    w("## 3. TRADES")
    w("")
    trade_records = load_trades(start, end)
    if not trade_records and not (REPO_ROOT / "data" / "runtime" / "ghost_trades.jsonl").exists():
        w("No data found — ghost_trades.jsonl missing.")
    else:
        stats = analyse_trades(trade_records)
        w(f"Entries:  {stats['entries']}")
        w(f"Exits:    {stats['exits']}")
        w(f"Wins:     {stats['wins']}")
        w(f"Losses:   {stats['losses']}")
        w(f"Unfilled timeouts: {stats['unfilled']}")
        w("")
        w(f"Gross P&L: {_fmt_usd(stats['gross_pnl'])}")
        w(f"Net P&L:   (fees not separately logged — gross = net)")
        w(f"Win rate (excl. unfilled): {stats['win_rate']:.1f}%  over {stats['resolved']} resolved")
        w("")
        bankroll_data = load_bankroll()
        if bankroll_data:
            w(f"Current bankroll: {_fmt_usd(bankroll_data.get('total_usd', 0))}  "
              f"(realized_pnl: {_fmt_usd(bankroll_data.get('realized_pnl_usd', 0))})")
        else:
            w("Current bankroll: No data found")
        w("")
        # Trade table
        col_w = [50, 9, 9, 9, 12, 21, 9, 14]
        headers = ["Market ID", "Side", "Entry $", "Exit $", "P&L", "Exit Reason", "Hold Min", "Entry Time"]
        header_row = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))
        w(header_row)
        w("-" * len(header_row))
        for row in stats["rows"]:
            ep = f"{row['entry_price']:.4f}" if row["entry_price"] is not None else "?"
            xp = f"{row['exit_price']:.4f}" if row["exit_price"] is not None else "?"
            cells = [
                row["market_id"][:col_w[0]],
                row["side"],
                ep,
                xp,
                f"{row['pnl']:.2f}",
                row["exit_reason"],
                f"{row['hold_min']:.1f}",
                row["entry_time"],
            ]
            w("  ".join(str(c).ljust(col_w[i]) for i, c in enumerate(cells)))
    w("")

    # ------------------------------------------------------------------ 4
    w("## 4. OPEN POSITIONS")
    w("")
    pos_data = load_positions()
    if pos_data is None:
        w("No data found — ghost_positions.json missing.")
    else:
        positions = pos_data.get("positions", {})
        if not positions:
            w("No open positions.")
        else:
            now_et = datetime.now(ET)
            w(f"Open positions: {len(positions)}")
            w("")
            w(f"{'Market ID':<50}  {'Side':<8}  {'Entry $':<9}  {'Entry Time':<20}  Age")
            w("-" * 110)
            for pos_id, pos in positions.items():
                mid = pos.get("market_id", pos_id)
                side = pos.get("action", pos.get("side", "?"))
                entry_p = pos.get("entry_price", "?")
                entry_ts_raw = pos.get("entry_time", pos.get("ts", ""))
                entry_ts = _ts_to_et(entry_ts_raw)
                entry_str = entry_ts.strftime("%Y-%m-%d %H:%M ET") if entry_ts else "?"
                if entry_ts:
                    age = now_et - entry_ts
                    age_str = f"{age.total_seconds()/3600:.1f}h"
                else:
                    age_str = "?"
                w(f"{mid:<50}  {side:<8}  {entry_p:<9}  {entry_str:<20}  {age_str}")
    w("")

    # ------------------------------------------------------------------ 5
    w("## 5. SIGNAL FUNNEL")
    w("")
    funnel = parse_signal_funnel(log_lines)
    w(f"[SIGNAL] ACTIONABLE:  {funnel['actionable']}")
    w(f"[SIGNAL] BLOCKED:     {funnel['blocked']}")
    w("")
    w("Block reason breakdown (from BLOCKED lines):")
    if funnel["block_reasons"]:
        for reason, cnt in funnel["block_reasons"].most_common():
            w(f"  {reason:<35} {cnt}")
    else:
        w("  (no reason tags parsed)")
    w("")
    w("Non-SIGNAL log line occurrences:")
    w(f"  no_source:       {funnel['no_source']}")
    w(f"  no_prob:         {funnel['no_prob']}")
    w(f"  perm_skipped:    {funnel['perm_skip']}")
    w(f"  illiquid_series:            {funnel['illiq']}")
    w(f"  deep_otm:                   {funnel['deep_otm']}")
    w(f"  extreme_entry_price:        {funnel['extreme_entry_price']}")
    w(f"  stale_price_ev_recheck:     {funnel['stale_ev_recheck']}")
    w(f"  large_div_extreme_market:   {funnel['large_div_extreme']}")
    w(f"  gt_stale_at_entry:          {funnel['gt_stale_at_entry']}")
    w("")

    # ------------------------------------------------------------------ 6
    w("## 6. GT COVERAGE")
    w("")
    gt = parse_gt_coverage(log_lines)
    if gt["source_hits"]:
        w(f"{'Source':<40}  {'Hits':>8}")
        w("-" * 52)
        for src, cnt in gt["source_hits"].most_common():
            w(f"{src:<40}  {cnt:>8}")
    else:
        w("No GT source routing lines found in log range.")
    w("")
    w(f"no_source log lines: {gt['no_source']}")
    w(f"no_prob   log lines: {gt['no_prob']}")
    w("")

    # ------------------------------------------------------------------ 7
    w("## 7. ERRORS")
    w("")
    error_patterns = parse_errors(log_lines)
    if not error_patterns:
        w("No WARNING or ERROR lines found in range.")
    else:
        w(f"{'Count':>6}  Message pattern (top 20, normalized)")
        w("-" * 100)
        for msg, cnt in error_patterns:
            w(f"{cnt:>6}  {msg}")
    w("")

    # ------------------------------------------------------------------ 8
    w("## 8. ANOMALIES")
    w("")
    anomalies = detect_anomalies(trade_records, start, end)
    if not anomalies:
        w("No anomalies detected.")
    else:
        for i, finding in enumerate(anomalies, 1):
            w(f"ANOMALY {i}: {finding}")
    w("")

    # ------------------------------------------------------------------ Footer
    if SCRIPT_ERRORS:
        w("---")
        w(f"Parse errors during report generation ({len(SCRIPT_ERRORS)}):")
        for err in SCRIPT_ERRORS:
            w(f"  {err}")

    return "\n".join(lines_out)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(f"Usage: python {sys.argv[0]} <START|auto> <END>", file=sys.stderr)
        print("  START/END format: YYYY-MM-DD  or  YYYY-MM-DD_HH-MM", file=sys.stderr)
        sys.exit(1)

    raw_start = sys.argv[1].strip()
    raw_end   = sys.argv[2].strip()

    auto_commit_msg = None
    if raw_start.lower() == "auto":
        start_dt = _resolve_auto_start()
        # Retrieve commit message for the report header
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%s"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=10,
            )
            auto_commit_msg = result.stdout.strip()
        except Exception:
            auto_commit_msg = "(could not retrieve commit message)"
    else:
        start_dt = _parse_arg_ts(raw_start)

    end_dt = _parse_arg_ts(raw_end)

    if start_dt >= end_dt:
        print("ERROR: START must be before END.", file=sys.stderr)
        sys.exit(1)

    report = build_report(start_dt, end_dt, auto_commit_msg)

    # Print to stdout
    print(report)

    # Save to file
    out_dir = REPO_ROOT / "diagnostics"
    out_dir.mkdir(exist_ok=True)

    def _safe_fname(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H-%M-%S")

    out_path = out_dir / f"diagnostic_{_safe_fname(start_dt)}_to_{_safe_fname(end_dt)}.txt"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(f"\n[Report saved to: {out_path}]", file=sys.stderr)


if __name__ == "__main__":
    main()
