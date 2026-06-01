"""Phase SQLite-1: dry-run migration of JSON mutable state into SQLite.

Default (no --apply) writes to a temp file under the OS temp dir, populates it
from data/runtime/ghost_positions.json and data/runtime/ghost_state.json,
verifies a round-trip read, prints a summary, deletes the temp DB, and exits 0.

With --apply: writes to data/runtime/bot_state.db and renames the JSON files
to <name>.json.migrated_YYYYMMDD. Does NOT modify or wire up any other code.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Allow `python scripts/migrate_to_sqlite.py` from repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from data.runtime import sqlite_store  # noqa: E402


_DEFAULT_POSITIONS_JSON = _REPO_ROOT / "data" / "runtime" / "ghost_positions.json"
_DEFAULT_STATE_JSON = _REPO_ROOT / "data" / "runtime" / "ghost_state.json"
_DEFAULT_PROD_DB = _REPO_ROOT / "data" / "runtime" / "bot_state.db"

# Known fields persisted by executor._save_positions() and bot._save_ghost_state().
# Anything else found in JSON is reported as a warning.
_EXPECTED_POSITION_FIELDS = {
    "market_id", "platform", "action", "entry_price", "size_usd",
    "ground_truth_prob", "source_confidence", "entry_time", "order_id",
    "resolution_date_iso", "question", "category", "tags",
    "fill_status", "limit_price_used",
}
_EXPECTED_STATE_FIELDS = {"saved_at", "total_usd", "realized_pnl_usd"}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _compare_round_trip(original: dict, restored: dict) -> list[str]:
    """Return a list of human-readable diffs. Empty list = perfect round-trip.

    `restored` is the dict returned by sqlite_store.get_position(); it omits
    `ticker` (which is the dict key in the source JSON) and reconstructs `tags`.
    """
    diffs: list[str] = []

    for key, val in original.items():
        if key not in restored:
            diffs.append(f"  - missing in DB: {key!r} (json={val!r})")
            continue
        if restored[key] != val:
            diffs.append(
                f"  - field {key!r} differs: json={val!r} db={restored[key]!r}"
            )

    extra = set(restored) - set(original) - {"ticker"}
    for key in sorted(extra):
        diffs.append(f"  - extra in DB: {key!r}={restored[key]!r}")

    return diffs


def _migrate(
    positions_json: Path,
    state_json: Path,
    db_path: Path,
) -> tuple[int, int, list[str]]:
    """Populate the SQLite DB at `db_path` from the two JSON files.

    Returns (positions_migrated, bankroll_rows, round_trip_errors).
    """
    sqlite_store.set_db_path(db_path)
    conn = sqlite_store.get_connection()
    sqlite_store.create_schema(conn)

    warnings: list[str] = []

    # ── Positions ────────────────────────────────────────────────────────
    positions_payload = _load_json(positions_json)
    positions = positions_payload.get("positions", {})
    print(f"  saved_at (positions): {positions_payload.get('saved_at', '<n/a>')}")
    print(f"  positions in JSON: {len(positions)}")

    for ticker, pos in positions.items():
        json_fields = set(pos.keys())
        unknown = json_fields - _EXPECTED_POSITION_FIELDS
        missing = _EXPECTED_POSITION_FIELDS - json_fields
        if unknown:
            warnings.append(
                f"  [position {ticker}] unknown fields (not in schema, dropped): "
                f"{sorted(unknown)}"
            )
        if missing:
            warnings.append(
                f"  [position {ticker}] expected fields missing in JSON: "
                f"{sorted(missing)}"
            )
        record = dict(pos)
        record["ticker"] = ticker
        sqlite_store.upsert_position(record)

    # ── Bankroll ─────────────────────────────────────────────────────────
    state_payload = _load_json(state_json)
    bankroll_rows = 0
    if state_payload:
        json_fields = set(state_payload.keys())
        unknown = json_fields - _EXPECTED_STATE_FIELDS
        missing = _EXPECTED_STATE_FIELDS - json_fields
        if unknown:
            warnings.append(
                f"  [ghost_state] unknown fields (dropped): {sorted(unknown)}"
            )
        if missing:
            warnings.append(
                f"  [ghost_state] expected fields missing: {sorted(missing)}"
            )
        sqlite_store.set_bankroll({
            "total_usd": state_payload["total_usd"],
            "realized_pnl_usd": state_payload.get("realized_pnl_usd", 0.0),
        })
        bankroll_rows = 1
        print(
            f"  bankroll: total_usd=${state_payload['total_usd']:.4f}, "
            f"realized_pnl_usd=${state_payload.get('realized_pnl_usd', 0.0):.4f}"
        )
    else:
        print("  bankroll: <no ghost_state.json found>")

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(w)

    # ── Verification round-trip ──────────────────────────────────────────
    print("\nVerification (per-position round-trip):")
    round_trip_errors: list[str] = []
    for ticker, original in positions.items():
        restored = sqlite_store.get_position(ticker)
        if restored is None:
            print(f"  {ticker}: NOT FOUND IN DB")
            round_trip_errors.append(f"{ticker}: missing from DB after upsert")
            continue
        diffs = _compare_round_trip(original, restored)
        if not diffs:
            print(f"  {ticker}: ROUND-TRIP OK")
        else:
            print(f"  {ticker}: ROUND-TRIP MISMATCH")
            for d in diffs:
                print(d)
            round_trip_errors.extend([f"{ticker}: {d.strip()}" for d in diffs])

    # Bankroll round-trip
    if state_payload:
        restored = sqlite_store.get_bankroll()
        if restored is None:
            round_trip_errors.append("bankroll: not found in DB after set")
            print("  bankroll: NOT FOUND IN DB")
        elif (
            restored["total_usd"] != state_payload["total_usd"]
            or restored["realized_pnl_usd"]
                != state_payload.get("realized_pnl_usd", 0.0)
        ):
            round_trip_errors.append(
                f"bankroll mismatch: json={state_payload} db={restored}"
            )
            print(f"  bankroll: ROUND-TRIP MISMATCH (json={state_payload}, db={restored})")
        else:
            print("  bankroll: ROUND-TRIP OK")

    sqlite_store.close_thread_connection()
    return len(positions), bankroll_rows, round_trip_errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Apply migration to production DB path and rename source JSON files. "
             "Default: dry-run to temp DB.",
    )
    parser.add_argument(
        "--db-path", type=Path, default=None,
        help="Override production DB path (only used with --apply).",
    )
    parser.add_argument(
        "--positions-json", type=Path, default=_DEFAULT_POSITIONS_JSON,
        help="Source ghost_positions.json path.",
    )
    parser.add_argument(
        "--state-json", type=Path, default=_DEFAULT_STATE_JSON,
        help="Source ghost_state.json path.",
    )
    args = parser.parse_args()

    if args.apply:
        target_db = args.db_path or _DEFAULT_PROD_DB
        print(f"=== APPLY mode === target DB: {target_db}")
        if target_db.exists():
            print(
                f"ERROR: target DB {target_db} already exists; refusing to overwrite",
                file=sys.stderr,
            )
            return 2
        target_db.parent.mkdir(parents=True, exist_ok=True)
        positions_n, bankroll_n, errors = _migrate(
            args.positions_json, args.state_json, target_db,
        )
        if errors:
            print(
                f"\nABORTING APPLY: {len(errors)} round-trip error(s)",
                file=sys.stderr,
            )
            target_db.unlink(missing_ok=True)
            return 3
        # Rename source JSON files
        stamp = datetime.now().strftime("%Y%m%d")
        for src in (args.positions_json, args.state_json):
            if src.exists():
                dest = src.with_suffix(src.suffix + f".migrated_{stamp}")
                shutil.move(str(src), str(dest))
                print(f"  renamed {src.name} -> {dest.name}")
        print(
            f"\nAPPLY complete: positions={positions_n}, bankroll_rows={bankroll_n}"
        )
        return 0

    # ── Dry-run ──────────────────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(
        prefix="bot_state_dryrun_", suffix=".db", delete=False
    ) as tf:
        temp_db = Path(tf.name)
    print(f"=== DRY-RUN === temp DB: {temp_db}")
    try:
        positions_n, bankroll_n, errors = _migrate(
            args.positions_json, args.state_json, temp_db,
        )
    finally:
        # Always clean up the temp file (plus any WAL/SHM sidecars).
        for ext in ("", "-wal", "-shm"):
            p = Path(str(temp_db) + ext)
            try:
                p.unlink(missing_ok=True)
            except Exception as exc:
                print(f"  warning: failed to clean up {p}: {exc}", file=sys.stderr)

    print(
        f"\nSummary: positions={positions_n}, bankroll_rows={bankroll_n}, "
        f"perm_skip_rows=0 (in-memory at migration time)"
    )
    if errors:
        print(f"\nFAIL: {len(errors)} round-trip error(s):", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print("\nDry-run OK. No production state modified. Run with --apply to commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
