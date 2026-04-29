"""
Phase 1B weather settlement validation.

Fetches settled Kalshi weather temperature markets, pulls the corresponding NWS CLI
report, compares predicted resolution against Kalshi's actual result.

Required pass rate before Phase 1C: 100% (or fully-explained discrepancies only).

Usage:
    python -m scripts.phase1b_weather_validation

Cache files (not committed):
    data/runtime/cli_validation_cache.json       — NWS CLI reports keyed by station:date
    data/runtime/kalshi_settled_cache.json       — settled Kalshi market results keyed by ticker

Output:
    data/runtime/phase1b_weather_validation.csv
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config import AppConfig
from data.ground_truth.weather_cli import fetch_cli_for_date
from data.ground_truth.weather_kalshi import WeatherMarket, _CITY_TO_CLI, parse_weather_ticker
from data.markets.kalshi import KalshiClient

KALSHI_CACHE = ROOT / "data" / "runtime" / "kalshi_settled_cache.json"
CLI_CACHE = ROOT / "data" / "runtime" / "cli_validation_cache.json"
CSV_OUT = ROOT / "data" / "runtime" / "phase1b_weather_validation.csv"

MARKETS_PER_SERIES = 5   # most recent settled markets to sample per city×type


# ── Cache helpers ─────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


# ── Kalshi settled market fetch ───────────────────────────────────────────────

_SETTLED_STATUS: Optional[str] = None   # resolved by probe_settled_status()

_CANDIDATE_STATUSES = ["finalized", "settled", "closed", "resolved"]


def probe_settled_status(client: KalshiClient) -> Optional[str]:
    """Return the first status value the API accepts for settled markets, or None.

    Kalshi's /markets endpoint has a whitelist of valid status values. We don't
    know which string it uses for resolved markets, so we probe the candidates.
    """
    for status in _CANDIDATE_STATUSES:
        try:
            data = client._get("/markets", params={
                "status": status,
                "series_ticker": "KXHIGHTPHX",
                "limit": 1,
            })
            if isinstance(data, dict):
                print(f"  OK — status={status!r} is accepted by the API")
                return status
        except Exception as exc:
            print(f"  [probe] status={status!r}: rejected ({exc})")
    return None


def fetch_settled_series(
    client: KalshiClient,
    city: str,
    market_type: str,
    n: int,
) -> list[dict]:
    """Return up to n raw market dicts for settled high/low-temp markets for city."""
    if _SETTLED_STATUS is None:
        return []
    prefix = "KXHIGHT" if market_type == "high" else "KXLOWT"
    series_ticker = f"{prefix}{city}"
    try:
        data = client._get("/markets", params={
            "status": _SETTLED_STATUS,
            "series_ticker": series_ticker,
            "limit": n,
        })
        return data.get("markets", [])
    except Exception as exc:
        print(f"  [warn] {series_ticker}: fetch failed ({exc})")
        return []


# ── Predict resolution from CLI temp ─────────────────────────────────────────

def predict_result(wm: WeatherMarket, observed_temp: int) -> str:
    """Return 'yes' or 'no' based on our GT logic."""
    if wm.threshold_type == "above":
        return "yes" if observed_temp > wm.threshold_value else "no"
    if wm.threshold_type == "below":
        return "yes" if observed_temp < wm.threshold_value else "no"
    # bracket: [bracket_low, bracket_high] inclusive
    assert wm.bracket_low is not None and wm.bracket_high is not None
    return "yes" if wm.bracket_low <= observed_temp <= wm.bracket_high else "no"


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    cfg = AppConfig.load()
    if not cfg.kalshi.enabled:
        print("ERROR: Kalshi not enabled in config — cannot fetch settled markets")
        sys.exit(1)

    client = KalshiClient(
        api_key=cfg.kalshi.api_key,
        api_secret=cfg.kalshi.api_secret,
        base_url=cfg.kalshi.base_url,
    )

    # ── Probe API capability ──────────────────────────────────────────────────
    print(f"Probing Kalshi API for settled-market status support "
          f"(candidates: {_CANDIDATE_STATUSES})...")
    global _SETTLED_STATUS
    _SETTLED_STATUS = probe_settled_status(client)
    if _SETTLED_STATUS is None:
        print(
            "\nSTOP: Kalshi /markets rejects all candidate status values for settled markets.\n"
            "Tried: " + ", ".join(_CANDIDATE_STATUSES) + "\n"
            "The existing get_markets() only returns open markets and cannot be used\n"
            "to fetch settled weather markets. Cannot proceed with validation until\n"
            "an alternative endpoint or data source is identified.\n"
            "\nNext steps to investigate:\n"
            "  1. Check Kalshi API docs for valid status values\n"
            "  2. Try GET /series/{ticker}/markets if that endpoint exists\n"
            "  3. Collect open weather markets today and wait for settlement"
        )
        sys.exit(1)
    print()

    kalshi_cache = load_json(KALSHI_CACHE)
    cli_cache = load_json(CLI_CACHE)

    # ── Collect settled markets ───────────────────────────────────────────────
    print(f"Fetching up to {MARKETS_PER_SERIES} settled markets per city×type "
          f"({len(_CITY_TO_CLI)} cities × 2 types)...")

    raw_settled: list[dict] = []  # {ticker, result, raw_item}
    n_from_cache = 0

    for city in sorted(_CITY_TO_CLI):
        for market_type in ("high", "low"):
            prefix = "KXHIGHT" if market_type == "high" else "KXLOWT"
            series_key = f"{prefix}{city}"

            # Check if we have cached results for this series.
            # Require "question" to be present — old cache entries without it
            # would cause T-prefix parse failures.
            cached_series = [
                v for k, v in kalshi_cache.items()
                if k.startswith(series_key)
                and v.get("result") in ("yes", "no")
                and v.get("question") is not None
            ]

            if len(cached_series) >= MARKETS_PER_SERIES:
                n_from_cache += len(cached_series)
                for entry in cached_series:
                    raw_settled.append(entry)
                continue

            items = fetch_settled_series(client, city, market_type, MARKETS_PER_SERIES)
            time.sleep(0.15)  # light throttle between series calls

            for item in items:
                ticker = item.get("ticker", "")
                result = item.get("result")
                status = item.get("status")
                if not ticker or result not in ("yes", "no") or status != "finalized":
                    continue
                # "title" is the question text on Kalshi market objects.
                question = item.get("title") or item.get("question") or ""
                entry = {"ticker": ticker, "result": result, "status": status, "question": question}
                kalshi_cache[ticker] = entry
                raw_settled.append(entry)

    save_json(KALSHI_CACHE, kalshi_cache)
    print(f"  {len(raw_settled)} settled markets collected "
          f"({n_from_cache} from cache)\n")

    if not raw_settled:
        print(
            "STOP: No settled weather markets found.\n"
            "Possible reasons:\n"
            "  • Kalshi has no finalized KXHIGHT*/KXLOWT* markets yet for this account\n"
            "  • The series names differ from the expected KXHIGHT{CITY}/KXLOWT{CITY} format\n"
            "  • status=finalized is accepted but returns an empty list (no data yet)\n"
            "Cannot validate — check series names against live Kalshi API."
        )
        sys.exit(1)

    # ── Parse and validate ────────────────────────────────────────────────────
    print("Validating settled markets against NWS CLI reports...")

    rows: list[dict] = []
    n_cli_miss = 0
    n_parse_fail = 0

    for entry in raw_settled:
        ticker = entry["ticker"]
        kalshi_result = entry["result"]
        question = entry.get("question") or None

        wm = parse_weather_ticker(ticker, question=question)
        if wm is None:
            print(f"  [warn] Could not parse ticker: {ticker}")
            n_parse_fail += 1
            continue

        # Check CLI cache
        cli_key = f"{wm.cli_station}:{wm.target_date.isoformat()}"
        cli_entry = cli_cache.get(cli_key)

        if cli_entry is None:
            report = fetch_cli_for_date(wm.cli_station, wm.target_date)
            if report is None:
                cli_entry = {"max_temp_f": None, "min_temp_f": None, "missing": True}
            else:
                cli_entry = {
                    "max_temp_f": report.max_temp_f,
                    "min_temp_f": report.min_temp_f,
                    "is_preliminary": report.is_preliminary,
                }
            cli_cache[cli_key] = cli_entry
            time.sleep(0.1)

        if cli_entry.get("missing"):
            n_cli_miss += 1
            rows.append({
                "ticker": ticker,
                "city": wm.city,
                "cli_station": wm.cli_station,
                "target_date": wm.target_date.isoformat(),
                "market_type": wm.market_type,
                "threshold_type": wm.threshold_type,
                "threshold_value": wm.threshold_value,
                "observed_temp": "",
                "predicted_result": "N/A",
                "actual_result": kalshi_result,
                "match": "",
                "note": "cli_missing",
            })
            continue

        observed_temp = (
            cli_entry["max_temp_f"] if wm.market_type == "high" else cli_entry["min_temp_f"]
        )
        if observed_temp is None:
            n_cli_miss += 1
            rows.append({
                "ticker": ticker,
                "city": wm.city,
                "cli_station": wm.cli_station,
                "target_date": wm.target_date.isoformat(),
                "market_type": wm.market_type,
                "threshold_type": wm.threshold_type,
                "threshold_value": wm.threshold_value,
                "observed_temp": "",
                "predicted_result": "N/A",
                "actual_result": kalshi_result,
                "match": "",
                "note": "temp_field_missing",
            })
            continue

        predicted = predict_result(wm, observed_temp)
        match = predicted == kalshi_result
        rows.append({
            "ticker": ticker,
            "city": wm.city,
            "cli_station": wm.cli_station,
            "target_date": wm.target_date.isoformat(),
            "market_type": wm.market_type,
            "threshold_type": wm.threshold_type,
            "threshold_value": wm.threshold_value,
            "observed_temp": observed_temp,
            "predicted_result": predicted,
            "actual_result": kalshi_result,
            "match": match,
            "note": "preliminary" if cli_entry.get("is_preliminary") else "",
        })

    save_json(CLI_CACHE, cli_cache)

    # ── Reports ───────────────────────────────────────────────────────────────
    scoreable = [r for r in rows if r["match"] != ""]
    n_match = sum(1 for r in scoreable if r["match"] is True)
    n_total = len(scoreable)
    mismatches = [r for r in scoreable if r["match"] is False]

    print(f"\n{'=' * 70}")
    print("REPORT A — OVERALL")
    print(f"{'=' * 70}")
    print(f"  Settled markets collected : {len(raw_settled)}")
    print(f"  Parse failures            : {n_parse_fail}")
    print(f"  CLI missing               : {n_cli_miss}")
    print(f"  Scoreable                 : {n_total}")
    if n_total > 0:
        acc = n_match / n_total
        print(f"\n  Match rate: {acc:.1%}  ({n_match}/{n_total})")
        if acc == 1.0:
            print("  PASS — GT pipeline matches Kalshi resolution 100%")
        else:
            print(f"  FAIL — {len(mismatches)} mismatch(es) found — do NOT proceed to Phase 1C")
    else:
        print("\n  No scoreable rows — check CLI availability and ticker parsing.")

    # Per-city breakdown
    print(f"\n{'=' * 70}")
    print("REPORT B — PER CITY")
    print(f"{'=' * 70}")
    city_groups: dict[str, list[bool]] = {}
    for r in scoreable:
        city_groups.setdefault(r["city"], []).append(r["match"] is True)
    for city in sorted(city_groups):
        outcomes = city_groups[city]
        n_c = len(outcomes)
        n_ok = sum(outcomes)
        flag = "" if n_ok == n_c else " ** MISMATCH"
        print(f"  {city:<6} {n_ok}/{n_c}{flag}")

    # Per market-type breakdown
    print(f"\n{'=' * 70}")
    print("REPORT C — PER MARKET TYPE")
    print(f"{'=' * 70}")
    type_groups: dict[str, list[bool]] = {}
    for r in scoreable:
        key = f"{r['market_type']}/{r['threshold_type']}"
        type_groups.setdefault(key, []).append(r["match"] is True)
    for key in sorted(type_groups):
        outcomes = type_groups[key]
        n_c = len(outcomes)
        n_ok = sum(outcomes)
        flag = "" if n_ok == n_c else " ** MISMATCH"
        print(f"  {key:<20} {n_ok}/{n_c}{flag}")

    # Mismatch detail
    if mismatches:
        print(f"\n{'=' * 70}")
        print("MISMATCHES — FULL DETAIL")
        print(f"{'=' * 70}")
        for r in mismatches:
            print(
                f"  {r['ticker']}\n"
                f"    cli_station={r['cli_station']}  date={r['target_date']}\n"
                f"    type={r['market_type']}/{r['threshold_type']}  "
                f"threshold={r['threshold_value']}  observed={r['observed_temp']}\n"
                f"    predicted={r['predicted_result']}  actual={r['actual_result']}\n"
                f"    note={r['note'] or 'none'}"
            )

    # Save CSV
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "ticker", "city", "cli_station", "target_date", "market_type",
        "threshold_type", "threshold_value", "observed_temp",
        "predicted_result", "actual_result", "match", "note",
    ]
    with open(CSV_OUT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV saved: {CSV_OUT}  ({len(rows)} rows)")


if __name__ == "__main__":
    main()
