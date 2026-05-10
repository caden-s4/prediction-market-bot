# Phase 15b dry-run — settle 19 stuck weather positions

**Generated:** 2026-05-10T22:26:10.564631+00:00
**Snapshot:** `audit\ghost_positions_stuck_19_20260511.json` (saved_at=2026-05-10T19:43:43.668753+00:00)
**Apply flag:** *(dry-run only — no files mutated)*

## Scope
- Positions in snapshot: **21**
- Stuck (past resolution): **19**
- Excluded (legitimately open): **2**

  - excluded: `KXAAAGASD-26MAY11-4.485`
  - excluded: `KXAAAGASD-26MAY11-4.515`

## Settled P&L computation

Uses Kalshi contract economics matching `resolution/executor.py:3843-3849` (the sports `game_final` exit path):
- `buy_yes`: `nc = size/entry; pnl = (settle - entry) * nc`
- `buy_no`:  `nc = size/(1-entry); pnl = (entry - settle) * nc`
- `settle = 1.0` if `result==yes`, `0.0` if `result==no`

| market | act | entry | size | status | result | settle | pnl | nc |
|---|---|---|---|---|---|---|---|---|
| `KXHIGHTDAL-26MAY09-B90.5` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXHIGHTDAL-26MAY09-T84` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXHIGHTHOU-26MAY09-T81` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXHIGHTHOU-26MAY09-T88` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXHIGHTMIN-26MAY09-B62.5` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXHIGHTMIN-26MAY09-T60` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXHIGHTOKC-26MAY09-B83.5` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXHIGHTOKC-26MAY09-T83` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXHIGHTSATX-26MAY09-T83` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXHIGHTSATX-26MAY09-T90` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXLOWTCHI-26MAY09-B48.5` | buy_yes | 0.8900 | 54.00 | finalized | yes | 1.00 | 6.67 | 60.67 |
| `KXLOWTLAX-26MAY09-B56.5` | buy_no | 0.8700 | 58.72 | finalized | yes | 1.00 | -58.72 | 451.69 |
| `KXLOWTLAX-26MAY09-B58.5` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXLOWTNOLA-26MAY09-B69.5` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXLOWTNOLA-26MAY09-T67` | buy_no | 0.9900 | 58.80 | finalized | yes | 1.00 | -58.80 | 5880.00 |
| `KXLOWTOKC-26MAY09-B53.5` | buy_no | 0.9100 | 58.75 | finalized | yes | 1.00 | -58.75 | 652.78 |
| `KXLOWTOKC-26MAY09-T56` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| `KXLOWTSEA-26MAY09-B47.5` | buy_no | 0.9100 | 58.75 | finalized | yes | 1.00 | -58.75 | 652.78 |
| `KXLOWTSEA-26MAY09-T50` | buy_yes | 0.0100 | 58.80 | finalized | no | 0.00 | -58.80 | 5880.00 |
| **Total** |  |  | **$1112.22** |  |  |  | **$-1051.55** |  |

Wins / Losses: **1 / 18**

## Proposed `ghost_trades.jsonl` exit records (one per settled position)

```jsonl
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTDAL-26MAY09-B90.5", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.2, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTDAL-26MAY09-T84", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.2, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTHOU-26MAY09-T81", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.2, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTHOU-26MAY09-T88", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.2, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTMIN-26MAY09-B62.5", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.2, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTMIN-26MAY09-T60", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.3, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTOKC-26MAY09-B83.5", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 23.8, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTOKC-26MAY09-T83", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 23.8, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTSATX-26MAY09-T83", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXHIGHTSATX-26MAY09-T90", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXLOWTCHI-26MAY09-B48.5", "exit_price": 1.0, "pnl": 6.6742, "pnl_pct": 0.1236, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T08:00:00+00:00", "market_id": "KXLOWTLAX-26MAY09-B56.5", "exit_price": 1.0, "pnl": -58.72, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 15.0, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T08:00:00+00:00", "market_id": "KXLOWTLAX-26MAY09-B58.5", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 15.0, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXLOWTNOLA-26MAY09-B69.5", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.5, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXLOWTNOLA-26MAY09-T67", "exit_price": 1.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 7.6, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXLOWTOKC-26MAY09-B53.5", "exit_price": 1.0, "pnl": -58.75, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 24.1, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T06:00:00+00:00", "market_id": "KXLOWTOKC-26MAY09-T56", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 24.1, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T08:00:00+00:00", "market_id": "KXLOWTSEA-26MAY09-B47.5", "exit_price": 1.0, "pnl": -58.75, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 15.1, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-10T08:00:00+00:00", "market_id": "KXLOWTSEA-26MAY09-T50", "exit_price": 0.0, "pnl": -58.8, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b", "hold_duration_minutes": 15.1, "exit_was_decisive_gt": true}
```

Each `ts` reflects the actual settlement moment (`resolution_date_iso` from the snapshot), not the current clock time — so the log chronology stays accurate.
`exit_reason='settled_retro_phase15b'` distinguishes these from normally-routed exits.