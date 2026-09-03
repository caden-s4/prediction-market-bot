# Phase D-Sports — Post-T_FIX Sports Signal Collapse Audit

**Date:** 2026-05-11
**Mode:** Diagnostic, read-only. No source modified.
**Question:** Why did sports ghost entries collapse from 277 pre-T_FIX (203 ESPN-derived) to 1 in the last 4 days?

## 1. Window

| Window | Span (UTC) | All-strategy entries | Sports entries | SportsLiveSource entries |
|---|---|---|---|---|
| Pre-T_FIX (full history) | 2026-03-13 → 2026-05-08 | 1,210 | 277 | 30 (Shock 23 + LateGame 7) |
| Pre-T_FIX 4-day window (Apr 1–4) | 2026-04-01 → 2026-04-04 | — | 2 | 2 (Shock) |
| Pre-T_FIX peak window (Mar 24–27) | 2026-03-24 → 2026-03-27 | — | 220 | 8 (Shock) |
| Post-T_FIX (last 4 days) | 2026-05-08 00:00Z → 2026-05-11 23:59Z | 129 | 1 | 0 |

**The single post-T_FIX sports entry** (file:`data/runtime/ghost_trades.jsonl`):

```
{"event": "entry", "ts": "2026-05-09T01:51:54.827027+00:00",
 "market_id": "KXNBAGAME-26MAY08NYKPHI-PHI", ...,
 "source": "ResolutionDetector/ConfirmedFinal", ...}
```

Source is **ResolutionDetector**, not SportsLiveSource. The live-source pipeline has **zero** entries in the post window.

**Note on T_FIX timestamp:** The user described "T_FIX" as a discrete fix, but the ghost-trade entry chronology shows a *gradual* decline, not a single-day drop:

| Date | Sports entries | Calendar context |
|---|---|---|
| 2026-03-24 | 145 | NCAA tournament round of 16 |
| 2026-03-27 | 39 | NCAA tournament Sweet 16 |
| 2026-03-28 | 7 | Final weekend before Final Four |
| 2026-03-30 | 2 | NCAA Tournament effectively over |
| 2026-04-02..04-24 | 1–3 / day | NBA regular season ending |
| 2026-04-25..05-08 | 0 | NBA playoffs (~2 games/night) |
| 2026-05-09 | 1 | (ResolutionDetector only) |

Decline aligns with NCAA Tournament + Women's College Basketball end (around 2026-03-30) and NBA regular season end (~2026-04-14). gate_events.jsonl only retains data from 2026-05-06 onward, so direct pre/post gate-event comparison is not possible. **Post-T_FIX window analysis below uses gate_events 2026-05-08 → 2026-05-11.**

## 2. Cause (a) — Are sports games actually running?

Source: `data/runtime/gate_events.jsonl`, sports tickers `KX(NBAGAME|MLB|NHL|NCAAMBGAME|NCAAWBGAME|NFLGAME)`.

| Sport-prefix | Total events 2026-05-06..12 | Distinct tickers |
|---|---|---|
| KXNBAGAME | 13,923 | 23 (NBA playoffs) |
| KXMLB | 16,738 | ~30 (season futures: `KXMLB-26-WSH`, `KXMLB-26-ATH`…) |
| KXNHL | 4,271 | season futures |
| KXNCAAMBGAME / KXNCAAWBGAME / KXNFLGAME | 0 | (out of season) |

**Cross-cycle data**: `logs/bot.log*` `ResolutionBot: sports live — 1 game(s) in progress, 0 shock(s), 0 resolution signal(s)` appears 135× during current log retention. Typically 1 NBA playoff game live at any moment.

**Scanner supplement** (`logs/bot.log:21:30:02`): `kalshi sports supplement → 4 additional markets` per cycle. Active KXNBAGAME tickers per cycle: 4–6.

**Verdict (a): Partially supported.** Market volume IS far lower than peak. NBA playoffs ≈ 2 games/night vs March NCAA tournament + WNBA tournament + NBA regular season = 50+ games/day. But sports markets ARE being scanned (8,631+ routing attempts on KXNBAGAME), so volume drop alone does not explain zero entries — the kill is downstream.

## 3. Cause (b) — Scanner gates

Source: `data/runtime/gate_events.jsonl` `gate=scanner_reject` for sports tickers.

