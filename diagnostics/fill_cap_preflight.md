# Phase Fill-Cap-A — Preflight for Walk-with-Clamp Fill Cap

Read-only audit. Scopes Phase Fill-Cap-B (implementation). No source modified.

Date: 2026-05-13
Author: preflight read

---

## 1. `empty_book_ghost` path

**Single emission site.** `resolution/executor.py:2329-2355` (inside `_try_execute`).

Trigger conditions (all must hold):
- `ob_live is None` — `_get_live_book(market)` returned None (REST `get_order_book` raised, or returned an empty book with no asks AND no bids; or WS book absent / stale > 30s and REST also empty).
- `not self._force_test` — `--force-test` bypasses this gate (executor.py:2330-2336, falls through with `ob_live=None`; `limit_price` later defaults to `signal.target_price`).
- `_is_game_market(mid) or _is_financial_bracket_market(mid)` — gate is scoped to game (`KXNBAGAME`, `KXNCAAMBGAME`, `KXNFLGAME`) and financial-bracket prefixes.
- `self._dry_run` — ghost-mode only. In live mode (else-branch at executor.py:2356-2364) the executor falls through with `ob_live=None`; `limit_price` defaults to `signal.target_price` and a 2-min orderbook cooldown is set on the limit-price step. The non-game, non-bracket empty-book case (else at executor.py:2365-2383) skips entirely and sets a 30-min cooldown (or 2-min during FRED hunt).

What happens to the trade attempt: hard return. `self._skip_empty_book_ghost_count += 1`, `log_gate_event(gate=executor_pretrade, decision=skip, reason=empty_book_ghost)`, no order placed, no cooldown set (urgent flag not cleared on this branch — note in Surprises).

**Exit-side equivalent.** None. There is **no empty-book check at exit**. `_get_current_price` (executor.py:4202-4247) is used by `_monitor_positions` and gates only on spread (`ILLIQUID_SPREAD_THRESHOLD`); when both sides are present and the spread is narrow it returns `ob.mid_price`, which the hard-stop, decay monitor, and game-final paths use as the exit price even if no resting depth exists at that mid. Ghost P&L is computed at this price irrespective of whether the depth could actually fill. See §4 for downstream effect.

There is a related `empty_book_snipe` reason at executor.py:3186-3222 (in `place_snipe_trade`) that hard-skips on `ob_live is None` OR `limit_price is None` regardless of mode — stricter than `_try_execute` because an empty book in the final-hour snipe window is itself suspect.

---

## 2. `slippage_adjusted_price` audit

**Over-fill bug confirmed.** `data/markets/base.py:72-92`.

```python
def slippage_adjusted_price(self, side: Side, size_usd: float) -> float:
    levels = self.yes_asks if side == Side.YES else list(reversed(self.yes_bids))
    remaining = size_usd
    total_cost = 0.0
    for level in levels:
        available_usd = level.size
        fill = min(remaining, available_usd)
        total_cost += fill * level.price
        remaining -= fill
        if remaining <= 0:
            break
    if remaining > 0:                            # ← bug at 88-91
        last_price = levels[-1].price if levels else 1.0
        total_cost += remaining * last_price     # over-fill: residual at worst-level price
    return total_cost / size_usd
```

Lines 88-91 assume infinite depth at the worst level once book depth is exhausted. Realistic behavior on Kalshi is the order rests at the limit and never fills the residual. The function returns a `total_cost / size_usd` average that includes phantom contracts.

Note the further subtle bug at line 78: `levels = self.yes_asks if side == Side.YES else list(reversed(self.yes_bids))`. `yes_bids` is documented as sorted descending (best bid first); reversing puts the worst bid first. For a Side.NO walk you want best bid first (highest bid = best fill). The reverse-then-iterate sees the worst bid first and walks upward, which is wrong.

### Callers

