# Phase D-Fidelity — Trade-Log Fidelity Audit

**Date:** 2026-05-11
**Mode:** Diagnostic, read-only. No source files modified.
**Question:** Quantify how three known bugs corrupt `data/runtime/ghost_trades.jsonl`, and identify the clean subset that can be trusted for the to-do #1 P&L diagnostic.

All counts in this document come from `scripts/scratch/audit_trade_log_fidelity.py` (gitignored — preserved at `scripts/scratch/audit_output.txt`). The script reads `data/runtime/ghost_trades.jsonl` and `data/runtime/ghost_positions.json` and writes nothing.

## 0. Inventory

| Field | Value | Source |
|---|---|---|
| Trade-log file | `data/runtime/ghost_trades.jsonl` | confirmed canonical |
| Total records | 3,449 | `wc -l` |
| `event=entry` | 1,343 | grep `"event": "entry"` |
| `event=exit` | 1,131 | grep `"event": "exit"` |
| `event=cap_blocked` | 975 | grep `"event": "cap_blocked"` |
| Oldest entry ts | 2026-03-13T18:11:19Z | first record |
| Newest entry ts | 2026-05-12T02:37:36Z | last entry |
| Oldest exit ts | 2026-03-22T23:14:55Z | — |
| Newest exit ts | 2026-05-12T03:45:07Z | — |

**Pairing** (each entry walked to the first unmatched exit on the same `market_id`, in chronological order):
- Paired entry/exit pairs: **1,099**
- Unpaired entries (no exit, never closed in log): **244**

Note: `event=exit` count (1,131) exceeds paired exits (1,099). The 32-record delta is orphan exits where no entry was found — likely pre-rotation history dropped from the log; out of scope for this audit since exits without entries cannot pollute size-clamping or post-fix sports analyses.

---

## Bug (a) — `size_usd` doesn't reflect executor-level clamping

**Approach:** For every paired entry/exit, back-compute the actual contracts and dollars implied by the recorded exit `pnl`:
- `buy_yes`: `nc = pnl / (exit_price - entry_price)`, `implied_size = nc * entry_price`
- `buy_no`: `nc = pnl / (entry_price - exit_price)`, `implied_size = nc * (1 - entry_price)`

Compare to the entry-record `size_usd`. Flag where `|recorded - implied| / recorded > 0.10`.

**Counts:**

| Bucket | Count | Fraction of paired |
|---|---|---|
| Paired total | 1,099 | 100% |
| `pnl == 0` (exit ≈ entry — unverifiable) | 880 | 80.07% |
| `cannot back-compute` (missing field) | 2 | 0.18% |
| Matches within 10% | 210 | 19.11% |
| **Clamped (mismatch > 10%, positive implied)** | **4** | **0.36%** |
| **pnl-sign-inverted (implied < 0)** | **3** | **0.27%** |

**Mismatch ratio distribution (clamped subset):** p50 = 0.91, p90 = 1.09, max = 1.09.

**Source breakdown (clamped, top sources):**
- Yahoo Finance/GC=F: 2
- Yahoo Finance/CL=F: 2

**Sample clamped trades** (top by ratio):

```
Sample 1: recorded=$22.93 implied=$47.85 ratio=108.68%
  ENTRY: {"event":"entry","ts":"2026-04-04T19:32:57.831205+00:00",
          "market_id":"KXWTI-26APR07-T115.99","action":"buy_no",
          "entry_price":0.0039,"size_usd":22.93,"source":"Yahoo Finance/CL=F"}
  EXIT : {"event":"exit","ts":"2026-04-07T18:21:27.446575+00:00",
          "exit_price":0.990101,"pnl":-47.375,"exit_reason":"resolution",
          "exit_price_original":0.0,
          "backfilled_at":"2026-04-09T21:15:09.307318+00:00",
          "backfill_reason":"APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a)"}
```

```
Sample 2: recorded=$524.14 implied=$48.13 ratio=90.82%
  ENTRY: {"event":"entry","ts":"2026-03-28T23:01:07.373710+00:00",
          "market_id":"KXGOLDD-26MAR3017-T4430","action":"buy_yes",
          "entry_price":0.50175,"size_usd":524.14,"source":"Yahoo Finance/GC=F"}
  EXIT : {"event":"exit","ts":"2026-03-29T23:37:03.140474+00:00",
          "exit_price":0.0031,"pnl":-47.8331,"exit_reason":"stop_loss"}
```

```
Sample 3: recorded=$30.61 implied=$46.54 ratio=52.04%
  ENTRY: {"event":"entry","ts":"2026-04-08T00:14:43.478542+00:00",
          "market_id":"KXBRENTD-26APR0817-T97.50","action":"buy_no",
          "entry_price":0.0003,"size_usd":30.61,"source":"Yahoo Finance/CL=F"}
  EXIT : {"event":"exit","ts":"2026-04-08T21:00:02.915954+00:00",
          "exit_price":0.9901,"pnl":-46.0788,"exit_reason":"resolution"}
```