| Ticker prefix | Reject reason | Count |
|---|---|---|
| KXNBAGAME | hours | 5,235 |
| KXMLB | hours | 16,738 |
| KXNHL | hours | 4,271 |

**All scanner rejections cite reason="hours"**. No other reasons (`excluded`, `category`, `price`, `financial_bracket_disabled`) appear for sports tickers in the window.

`resolution/scanner.py:801` defines game-window-eligible prefixes:
```python
_GAME_SERIES_PREFIXES = ("KXNBAGAME", "KXNCAAMBGAME", "KXNFLGAME", "KXNCAAWBGAME")
```
Game-series tickers get a 48h scan window. **`KXMLB-26-*` and `KXNHL-*` are season futures** (e.g. `KXMLB-26-WSH` = "Will Washington win the 2026 World Series?"), `close_time` is months out → fails `hours_left ≤ 48h` → rejected. This is correct behavior.

For KXNBAGAME, 5,235 "hours" rejections are series-supplement queries that returned scheduled-for-later-than-48h game tickers — also correct.

**Verdict (b): Not the cause.** Scanner is rejecting only what it should (season futures and games beyond 48h window). Of 23 distinct KXNBAGAME tickers, all reached gt_routing.

## 4. Cause (c) — Sports signals through executor (PRIMARY KILL POINT)

Source: `data/runtime/gate_events.jsonl` for sports tickers.

| Gate | Decision distribution | Count |
|---|---|---|
| scanner_reject | (see §3) | 26,244 |
| **gt_routing** | **skip / source_returned_none** | **8,631** |
| executor_pretrade | skip / empty_book_ghost | 4 |
| invariant_violation | ws_rest_mid_disagreement | 53 |

`gt_routing` reason distribution is **homogeneous**:

```
8631 source_returned_none
```

100% of routing failures carry the same `extra.none_reasons`:

```
["SportsLiveSource: returned None (no relevant data found)",
 "SportsDataSource: returned None (no relevant data found)"]
```

(Confirmed via `grep '"gate": "gt_routing"' | uniq -c` → single combination.)

**Both sports sources return None on every routing attempt.** This is cause (c) — the hypothesis-statement criterion: *"are sports signals dying at `gt_routing:source_returned_none`? That would indicate SportsLiveSource / SportsDataSource is returning `tradeable=False` or None where it previously returned tradeable results."*

### Why does SportsLiveSource return None?

`data/sports/live_source.py:94-186` returns `None` unless:
1. Cached shock signal with `confidence >= 0.85` (line 140) — requires **final period** per `data/sports/shock_detector.py` `_score_confidence` rules (CLAUDE.md `.claude/rules/sports.md`).
2. OR non-shock late-game signal with `prob_home > 0.85` AND `_in_final_period(...)` AND `secs < 300` (lines 279, 284, 291, 300).

Otherwise returns `None`.

**Evidence from log:** 27 shock events recorded in `logs/bot.log*` during the window:

| `final_period` | uncertain | `conf` band | count |
|---|---|---|---|
| False | False | conf=0.00 | 21 |
| False | True | conf=0.00 | 2 |
| True | True | conf=0.00 | 2 |
| True | False | **conf=0.92** | 4 (only these 4 are tradeable) |

Sample:
```
2026-05-11 21:32:50 ShockDetector: NBA Los Angeles Lakers vs Oklahoma City Thunder
  | prob 0.32→0.45 shock=0.13 trigger='shooting foul' conf=0.00 final_period=False uncertain=False
```

Of 27 shocks observed, **only 4** met the `final_period=True` AND `conf>=0.85` criteria — and those 4 are exactly the 4 events that reached executor_pretrade (cause e).

### Why does SportsDataSource return None?

`data/ground_truth/sports.py:202-234` returns `None` when no events match the market (`_match_event` returns None) or when the in-progress game does not clear the "final-period + substantial-lead" gate. For NBA playoff games in regulation, this gate is rarely satisfied — same fundamental restriction as SportsLiveSource.

**Verdict (c): Primary cause.** 8,631 routing attempts → 0 returned a tradeable result. Both sources are silent by design unless the game is in final period with a decisive lead. With only ~1 NBA playoff game in progress at a time, the trade window is narrow, and any market that doesn't satisfy the final-period gate at the moment of scan gets rejected.

## 5. Cause (d) — ESPN cache / LiveGameMonitor health