| File:line | Caller | Use | Over-fill matters? |
| --- | --- | --- | --- |
| `legacy/pipeline/stage2_market.py:109` | `MarketAnalysis.analyze` (Stage 2) — `analysis.slippage_yes` | EV-of-buy comparison for `preferred_side` selection | **Yes, but isolated to `legacy/`.** Picks YES vs NO based on `slippage_yes` and `slippage_no`. Under-estimating slippage on a thin book → wrong side preferred. |
| `legacy/pipeline/stage2_market.py:113` | `MarketAnalysis.analyze` (Stage 2) — `analysis.slippage_no` | Same | **Same.** |
| `tests/test_market_core.py:33` | `test_orderbook_slippage_adjusted_price_consumes_multiple_levels` | Unit test, 100 USD against 50+100 depth (full coverage; never exercises lines 88-91). | No. |

The `legacy/pipeline/` tree is **not** imported by `bot.py`, `main.py`, `resolution/executor.py`, `resolution/scanner.py`, or anything in `data/ground_truth/` (verified by absence from any of the active module chains documented in CLAUDE.md). In current execution, the over-fill bug has **zero production callers**. The function is exposed on `OrderBook` and is reachable as a primitive, but no entry/exit path in the executor reads it. The walk-with-clamp implementation can either fix it in place (and adopt it from executor) or implement a separate walk in the executor and leave the legacy code as-is. Recommendation: fix in place, since the primitive is correct in spirit and the bug is a 4-line change.

The reverse-iteration bug (line 78) affects NO-side legacy callers and is independent of the over-fill bug.

---

## 3. Entry fill sites

### `_try_execute` — `resolution/executor.py:2265-3104`

**Limit price computation:** executor.py:2893-2975 (YES action: live ask; NO action: live bid; fallback to `signal.target_price` on empty book for game/bracket markets when `_force_test` or specific ghost-mode bypass; otherwise SKIP with cooldown).

**Size computation:** executor.py:2812-2839. `size_usd = self._compute_size(signal, score.source_confidence, score.resolution_clarity)` then rollover 25% reduction at 2828, series-cap check at 2841-2890. Final `size_usd` is in dollars (USD notional).

**Variables at insertion point** (just before `_place_order` at executor.py:3014):
- `mid` — market_id (str)
- `market` — `Market` object
- `signal` — `GapSignal`
- `signal.action` — `"buy_yes"` or `"buy_no"`
- `size_usd` — float, capital allocation in USD
- `limit_price` — float, YES-price-space; live `best_yes_ask` for buy_yes, live `best_yes_bid` for buy_no, or `signal.target_price` fallback
- `ob_live` — `OrderBook` or `None` (None only under force-test, game-empty-book ghost-mode-only fallthrough is unreachable here; for `_try_execute` the empty-book ghost branch has already returned at 2355)
- `fee` — float, taker fee (per-contract decimal)

**Insertion point for walk-with-clamp:** immediately after limit_price determination (executor.py:2975) and before the `MIN_EFFECTIVE_ENTRY_PRICE` extreme-cost guard at 2982-2991. At that point both `size_usd` and `limit_price` and `ob_live` are settled; downstream gates (extreme-price floor, bankroll reserve, `_place_order`) read `size_usd` directly. Clamping `size_usd` here propagates to all downstream consumers.

A second consideration: when `ob_live is None` (force-test or game/bracket empty-book live-mode bypass), there is no book to walk. The clamp must short-circuit to "no clamp / use signal.target_price" or skip; the existing fallback to `signal.target_price` is already in place, so the clamp should no-op in that case.

### `place_snipe_trade` — `resolution/executor.py:3106-3340+`

**Limit price computation:** executor.py:3203-3208. Same pattern as `_try_execute`: YES → live ask, NO → live bid. No fallback — empty book hits `REASON_EMPTY_BOOK_SNIPE` at 3186-3222 and returns None.

**Size computation:** executor.py:3248-3259. `size_usd = self._compute_size(snipe_gap, signal.confidence)` then optional `max_risk_usd` ceiling (3252-3259, only set on `weather_peak_snipe`).

