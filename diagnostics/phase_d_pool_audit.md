# Phase D-Pool — Position-Pooling Diagnostic

Read-only audit of `resolution/executor.py` position tracking,
`shared/paper_log.py` JSONL writes, and on-disk ghost state.
Anchored on the +$633.98 phantom-pnl exit on
`KXAAAGASD-26MAY13-4.470` (2026-05-12T13:27:53Z).

No source modified. No fix proposed.

---

## TL;DR

**Position pooling is REFUTED as the mechanism.**
Every write to `self._positions` is a direct
`self._positions[mid] = TradeRecord(...)` keyed by `market_id` only —
no merge, no append-to-list, no pooling. Both signal-driven and snipe
entry points guard with `if mid in self._positions: return None`. The
on-disk `ghost_positions.json` is `{market_id: {…single record…}}`,
not a list-of-entries.

The +$633.98 exit is the **mathematically correct output of the
existing P&L formula** applied to a single-entry position whose YES
price (entry_price=0.94, action=buy_no, size_usd=$45.83) implied a
NO cost of $0.06/contract → 763.83 contracts. With exit mid YES=0.11:

    pnl = (entry_price − current_price) × num_contracts
        = (0.94 − 0.11) × (45.83 / (1 − 0.94))
        = 0.83 × 763.83
        = $633.98   ← matches JSONL exactly

The mechanism is **deep-OTM leverage compounded by an illiquid
mid-price artifact in ghost mode without a depth-based fill cap.**

---

## 1. Write sites to `self._positions` in `resolution/executor.py`

Every write is a complete key assignment. None merge, none append.

| Line | Function | Op | Keying | Pre-guard |
|---|---|---|---|---|
| 3043 | `_try_execute` (signal-driven entry) | `self._positions[mid] = TradeRecord(...)` | `mid` only | line 2273 `if mid in self._positions: return None` |
| 3324 | `place_snipe_trade` (weather snipe entry) | `self._positions[mid] = TradeRecord(...)` | `mid` only | line 3166 `if mid in self._positions: return None` |
| 4390 | `_load_positions` (startup restore from disk) | `self._positions[mid] = TradeRecord(...)` | `mid` only | iterates a `dict` keyed by `mid` — no duplicates possible |
| 4688 | `_adopt_exchange_position` (live reconcile) | `self._positions[market_id] = TradeRecord(...)` | `market_id` only | called only from `_reconcile_with_exchange` for IDs the bot wasn't tracking |

No write site preserves prior state for the same `mid`. No write site
keys by anything other than `mid`. No code path inserts into a list
under a `mid` key.

**Implication:** at any instant `self._positions[mid]` holds at most
one `TradeRecord`. Pooling-by-overwrite (where a second entry silently
replaces the first while the JSONL records each as independent) is
prevented by the `if mid in self._positions: return None` guards at
both signal-driven (line 2273) and snipe (line 3166) entry points.

## 2. Read sites of `self._positions` in `resolution/executor.py`

Filtered to sites that flow into exit / P&L / log writes.

| Line | Context | Fields consumed |
|---|---|---|
| 2273, 3166 | Entry dedup guard — `mid in self._positions` | membership only |
| 2849, 3275 | Per-series exposure cap aggregation | `r.size_usd` summed over series |
| 3604 | `_check_pending_ghost_fills` iteration | `rec.fill_status`, `rec.entry_time`, `rec.entry_price`, `rec.limit_price_used`, `rec.market` |
| 3673 | Financial hard-stop iteration | `rec.entry_price`, `rec.action`, `rec.size_usd`, `rec.market`, `rec.signal` |
| 3756 | Decay-monitor open-position build | `rec.entry_price`, `rec.action`, `rec.size_usd`, `rec.ground_truth_prob`, `rec.source_confidence`, `rec.distance_pct`, `rec.signal` |
| 3840 | `_exit_ghost_positions_for_finals` | `rec` for final-game settlement |
| 3895 | `_exit_position` POP — `self._positions.pop(market_id, None)` | the entire `TradeRecord` (cost basis side of pnl math) |
| 4103 | `get_open_positions` (TUI / CLI) | read-only enumeration |

**All cost-basis fields consumed at exit (`entry_price`, `size_usd`,
`action`) are read from the single `TradeRecord` instance.** There is
no place where multiple entries on the same `mid` could leak into a
single exit log line, because no such multiple entries exist.

