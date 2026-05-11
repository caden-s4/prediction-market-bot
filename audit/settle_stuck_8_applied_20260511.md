# Phase 15b-bis applied — 8 stuck weather positions settled

**Mutation timestamp:** 2026-05-11T20:28:20+00:00
**Source snapshot:** `audit/ghost_positions_stuck_8_20260511.json` (saved_at=2026-05-11T04:41:38.472882+00:00)
**Bot status during mutation:** DOWN (`main.py` stopped; legacy PID 6420 / `bot_v4.py` was running at dry-run time but was also stopped before mutation completed — `tasklist` shows no python processes)

## Summary
- Settled positions: **8**
- Total notional:    **$469.64**
- Realized P&L:      **-$469.64**
- Wins / Losses:     **0 / 8**
- Exit reason tag:   `settled_retro_phase15b_bis`

## File mutations
- `data/runtime/ghost_trades.jsonl`: appended **8** exit records (3,414 -> 3,422 lines)
- `data/runtime/ghost_positions.json`: removed **8** positions (8 -> 0); atomic write via tmp+rename
- `saved_at` rewritten to `2026-05-11T20:28:20.558302+00:00`

## Positions remaining in live `ghost_positions.json`
*(none — file is now `{"positions": {}}`)*

## Per-position outcomes
| market | side | entry | size | result | pnl | settled at |
|---|---|---:|---:|---|---:|---|
| `KXLOWTATL-26MAY10-B59.5` | buy_no  | 0.9900 | 58.71 | yes | -$58.71 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTATL-26MAY10-T62`   | buy_yes | 0.0100 | 58.71 | no  | -$58.71 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTDC-26MAY10-B55.5`  | buy_no  | 0.9400 | 58.68 | yes | -$58.68 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTDC-26MAY10-T58`    | buy_yes | 0.0100 | 58.71 | no  | -$58.71 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTMIA-26MAY10-B78.5` | buy_no  | 0.9800 | 58.70 | yes | -$58.70 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTMIA-26MAY10-T81`   | buy_yes | 0.0100 | 58.71 | no  | -$58.71 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTPHIL-26MAY10-B51.5`| buy_no  | 0.9900 | 58.71 | yes | -$58.71 | 2026-05-11T05:00:00+00:00 |
| `KXLOWTPHIL-26MAY10-T54`  | buy_yes | 0.0100 | 58.71 | no  | -$58.71 | 2026-05-11T05:00:00+00:00 |

Pattern: 4 cities (ATL, DC, MIA, PHIL), each with a paired `T##` (buy_yes @ ~0.01) and `B##.5` (buy_no @ 0.94–0.99) — same legacy weather_snipe bracket-pair signature as Phase 15b. All 8 lost (100% loss rate this batch; legacy weather_snipe historical loss rate ~94%).

## Cumulative recovery (Phase 15b + Phase 15b-bis)
| Phase | Positions | Notional | Realized P&L | Settled at |
|---|---:|---:|---:|---|
| 15b     | 19 | $1,112.22 | -$1,051.55 | 2026-05-10 |
| 15b-bis |  8 |   $469.64 |   -$469.64 | 2026-05-11 |
| **Total** | **27** | **$1,581.86** | **-$1,521.19** | — |

Attribution preserved across both phases — no positions silently dropped by the startup cleanup at `executor.py:4363`.

## Recurrence flag — Phase 15c is now urgent
**This is the second occurrence in 24 hours.** Legacy `weather_snipe` continued firing overnight Sunday → Monday after Phase 15b cleaned the prior batch, entering 8 more stuck positions on KXLOWT* 26MAY10 markets that resolved at 05:00Z while the bot was up but had no runtime settlement path for non-sports markets.

Phase 15c (runtime settlement query for non-sports markets at the executor level) is the durable fix. Without it, the same gap will reopen on the next overnight weather cycle and another 8–20 positions will be silently dropped at the next bot restart.

## Notes
- The `--apply` script print at the end raised a Windows cp1252 encoding error on a `→` arrow character; both file mutations completed before that print, so data is intact (verified by tail of ghost_trades.jsonl and re-read of ghost_positions.json post-run). The `_write_applied_doc()` step was skipped by the crash — this doc was written manually in its place with the same content the function would have produced, plus the cumulative + recurrence sections per the Phase 15b-bis spec.
- `exit_was_decisive_gt=True` on every record — Kalshi settlement is binary and authoritative.
- `hold_duration_minutes` (18.4–18.5) reflects entry_time → resolution_date_iso, not entry_time → now. The trade *did* hold to settlement; the gap was purely in the bot's accounting.
- The phantom $1,112 bankroll reserve from yesterday's race condition is also cleared, because in-memory state was lost when the bot stopped.

## Pre-restart state (Step 6 verification)
- [x] `ghost_positions.json` has 0 entries
- [x] `ghost_trades.jsonl` line count incremented by 8 (3,414 → 3,422)
- [x] Bot remains DOWN (`tasklist` shows zero python processes; both `main.py` and the legacy `bot_v4.py --loop --execute --live` are stopped)
- [x] No source files modified (only the gitignored scratch script + audit artifacts)

Refs Phase 15b, Phase 15a.