**Variables at insertion point:**
- `mid`, `market`, `signal` (SnipeSignal), `signal_class`
- `signal.action` — `"buy_yes"` / `"buy_no"`
- `size_usd` — float
- `limit_price` — float (guaranteed non-None; empty-book already returned)
- `ob_live` — `OrderBook` (guaranteed non-None at this point)

**Insertion point for walk-with-clamp:** immediately after `size_usd` is finalized (executor.py:3259) and before the per-series exposure cap at 3271-3298. Or after the series cap and before `_place_order` at 3314 — either works; placing it before series cap means clamped size feeds the cap check, which is the right semantics.

Both entry sites end at `self._place_order(market, signal_or_snipe_gap, size_usd, fee, limit_price=limit_price)`. `_place_order` (executor.py:3415-3497) is a thin write-only path — ghost path returns a `ghost_<id>` string; live path constructs an `Order(price=limit_price, size_usd=size_usd, ...)` and calls `client.place_order`. No book-walking happens here. Clamp must be applied upstream.

---

## 4. Exit path taxonomy

All exits funnel through `_exit_position(market_id, realized_pnl_usd, current_price=..., exit_reason=...)` at executor.py:3885-4010. The cap applies to anything that consumes book depth on exit. Today **no live exit path actually places a counter-order on Kalshi** — `kalshi.py:924-948` `close_position()` only cancels resting orders; filled contracts are held to resolution. The walk-with-clamp on exit is therefore primarily a ghost-mode P&L accuracy concern today, and a forward-looking concern for when live exits are wired.

Exit price used for P&L: `current_price` argument, which is the YES mid from `_get_current_price` (executor.py:4202-4247). `_get_current_price` returns `ob.mid_price` after gating on spread (`ILLIQUID_SPREAD_THRESHOLD`); it does **not** check fillable depth.

### Trader-initiated exits (subject to fill cap)

| Path | File:line | Trigger | Exit price source | Size source |
| --- | --- | --- | --- | --- |
| Decay early-exit | `executor.py:3787-3806` (dispatch) ← `decay_monitor.py:213-226` (`DecayAction.EARLY_EXIT`) | Capture ratio ≥ dynamic threshold from lookup table (`decay_monitor.py:79-119`) | `decision.position.current_price` (= `_get_current_price` mid; executor.py:3757) | `rec.size_usd` (full position) |
| Decay stop-loss (standard) | `executor.py:3787-3806` ← `decay_monitor.py:229-241` (`DecayAction.STOP_LOSS`) | hours_left > 4h AND capture ≤ -0.50 | Same | Full position |
| Decay stop-loss (urgent) | `executor.py:3787-3806` ← `decay_monitor.py:248-260` (urgent escalation) | hours_left < 0.75 AND capture ≤ -0.30 | Same | Full position |
| Decay approach-exit | `executor.py:3787-3806` ← `decay_monitor.py:263-306` (`DecayAction.APPROACH_EXIT`) | hours_left < 0.25 AND source_confidence < 0.90 | Same | Full position |
| Financial hard stop | `executor.py:3674-3749` (loop), `3736` (call) | financial source AND adverse move ≥ `FINANCIAL_HARD_STOP_THRESHOLD` (with deep-ITM mid-collapse guard) | `current_price` from `_get_current_price` (executor.py:3677) | Full position |
| Ghost pending-fill timeout | `executor.py:3598-3651` (`_check_pending_ghost_fills`), `3635-3639` (call) | Ghost order in `fill_status="pending"` for ≥ `_GHOST_FILL_TIMEOUT_MINUTES` and price didn't reach limit | `current_price` if available else `rec.entry_price` | n/a — this is a **cancellation**, not a counter-order fill. `realized_pnl_usd=0.0`. The fill cap does **not** apply here (no book is consumed). |

Note: the ghost pending-fill timeout looks like an exit but is semantically a cancel; treat as exempt from the fill cap.

### Resolution exits (exempt from fill cap)