## 3. Exit P&L computation path

Trigger → P&L compute → JSONL write:

```
_monitor_positions()                                  (executor.py:3653)
  │
  ├─ Pass 1: financial hard-stop                      (3674)
  │   └─ pnl computed inline:                         (3719–3725)
  │        if action == buy_yes:
  │            nc  = size_usd / entry_price
  │            pnl = (current − entry) × nc
  │        else:
  │            no_entry = 1 − entry_price
  │            nc  = size_usd / no_entry
  │            pnl = (entry − current) × nc
  │   └─ _exit_position(mid, pnl, current_price, …)   (3736)
  │
  └─ Pass 2: decay monitor                            (3777)
      └─ _decay.evaluate(open_positions)              → DecayMonitor._evaluate_one
                                                       (decay_monitor.py:179–319)
            • num_contracts and current_gain computed identically to the
              hard-stop formula above (lines 186–194)
            • returns DecayDecision(current_gain_usd=current_gain, …)
      └─ _exit_position(mid, decision.current_gain_usd, current_price, …)
                                                      (executor.py:3798)

_exit_position(market_id, realized_pnl_usd, current_price, …)
                                                      (executor.py:3885)
  │
  ├─ rec = self._positions.pop(market_id, None)        (3895)
  ├─ recomputes _nc from rec.action, rec.entry_price,  (3925–3929)
  │   rec.size_usd — same formula as above; used for
  │   capture-ratio fallback and ResolvedPosition.num_contracts
  ├─ resolved_positions.append(ResolvedPosition(…))    (3959)
  └─ if self._paper_log is not None:                    (3976)
      _pnl_pct = realized_pnl_usd / rec.size_usd
      _paper_log.log_exit(
          market_id   = market_id,
          exit_price  = exit_price (= current_price)
          pnl         = realized_pnl_usd,
          pnl_pct     = _pnl_pct,
          exit_reason = …,
          …
      )
```

**Key observation:** `realized_pnl_usd` is computed once (by the
hard-stop block at 3721/3725 or by the decay monitor at 189/194)
from the *single* TradeRecord and the *current* `ob.mid_price`. The
`_exit_position` writer does NOT recompute pnl from any accumulated
state — it just passes the upstream number through. `pnl_pct` is
`pnl / size_usd`, where `size_usd` is the single-entry cost basis.

`current_price` (= JSONL `exit_price`) is fetched by
`_get_current_price` (executor.py:4202) as the order-book mid,
subject to a `spread > ILLIQUID_SPREAD_THRESHOLD = 0.85` gate
(executor.py:101). Below the 0.85-spread threshold, the mid is
returned even on thin books.

## 4. JSONL write sites (`shared/paper_log.py`)

Three writers, all in `PaperTradeLog`:

| Method | Caller | Record fields | Source-of-truth |
|---|---|---|---|
| `log_entry` (paper_log.py:39) | executor.py:3067 (`_try_execute`), 3346 (`place_snipe_trade`) | event, ts, market_id, platform, action, entry_price, size_usd, gt_prob, gap, confidence, source, tier, question, signal_class | per-entry locals at call site — NOT read from `_positions` (note: `_positions[mid].entry_time` IS read at 3079/3358 — but `_positions[mid]` is the just-written record from line 3043/3324, so it tautologically matches) |
| `log_cap_blocked` (paper_log.py:80) | executor.py:2879 (rejected before any position was created) | event, ts, market_id, action, entry_price, size_usd, gt_prob, gap, series_root, series_exposure, max_series_exposure, reason | per-signal locals; no `_positions` touched |
| `log_exit` (paper_log.py:114) | executor.py:3980 (`_exit_position`) | event, ts, market_id, exit_price, pnl, pnl_pct, exit_reason, hold_duration_minutes, exit_was_decisive_gt | `realized_pnl_usd` from caller (decay-monitor `current_gain` or hard-stop inline math); `rec.size_usd` from the popped TradeRecord; `current_price` from `_get_current_price` |

**No log writer touches a pooled / aggregated container.** Each line
is one event with values computed from one TradeRecord (entry log) or
one TradeRecord + one current_price + one realized_pnl_usd (exit log).

