# Dry-run — settle 8 stuck weather positions

**Generated:** 2026-05-11T20:28:19.323516+00:00
**Snapshot:** `audit\ghost_positions_stuck_8_20260511.json` (saved_at=2026-05-11T04:41:38.472882+00:00)
**Apply flag:** *(dry-run only — no files mutated)*

## Scope
- Positions in snapshot: **8**
- Stuck (past resolution): **8**
- Excluded (legitimately open): **0**


## Settled P&L computation

Uses Kalshi contract economics matching `resolution/executor.py:3843-3849` (the sports `game_final` exit path):
- `buy_yes`: `nc = size/entry; pnl = (settle - entry) * nc`
- `buy_no`:  `nc = size/(1-entry); pnl = (entry - settle) * nc`
- `settle = 1.0` if `result==yes`, `0.0` if `result==no`

| market | act | entry | size | status | result | settle | pnl | nc |
|---|---|---|---|---|---|---|---|---|
| `KXLOWTATL-26MAY10-B59.5` | buy_no | 0.9900 | 58.71 | finalized | yes | 1.00 | -58.71 | 5871.00 |
| `KXLOWTATL-26MAY10-T62` | buy_yes | 0.0100 | 58.71 | finalized | no | 0.00 | -58.71 | 5871.00 |
| `KXLOWTDC-26MAY10-B55.5` | buy_no | 0.9400 | 58.68 | finalized | yes | 1.00 | -58.68 | 978.00 |
| `KXLOWTDC-26MAY10-T58` | buy_yes | 0.0100 | 58.71 | finalized | no | 0.00 | -58.71 | 5871.00 |
| `KXLOWTMIA-26MAY10-B78.5` | buy_no | 0.9800 | 58.70 | finalized | yes | 1.00 | -58.70 | 2935.00 |
| `KXLOWTMIA-26MAY10-T81` | buy_yes | 0.0100 | 58.71 | finalized | no | 0.00 | -58.71 | 5871.00 |
| `KXLOWTPHIL-26MAY10-B51.5` | buy_no | 0.9900 | 58.71 | finalized | yes | 1.00 | -58.71 | 5871.00 |
| `KXLOWTPHIL-26MAY10-T54` | buy_yes | 0.0100 | 58.71 | finalized | no | 0.00 | -58.71 | 5871.00 |
| **Total** |  |  | **$469.64** |  |  |  | **$-469.64** |  |

Wins / Losses: **0 / 8**

## Proposed `ghost_trades.jsonl` exit records (one per settled position)

```jsonl
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTATL-26MAY10-B59.5", "exit_price": 1.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTATL-26MAY10-T62", "exit_price": 0.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTDC-26MAY10-B55.5", "exit_price": 1.0, "pnl": -58.68, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTDC-26MAY10-T58", "exit_price": 0.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.4, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTMIA-26MAY10-B78.5", "exit_price": 1.0, "pnl": -58.7, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.5, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTMIA-26MAY10-T81", "exit_price": 0.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.5, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTPHIL-26MAY10-B51.5", "exit_price": 1.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.5, "exit_was_decisive_gt": true}
{"event": "exit", "ts": "2026-05-11T05:00:00+00:00", "market_id": "KXLOWTPHIL-26MAY10-T54", "exit_price": 0.0, "pnl": -58.71, "pnl_pct": -1.0, "exit_reason": "settled_retro_phase15b_bis", "hold_duration_minutes": 18.5, "exit_was_decisive_gt": true}
```

Each `ts` reflects the actual settlement moment (`resolution_date_iso` from the snapshot), not the current clock time — so the log chronology stays accurate.
`exit_reason='settled_retro_phase15b_bis'` distinguishes these from normally-routed exits.