Source: `logs/bot.log*` (rotated logs).

| Log signal | Count (4-log rotation) |
|---|---|
| `LiveGameMonitor` total log lines | 2,131 |
| `LiveGameMonitor: stale` | 0 |
| `LiveGameMonitor: ESPN error for nfl` | 755 |
| `LiveGameMonitor: ESPN error for nba` | 1 |
| `LiveGameMonitor: ESPN error for ncaab` | 1 |
| `LiveGameMonitor: ESPN error for ncaaw` | 1 |
| `... went FINAL` / `... CONFIRMED FINAL` (in active window) | ~1,400 |

NFL 400 errors are off-season — known harmless per `CLAUDE.md` (Known Issues). NBA/NCAAB/NCAAW each show 1 transient error in 4-log rotation. No `stale`, no `empty snapshot`, no refresh failures.

Real games detected in window:
- NBA Cleveland Cavaliers vs Detroit Pistons → CONFIRMED FINAL
- NBA Minnesota Timberwolves vs San Antonio Spurs → CONFIRMED FINAL
- NBA Los Angeles Lakers vs Oklahoma City Thunder → in-progress shock detected
- NBA New York Knicks vs Philadelphia 76ers → CONFIRMED FINAL
- NCAAB Michigan vs UConn → CONFIRMED FINAL
- NCAAW UCLA vs South Carolina → CONFIRMED FINAL

**Verdict (d): Not the cause.** ESPN poll is healthy, snapshots populated, games detected and confirmed. The downstream `gt_routing:source_returned_none` is NOT due to missing ESPN data; it is due to the sources' final-period-only emission rules.

## 6. Cause (e) — Limit-price gap / fill failure

Source: `data/runtime/gate_events.jsonl` `gate=executor_pretrade` for sports tickers.

All 4 sports tickers that survived gt_routing in the post window:

```
2026-05-09T21:58:21.331Z  KXNBAGAME-26MAY09DETCLE-DET  empty_book_ghost
2026-05-10T22:15:17.132Z  KXNBAGAME-26MAY10NYKPHI-PHI  empty_book_ghost
2026-05-11T02:25:47.489Z  KXNBAGAME-26MAY10SASMIN-SAS  empty_book_ghost
2026-05-12T02:45:06.381Z  KXNBAGAME-26MAY11DETCLE-DET  empty_book_ghost
```

Reason `empty_book_ghost` is emitted at `resolution/executor.py:2336-2350` — ghost-mode-only block when game/bracket market has an empty orderbook (would produce unfillable PENDING ghost orders).

No `large_divergence_extreme_market` or `extreme_entry_price` rejections on sports tickers in the window.

No `event: entry` records for sports tickers in `ghost_trades.jsonl` with timestamps overlapping these 4 pretrade skips — confirmed the gate blocked entries (not downstream issue).

**Verdict (e): Secondary cause, ghost-mode-specific.** When sports signals DO fire (4 occurrences), all 4 are blocked at the ghost-mode empty-book guard. In live trading mode (`executor.py:2351-2359`), these would fall through to use `signal.target_price` as the limit and proceed. **This compounds the kill but is not the dominant factor.**

## 7. Cross-check — Scanner pool size

Cycle-level summary lines confirm scanner is delivering markets:

- `ResolutionScanner: kalshi sports supplement → 4-6 additional markets` per cycle (file:`logs/bot.log:21:30:02` and adjacent)
- `ResolutionBot: sports live — 1 game(s) in progress, 0 shock(s), 0 resolution signal(s)` × 135 cycles
- T1=50, T2_batch=150 — non-sports markets dominate downstream pool

23 distinct KXNBAGAME tickers reached `gt_routing` over 6 days — confirming the scanner pool is being filled correctly with the right markets. The kill is downstream of scanner.

## 8. Verdict

### Primary cause: (c) GT routing — both sports sources return None for nearly all post-T_FIX scans

- **Evidence:** 8,631 `gt_routing:source_returned_none` events on KXNBAGAME tickers in `data/runtime/gate_events.jsonl`, 100% with identical `none_reasons` (both `SportsLiveSource` and `SportsDataSource` returned None).
- **Mechanism:** Both sources are silent by design unless a game is in the final period with either (i) a shock magnitude ≥ 0.15 in <300s remaining (`data/sports/shock_detector.py` `_score_confidence`), or (ii) a probability > 0.85 in final period (`data/sports/live_source.py:279`). Outside those windows they correctly return `None`.
- **Confidence:** **High.**