The `cap_blocked` records on this same ticker on 2026-05-06 (28
events) are NOT entries — they were signals stopped at the series cap
before any TradeRecord existed. They cannot influence later exits.

## 5. Forensic: `KXAAAGASD-26MAY13-4.470`

### Chronology

All records on this exact ticker, in order (line numbers in
`data/runtime/ghost_trades.jsonl`):

```
L3450  entry  2026-05-12T13:06:52  buy_no  entry=0.94   size=$45.83  conf=0.78
L3452  EXIT   2026-05-12T13:27:53  exit=0.11   pnl=+633.98   reason=early_exit  hold=21.0min
L3454  entry  2026-05-12T14:07:20  buy_no  entry=0.20   size=$42.12  conf=0.78
L3459  EXIT   2026-05-12T14:15:12  exit=0.50   pnl= −15.80   reason=stop_loss   hold= 7.9min
L3462  entry  2026-05-12T14:46:07  buy_no  entry=0.51   size=$46.72  conf=0.80
L3473  EXIT   2026-05-12T15:47:32  exit=0.965  pnl= −43.38   reason=stop_loss   hold=61.4min
L3480  entry  2026-05-12T16:28:24  buy_no  entry=0.94   size=$46.98  conf=0.80
L3615  EXIT   2026-05-13T03:45:38  exit=0.99   pnl= −39.15   reason=resolution  hold=677.2min
```

Each entry is followed by exactly one exit before the next entry.
**No interleaved entries.** No two entries are simultaneously open
on this ticker. The on-disk `ghost_positions.json` (read 2026-05-13
19:35Z) does not contain this ticker — it resolved at 03:45Z.

### Math on the +$633.98 record

Formula (decay_monitor.py:190–194, identical at executor.py:3722–3725
and executor.py:3925–3929):

```
buy_no:
  no_cost_per_contract = 1 − entry_price
  num_contracts        = size_usd / no_cost_per_contract
  pnl                  = (entry_price − current_price) × num_contracts
  pnl_pct              = pnl / size_usd               (in _exit_position)
```

Apply to L3450/L3452:

```
no_cost       = 1 − 0.94      = 0.06
num_contracts = 45.83 / 0.06  = 763.833…
pnl           = (0.94 − 0.11) × 763.833
              = 0.83 × 763.833
              = 633.982…       → JSONL pnl = 633.9817   ✓ exact match
pnl_pct       = 633.9817 / 45.83
              = 13.8333…       → JSONL pnl_pct = 13.8333 ✓ exact match
```

Cross-check the formula on the other three cycles of this ticker
(no anomalies):

```
L3454/L3459: nc = 42.12/0.80 = 52.65; pnl = (0.20−0.50)·52.65 = −15.795 ✓
L3462/L3473: nc = 46.72/0.49 = 95.347; pnl = (0.51−0.965)·95.347 = −43.383 ✓
L3480/L3615: nc = 46.98/0.06 = 783.0;  pnl = (0.94−0.99)·783.0 = −39.15  ✓
```

**Every exit on this ticker matches the formula to the cent.** The
formula is internally consistent. Position pooling is not the
mechanism — every exit cleanly traces to a single matching entry.

### What produces $633.98

The two factors that make this number large:

1. **Deep-OTM leverage at entry.** `buy_no` at YES=0.94 means we paid
   $0.06/contract. With $45.83 sized, that is 763 contracts. Any 1¢
   adverse YES move = $7.64 hit; any 1¢ favourable YES move = $7.64
   gain. The leverage is real Kalshi contract math, not a bug.

2. **A 0.83-wide YES-mid swing in 21 minutes on an illiquid bracket
   book.** YES dropped 0.94 → 0.11. For a daily gas-bracket market
   that close to resolution, that is not a normal repricing — it is
   almost certainly a thin-book mid-price artifact. The illiquid-
   spread gate at `_get_current_price` (executor.py:4234–4244) only
   suppresses mids when `ask − bid > 0.85`; a book like bid=0.005 /
   ask=0.215 → mid=0.11 / spread=0.21 passes the gate and is treated
   as a real price.

The same 763-contract leverage works in reverse on cycle 4
(L3480/L3615): entry=0.94, exit=0.99, pnl=−$39.15. A small adverse
move (5¢ YES) on the same leverage produces a small dollar loss
($39), not a phantom — confirming that leverage alone isn't the
anomaly; leverage *plus* the 0.83-wide exit price *is*.

