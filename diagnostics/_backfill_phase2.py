"""
Phase 2 backfill — APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a).

Modifies exactly 9 exit records in data/runtime/ghost_trades.jsonl.
Read the full Phase 1 / Phase 2 spec before editing this script.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
JSONL_PATH = REPO_ROOT / "data" / "runtime" / "ghost_trades.jsonl"

# ── The 9 target EXIT records from Phase 1 Section C ───────────────────────
# Keyed by (market_id, ts) — both exist on exit records and are unique enough.
# exit_price / pnl are stored for cross-validation; action/entry_price/size_usd
# are the values from the LAST entry before this exit (Phase 1 "entries_by_mid"
# logic) — used to drive the recovery formula and confirm correctness.

TARGETS: list[dict] = [
    {
        "market_id":   "KXWTI-26APR07-T106.99",
        "ts":          "2026-04-07T18:15:47.236727+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -46.1867,
        "action":      "buy_yes",
        "entry_price": 0.9968,
        "size_usd":    46.65,
    },
    {
        "market_id":   "KXWTI-26APR07-T115.99",
        "ts":          "2026-04-07T18:21:27.446575+00:00",
        "exit_reason": "resolution",
        "exit_price":  0.0,
        "pnl":         -47.375,
        "action":      "buy_no",
        "entry_price": 0.0028,
        "size_usd":    47.85,
    },
    {
        "market_id":   "KXWTI-26APR07-T107.99",
        "ts":          "2026-04-07T18:21:27.450750+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -46.0678,
        "action":      "buy_yes",
        "entry_price": 0.9966,
        "size_usd":    46.53,
    },
    {
        "market_id":   "KXBRENTD-26APR0717-T111",
        "ts":          "2026-04-07T20:46:50.333431+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -0.187,
        "action":      "buy_yes",
        "entry_price": 0.9941,
        "size_usd":    46.48,
    },
    {
        "market_id":   "KXBRENTD-26APR0717-T110.50",
        "ts":          "2026-04-07T20:46:50.338413+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -0.222,
        "action":      "buy_yes",
        "entry_price": 0.9948,
        "size_usd":    46.99,
    },
    {
        "market_id":   "KXBRENTD-26APR0717-T108",
        "ts":          "2026-04-07T20:46:50.341404+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -0.2257,
        "action":      "buy_yes",
        "entry_price": 0.9962,
        "size_usd":    48.87,
    },
    {
        "market_id":   "KXBRENTD-26APR0717-T110",
        "ts":          "2026-04-07T20:46:50.344394+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -0.2096,
        "action":      "buy_yes",
        "entry_price": 0.9951,
        "size_usd":    46.34,
    },
    {
        "market_id":   "KXBRENTD-26APR0717-T107",
        "ts":          "2026-04-07T20:53:43.813564+00:00",
        "exit_reason": "resolution",
        "exit_price":  1.0,
        "pnl":         -0.3435,
        "action":      "buy_yes",
        "entry_price": 0.9976,
        "size_usd":    48.95,
    },
    {
        "market_id":   "KXWTI-26APR08-T104.99",
        "ts":          "2026-04-08T18:17:14.018482+00:00",
        "exit_reason": "resolution",
        "exit_price":  0.0,
        "pnl":         -33.9594,
        "action":      "buy_no",
        "entry_price": 0.0031,
        "size_usd":    34.3,
    },
]


# ── Helpers ─────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def sha256_line(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()

def recover_exit_price(action: str, entry_price: float, pnl: float, size_usd: float) -> float:
    if action == "buy_yes":
        return entry_price + (pnl * entry_price / size_usd)
    elif action == "buy_no":
        return entry_price - (pnl * (1 - entry_price) / size_usd)
    raise ValueError(f"Unknown action: {action}")

def verify_pnl(action: str, entry_price: float, exit_price: float, size_usd: float) -> float:
    if action == "buy_yes":
        return (exit_price - entry_price) * (size_usd / entry_price)
    elif action == "buy_no":
        return (entry_price - exit_price) * (size_usd / (1 - entry_price))
    raise ValueError(f"Unknown action: {action}")

def abort(msg: str) -> None:
    print(f"\n*** ABORT: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Step 1: Backup ───────────────────────────────────────────────────────────

def step1_backup() -> tuple[Path, str]:
    now_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_path = JSONL_PATH.parent / f"ghost_trades.jsonl.bak.{now_str}"

    if bak_path.exists():
        abort(f"Backup file already exists: {bak_path}  — refusing to overwrite.")

    print(f"Step 1: Creating backup → {bak_path}")
    bak_path.write_bytes(JSONL_PATH.read_bytes())

    orig_sha = sha256_file(JSONL_PATH)
    bak_sha  = sha256_file(bak_path)
    if orig_sha != bak_sha:
        abort(f"Backup SHA256 mismatch!  orig={orig_sha}  bak={bak_sha}")

    print(f"  Original SHA256 : {orig_sha}")
    print(f"  Backup  SHA256  : {bak_sha}  ✓ byte-for-byte match")
    return bak_path, orig_sha


# ── Step 2: Load file and identify target lines ──────────────────────────────

def step2_load_and_identify() -> tuple[list[bytes], list[dict], dict[int, int]]:
    """
    Returns:
        raw_lines   : list of raw bytes per line (preserves original encoding)
        records     : list of parsed dicts (index matches raw_lines)
        target_map  : {raw_lines index → TARGETS index}
    """
    print(f"\nStep 2: Loading and identifying 9 target exit records...")

    with open(JSONL_PATH, "rb") as f:
        raw_lines = f.readlines()

    print(f"  Total lines in file: {len(raw_lines)}")

    # Parse every line
    records: list[dict | None] = []
    for i, raw in enumerate(raw_lines):
        stripped = raw.rstrip(b"\r\n")
        if not stripped:
            records.append(None)
            continue
        try:
            records.append(json.loads(stripped.decode("utf-8")))
        except Exception as exc:
            abort(f"JSON parse error at line {i}: {exc}")

    # Build composite-key lookup: (market_id, ts) → line index
    # For exit records only
    exit_key_to_line: dict[tuple, int] = {}
    for i, rec in enumerate(records):
        if rec is None:
            continue
        if rec.get("event") == "exit":
            key = (rec["market_id"], rec["ts"])
            if key in exit_key_to_line:
                abort(
                    f"Duplicate exit key {key} at lines "
                    f"{exit_key_to_line[key]} and {i}"
                )
            exit_key_to_line[key] = i

    # Match each target
    target_map: dict[int, int] = {}  # line_index → target_index
    for ti, tgt in enumerate(TARGETS):
        key = (tgt["market_id"], tgt["ts"])
        line_idx = exit_key_to_line.get(key)
        if line_idx is None:
            abort(
                f"Target {ti} not found in file: market_id={tgt['market_id']} "
                f"ts={tgt['ts']}"
            )
        # Cross-validate stored exit_price and pnl match the target spec
        rec = records[line_idx]
        if rec["exit_price"] != tgt["exit_price"]:
            abort(
                f"Target {ti} exit_price mismatch at line {line_idx}: "
                f"file has {rec['exit_price']!r}, spec says {tgt['exit_price']!r}"
            )
        if abs(rec["pnl"] - tgt["pnl"]) > 0.001:
            abort(
                f"Target {ti} pnl mismatch at line {line_idx}: "
                f"file has {rec['pnl']}, spec says {tgt['pnl']}"
            )
        if rec["exit_reason"] != tgt["exit_reason"]:
            abort(
                f"Target {ti} exit_reason mismatch at line {line_idx}: "
                f"file has {rec['exit_reason']!r}, spec says {tgt['exit_reason']!r}"
            )
        target_map[line_idx] = ti
        print(
            f"  [{ti+1}/9] line={line_idx}  {tgt['market_id']}  "
            f"ts={tgt['ts']}  ✓"
        )

    if len(target_map) != 9:
        abort(f"Expected 9 targets, found {len(target_map)}.")

    print(f"  All 9 targets identified. ✓")
    return raw_lines, records, target_map


# ── Step 3: Compute and verify recovered prices ──────────────────────────────

def step3_recover(target_map: dict[int, int]) -> dict[int, float]:
    """
    Returns {line_index → recovered_exit_price} for all 9 targets.
    Aborts if any recovery is out of range or doesn't reproduce pnl.
    """
    print(f"\nStep 3: Computing and verifying recovered exit prices...")
    recovered_prices: dict[int, float] = {}

    for line_idx, ti in target_map.items():
        tgt = TARGETS[ti]
        action      = tgt["action"]
        entry_price = tgt["entry_price"]
        size_usd    = tgt["size_usd"]
        pnl         = tgt["pnl"]

        raw_recovered = recover_exit_price(action, entry_price, pnl, size_usd)

        if not (-0.01 <= raw_recovered <= 1.01):
            abort(
                f"Target {ti} ({tgt['market_id']}): recovered price "
                f"{raw_recovered:.6f} is outside [-0.01, 1.01]. "
                f"action={action} entry_price={entry_price} "
                f"pnl={pnl} size_usd={size_usd}"
            )

        clamped = max(0.0, min(1.0, raw_recovered))
        rounded = round(clamped, 6)

        check_pnl = verify_pnl(action, entry_price, rounded, size_usd)
        if abs(check_pnl - pnl) > 0.01:
            abort(
                f"Target {ti} ({tgt['market_id']}): pnl verification failed. "
                f"recovered={rounded} -> computed_pnl={check_pnl:.4f} "
                f"vs recorded_pnl={pnl}  diff={abs(check_pnl-pnl):.4f}"
            )

        recovered_prices[line_idx] = rounded
        print(
            f"  [{ti+1}/9] {tgt['market_id']}: "
            f"{tgt['exit_price']} → {rounded}  "
            f"(pnl verify diff={abs(check_pnl-pnl):.6f} ✓)"
        )

    print(f"  All 9 recoveries verified. ✓")
    return recovered_prices


# ── Step 4 & 5: Modify records in memory and write ───────────────────────────

def step4_5_modify_and_write(
    raw_lines: list[bytes],
    records: list[dict | None],
    target_map: dict[int, int],
    recovered_prices: dict[int, float],
) -> tuple[list[bytes], str]:
    """
    Returns (new_raw_lines, new_sha256).
    """
    print(f"\nStep 4+5: Modifying records in memory and writing corrected file...")

    backfill_ts = datetime.now(timezone.utc).isoformat()

    # Detect line ending from file sample
    crlf_count = sum(1 for raw in raw_lines[:200] if raw.endswith(b"\r\n"))
    lf_count   = sum(1 for raw in raw_lines[:200] if raw.endswith(b"\n") and not raw.endswith(b"\r\n"))
    use_crlf   = crlf_count > lf_count
    line_end   = b"\r\n" if use_crlf else b"\n"
    print(f"  Line ending detected: {'CRLF' if use_crlf else 'LF'}")

    new_raw_lines: list[bytes] = []
    for i, raw in enumerate(raw_lines):
        if i not in target_map:
            new_raw_lines.append(raw)
            continue

        ti  = target_map[i]
        rec = records[i]
        # Deep-copy by re-parsing from raw (never mutate in-place)
        new_rec = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))

        new_rec["exit_price_original"] = new_rec["exit_price"]
        new_rec["exit_price"]          = recovered_prices[i]
        new_rec["backfilled_at"]       = backfill_ts
        new_rec["backfill_reason"]     = "APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a)"

        new_raw = json.dumps(new_rec, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + line_end
        new_raw_lines.append(new_raw)

        print(
            f"  [{ti+1}/9] line={i} {new_rec['market_id']}: "
            f"exit_price {new_rec['exit_price_original']} → {new_rec['exit_price']}"
        )

    # Write
    with open(JSONL_PATH, "wb") as f:
        for line in new_raw_lines:
            f.write(line)

    new_sha = sha256_file(JSONL_PATH)
    print(f"\n  File written. New SHA256: {new_sha}")
    return new_raw_lines, new_sha


# ── Step 5 verification: re-read and check ───────────────────────────────────

def step5_verify(
    original_raw_lines: list[bytes],
    new_raw_lines:      list[bytes],
    target_map:         dict[int, int],
    recovered_prices:   dict[int, float],
    bak_path:           Path,
) -> None:
    print(f"\nStep 5 (post-write verification): Re-reading corrected file...")

    with open(JSONL_PATH, "rb") as f:
        disk_lines = f.readlines()

    # Line count must match original
    if len(disk_lines) != len(original_raw_lines):
        # Restore and abort
        bak_path.replace(JSONL_PATH)
        abort(
            f"Line count mismatch after write: "
            f"original={len(original_raw_lines)} new={len(disk_lines)}. "
            f"Restored from backup."
        )
    print(f"  Line count: {len(disk_lines)} (unchanged ✓)")

    # Check target records have new fields
    for i, raw in enumerate(disk_lines):
        if i not in target_map:
            continue
        try:
            rec = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
        except Exception as exc:
            bak_path.replace(JSONL_PATH)
            abort(f"Parse error re-reading target line {i}: {exc}. Restored.")

        for field in ("exit_price_original", "backfilled_at", "backfill_reason"):
            if field not in rec:
                bak_path.replace(JSONL_PATH)
                abort(f"Target line {i} missing field {field!r} after write. Restored.")

        if rec["exit_price"] != recovered_prices[i]:
            bak_path.replace(JSONL_PATH)
            abort(
                f"Target line {i} exit_price mismatch after write: "
                f"{rec['exit_price']} != {recovered_prices[i]}. Restored."
            )

    print(f"  All 9 target records have correct new exit_price + audit fields. ✓")

    # SHA256 every non-target line must match original
    mismatches: list[tuple[int, str, str]] = []
    for i in range(len(original_raw_lines)):
        if i in target_map:
            continue
        orig_h = sha256_line(original_raw_lines[i])
        new_h  = sha256_line(disk_lines[i])
        if orig_h != new_h:
            mismatches.append((i, orig_h, new_h))

    if mismatches:
        bak_path.replace(JSONL_PATH)
        details = "\n".join(
            f"  line {i}: orig={oh[:16]}...  new={nh[:16]}..."
            for i, oh, nh in mismatches[:10]
        )
        abort(
            f"{len(mismatches)} non-target line(s) changed after write!\n"
            f"{details}\n"
            f"Restored from backup."
        )

    print(f"  SHA256 of all {len(original_raw_lines) - len(target_map)} non-target lines: all match ✓")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=" * 70)
    print("Phase 2 Backfill — APPROACH_EXIT exit_price correction")
    print("=" * 70)

    if not JSONL_PATH.exists():
        abort(f"Source file not found: {JSONL_PATH}")

    # Step 1
    bak_path, orig_sha = step1_backup()

    # Step 2
    raw_lines, records, target_map = step2_load_and_identify()

    # Step 3
    recovered_prices = step3_recover(target_map)

    # Steps 4 + 5 (write)
    new_raw_lines, new_sha = step4_5_modify_and_write(
        raw_lines, records, target_map, recovered_prices
    )

    # Step 5 (verify written file)
    step5_verify(raw_lines, new_raw_lines, target_map, recovered_prices, bak_path)

    # Final summary
    print("\n" + "=" * 70)
    print("BACKFILL COMPLETE")
    print("=" * 70)
    print(f"Backup path       : {bak_path}")
    print(f"Original SHA256   : {orig_sha}")
    print(f"New file SHA256   : {new_sha}")
    print(f"Records modified  : {len(target_map)}")
    print()
    print(f"{'#':<3} {'market_id':<40} {'old_exit':<10} {'new_exit':<12} {'pnl'}")
    print("-" * 80)
    for line_idx, ti in sorted(target_map.items(), key=lambda kv: kv[1]):
        tgt = TARGETS[ti]
        print(
            f"{ti+1:<3} {tgt['market_id']:<40} "
            f"{tgt['exit_price']:<10} "
            f"{recovered_prices[line_idx]:<12} "
            f"{tgt['pnl']}"
        )


if __name__ == "__main__":
    main()