### Compounding cause: (a) seasonal market drought

- **Evidence:** Sports entry chronology (§1) shows decline aligns with NCAA tournament end (~2026-03-30) and NBA regular season end (~2026-04-14). Cycle summary shows only 1 game live at any time vs March's tournament density.
- **Mechanism:** With only ~2 NBA playoff games per night, each game spends most of its duration in regulation. The narrow final-period trade window from cause (c) compounds with low game volume.
- **Confidence:** **High** (calendar evidence).

### Minor secondary cause: (e) ghost-mode empty-book guard

- **Evidence:** 4 of 4 signals that survived gt_routing were skipped at executor_pretrade with `empty_book_ghost`.
- **Mechanism:** Ghost-mode-specific guard at `resolution/executor.py:2336-2350` skips game markets with empty orderbooks. In live mode the equivalent path falls through to `signal.target_price` (line 2351-2359).
- **Confidence:** **Medium** for the magnitude — only 4 observations.

### Causes ruled out

- **(b) Scanner gates** — rejecting only season futures (`KXMLB-26-*`) and out-of-window games, which is correct behavior.
- **(d) ESPN/LiveGameMonitor health** — healthy. 0 stale warnings, transient NBA/NCAAB/NCAAW errors at ≤1/log-rotation, NFL 400s are known-harmless off-season.

### Why this is NOT a discrete "T_FIX broke something"

The user's framing of "T_FIX → collapse" suggests a single-day regression. The data shows otherwise:

- Sports entry counts decline gradually from 2026-03-27 onward, not at a single inflection.
- gate_events.jsonl coverage starts 2026-05-06 — too late to directly compare pre/post a hypothetical April fix.
- The two architectural constraints that gate sports signals — final-period requirement in `live_source.py:140` and `shock_detector._score_confidence`'s rule that conf=0 outside final period — pre-date the entire ghost-trade history (no commits in the active log window touched these gates).

Plausibly relevant April commits (read-only inspection — no further investigation in this audit):
- `3cf8cf9` (Apr 13): "Block LARGE_DIVERGENCE ghost trades when market price is at extremes" — could affect sports trades when prices have already converged. Commit message states prior behavior was "technically blocked" by other gates, so net trades may be unchanged.
- `594af95` (Apr 12): "Replace time-decay min_gap with post-fee slippage buffer" — could raise the gap bar.
- `470b0a3` (Apr 15): "Centralize GT freshness check, add entry-side gate" — could reject sports signals if snapshot age >threshold.

These commits land AFTER the main decline (post-Mar 30). They cannot explain the Mar 27 → Mar 30 step.

## 9. Unexpected findings (logged, not acted on)

1. **53 `invariant_violation:ws_rest_mid_disagreement` events on KXNBAGAME-26MAY07LALOKC-LAL.** WS mid 0.01-0.02 vs REST mid 0.255, delta ≈ -0.24. Pattern suggests either WS book stale relative to REST or NBA playoff orderbook has tight 1-cent quotes with mid sitting at extremes. Not a trade kill — diagnostic only.
2. **`SportsDataSource` returns None even for confirmed-final NBA games scanned post-game.** E.g. `KXNBAGAME-26MAY09DETCLE-DET` made it to pretrade twice (May 9 21:58Z, May 11 02:25Z) — once in final-period live, once post-final. The post-final ResolutionDetector path produced the only entry; SportsDataSource's `_match_event` may be inconsistent.
3. **gate_events.jsonl retention is short** — only 6 days, makes pre/post comparison for any historical "fix" impossible without restoring older runtime state.

## 10. Recommendations for follow-up phase (no code changes in this audit)

If the goal is to restore sports trade frequency in the playoff/low-volume window:
1. **Audit confidence-tier thresholds** in `data/sports/shock_detector.py._score_confidence` — is the 0.85 conf gate calibrated for low-volume playoff schedules where 4-shock-per-window is the ceiling? Consider whether the 0.78 final-period tier should be tradeable when game volume is low.
2. **Extend SportsDataSource's tradeable window** beyond final-period-with-substantial-lead. Pre-game prob (e.g. moneyline-derived) might be tradeable when the gap is large enough — currently it returns None for pre-game.
3. **Live-mode parity check** for the ghost `empty_book_ghost` gate — confirm the live-mode fallback to `signal.target_price` actually executes for game markets, since those 4 blocked signals would be the only sports trades available.

