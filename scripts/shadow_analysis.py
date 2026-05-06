"""Shadow signal analysis: join SHADOW_SIGNAL log entries against Kalshi
settlement data and report per-bucket win rate and realized edge.

Buckets are by minutes_to_close at signal time:
    0-30 | 30-60 | 60-90 | 90-120 | 120-180 | 180-240+

Run as:
    python -m scripts.shadow_analysis logs/bot.log [logs/bot.log.1 ...]
"""
from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig
from data.markets.kalshi import KalshiClient

# ── Format:
# ResolutionBot: SHADOW_SIGNAL <ticker> action=<a> target_price=<tp>
#   edge=<e> gt_prob=<g> bracket=<br> asos_temp_f=<t> market_mid=<mm>
#   minutes_to_close=<m>
_SIGNAL_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    r".*?SHADOW_SIGNAL (\S+) "
    r"action=(\S+) "
    r"target_price=([\d.]+) "
    r"edge=([\d.]+) "
    r"gt_prob=([\d.]+) "
    r"bracket=(\S+) "
    r"asos_temp_f=(\S+) "
    r"market_mid=([\d.]+) "
    r"minutes_to_close=(\d+)"
)

BUCKETS: List[Tuple[int, int]] = [
    (0, 30), (30, 60), (60, 90), (90, 120), (120, 180), (180, 9999),
]
BUCKET_LABELS = ["0-30", "30-60", "60-90", "90-120", "120-180", "180-240+"]


@dataclass
class SignalEntry:
    timestamp: str
    ticker: str
    action: str
    target_price: float
    edge: float
    gt_prob: float
    bracket: str
    asos_temp_f: Optional[float]
    market_mid: float
    minutes_to_close: int
    realized_outcome: Optional[str] = None  # "yes" or "no" once settled
    realized_pnl: Optional[float] = None


# ── Log parsing ───────────────────────────────────────────────────────────────

def parse_logs(paths: List[str]) -> List[SignalEntry]:
    seen: set = set()
    entries: List[SignalEntry] = []
    warnings = 0
    for path in paths:
        p = Path(path)
        if not p.exists():
            print(f"WARNING: log file not found: {path}")
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "SHADOW_SIGNAL" not in line:
                continue
            m = _SIGNAL_RE.search(line)
            if not m:
                print(f"WARNING: unparseable SHADOW_SIGNAL line (old format or corrupt): {line[:140]}")
                warnings += 1
                continue
            ts, ticker, action, tp, edge, gt, bracket, asos_s, mid_s, mtc = m.groups()
            key = (ts, ticker)
            if key in seen:
                continue
            seen.add(key)
            asos_f: Optional[float] = None
            try:
                asos_f = float(asos_s)
            except ValueError:
                pass
            try:
                entries.append(SignalEntry(
                    timestamp=ts,
                    ticker=ticker,
                    action=action,
                    target_price=float(tp),
                    edge=float(edge),
                    gt_prob=float(gt),
                    bracket=bracket,
                    asos_temp_f=asos_f,
                    market_mid=float(mid_s),
                    minutes_to_close=int(mtc),
                ))
            except Exception as exc:
                print(f"WARNING: field parse error ({exc}): {line[:140]}")
                warnings += 1
    if warnings:
        print(f"  {warnings} line(s) skipped due to parse warnings.")
    return entries


# ── Kalshi resolution ─────────────────────────────────────────────────────────

_SETTLED_STATUSES = {"finalized", "settled", "closed", "resolved"}


def fetch_resolution(ticker: str, client: KalshiClient) -> Optional[str]:
    """Return 'yes', 'no', or None if the market is not yet settled."""
    market = client.get_market(ticker)
    if market is None:
        return None
    raw = getattr(market, "raw", None) or {}
    status = (raw.get("status") or "").lower()
    if status not in _SETTLED_STATUSES:
        return None
    result = (raw.get("result") or "").lower()
    return result if result in ("yes", "no") else None


# ── Analysis helpers ──────────────────────────────────────────────────────────