```
Sample 4: recorded=$529.24 implied=$608.88 ratio=15.05%
  ENTRY: {"event":"entry","ts":"2026-03-29T00:48:24.100672+00:00",
          "market_id":"KXGOLDD-26MAR3017-T4410","action":"buy_yes",
          "entry_price":0.5023,"size_usd":529.24,"source":"Yahoo Finance/GC=F"}
  EXIT : {"event":"exit","ts":"2026-03-29T23:37:02.594424+00:00",
          "exit_price":0.0031,"pnl":-605.1271,"exit_reason":"hard_stop"}
```

**Sample 2 reproduces the user's "$524 recorded vs ~$48 actual" example exactly** (KXGOLDD bracket, 2026-03-28). The recorded size was ~10× the size that produced the realized pnl.

### Sub-finding within (a): pnl-sign-inverted records

3 paired records produce a **negative** implied size — recorded `pnl` has the wrong sign for the position direction. Example:

```
Sample 1: recorded=$47.78 implied=$-47.85
  ENTRY: action=buy_no, entry_price=0.0003, size_usd=47.78
  EXIT : exit_price=0.49965, pnl=+23.8996 (YES went UP — buy_no should LOSE, not gain)
```

All 3 sign-inverted samples are on KXBRENTD 2026-03-31 with `exit_reason=hard_stop` and `hold_duration_minutes=0.0`. They look like the same pre-c656d3a pnl-direction bug as Sample 1 of the clamped bucket — note Sample 1 there carries an explicit `backfill_reason: "APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a)"` tag, confirming a known historical pnl-encoding regression. This is *distinct* from clamping but corrupts the same downstream metric.

**Total records with corrupted economics** (clamped + sign-inverted): **7 / 1,099 = 0.64% of paired trades**.

**Caveat — large unverifiable bucket:** 880 of 1,099 paired trades (80%) exit with `pnl == 0`. These are dominated by `exit_reason=unfilled_timeout` (ghost-pending orders that auto-cancelled without filling). They cannot be validated against the back-compute test — `size_usd` clamping could still have happened on them, but there is no exit-pnl signal to detect it. **The 0.36% clamped rate is therefore a lower bound on the true clamping rate.**

**Remediation recommendation: tag-and-filter.**
The 4 clamped + 3 sign-inverted records can be tagged by re-running this audit on demand. The 0.64% prevalence (and likely higher within the 80% pnl-zero bucket) makes a backfill not worth the source-touching risk. To-do #1's P&L diagnostic should drop the 7 known-corrupt records and any record with the `backfill_reason: "APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a)"` tag. A ship-fix-first phase is *not* required since the underlying executor code paths now write a single coherent `size_usd` per trade (entry record and `_positions[mid].size_usd` both bind to the same local variable at `resolution/executor.py:3045` / `3067` and `3325` / `3345`); the corruption is historical.

---

## Bug (b) — Audit-script position closures don't write exit events

**Hypothesis as stated:** Commits `c42fe67` and `bd7709e` closed weather positions in `ghost_positions.json` without writing matching `event=exit` rows to `ghost_trades.jsonl`.

**Hypothesis test — falsified:**

```
$ git show --stat c42fe67
audit: settle 19 stuck legacy weather_snipe ghost positions
  Commit message states: "appends to ghost_trades.jsonl ...,
  removes from ghost_positions.json."

$ git show --stat bd7709e
audit: settle 8 additional stuck weather_snipe positions (Phase 15b-bis)
  Cumulative: 27 positions across 15b + 15b-bis
```

```
$ grep -c "settled_retro" data/runtime/ghost_trades.jsonl
27
```

All 27 weather positions settled by these two commits have paired `event=exit` rows with `exit_reason=settled_retro_phase15b`. Spot-check:

```
KXHIGHTDAL-26MAY09-B90.5:
  ENTRY ts=2026-05-10T05:52:49Z action=buy_no size_usd=58.8 source=WeatherSnipe
  EXIT  ts=2026-05-10T06:00:00Z exit_price=1.0 pnl=-58.8
        exit_reason=settled_retro_phase15b exit_was_decisive_gt=true
```

Those two commits are **not** orphan-source. The audit doc `audit/settle_stuck_applied_20260511.md:14` even reports the line-count delta (3381 → 3400 lines) — exit records were written.

**However — the broader orphan-entry phenomenon is real:**

| Field | Value |
|---|---|
| Current open ghost positions | 2 |
| Entries with no paired exit | 244 |
| Orphan entries (no exit AND not currently open) | **244** |