**No source modified in this phase.** Verify via `git status` and `git diff --stat`.

---

## Phase D-Sports-2 Follow-up — Disambiguate seasonal vs. routing regression

**Date:** 2026-05-11
**Mode:** Diagnostic, read-only. No source modified.
**Question:** Is the pre→post collapse a seasonal artifact (pre-T_FIX entries lived in game-states that no longer exist on the calendar), or a behavioral regression (SportsLiveSource / SportsDataSource previously fired on states it no longer does)?

### Step 1 — Pre-T_FIX SportsLiveSource NBA entry game-state distribution

Filtered `data/runtime/ghost_trades.jsonl` to `event=entry`, `source` containing `SportsLiveSource`, ticker prefix `KXNBAGAME`, `ts < 2026-03-27` (decline start per §1). 7 records qualified pre-2026-03-27; the 5 most recent:

| # | Entry ts (UTC) | Ticker | source | gt_prob | conf | entry_price | gap |
|---|---|---|---|---|---|---|---|
| 1 | 2026-03-26T04:26:30Z | KXNBAGAME-26MAR25HOUMIN-MIN | SportsLiveSource/Shock | 0.05 | 0.92 | 0.245 | 0.195 |
| 2 | 2026-03-26T04:25:32Z | KXNBAGAME-26MAR25HOUMIN-MIN | SportsLiveSource/Shock | 0.05 | 0.92 | 0.185 | 0.135 |
| 3 | 2026-03-24T01:15:37Z | KXNBAGAME-26MAR23OKCPHI-PHI | SportsLiveSource/Shock | 0.03 | 0.92 | 0.255 | 0.225 |
| 4 | 2026-03-23T02:26:43Z | KXNBAGAME-26MAR22MINBOS-BOS | SportsLiveSource/LateGame | 0.05 | 0.85 | 0.255 | 0.205 |
| 5 | 2026-03-23T02:21:19Z | KXNBAGAME-26MAR22MINBOS-BOS | SportsLiveSource/Shock | 0.05 | 0.92 | 0.255 | 0.205 |

`ghost_trades.jsonl` entry records do not embed game-state fields (quarter, seconds_remaining, shock_magnitude). `logs/bot.log*` retention begins 2026-05-10 20:33 local — no March data available. **Game state is therefore inferred from the source-suffix tag, which is itself a code-enforced contract**:

- `SportsLiveSource/Shock` with `confidence == 0.92`: per `data/sports/shock_detector.py:98-108` `_score_confidence`, conf=0.92 requires `final_period=True AND shock ≥ 0.25 AND secs_remaining < _TIER1_SECS (120s)`. The `_score_confidence` function explicitly returns 0.0 when `not final_period` (line 101). A cached shock with conf 0.92 cannot exist outside the final period.
- `SportsLiveSource/Shock` with `confidence == 0.85`: requires `final_period=True AND shock ≥ 0.15 AND secs_remaining < _TIER2_SECS (300s)`.
- `SportsLiveSource/LateGame` with `confidence == 0.85`: per `data/sports/live_source.py:279-291`, requires `prob_home > 0.85`, `_in_final_period(...)` (line 284) directly, and `secs < 300` (line 291).

**Distribution across 5 samples**:
- 4× shock @ conf 0.92 → `final_period=True AND shock≥0.25 AND <120s remaining` (extreme late-game decisive-shift state)
- 1× late-game @ conf 0.85 → `final_period=True AND prob>0.85 AND <300s remaining` (final-minutes blowout)

**Git verification of code-time invariance**: at commit `3077e49` (latest sports-source edit before the pre-T_FIX samples, contemporary with the entries), `_score_confidence` is **byte-identical** to HEAD for the four code lines that gate the final-period requirement (lines 98-108). `live_source.py:140` (`shock.confidence >= 0.85`) and the LateGame path are also identical. **No commit between 2026-03-22 and HEAD relaxed or tightened the final-period gate** (`git log --since="2026-03-01" --until="2026-03-27" -- data/sports/live_source.py data/sports/shock_detector.py`: only Phase 1/Phase 3 commits plus `cc75c90` (NCAAMBGAME no_source fix) and `3077e49` (debug log addition) — none touch `_score_confidence` or the conf-0.85 threshold).