## 6. On-disk shape: `ghost_positions.json`

Top-level object (one per `_save_positions` call):

```json
{
  "saved_at": "2026-05-13T19:35:11.087380+00:00",
  "positions": {
    "<market_id_A>": { ...single TradeRecord-derived dict... },
    "<market_id_B>": { ...single TradeRecord-derived dict... },
    …
  }
}
```

Each `market_id` key maps to ONE record. The serializer
(`_save_positions`, executor.py:4262–4283) iterates
`self._positions.items()` and writes one dict per key. The loader
(`_load_positions`, executor.py:4338) iterates the dict and writes
`self._positions[mid] = TradeRecord(...)` per entry. There is no
shape on disk that could carry a list of pooled entries per ticker.

Current file (2026-05-13 19:35Z) contains ~30 keys, all of form
`KXAAAGASD-26MAYxx-<strike>` and similar — distinct strikes, not
duplicate entries on one strike. The audit-target ticker
`KXAAAGASD-26MAY13-4.470` is not present (already exited via
`resolution` at 03:45Z).

## 7. Recent git changes touching position-tracking

`git log --since="30 days ago"` filtered to `executor.py`,
`decay_monitor.py`, `paper_log.py`:

```
9c340d1 executor: tag baseline entries by signal_type, not hardcoded "baseline"
5411f72 executor: tag pipeline-baseline entries as "baseline", not fallback "unknown"
6b49500 paper_log: persist signal_class to ghost_trades.jsonl entry records
a7adf4e weather: add peak-snipe v1 (ghost-only) for highs+lows on 4 cities
a3005bf executor: emit structured gate events at pre-trade skip points
06cd7d6 executor: sync Kalshi WS subscriptions to T1+T2 each cycle
be1e905 gates: loosen confidence/freshness/brackets for diagnostic visibility
eb5cc59 scanner: use WS orderbook cache in T2 refresh when fresh
cf7332f Add tui_state.json snapshot writer
994fc24 Add structured snipe diagnostic logging for overnight run
012fa3f Weather Phase 1C: ASOS-driven snipe strategy   ← adds place_snipe_trade
7abdcb2 Move runtime state files to data/runtime/
b716181 Skip ghost trades on empty game/bracket orderbooks
02a99d7 Re-fetch GT before freshness gate when signal is about to expire
e618c21 Loosen four gates for ghost-mode edge discovery
470b0a3 Centralize GT freshness check, add entry-side gate
0450f21 Centralize YES-team resolution so home/away bug cannot recur
3cf8cf9 Block LARGE_DIVERGENCE ghost trades when market price is at extremes
```

`decay_monitor.py` was touched twice in ~60 days:

```
470b0a3 Centralize GT freshness check, add entry-side gate
c656d3a Fix APPROACH_EXIT exit_price/pnl mismatch, add GT freshness guard  (2026-04-08)
```

`c656d3a` is directly relevant: it made `exit_price` always reflect
the live order-book mid at the moment of exit, and clarified that
`exit_was_decisive_gt` carries decisive-GT semantics separately. None
of these commits altered the per-mid keying of `_positions` or
introduced any aggregation logic. `place_snipe_trade` (012fa3f) added
a second write site, but it preserves the same `if mid in
self._positions: return None` guard and the same direct assignment
shape — no pooling.

`be1e905` ("Loosen four gates for ghost-mode edge discovery") and
`e618c21` ("Loosen four gates for ghost-mode edge discovery") are
relevant context: they relaxed entry-side gating but did NOT touch
the exit-side P&L formula or position-state shape.

## 8. Live vs ghost code path

Both modes go through the identical `_monitor_positions` →
`_exit_position` → P&L formula path. The fork is only inside
`_place_order` (executor.py:3415):

```
_place_order(market, signal, size_usd, fee, limit_price)
  │
  ├─ if dry_run OR is_paper_only(source):
  │      ghost_id = f"ghost_{market_id}_{int(time.time())}"
  │      return ghost_id                                # ← NO depth check
  │
  └─ else:
        order = Order(market_id, side, price=order_price,
                      size_usd=size_usd, fee_rate_bps=…)
        result = client.place_order(order)              # ← exchange fills
        return result.order_id  (or None on failure)
```

