# Phase 15b applied — 19 stuck weather positions settled

**Mutation timestamp:** 2026-05-10T22:26:10.564631+00:00
**Source snapshot:** `audit\ghost_positions_stuck_19_20260511.json` (saved_at=2026-05-10T19:43:43.668753+00:00)

## Summary
- Settled positions: **19**
- Total notional:    **$1112.22**
- Realized P&L:      **$-1051.55**
- Wins / Losses:     **1 / 18**
- Exit reason tag:   `settled_retro_phase15b`

## File mutations
- `data\runtime\ghost_trades.jsonl`: appended **19** exit records (3381 → 3400 lines)
- `data\runtime\ghost_positions.json`: removed **19** positions (22 → 3)

## Positions remaining in live `ghost_positions.json`
- `KXAAAGASD-26MAY11-4.485`
- `KXAAAGASD-26MAY11-4.515`
- `KXAAAGASD-26MAY11-4.520`

## Per-position outcomes
| market | pnl | pnl_pct | settled at |
|---|---|---|---|
| `KXHIGHTDAL-26MAY09-B90.5` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTDAL-26MAY09-T84` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTHOU-26MAY09-T81` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTHOU-26MAY09-T88` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTMIN-26MAY09-B62.5` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTMIN-26MAY09-T60` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTOKC-26MAY09-B83.5` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTOKC-26MAY09-T83` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTSATX-26MAY09-T83` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXHIGHTSATX-26MAY09-T90` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTCHI-26MAY09-B48.5` | $6.67 | 12.36% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTLAX-26MAY09-B56.5` | $-58.72 | -100.00% | 2026-05-10T08:00:00+00:00 |
| `KXLOWTLAX-26MAY09-B58.5` | $-58.80 | -100.00% | 2026-05-10T08:00:00+00:00 |
| `KXLOWTNOLA-26MAY09-B69.5` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTNOLA-26MAY09-T67` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTOKC-26MAY09-B53.5` | $-58.75 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTOKC-26MAY09-T56` | $-58.80 | -100.00% | 2026-05-10T06:00:00+00:00 |
| `KXLOWTSEA-26MAY09-B47.5` | $-58.75 | -100.00% | 2026-05-10T08:00:00+00:00 |
| `KXLOWTSEA-26MAY09-T50` | $-58.80 | -100.00% | 2026-05-10T08:00:00+00:00 |

## Notes
- The `pending #2` per-signal-class PnL gate now has ~$1052 of legacy weather_snipe loss data to calibrate against.
- `exit_was_decisive_gt=True` on every record — the Kalshi settlement is binary and authoritative.
- `hold_duration_minutes` = entry_time → resolution_date (not entry_time → now). The trade *did* hold to settlement; the gap is just in the bot's accounting.
- Phase 15c (runtime settlement query for non-sports markets) is separate and still pending — without it this same gap will reopen on the next overnight weather cycle.

## Signal-class breakdown
All 19 settled positions were from the **legacy `weather_snipe`** strategy
(the final-60-min decisive-snipe class — `strategies/weather_snipe.py`).
Identifiable from market_id prefixes (`KXHIGHT*` / `KXLOWT*` daily HIGH/LOW)
and from the entry timestamps (05:35–07:45 UTC, the snipe window for
midnight-local resolution). **Zero** entries from the new Phase 14b
`weather_peak_snipe` class — those are correctly being tracked under
`source="WeatherPeakSnipe"` in `ghost_trades.jsonl` (the KXHIGHMIA-26MAY10
entries earlier in the log) and have already exited via the live decay
monitor since those markets are still open. The stuck-position problem is
scoped to the legacy decisive-snipe path.

## Snapshot vs live file divergence
The snapshot at 19:43:43 had 21 positions. The live file just before
`--apply` had **22** — one extra: `KXAAAGASD-26MAY11-4.520`, opened by the
running bot at 21:53:43 (visible in `ghost_trades.jsonl` as an `event:entry`
record). That entry is legitimate (future resolution, gas-prices bracket,
FRED-routed), so my script correctly left it alone. Post-apply the live
file has 3 KXAAAGASD-26MAY11 positions — all 3 are real, all 3 resolve
2026-05-11 03:59 UTC.

## ⚠ Race-condition caveat — file removal is fragile until restart
The 19 exit records in `ghost_trades.jsonl` are **permanent** (append-only,
the bot does not truncate this file). The P&L attribution is safely in the
trade log.

However, the **bot's in-memory `self._positions` dict still contains the
19 stuck weather positions**. This script can't touch the running bot's
memory. On the bot's next `_save_positions()` call (fires every time a
position opens or exits — `executor.py:3983`), it will overwrite
`ghost_positions.json` with its in-memory state, **restoring the 19 stale
entries to disk**.

Consequences of this race:
- **Cosmetic:** the TUI / `p` command will show the 19 stuck positions again
  whenever the bot writes the file. Confusing but not harmful.
- **Bankroll:** the bot has $1,112.22 still reserved against these dead
  positions internally. New trades may be undersized or blocked by series
  caps until the bot is restarted. This is the only operational impact.
- **No double-counting:** Phase 15a verified there is no runtime settlement
  query for weather markets — the bot has no path to write its own exit
  records for these tickers, so a second wave of `exit` events for the same
  `market_id` cannot fire. The 19 retroactive exits in `ghost_trades.jsonl`
  are the canonical record.
- **On next bot restart:** the load-time expiry path at `executor.py:4363`
  silently drops past-resolution entries from `ghost_positions.json`. With
  this phase's exit records already in the trade log, the silent drop is
  now harmless — P&L attribution is preserved.

**Recommendation:** the file revert is benign in P&L terms; the bankroll
reservation is the only real cost. Restart at the next convenient moment
to clear in-memory state. Phase 15c remains the durable fix.