**Finding**: All 5 pre-T_FIX entries were code-structurally restricted to `final-period + decisive-lead-or-late-shock` states. The hypothetical regression where "the source previously fired on pre-game or early-period states" is falsified by the source code as it existed at entry time.

### Step 2 — Post-T_FIX NBA throughput for one 24h window

Selected 2026-05-07 — the busiest KXNBAGAME day in `gate_events.jsonl` (3,416 events vs 2,036–2,718 on adjacent days).

| Metric | Value | Method |
|---|---|---|
| Total KXNBAGAME events 2026-05-07 | 3,416 | `grep '"ticker": "KXNBAGAME' gate_events.jsonl \| grep '"ts": "2026-05-07'` |
| Distinct KXNBAGAME tickers that day | 23 | + `grep -oE 'KXNBAGAME-[A-Z0-9-]+' \| sort -u` |
| Events at `gate=gt_routing` | 1,816 | + `grep '"gate": "gt_routing"'` |
| Events at `gate=scanner_reject` | 1,600 | + `grep '"gate": "scanner_reject"'` |
| Distinct tickers reaching gt_routing | 9 | + `grep '"gate": "gt_routing"' \| grep -oE 'KXNBAGAME-[A-Z0-9-]+' \| sort -u` |
| gt_routing reasons (distribution) | `1816 source_returned_none` | + `grep -oE '"reason": "[^"]*"' \| sort \| uniq -c` |
| Distinct tickers with **any** gt_routing decision ≠ `source_returned_none` | **0** | `grep '"gate": "gt_routing"' \| grep -v 'source_returned_none'` returns empty |

`none_reasons` extra field is 100% homogeneous across the 1,816 events: `["SportsLiveSource: returned None (no relevant data found)", "SportsDataSource: returned None (no relevant data found)"]`.

**Finding**: On a full day of NBA playoff activity, 23 distinct game tickers existed in market and 9 of them reached the GT router (the other 14 were rejected at scanner for `hours` — out-of-window or already-past games, expected). Of the 9, **zero** ever produced a tradeable GT in 1,816 attempts. The router is silent because the game-state gate is silent, not because the router never received markets.

### Step 3 — Per-signal live-mode behavior projection for the 4 empty_book_ghost rejections

Source: `data/runtime/gate_events.jsonl` `gate=executor_pretrade` `reason=empty_book_ghost` for KXNBAGAME tickers.

| # | Gate ts (UTC) | Ticker | extra | Log evidence (local time) | Phase |
|---|---|---|---|---|---|
| 1 | 2026-05-09T21:58:21Z | KXNBAGAME-26MAY09DETCLE-DET | `null` | No log lines retained (rotation cutoff is 2026-05-10 20:33 local) | unknown |
| 2 | 2026-05-10T22:15:17Z | KXNBAGAME-26MAY10NYKPHI-PHI | `null` | No log lines retained (rejection at 2026-05-10 18:15 local, ~2h before log start) | unknown |
| 3 | 2026-05-11T02:25:47Z | KXNBAGAME-26MAY10SASMIN-SAS | `null` | bot.log.3: 20:36:37 orderbook_snapshot `(0 bids, 0 asks)`; "prioritizing just-finalized" repeated every 7-8 min from 21:02 through 22:39 spanning the rejection at 22:25 local | post-game |
| 4 | 2026-05-12T02:45:06Z | KXNBAGAME-26MAY11DETCLE-DET | `null` | bot.log.1: orderbook 40b/59a at 13:00 (pre-game), 40b/59a at 13:42 (in-game), `(0 bids, 0 asks)` at 19:42; "prioritizing just-finalized" at 21:07, 21:15 — game ended ~19:42 local, rejection at 22:45 local | post-game |

`gate_events.jsonl` `extra` is `null` on all 4 — confirmed by inspecting `resolution/executor.py:2336-2342`: the `log_gate_event` call for `REASON_EMPTY_BOOK_GHOST` passes only `ticker/gate/decision/reason/platform`, no orderbook payload. Bid/ask/mid/spread context is not preserved in gate events for this skip.