| Path | File:line | Trigger | Notes |
| --- | --- | --- | --- |
| Sports game-final settlement (ghost only) | `executor.py:3809-3883` (`_exit_ghost_positions_for_finals`), `3876-3881` (call) | `drain_ghost_exits()` returns a winner; YES team determined via `get_yes_team(market_id)` | `current_price=correct_prob` set to 0.0 or 1.0 (binary settlement). Live positions are explicitly skipped at executor.py:3843-3848 — they "need real order handling," which today doesn't exist. |

There is currently **no general "market settled at 1.00 / 0.00" detection path** for non-sports markets in the executor — bracket/financial resolutions are not actively closed. They sit open until the underlying expires; Kalshi's natural settlement is the closing mechanism for live positions. So the only "resolution" exit path today is `_exit_ghost_positions_for_finals`.

### Common downstream — `_exit_position` (executor.py:3885-4010)

Pops `_positions[market_id]`, releases bankroll with realized P&L, sets per-market and per-series exit cooldowns (30 min), back-calculates exit_price if missing, records ResolvedPosition + PaperTradeLog, then in live mode only calls `client.close_position(market_id)` to cancel resting orders. The exit price used for P&L is whatever the caller passed in as `current_price` — there is no book check inside `_exit_position` itself.

**Implication for Fill-Cap-B:**
- On ghost-mode trader-initiated exits, clamp the exit P&L to what the book could actually have absorbed at the entry-vs-exit price differential. The realistic exit is "walk the book on the opposite side until either `nc` contracts are filled or depth runs out." If depth runs out, the residual is left open → emit the Q2b exit-blocked event and skip the exit (or partial-close with the filled portion).
- On live-mode exits, today's `close_position()` does not place a fill order, so the cap is a no-op for live until a counter-order path is implemented. Worth wiring the clamp into the call site anyway so the code is ready.

---

## 5. Legacy oversize positions

`data/runtime/ghost_positions.json` (saved 2026-05-13T19:35:11Z) has 5 open ghost positions, all on the `KXAAAGASD` AAA-Gas-Daily bracket series, all `buy_no`. Cost per NO contract = `1 - entry_price` (YES price).

| Market ID | Action | entry_price (YES) | NO cost/contract | size_usd | Implied contracts | Flag |
| --- | --- | --- | --- | --- | --- | --- |
| KXAAAGASD-26MAY14-4.515 | buy_no | 0.73 | 0.27 | $46.77 | **173.2** | Likely oversize for KXAAAGASD book depth |
| KXAAAGASD-26MAY14-4.525 | buy_no | 0.65 | 0.35 | $52.40 | **149.7** | Borderline |
| KXAAAGASD-26MAY14-4.530 | buy_no | 0.29 | 0.71 | $50.52 | 71.2 | Low |
| KXAAAGASD-26MAY14-4.535 | buy_no | 0.09 | 0.91 | $42.63 | 46.8 | Low |
| KXAAAGASD-26MAY14-4.540 | buy_no | 0.06 | 0.94 | $38.57 | 41.0 | Low |

Strict `size_usd / entry_price > 200` heuristic from the prompt presumes `buy_yes` (cost per contract = entry_price). For these `buy_no` records the relevant denominator is `1 - entry_price`. Under that re-cast heuristic, no position exceeds 200 contracts, but positions 1 (173) and 2 (150) are large enough that ghost P&L could meaningfully diverge from a real exit on KXAAAGASD's thin book. The b1 partial-fill handling load is small (~2 positions warrant scrutiny; 0 are flagrant).

No `buy_yes` legacy positions are present, so the sub-penny YES-cost case (the one that produces 500-50,000 contract positions) is not represented in the current snapshot. Worth re-running this audit after a session that included `buy_yes` at low YES prices.

---

## 6. Gate event conventions

### Patterns observed

**Gate (`gate=`):** snake_case identifier of the pipeline stage. The five canonical gates are constants in `monitoring/gate_names.py:8-12`:
- `scanner_reject`, `gt_routing`, `confidence`, `executor_pretrade`, `snipe`

