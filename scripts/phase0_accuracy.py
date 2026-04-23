"""
Phase 0b directional accuracy scraper.

Reads ghost_trades.jsonl, fetches Kalshi settlement for each unique entry
ticker via get_market(), computes directional accuracy overall and per
market series / GT source.

Usage:
    python scripts/phase0_accuracy.py

Cache: data/runtime/settlement_cache.json  (skip API call on re-run)
CSV:   data/runtime/phase0_accuracy_results.csv
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig
from data.ground_truth.financial import _extract_series_prefix
from data.markets.kalshi import KalshiClient

GHOST_TRADES = ROOT / "ghost_trades.jsonl"
CACHE_FILE = ROOT / "data" / "runtime" / "settlement_cache.json"
CSV_OUT = ROOT / "data" / "runtime" / "phase0_accuracy_results.csv"

LOW_VOL_THRESHOLD = 10


# ── Wilson score 95% CI ───────────────────────────────────────────────────────

def wilson_ci(n_correct: int, n_total: int) -> tuple[float, float]:
    """Return (lo, hi) Wilson score interval at 95%."""
    if n_total == 0:
        return (0.0, 0.0)
    z = 1.96
    p = n_correct / n_total
    denom = 1 + z * z / n_total
    centre = (p + z * z / (2 * n_total)) / denom
    margin = z * math.sqrt(p * (1 - p) / n_total + z * z / (4 * n_total * n_total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


# ── Load entries ──────────────────────────────────────────────────────────────

def load_entries() -> list[dict]:
    """Load ghost_trades.jsonl, keep event==entry, deduplicate by market_id (first seen)."""
    seen: set[str] = set()
    entries: list[dict] = []
    with open(GHOST_TRADES, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "entry":
                continue
            mid = rec.get("market_id", "")
            if mid in seen:
                continue
            seen.add(mid)
            entries.append(rec)
    return entries


# ── Settlement cache ──────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cache(cache: dict) -> None:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2)


def cache_entry_is_final(entry: dict) -> bool:
    """Return True only if the cached entry has a proper finalized result."""
    return (
        entry.get("status") == "finalized"
        and entry.get("result") in ("yes", "no")
    )


# ── Fetch settlement ──────────────────────────────────────────────────────────

def fetch_settlement(
    client: KalshiClient,
    ticker: str,
    retry_delay: float = 2.0,
) -> Optional[dict]:
    """
    Call get_market() for ticker. Returns dict with result/settlement_ts/
    settlement_value_dollars/status, or None on failure.
    Retries once on exception with retry_delay seconds backoff.
    """
    for attempt in range(2):
        try:
            market = client.get_market(ticker)
            if market is None:
                return None
            raw = market.raw or {}
            return {
                "status": raw.get("status"),
                "result": raw.get("result"),
                "settlement_ts": raw.get("settlement_ts"),
                "settlement_value_dollars": raw.get("settlement_value_dollars"),
            }
        except Exception as exc:
            if attempt == 0:
                print(f"  [warn] {ticker}: fetch error ({exc}), retrying in {retry_delay}s")
                time.sleep(retry_delay)
            else:
                print(f"  [fail] {ticker}: gave up after retry ({exc})")
                return None
    return None  # unreachable


# ── Series classification ─────────────────────────────────────────────────────

def classify_series(market_id: str) -> str:
    prefix = _extract_series_prefix(market_id)
    # _extract_series_prefix already handles both financial brackets and
    # date-segment tickers. For sports/others the regex may return the
    # whole ID if there's no date segment — take first dash-segment as fallback.
    if prefix == market_id and "-" in market_id:
        prefix = market_id.split("-")[0]
    return prefix


# ── Report helpers ────────────────────────────────────────────────────────────

def _acc_row(label: str, n_correct: int, n_total: int) -> str:
    if n_total == 0:
        return f"  {label:<40} N=0    acc=N/A"
    acc = n_correct / n_total
    lo, hi = wilson_ci(n_correct, n_total)
    return (
        f"  {label:<40} N={n_total:<5d} acc={acc:.1%}  95%CI [{lo:.1%}, {hi:.1%}]"
    )


def _bucket_report(label: str, groups: dict[str, list[bool]]) -> None:
    print(f"\n{label}")
    print("-" * 70)
    low_vol: list[bool] = []
    rows = []
    for key, outcomes in groups.items():
        if len(outcomes) >= LOW_VOL_THRESHOLD:
            n_correct = sum(outcomes)
            rows.append((len(outcomes), key, n_correct))
        else:
            low_vol.extend(outcomes)
    # sort by N descending
    rows.sort(key=lambda r: r[0], reverse=True)
    for n_total, key, n_correct in rows:
        print(_acc_row(key, n_correct, n_total))
    if low_vol:
        print(_acc_row(
            f"LOW_VOLUME (<{LOW_VOL_THRESHOLD} trades, {len(groups) - len(rows)} series)",
            sum(low_vol),
            len(low_vol),
        ))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading entries from ghost_trades.jsonl...")
    entries = load_entries()
    print(f"  {len(entries)} unique entry tickers found")

    cfg = AppConfig.load()
    if not cfg.kalshi.enabled:
        print("ERROR: Kalshi not enabled in config")
        sys.exit(1)

    client = KalshiClient(
        api_key=cfg.kalshi.api_key,
        api_secret=cfg.kalshi.api_secret,
        base_url=cfg.kalshi.base_url,
    )

    cache = load_cache()
    print(f"  {sum(1 for v in cache.values() if cache_entry_is_final(v))} tickers already cached as finalized")

    # ── Fetch settlements ─────────────────────────────────────────────────────
    tickers = [e["market_id"] for e in entries]
    to_fetch = [t for t in tickers if t not in cache or not cache_entry_is_final(cache[t])]
    print(f"\nFetching {len(to_fetch)} tickers (skipping {len(tickers) - len(to_fetch)} cached)...")

    for i, ticker in enumerate(to_fetch, 1):
        result = fetch_settlement(client, ticker)
        if result is not None:
            cache[ticker] = result
        else:
            cache[ticker] = {"status": "fetch_failed", "result": None,
                             "settlement_ts": None, "settlement_value_dollars": None}
        if i % 50 == 0:
            print(f"  Fetched {i}/{len(to_fetch)} tickers")
            save_cache(cache)  # periodic save

    save_cache(cache)
    print(f"  Done. Cache saved to {CACHE_FILE}")

    # ── Classify outcomes ─────────────────────────────────────────────────────
    finalized: list[dict] = []
    n_still_open = 0
    n_fetch_failed = 0

    for entry in entries:
        mid = entry["market_id"]
        cached = cache.get(mid, {})
        status = cached.get("status")
        result = cached.get("result")

        if status == "fetch_failed":
            n_fetch_failed += 1
            continue
        if status != "finalized" or result not in ("yes", "no"):
            n_still_open += 1
            continue

        action = entry.get("action", "")
        correct = (
            (action == "buy_yes" and result == "yes") or
            (action == "buy_no" and result == "no")
        )
        finalized.append({
            "market_id": mid,
            "series": classify_series(mid),
            "source": entry.get("source", "unknown"),
            "action": action,
            "gt_prob": entry.get("gt_prob", ""),
            "entry_price": entry.get("entry_price", ""),
            "confidence": entry.get("confidence", ""),
            "entry_ts": entry.get("ts", ""),
            "result": result,
            "settlement_ts": cached.get("settlement_ts", ""),
            "correct": correct,
        })

    # ── Report A: Overall ─────────────────────────────────────────────────────
    n_total = len(entries)
    n_fin = len(finalized)
    n_correct_total = sum(1 for r in finalized if r["correct"])

    print("\n" + "=" * 70)
    print("REPORT A — OVERALL")
    print("=" * 70)
    print(f"  Total unique entry tickers : {n_total}")
    print(f"  Finalized (settled)        : {n_fin}")
    print(f"  Still open / not finalized : {n_still_open}")
    print(f"  Fetch failures             : {n_fetch_failed}")
    if n_fin > 0:
        acc = n_correct_total / n_fin
        lo, hi = wilson_ci(n_correct_total, n_fin)
        print(f"\n  Directional accuracy : {acc:.1%}  ({n_correct_total}/{n_fin})")
        print(f"  95% Wilson CI        : [{lo:.1%}, {hi:.1%}]")
    else:
        print("\n  No finalized trades to score.")

    # ── Report B: Per series ──────────────────────────────────────────────────
    series_groups: dict[str, list[bool]] = {}
    for r in finalized:
        series_groups.setdefault(r["series"], []).append(r["correct"])

    print("\n" + "=" * 70)
    print("REPORT B — PER SERIES")
    print("=" * 70)
    _bucket_report("Series breakdown", series_groups)

    # ── Report C: Per source ──────────────────────────────────────────────────
    source_groups: dict[str, list[bool]] = {}
    for r in finalized:
        source_groups.setdefault(r["source"], []).append(r["correct"])

    print("\n" + "=" * 70)
    print("REPORT C — PER GT SOURCE")
    print("=" * 70)
    _bucket_report("Source breakdown", source_groups)

    # ── Save CSV ──────────────────────────────────────────────────────────────
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "market_id", "series", "source", "action", "gt_prob",
        "entry_price", "confidence", "entry_ts", "result", "settlement_ts", "correct",
    ]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(finalized)
    print(f"\nCSV saved: {CSV_OUT}  ({len(finalized)} rows)")

    # ── Fetch failures summary ────────────────────────────────────────────────
    failed_tickers = [
        mid for mid in tickers
        if cache.get(mid, {}).get("status") == "fetch_failed"
    ]
    if failed_tickers:
        print(f"\nFetch failures ({len(failed_tickers)}):")
        for t in failed_tickers:
            print(f"  {t}")


if __name__ == "__main__":
    main()