The 244 orphan entries are distinct from the 27 weather settles (which are correctly paired). They come from other mechanisms — most likely `ghost-clear`, bot restart drops of in-memory positions, or pre-rotation history.

**Orphan breakdown by source (top 15):**

```
   62  Yahoo Finance/CL=F
   58  WeatherSnipe
   32  Yahoo Finance/NQ=F
   26  FRED/GASREGCOVW
   18  Yahoo Finance/GC=F
   11  SportsLiveSource/Shock
   10  ESPN/basketball/nba
    6  Yahoo Finance/ES=F
    6  SportsLiveSource/LateGame
    5  Yahoo Finance/^TNX
    4  ESPN/basketball/mens-college-basketball
    2  ESPN/basketball/womens-college-basketball
    2  WeatherPeakSnipe
    1  ESPN/baseball/mlb
    1  ResolutionDetector/ConfirmedFinal
```

**Orphan breakdown by entry month:**

```
  2026-03: 102
  2026-04:  60
  2026-05:  82
```

The 60 WeatherSnipe/WeatherPeakSnipe orphans are **separate from** the 27 settled-by-audit-script positions and likely represent the population of stuck weather positions before Phase 15a was discovered and before the runtime settle-query Phase 15c (still pending) lands. The 102 Mar orphans cluster around 2026-03-13–17 (initial bot operation period) and are likely from `ghost-clear` operations during early testing.

**Sample orphan entries:**

```
Sample 1: KXTNOTEW-26MAR13-T3.95, 2026-03-13T18:11Z, buy_no @0.50, $51.00, Yahoo/^TNX
Sample 2: KXGOLDW-26MAR1317-T4739.99, 2026-03-13T18:11Z, buy_yes @0.50, $12.75, Yahoo/GC=F
Sample 3: KXINX-26MAR13H1600-T6475, 2026-03-13T18:11Z, buy_no @0.50, $6.07, Yahoo/ES=F
Sample 4: KXNASDAQ100-26MAR13H1600-T23600, 2026-03-13T18:11Z, buy_no @0.50, $6.16, Yahoo/NQ=F
Sample 5: KXINX-26MAR17H1600-T6275, 2026-03-17T18:27Z, buy_no @0.50, $5.10, Yahoo/ES=F
```

All five samples are open at entry mid (0.50), suggesting they came from a period when the scanner overlay had not yet populated live orderbook prices — and were subsequently closed without writing an exit, leaving the entry orphaned in the log.

**Remediation recommendation: filter only.**
The stated hypothesis is falsified — the named commits do write exits. For the broader 244-orphan phenomenon, the trade log loses no money (pnl is never computed on these), but it overstates volume. To-do #1's P&L diagnostic should **count orphans separately** and **exclude them** from any per-source / per-strategy P&L computation. No backfill is feasible because the executor's closure path for the orphan-producing mechanism (whatever it is — likely `ghost-clear` or restart drop) does not preserve the resolved price.

A `ship-fix-first` would be useful if the goal is to **prevent new orphans**, by auditing all `_positions` removal paths in `resolution/executor.py` and any `ghost-clear` admin path for paired `paper_log.log_exit` calls. That's a separate phase — recommend deferring it behind to-do #1 (the current to-do #1 can proceed against the clean subset).

---

## Bug (c) — Pre-2026-04-15 sports rows with corrupted exit_price

**Approach:** Count `event=exit` records that (i) pair to an entry whose source is sports (SportsLiveSource / SportsDataSource / ResolutionDetector / ShockDetector / ESPN), or (ii) have a `market_id` starting with a sports game prefix (KXNBAGAME / KXNCAAMBGAME / KXNFLGAME / KXNCAAWBGAME / KXMLB / KXNHL), and have `ts < 2026-04-15T00:00:00Z`.

**Counts:**

| Field | Value |
|---|---|
| Total sports exits (paired set) | 244 |
| `ts < 2026-04-15` (suspect, pre-T_FIX ResDet) | **238** |
| `ts >= 2026-04-15` (clean) | 6 |
| Pre-fix fraction of sports exits | **97.54%** |
| Pre-fix fraction of all 1,131 exits | **21.04%** |

The pre-T_FIX ResolutionDetector fix is commit `0450f21` (referenced in the prompt; the home-vs-YES conflation fix). 21% of the entire trade log's exits are sports rows from before that fix.

**Remediation recommendation: tag-and-filter (per user direction).**

No backfill. Any P&L analysis script that touches `ghost_trades.jsonl` should apply the filter:

```python
def is_corrupt_sports_exit(exit_rec: dict, entry_rec: dict) -> bool:
    ts = datetime.fromisoformat(exit_rec["ts"])
    if ts >= datetime(2026, 4, 15, tzinfo=timezone.utc):
        return False
    return _is_sports(entry_rec, exit_rec.get("market_id"))
```