One additional literal gate name is used outside `gate_names.py`: `invariant_violation`, emitted by `gap_detector.py:225`, `scanner.py:54`, `kalshi.py:45`, `kalshi_ws.py:44`. Documented in `audit/invariants_phase11.md` and special-cased by the gate-funnel script.

**Decision (`decision=`):** short verb describing the outcome. Observed values:
- `skip` — most common; the pipeline aborted at this gate
- `reject` — scanner_reject path
- `pass` — used only in `gate_events.py` smoke test
- `evaluated` — used in `weather_peak_snipe.py:638` for "saw the signal but no action"
- `implausible_gap`, `ws_rest_mid_disagreement`, `kalshi_mid_out_of_range` — invariant_violation gate uses `decision` to carry the reason code (special-cased in funnel script at gate_funnel.py:141-144)

**Reason (`reason=`):** snake_case constant from `gate_names.py:17-49`, namespaced informally by gate (e.g. `REASON_EMPTY_BOOK_GHOST` for executor_pretrade, `REASON_NO_SOURCE_MATCHED` for gt_routing). Optional; may be `None` (rendered `(none)` by the funnel).

**Extra (`extra=`):** optional dict for structured fields (e.g. `{"series_root": ..., "existing_pct": ..., "cap_pct": ...}`). Dropped if not JSON-serializable. Used by `--detail` view to show "sample extra" per (gate, reason).

### Proposed names for Fill-Cap-B

**Q1: fill_cap event (every clamped or full fill, entry and exit).**

The fill cap is not a reject — every entry/exit produces an event indicating either "full fill" or "clamp" (size reduced). Two design options:

1. **Dedicated gate, full/clamp decision.** Add `GATE_FILL_CAP = "fill_cap"`. Decision = `full` when book depth ≥ requested size, `clamp` when clamped. This is the cleanest fit, mirrors `invariant_violation`'s separate-category pattern. Reason field carries the side (entry/exit) and side-of-book.
   - `GATE_FILL_CAP = "fill_cap"`
   - decisions: `"full"`, `"clamp"`
   - `REASON_ENTRY_BUY_YES = "entry_buy_yes"`, `REASON_ENTRY_BUY_NO = "entry_buy_no"`, `REASON_EXIT_BUY_YES = "exit_buy_yes"`, `REASON_EXIT_BUY_NO = "exit_buy_no"` (carry side + direction). Or simpler: use `extra={"phase": "entry"/"exit", "side": "yes"/"no", "requested_usd": X, "clamped_usd": Y, "fill_avg_price": Z}`.

2. **Reuse executor_pretrade and a new executor_exit, with reasons.** Less intrusive on the funnel script. Add reasons `REASON_FILL_CAP_FULL = "fill_cap_full"` and `REASON_FILL_CAP_CLAMP = "fill_cap_clamp"` under executor_pretrade. Decision stays `pass` or `clamp`.

**Recommendation:** option 1. The fill cap is a distinct concept (post-gate, pre-order observation) and deserves its own bucket in funnel output. Also: entry and exit emissions are symmetric, so a single dedicated gate captures both cleanly.

**Q2: exit-blocked event for insufficient book (Q2b case — position kept open).**

The exit was attempted but the book couldn't absorb enough size to close meaningfully. The position stays open. This *is* a skip-style decision.

- `GATE_EXECUTOR_EXIT = "executor_exit"` (new — symmetric with `executor_pretrade`)
- `REASON_BOOK_INSUFFICIENT_ON_EXIT = "book_insufficient_on_exit"`
- decision = `"skip"` (consistent with other gate-blocks)
- `extra={"requested_contracts": N, "fillable_contracts": M, "fill_avg_price": P, "exit_reason": "early_exit"|"stop_loss"|...}` to record what blocked it and what the originating exit reason was

Both fit existing conventions: snake_case gate, snake_case reason, decisions reuse `skip` (for the block) and add `full`/`clamp` (for the new observation gate, parallel to existing `evaluated` from weather_peak_snipe and the per-reason decisions from `invariant_violation`).

---

## 7. Gate funnel script