**Ghost mode never validates `size_usd` against book depth.** It
returns a synthetic order_id and `_try_execute` immediately writes
the full requested `size_usd` into the TradeRecord. The CLAUDE.md
"Open issues" section flags this explicitly:
*"Ghost fill size cap against orderbook depth: NOT IMPLEMENTED."*

**Live mode pushes size to the exchange**, which either fills,
partial-fills, or rejects against actual resting depth. In the
+$633.98 scenario the live path would either (a) fail to fill 763
contracts at NO=$0.06 on an illiquid daily gas bracket and partial-
fill to whatever depth existed (likely a few dollars of NO), or (b)
fill at progressively worse prices (e.g. up the ask ladder), which
the TradeRecord.entry_price wouldn't capture.

So the **same bug class exists in the live path**, but is
self-limited by the exchange's actual matching. Live mode could still
produce a smaller version of the same phantom-leverage signal if the
exchange partial-fills and the bot records the full requested
`size_usd` as the TradeRecord cost basis (audit-worthy follow-up,
but not what produced the +$633.98 ghost line).

## 9. Verdict — mechanisms ranked by evidence weight

### Mechanism A — Deep-OTM leverage × illiquid-mid artifact, no ghost depth cap  *(STRONGEST)*

**What:** Ghost mode sizes by `$size_usd` and writes that to
`TradeRecord.size_usd` without any check against order-book depth.
The P&L formula at exit treats `num_contracts = size_usd /
no_cost_per_contract` as the position size. When entry is deep-OTM
(no_cost = $0.06 → 763 contracts for $45.83) and the exit
`current_price` jumps 0.83 due to a thin-book mid swing that passes
the 0.85-spread gate, the formula correctly produces a 13.8×
"return" — which is real Kalshi contract math but unrealizable in a
real market that lacks the depth to fill 763 contracts.

**Evidence supporting:**
- The +$633.98 number matches the formula to four decimals.
- The other three cycles on the same ticker also match the formula
  exactly, and produce small (∼$15–$43) loss numbers because their
  entry-to-exit price swing was small.
- `_positions` and on-disk state both confirm there is only ever one
  entry per `mid` at a time.
- CLAUDE.md explicitly flags absent ghost depth cap as an open issue.
- 21-minute YES move from 0.94 → 0.11 on a daily gas-bracket market
  is implausible as a real reprice but plausible as a book artifact.

**Evidence against:**
- None observed. The math is reproducible, the price source is
  identified, and the size-vs-depth gap is documented.

**Distinguishes from B/C:** A is a *fill-realism* problem. The pnl
number is what the formula says, given a TradeRecord that records
the full requested size as filled. B/C would require state shape
that doesn't exist.

### Mechanism B — Position pooling on `_positions[mid]`  *(REFUTED)*

**What:** Multiple entries on the same `mid` accumulate into a single
record (or list) and a single exit reads the aggregated state.

**Evidence supporting:** None.

**Evidence against:**
- All four writes to `_positions` (executor.py:3043, 3324, 4390,
  4688) are direct `[mid] =` assignments.
- Both entry-decision paths guard `if mid in self._positions: return
  None` (2273, 3166), preventing the dict entry from being
  overwritten while an exit is still pending.
- `ghost_positions.json` is `{market_id: {…single record…}}` —
  cannot represent pooled state.
- The four cycles on `KXAAAGASD-26MAY13-4.470` are strictly
  interleaved entry-exit-entry-exit, never two entries open at once.

**Distinguishes from A:** B would require either two writes overlapping
on one key (blocked by the guard) or a list-shaped value (not in
schema). Neither is present.

### Mechanism C — `pnl_pct` interpretation as percentage vs multiplier  *(REPORTING-LAYER ONLY)*

**What:** `pnl_pct = pnl / size_usd` (executor.py:3979). For the
+$633.98 record this is 13.8333, which looks like 1383% to a reader
expecting a percent. The daily-summary aggregator (paper_log.py:255–
270) sums `pnl` (correct) and uses `pnl_pct` only as a per-trade
field; downstream reports may display it as a percent.

**Evidence supporting:** the raw value is 13.83, not 0.1383. Any
report that calls this "13.83%" is wrong.

