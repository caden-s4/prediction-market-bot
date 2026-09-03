# Phase WS-Diag-A2 — Parallel diagnostic reads

Observational only. No verdicts, no fix proposals.

## gate_funnel summary (30-h window)

Source: `diagnostics/gate_funnel_30h.txt`. Window per the script header: 2026-05-31T12:35:42Z → 2026-06-01T18:35:42Z. Note: 58 unparseable lines were skipped by the script (no impact on totals reported below).

Top 10 (gate, reason) combinations by count:

| Rank | Gate                  | Reason                          | Count       |
|------|-----------------------|---------------------------------|-------------|
| 1    | scanner_reject        | category                        | 289,743     |
| 2    | scanner_reject        | financial_bracket_disabled      | 39,060      |
| 3    | scanner_reject        | hours                           | 10,489      |
| 4    | scanner_reject        | legacy_weather_snipe_disabled   | 3,330       |
| 5    | scanner_reject        | economic_bracket_disabled       | 2,614       |
| 6    | gt_routing            | source_returned_none            | 1,614       |
| 7    | gt_routing            | source_not_tradeable            | 1,487       |
| 8    | confidence            | source_below_gate               | 296         |
| 9    | invariant_violation   | ws_rest_mid_disagreement        | 54          |
| 10   | fill_cap              | (none)                          | 29          |

Total events by gate over the window (from the same source):
- `scanner_reject`: 345,236
- `gt_routing`: 3,101
- `confidence`: 296
- `executor_pretrade`: 1
- `fill_cap`: 29
- `snipe`: 27 (14 none + 13 price_gate)
- `invariant_violation`: 75 (54 ws_rest_mid_disagreement + 21 implausible_gap)

No interpretation included per phase scope.

## Stuck positions

Phase context (from prior diagnostic): two ghost positions had remained in `data/runtime/ghost_positions.json` past their `resolution_date_iso`.

Status at the time of this phase's read (post-restart at 11:32:08 PST, ghost_positions.json saved_at `2026-06-01T18:35:15.150396+00:00`):

| Ticker | Present in ghost_positions.json? | Cleared by | Bot log evidence |
|--------|----------------------------------|------------|------------------|
| `KXMLBEXTRAS-26MAY231605DETBAL-EXTRAS` | **No** | Bot-startup stale-expiry-on-load logic, 11:32:08 | `logs/bot.log:27846` — "skipping expired position KXMLBEXTRAS-26MAY231605DETBAL-EXTRAS (market already resolved)" |
| `KXAAAGASM-26MAY31-4.33`              | **No** | Bot-startup stale-expiry-on-load logic, 11:32:08 | `logs/bot.log:27847` — "skipping expired position KXAAAGASM-26MAY31-4.33 (market already resolved)" |

Bot log line 27848 confirms aggregate result: "expired 2 stale ghost position(s) on load (resolution date already passed)".

Because the file was rewritten by the bot at startup before this phase could read it, the pre-restart `entry_time`, `resolution_date_iso`, and `size_usd` for the two cleared tickers are no longer present on disk. They are recoverable from `data/runtime/ghost_trades.jsonl` history if needed by a future phase. For this report:
- Hours past `resolution_date_iso` at the moment of clear: not measurable from this snapshot — file already rewritten. (`KXAAAGASM-26MAY31-…` ticker name implies a 2026-05-31 resolution; at 11:32:08 PST on 2026-06-01 that is ~36 h overdue. `KXMLBEXTRAS-26MAY23…` implies a 2026-05-23 game; ~9 days overdue. These are inferences from the ticker IDs, not from the file.)
- Total bankroll locked across both: not measurable from this snapshot — file already rewritten.

`ghost_positions.json` currently holds 2 open positions, neither of which is one of the two stuck tickers:
- `KXTRUFGAS-26JUN01-T4.26` — buy_yes, $31.19, entry_price 0.988, resolution 2026-06-01T23:59:00+00:00
- `KXTRUFGAS-26JUN01-T4.34` — buy_no, $27.92, entry_price 0.470, resolution 2026-06-01T23:59:00+00:00

No mutation performed on `ghost_positions.json`.

## KXTRUFGAS-26JUN01-T4.34 status

Source: `data/runtime/ghost_trades.jsonl`, filtered to `market_id == "KXTRUFGAS-26JUN01-T4.34"`. All timestamps below are UTC (file convention).

| Metric | Value |
|---|---|
| Entry records | 13 |
| Exit records | 16 |
| Most recent entry | 2026-06-01T18:33:17.256840+00:00 (= **11:33:17 PST**, i.e., immediately after bot restart) |
| Most recent exit | 2026-06-01T18:41:08.295346+00:00 (= 11:41:08 PST) |
| Total cumulative size_usd entered | **$403.30** |
| Total realized P&L | **−$212.10** |
| stop_loss exits | 13 |
| stop_loss_partial exits | 3 |
| Other exit reasons | 0 |

Open position on disk after the last exit: **yes** — `KXTRUFGAS-26JUN01-T4.34` is still present in `ghost_positions.json` with `size_usd: 27.92` (post-partial-exit residual), `action: buy_no`, `entry_price: 0.470`.

Per-entry recurrence: 13 distinct entries from 2026-06-01T11:57:26Z (= 04:57 PST) through 2026-06-01T18:33:17Z (= 11:33 PST). Every entry has been followed by a stop_loss exit (some entries fragmenting into multiple `stop_loss_partial` exits — 16 exits across 13 entries). Each entry was placed at a market price below `entry_price` of 0.5 (range 0.023 → 0.471) and was stopped out at a higher price (range 0.295 → 0.99), consistent with the market trending toward YES while the bot keeps re-entering NO based on `gt_prob=0.02` from `FRED/GASREGCOVW`.

No cleanup, no guard, no fix proposed in this phase.