`scripts/gate_funnel.py` does **not** require code changes to surface new gates — they will appear in the output automatically. Lines 154-167:

```python
if args.gate:
    gates_to_show = [args.gate] if args.gate in gate_counts else list(gate_counts.keys())
else:
    seen = set()
    gates_to_show = []
    for g in PIPELINE_ORDER:
        if g in gate_counts:
            gates_to_show.append(g)
            seen.add(g)
    for g in gate_counts:
        if g not in seen:
            gates_to_show.append(g)
```

Any gate not in `PIPELINE_ORDER` is appended to the display order *after* the canonical pipeline. For `fill_cap` and `executor_exit` to appear in pipeline order rather than as trailing items, a one-line update to `PIPELINE_ORDER` (gate_funnel.py:23-29) is needed:

```python
PIPELINE_ORDER = [
    "scanner_reject",
    "gt_routing",
    "confidence",
    "executor_pretrade",
    "fill_cap",          # new — between pretrade and exit
    "executor_exit",     # new — symmetric with executor_pretrade
    "snipe",
]
```

Optional, not required for events to appear. Recommended for legibility.

One additional consideration: if option 1 from §6 is adopted (decisions `full`/`clamp` rather than just `skip`/`reject`), the funnel still works — it groups by `reason`, and `decision` is shown via the `--detail` extra sample. If we want `decision` to drive the breakdown (like the `invariant_violation` special case at gate_funnel.py:141-144), a similar conditional would need adding for `gate == "fill_cap"`. Not necessary for v1 if reason/extra carries the discriminator.

---

## 8. Surprises

1. **`empty_book_ghost` does not clear urgent flag.** The other ghost-mode-skip exits in `_try_execute` (e.g. order-book cooldown at 2294, no-YES-ask cooldown at 2940, generic empty-book skip at 2382) call `self._registry.clear_urgent(mid)` to demote T1→T2. The `empty_book_ghost` branch (2348-2355) does not. May or may not be intentional; flag for review during Fill-Cap-B in case the clamp-driven skip needs to clear urgent similarly.

2. **Exit-side `_get_current_price` returns `mid_price`, not the side-specific best price.** Ghost P&L uses `mid_price` as the realized exit price for trader-initiated exits. A realistic exit would consume the opposite side (sell YES into the bid; cover NO into the ask). The walk-with-clamp on exit must walk the **opposite side** to entry, not the same side. Specifically:
   - `buy_yes` exit → walks YES bids (descending, best bid first) to sell the YES contracts
   - `buy_no` exit → walks YES asks (ascending, best ask first) to buy YES (which closes a NO position)
   This is the inverse of entry, where `buy_yes` walks YES asks and `buy_no` walks YES bids.

3. **`slippage_adjusted_price` has a second bug at line 78.** `levels = list(reversed(self.yes_bids))` reverses descending-sorted bids, putting worst bid first. Any NO-side walk in the legacy stage2_market.py is computing slippage from the wrong direction. Independent of the over-fill bug but worth fixing in the same patch if we touch the primitive.

4. **Live exit on Kalshi is a no-op for filled contracts.** `kalshi.py:924-948` `close_position` only cancels resting orders. No counter-order is placed for filled contracts; `_exit_position`'s call to `client.close_position` at executor.py:4000 will return immediately with "no resting orders to cancel." This means the live fill-cap on exit is a no-op today — entirely theoretical until a counter-order path is built. Worth wiring the cap in anyway so future code is ready, but expect zero impact in live mode.

5. **Force-test mode bypasses the entry empty-book gate.** At executor.py:2329-2336, `_force_test` sets `ob_live=None` and lets `limit_price` fall back to `signal.target_price`. The walk-with-clamp must short-circuit cleanly when `ob_live is None` — there's no book to walk. Recommendation: when `ob_live is None`, emit a `fill_cap:no_book` event with decision `"full"` and full size (no clamp possible), or skip the cap entirely. Don't crash.