where `_is_sports` checks the entry's `source` field against the sports tag list and the `market_id` against the sports prefix list. Tag in analysis only; do not modify source data.

---

## Cross-bug interaction

**Overlap matrix** (of the 1,099 paired trades):

| Affected by | Count |
|---|---|
| (a) clamped only | 4 |
| (c) pre-fix sports only | 9 |
| (a) AND (c) | 0 |
| pnl-sign-inverted only | 3 |
| pnl-unverifiable (`pnl == 0` or missing data) | 882 |
| **CLEAN — usable for to-do #1** | **201** |

Plus the 244 orphan entries excluded by (b).

**Notes:**
- A trade affected by (b) (orphan entry) cannot be checked for (a) because there is no exit pnl to back-compute size from. The 244 orphans are simply excluded from the clean set.
- A trade affected by (c) might also have been clamped — but no overlap was observed in the 9 sports pre-fix exits where back-compute was possible.
- The 882 pnl-unverifiable trades are excluded because we cannot prove they were not clamped. They include all `unfilled_timeout` exits (ghost-pending orders that auto-cancelled). If to-do #1 needs more sample, this bucket is the place to recover — but each entry must be cross-checked against the live `ghost_positions.json` snapshot or against external Kalshi settlement data.

**Bottom line: 201 paired trades** (18.3% of the 1,099 paired set, 15.0% of all 1,343 entries) are clean from all three bugs and have a back-computable exit economics. This is the usable dataset for to-do #1.

---

## Final recommendation — filter logic for to-do #1's P&L diagnostic

Apply this filter cascade in order:

1. **Drop unpaired entries.** Iterate `ghost_trades.jsonl`, build the entry/exit pairing exactly as in `scripts/scratch/audit_trade_log_fidelity.py`. Skip entries with no paired exit.
2. **Drop pre-2026-04-15 sports exits.** If the entry's `source` is sports-tagged (SportsLiveSource / SportsDataSource / ResolutionDetector / ShockDetector / ESPN), or the `market_id` starts with KXNBAGAME / KXNCAAMBGAME / KXNFLGAME / KXNCAAWBGAME / KXMLB / KXNHL, AND the exit `ts < 2026-04-15T00:00:00Z`: skip.
3. **Drop records with `backfill_reason: "APPROACH_EXIT exit_price/pnl mismatch (pre-c656d3a)"`.** These carry the explicit pre-fix tag.
4. **Drop records where back-computed implied_size has wrong sign or differs from recorded `size_usd` by more than 10%.** These are bug (a) and the sign-inverted sub-finding.
5. **Decide policy for `pnl == 0` exits.** They are 80% of paired trades. If to-do #1 is a win-rate analysis, include them as "no-pnl" outcomes (mostly `unfilled_timeout`). If it is a per-trade P&L bleed analysis, exclude them — they contribute zero to dollars but distort denominators.

After step 1–4, ~201 trades remain. After step 5 (exclude `pnl==0`), all 201 contribute non-zero P&L.

---

## Remediation work that needs to ship before future P&L analyses can be trusted

One phase per bug, ordered by impact and effort:

1. **(b) Orphan-prevention phase.** Audit every `_positions` removal path in `resolution/executor.py` (`pop(mid)`, `del`, restart-time drops in `_load_positions`, ghost-clear admin path in main.py / tui.py). Each path must call `paper_log.log_exit` with a synthetic exit price + reason tag before removing the position. This addresses the 244-orphan phenomenon and prevents recurrence. *Estimated scope: read-only audit + targeted edits, no executor logic changes.*
2. **(c) Sports pre-fix backfill — NOT RECOMMENDED.** Per user direction. Tag-and-filter only.
3. **(a) Clamping-source investigation — DEFER.** The clamped/inverted records appear to be historical (all paired exits before 2026-04-09 backfill tag). Current executor paths bind a single `size_usd` to both the entry log and `_positions`. Recommend revisit only if new clamped records appear post-2026-04-15.

---

## Methodology — script and reproducibility

- Script: `scripts/scratch/audit_trade_log_fidelity.py` (gitignored)
- Raw output: `scripts/scratch/audit_output.txt`
- Source files read: `data/runtime/ghost_trades.jsonl`, `data/runtime/ghost_positions.json`
- Source files modified: none
- Run: `python -X utf8 scripts/scratch/audit_trade_log_fidelity.py`

Every count in this document comes from one of:
- `wc -l` / `grep -c` on `ghost_trades.jsonl`
- `git show --stat <hash>` on the audit-script commits
- `python -X utf8 scripts/scratch/audit_trade_log_fidelity.py` (deterministic over the trade log + positions snapshot)