WS evidence for tickers within log retention (rejections #3 and #4) shows the rejection occurred during the **post-finalization window**: the orderbook had collapsed to 0/0 hours before the rejection, and the executor was being driven by `ResolutionDetector` "prioritizing just-finalized" cycles (the same path that produced the lone post-T_FIX ghost entry on KXNBAGAME-26MAY08NYKPHI-PHI). For these post-game tickers there is no opposing book — makers have already left.

**Per-signal live-mode projection**:

| # | Live-mode path | Projected outcome |
|---|---|---|
| 1 | DETCLE May 9 — no log evidence; cannot directly verify phase. Game was on May 9; rejection at 17:58 local could be in-game or post-game | inconclusive (no orderbook trace) |
| 2 | NYKPHI May 10 — no log evidence; rejection at 18:15 local | inconclusive (no orderbook trace) |
| 3 | SASMIN May 10 — **post-game**, book 0/0 since pre-game day; in live mode falls through to `signal.target_price` (executor.py:2351-2359) | order would sit unfilled — no maker counterparty post-finalization; market would resolve and cancel the order |
| 4 | DETCLE May 11 — **post-game**, book emptied at 19:42 local (~3h before rejection); in live mode falls through to `signal.target_price` | order would sit unfilled — same as #3 |

For the two verifiable rejections (#3, #4), a live order would **not have filled** — the book was empty because the game was over, not because of transient WS delivery gaps. The `empty_book_ghost` gate is correctly suppressing what would have been unproductive resting orders.

The other two (#1, #2) are not directly verifiable from logs. They share the same post-finalization profile by ticker construction (both have `ts > game_date end-of-day window`), but this is suggestive, not proven.

**Finding**: The 4 `empty_book_ghost` skips appear to be ResolutionDetector-driven post-game signals, not live in-game signals. Even in live mode, 2 of 4 (the verifiable ones) would have produced unfilled resting orders, not trades. The ghost-mode guard is not masking lost trades.

### Updated Verdict

**(A) Pure seasonal.**

Justification:
1. **Step 1**: Pre-T_FIX entries were code-structurally restricted to `final-period + late-game-decisive-or-shock` game states (`shock_detector.py:98-108`, `live_source.py:140`, `live_source.py:279-291`). The gates are byte-identical between commit `3077e49` (pre-T_FIX) and HEAD. No state outside the final-period window has ever been tradeable since at least 2026-03-22.
2. **Step 2**: On the busiest post-T_FIX NBA playoff day (2026-05-07), 9 distinct tickers reached the GT router across 1,816 attempts, all returning `source_returned_none`. The router never receives a tradeable result because the in-progress games rarely satisfy the final-period gate during a scan cycle (~1 game/night, mostly in regulation).
3. **Step 3**: The 4 `empty_book_ghost` skips are post-game tickers driven by ResolutionDetector — not live in-game signals. The 2 verifiable cases would not have filled in live mode either.

There is no evidence of a behavioral regression. The collapse is explained entirely by: (i) NBA playoff calendar density (~1-2 games/night vs March's NCAA Tournament + NBA regular season aggregate), (ii) each game spends ≤25% of duration inside the final-period gate, and (iii) within that final-period window, the shock-magnitude or prob-threshold conditions further narrow the firing window. With the pre-T_FIX 0.92-conf-shock entries requiring `<120s remaining + Δprob ≥ 0.25`, the per-game firing probability is intrinsically low; the March volume came from raw game count.

### Recommendation for to-do #2

**Remove to-do #2.** The compounding-cause framing was load-bearing on the hypothesis that "something architectural changed and is hiding under the calendar drop." The data does not support that hypothesis: the gate code at entry time was identical to current code; current playoff days show full scanner→router throughput with zero tradeable GTs because final-period + decisive-shift events are intrinsically rare at 1-2 games/night. The original §10 follow-up items (audit confidence tiers, extend SportsDataSource pre-game window, live-mode parity for empty_book_ghost) remain valid as forward-looking design questions, but they are not regression-recovery items.

**Verification:** every numeric claim in this section is sourced from one of: `data/runtime/ghost_trades.jsonl` (grep on `SportsLiveSource` / ticker prefix / date filter), `data/runtime/gate_events.jsonl` (grep on ticker prefix + date + gate name), `data/sports/shock_detector.py:87-108`, `data/sports/live_source.py:140,279-291`, `resolution/executor.py:2320-2378`, `logs/bot.log[.1-.3]` for WS orderbook lines, and `git show 3077e49:data/sports/*.py` for pre-T_FIX code identity.