6. **`signal.target_price` fallback in game/bracket live-mode empty-book case (executor.py:2356-2364).** Same as above. The walk-with-clamp will be called with `ob_live=None` and a `limit_price` derived from `signal.target_price`. Handle as in #5.

7. **`MIN_EFFECTIVE_ENTRY_PRICE` extreme-cost guard runs after the limit-price determination (executor.py:2982-2991).** This already serves as a crude over-leverage gate — at $0.01/contract, a $500 position implies 50,000 contracts. The fill cap will subsume this gate's intent for most cases, but the guard remains useful as defense-in-depth. Don't remove it as part of Fill-Cap-B.

8. **Reserve-then-place ordering at executor.py:3010-3015.** `self._bankroll.reserve(mid, size_usd)` happens before `_place_order`, and on order-failure the reserve is released in `_place_order` (executor.py:3450, 3474, 3496). The clamp must be applied before bankroll reserve to avoid reserving phantom dollars that would have been clamped away. Confirm insertion point at executor.py:2975 (before the `MIN_EFFECTIVE_ENTRY_PRICE` guard) is upstream of the reserve at 3010. It is.

9. **Snipe path has its own series-cap, dedup, and bankroll checks (executor.py:3271-3312).** Walk-with-clamp must run before those for size consistency. Recommended insertion at executor.py:3259 (after `max_risk_usd` clamp, before series-cap loop). The same `clamped_size` flows through everything.

10. **No global "clamped_size" return value vs. in-place mutation.** The walk-with-clamp can either return `(clamped_size, fill_avg_price, fillable_contracts)` or mutate `size_usd` in place. Suggest returning a small dataclass / tuple for clarity. The existing `limit_price` variable carries the best-level price; the clamp's `fill_avg_price` will differ (volume-weighted across consumed levels) — log both in the `extra` payload.

11. **`_compute_size` already does ghost-mode cap (executor.py:3551-3555, `GHOST_SIZING_BANKROLL_CAP`).** The walk-with-clamp is downstream of this. The interaction: ghost-mode `size_usd` is already lower than live-mode for the same Kelly fraction, so the clamp will fire less often in ghost. Documents that the cap's main forward-looking value is for live mode.

12. **PaperTradeLog records `entry_price=limit_price` (executor.py:3071), not the clamped fill average.** If we clamp, the paper log should record the volume-weighted fill average, not the best-level limit price, so ghost P&L on exit reflects what a real fill would have averaged. This is a small but real change in `log_entry`'s `entry_price` semantics. Same applies to `TradeRecord.entry_price` at executor.py:3049 — used by the decay monitor and hard-stop loop. Changing it from "best level price" to "fill average" affects capture-ratio math; verify behavior under existing tests in Fill-Cap-B.

13. **`_get_live_book` may have stale WS data while REST is empty.** executor.py:4150-4163 shows the WS-then-REST path. WS book age is checked against 30s. Walk-with-clamp should use whatever book `_get_live_book` returns — don't re-fetch. The Kalshi 8 req/s budget cannot tolerate another `get_order_book` call per signal.

---

## Summary

- Single entry fill site to instrument: `_try_execute` at executor.py:~2975 + `place_snipe_trade` at executor.py:~3259.
- Trader-initiated exits all funnel into `_exit_position(market_id, ...)`. Five exit reasons subject to the cap (`early_exit`, `stop_loss` x2 variants, `resolution`/approach_exit, `hard_stop`). `unfilled_timeout` is a cancel (exempt). `game_final` is resolution (exempt).
- `slippage_adjusted_price` primitive has 2 bugs and no production callers — safe to fix in place or replace.
- No flagrant oversize positions in current `ghost_positions.json`; largest is 173 contracts on `KXAAAGASD-26MAY14-4.515`.
- Gate event names fit existing conventions: new `GATE_FILL_CAP` for the per-fill observation, new `GATE_EXECUTOR_EXIT` for the Q2b skip. Funnel script works without code changes but a one-line `PIPELINE_ORDER` update improves legibility.
- Twelve discrete surprises listed in §8 — none blocking, all manageable in Fill-Cap-B.