def _assign_bucket(minutes_to_close: int) -> int:
    for i, (lo, hi) in enumerate(BUCKETS):
        if lo <= minutes_to_close < hi:
            return i
    return len(BUCKETS) - 1


def _compute_pnl(action: str, target_price: float, outcome: str) -> float:
    won = (action == "buy_yes" and outcome == "yes") or (action == "buy_no" and outcome == "no")
    return (1.0 - target_price) if won else (-target_price)


# ── Main analysis ─────────────────────────────────────────────────────────────

def run_analysis(entries: List[SignalEntry], client: KalshiClient) -> None:
    unique_tickers = {e.ticker for e in entries}
    print(f"\nQuerying Kalshi for {len(unique_tickers)} unique ticker(s)...")
    resolution: Dict[str, Optional[str]] = {}
    for ticker in sorted(unique_tickers):
        resolution[ticker] = fetch_resolution(ticker, client)

    for e in entries:
        outcome = resolution[e.ticker]
        if outcome is not None:
            e.realized_outcome = outcome
            e.realized_pnl = _compute_pnl(e.action, e.target_price, outcome)

    settled = [e for e in entries if e.realized_outcome is not None]
    skipped = len(entries) - len(settled)
    if skipped:
        print(f"{skipped} signal(s) on unsettled markets, excluded from analysis.")

    if not settled:
        print("No settled signals to analyze.")
        return

    # Per-bucket summary
    buckets: List[List[SignalEntry]] = [[] for _ in BUCKETS]
    for e in settled:
        buckets[_assign_bucket(e.minutes_to_close)].append(e)

    print(f"\n{'bucket':<12} {'n':>4} {'win_rate':>10} {'mean_realized_edge':>19}  note")
    print("-" * 64)
    for label, bucket in zip(BUCKET_LABELS, buckets):
        n = len(bucket)
        if n == 0:
            print(f"{label:<12} {'0':>4} {'—':>10} {'—':>19}")
            continue
        wins = sum(1 for e in bucket if (e.realized_pnl or 0) > 0)
        win_rate = wins / n
        mean_edge = sum(e.realized_pnl for e in bucket if e.realized_pnl is not None) / n
        note = "⚠ n<10" if n < 10 else ""
        print(f"{label:<12} {n:>4} {win_rate:>9.1%} {mean_edge:>18.2%}  {note}")

    # CSV output
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = ROOT / "data" / "runtime" / f"shadow_analysis_{ts_str}.csv"
    fieldnames = [
        "timestamp", "ticker", "action", "target_price", "edge",
        "gt_prob", "bracket", "asos_temp_f", "market_mid",
        "minutes_to_close", "bucket", "realized_outcome", "realized_pnl",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in settled:
            w.writerow({
                "timestamp": e.timestamp,
                "ticker": e.ticker,
                "action": e.action,
                "target_price": e.target_price,
                "edge": e.edge,
                "gt_prob": e.gt_prob,
                "bracket": e.bracket,
                "asos_temp_f": e.asos_temp_f,
                "market_mid": e.market_mid,
                "minutes_to_close": e.minutes_to_close,
                "bucket": BUCKET_LABELS[_assign_bucket(e.minutes_to_close)],
                "realized_outcome": e.realized_outcome,
                "realized_pnl": e.realized_pnl,
            })
    print(f"\nCSV written: {out_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python -m scripts.shadow_analysis <log_path> [...]")
        sys.exit(0)

    log_paths = sys.argv[1:]
    print(f"Parsing {len(log_paths)} log file(s)...")
    entries = parse_logs(log_paths)
    print(f"Found {len(entries)} unique SHADOW_SIGNAL entries.")

    if not entries:
        print("Nothing to analyze.")
        sys.exit(0)

    cfg = AppConfig.load()
    if not cfg.kalshi.enabled:
        print("ERROR: Kalshi not enabled in config — cannot fetch settlement data.")
        sys.exit(1)
    client = KalshiClient(
        api_key=cfg.kalshi.api_key,
        api_secret=cfg.kalshi.api_secret,
        base_url=cfg.kalshi.base_url,
    )
    run_analysis(entries, client)


if __name__ == "__main__":
    main()