**Evidence against being the *root* mechanism:** the underlying pnl
in dollars *is* $633.98 — Mechanism C only mis-labels the magnitude,
it does not create it. If A is fixed, C also becomes a non-issue
because such 13× ratios won't appear.

**Distinguishes from A:** A makes the dollar value wrong. C makes the
display interpretation wrong. They are orthogonal.

### Multiple-mechanism explanation?

**No.** Mechanism A *alone* explains the +$633.98 record completely.
The math is reproducible from the single recorded entry, with no need
to invoke pooling, sign-inversion, or any other state-shape bug.

### Need for runtime instrumentation?

No new runtime instrumentation is required to *explain* the +$633.98
record. The existing JSONL plus the formula traced above is
sufficient. If the goal is to validate Mechanism A live, helpful
additions would be:

- Log `ob_live.yes_bid_size` and `ob_live.yes_ask_size` at entry,
  and the inferred fillable-contracts ceiling, so the gap between
  requested size and book depth is visible per trade.
- Log the bid/ask spread alongside `current_price` at exit, so any
  pre-fix audit can identify which exits crossed an illiquid book.
- A periodic histogram of `|pnl| / size_usd` per source — any value
  above ~2.0 on Kalshi binary contracts is a strong leverage-artifact
  signal worth flagging.

These belong in a separate phase. No code is being changed here.

---

## Surfaced for follow-up (NOT investigated, NOT fixed)

The following items were noticed during the audit and may merit their
own diagnostic phases. They are listed verbatim for triage; do not
treat as committed work.

1. **Live-mode partial-fill cost-basis truth.** `_place_order` live
   path returns the exchange `order_id` and the caller writes the
   full requested `size_usd` to `TradeRecord.size_usd`
   (executor.py:3043, 3324). Unclear whether actual filled
   quantity / fill price is reconciled back into the TradeRecord
   before `_exit_position` reads it. If not, the same phantom-pnl
   class can affect live exits, scaled down by however much actually
   filled. Audit point: trace `client.place_order(...)` → `Result`
   → TradeRecord update. Cross-reference with
   `_reconcile_with_exchange` (executor.py:4449).

2. **Illiquid-mid pollution of `current_price` at exit.** The
   `_get_current_price` gate at executor.py:4234 only rejects mids
   when `ask − bid > 0.85`. Brackets with bid=0.005 ask=0.21 → mid=0.11
   pass; that mid then drives both decay-monitor capture-ratio
   triggers AND `_exit_position`'s recorded `exit_price`. The gate
   was added c656d3a (2026-04-08) specifically for the
   exit-price/pnl mismatch class. Worth checking whether 0.85 is the
   right threshold for daily gas brackets, or whether a depth-side
   complement (suppress mid if either side has < N contracts) is
   needed.

3. **Decay-monitor early-exit on illiquid mid.** A 13.8× apparent
   capture ratio at 21 minutes triggers `EARLY_EXIT` on a
   `capture_ratio >= exit_thresh` test (decay_monitor.py:213). The
   capture ratio is computed against `theo_max` which uses
   `ground_truth_prob` (executor.py: 0.02 for this trade). If the
   reported gain is artifactual, the exit is artifactual too. Worth
   instrumenting how many ghost EARLY_EXIT events occur with
   `current_gain / theo_max > 2.0` — that is the population most
   likely to be fake.

4. **`pnl_pct` semantics in JSONL.** Field is `pnl / size_usd`
   (multiplier), not a percentage. paper_log.py docstring (line 13)
   names it `pnl_pct` without qualifying. Reports that display this
   as a percent will overstate returns by a factor of 100.
   Reporting-layer issue; no exit math impact.

5. **Series-cap aggregation accuracy.** The per-series exposure cap
   sums `r.size_usd` (executor.py:2849, 3275). Because that field is
   the *requested* size and not the *filled* size, the cap is
   computed against potentially inflated exposure in live mode.
   Same root cause as item 1.

6. **`_resolved_positions.append` happens before the optional live
   `client.close_position` call (executor.py:3959 vs 3991–4004) and
   before `_save_positions` (3990).** If the exit-side exchange call
   fails, the resolved history shows the position as closed at a
   `current_price` the bot saw, even though the real exchange
   position is still open. Not part of the ghost +$633.98 incident,
   but a live-side audit point.

---

*End of Phase D-Pool audit. No source modified.*
