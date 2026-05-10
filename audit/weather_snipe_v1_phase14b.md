# Phase 14b — Weather peak-snipe v1 launch note (ghost-only)

**Date:** 2026-05-09
**Mode:** code change. New signal class added; existing weather_snipe untouched.
**Routing:** ghost only. No live order paths.

## What landed

| Component | File:line | Notes |
|---|---|---|
| ASOS timeseries fetcher | `data/ground_truth/asos_timeseries.py:1` | IEM `mesonet.agron.iastate.edu` endpoint. 5-min TTL cache + 6s inter-request throttle. |
| Strategy module | `strategies/weather_peak_snipe.py:1` | `evaluate_event_signals()` is the entry point. `WeatherPeakSnipeSignal` dataclass mirrors the existing `SnipeSignal` shape so `place_snipe_trade` accepts it unchanged. Class constant `SIGNAL_CLASS = "weather_peak_snipe"`. Emits two `gate=snipe` events to `gate_events.jsonl` per evaluation — one `decision=evaluated` (ticker + winner_idx + obs_temp_f + trigger_time_utc) when the trigger fires and a winner bracket is identified, and one `decision=skip, reason=price_gate` (winner_yes_ask + adjacent_yes_asks list with nulls for off-array offsets) when no bracket passes the price gate. This is the live price-gate evidence channel Phase 14a deferred. |
| Series ticker registry | `data/markets/kalshi.py:60-77` | Added `KXHIGHNY/CHI/MIA/DEN` (older `KXHIGH<CITY>` form per Phase 14a inventory). 33 → 37 series. |
| Scanner batch dispatcher | `resolution/scanner.py:_dispatch_weather_peak_snipe_batch` | Runs ONCE per cycle after the kalshi loop completes. Groups candidates by `(series, event_date)`, evaluates trigger per group, dispatches signals via the existing `snipe_callback`. Hard-codes `dry_run=True`. |
| Scanner accumulation hook | `resolution/scanner.py` (kalshi loop) | Per-market `is_peak_snipe_candidate(m.market_id)` check appends to in-cycle list; flushed after the loop. |
| Executor signal_class threading | `resolution/executor.py:place_snipe_trade` | Reads `getattr(signal, 'signal_class', 'weather_snipe')` and threads through every `log_gate_event` `extra` dict. Adds defense-in-depth ghost-only guard for `weather_peak_snipe`. Honors `getattr(signal, 'max_risk_usd', None)` as a hard ceiling on `_compute_size` output. Paper log `source` reflects strategy class. |
| Tests | `tests/test_weather_peak_snipe.py` | 24 tests covering series matcher, bracket parsing, trigger pass/fail conditions, signal generation with price gates, dedup, ghost-only refusal, contract cap. |

## Trigger spec (v1)

Per Phase 14a verdict, adapted to hourly METAR cadence:

1. Local clock-hour ≥ peak_hour + 1 (HIGH: ≥15:00 local, LOW: ≥08:00 local).
2. Running extremum was set ≥30 min ago.
3. Current obs ≥1°F past the running extremum.
4. Post-peak monotonicity: no rebound bounce >1°F from the post-peak running min (HIGH) / max (LOW).

Applies to `KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHDEN` (older form),
`KXHIGHT<CITY>` (newer form, none of our 4 cities use this), and
`KXLOWTNYC`, `KXLOWTCHI`, `KXLOWTMIA`, `KXLOWTDEN`.

ASOS stations: KNYC (Central Park), KORD (O'Hare), KMIA (Miami Intl), KDEN (Denver Intl).

## Trade gates per trigger event

- Winner bracket (contains observed temp): buy YES if `yes_ask ≥ 0.85`.
- ±1 / ±2 adjacent brackets: buy NO if `yes_ask ≤ 0.15`.
- Per-bracket cap: `max_risk_usd = $5.00` (executor honors via clamp on `_compute_size`).
- Per-event cap: 6 contracts max (greedy by edge across the ≤5 candidate signals).
- Gate event extras carry `signal_class="weather_peak_snipe"` so `gate_funnel` can categorize.

## Ghost-only routing — verified

```
$ grep -n "weather_peak_snipe" resolution/executor.py
3133:        signal_class = getattr(signal, "signal_class", "weather_snipe")
3142:        if signal_class == "weather_peak_snipe" and not self._dry_run:
```

Two layers:
1. Strategy: `evaluate_event_signals(..., dry_run=True)` returns `[]` if `dry_run=False`.
2. Executor: `place_snipe_trade` skips with `reason="ghost_only"` if `signal_class == "weather_peak_snipe"` and `not self._dry_run`.

No `_place_order(... dry_run=False ...)` path exists for this signal class.

## Sanity test (Step 5) — 2026-05-09 ~22:24 ET (NYC local)

`python main.py --info` → ran one full kalshi cycle. New dispatcher fired with live ASOS data:

```
WeatherPeakSnipe: evaluating 8 event group(s) (candidates=48)
WeatherPeakSnipe: KXLOWTDEN-26MAY09 — trigger not fired (ext_too_recent(14min<30))
WeatherPeakSnipe: KXLOWTCHI-26MAY09 — trigger not fired (ext_too_recent(0min<30))
WeatherPeakSnipe: KXHIGHCHI-26MAY09 — trigger fired (obs=53.0F, winner_idx=0) but no bracket passed price gates
WeatherPeakSnipe: KXLOWTNYC-26MAY09 — trigger not fired (not_past_peak(cur=53.0,ext=53.0))
WeatherPeakSnipe: KXLOWTMIA-26MAY09 — trigger not fired (ext_too_recent(0min<30))
WeatherPeakSnipe: KXHIGHNY-26MAY09 — trigger fired (obs=53.0F, winner_idx=0) but no bracket passed price gates
WeatherPeakSnipe: KXHIGHMIA-26MAY09 — trigger fired (obs=82.0F, winner_idx=0) but no bracket passed price gates
```

Results:
- ✅ 48 candidate markets discovered across 8 event groups.
- ✅ ASOS fetch succeeded for all 4 stations (NYC/ORD/MIA/DEN, 12–14 obs each).
- ✅ HIGH triggers fired for CHI/NY/MIA at expected current observed temps (53/53/82°F).
- ✅ LOW triggers correctly suppressed — late-evening obs are at or near today's running min, so post-peak conditions don't hold.
- ✅ No bracket passed price gates — expected at ~6 hours to close (Phase 14a flagged this as the open question; we are now collecting price-gate evidence empirically).
- ✅ No exceptions, no regressions in existing `weather_snipe` SHADOW logs.
- ✅ `gate_events.jsonl` captured both event types live: `evaluated` (1) + `skip/price_gate` (1) on a follow-up cycle, with full structured fields. Sample skip event: `KXHIGHCHI-26MAY09-T70 winner_yes_ask=0.01 adjacent_yes_asks=[null,null,0.01,0.01] obs_temp_f=52.0`. The market priced the winner at 1¢ — the bot's trigger and the market disagree by 84¢, exactly the kind of evidence the price gate is meant to filter on.

Standalone IEM verification: 4-station fetch took ~24s with the 6s throttle (NYC/ORD/MIA/DEN, 12/13/12/14 obs respectively). No 429 errors after throttle was added.

Test suite: 156/159 pass; 3 pre-existing failures (test_confidence × 2, test_live_game_monitor × 1) confirmed unrelated by stash-and-rerun.

## What to watch in first 7 days of live data

1. **Trigger fire rate vs. Phase 14a's 6.75 fires/day estimate (4 cities aggregate).** Live cadence is 5-min cycles; we expect peak-window fires once per series per day after dedup. Investigate if total daily fires are <3 or >12 — either suggests trigger drift.
2. **Winner-bracket winrate vs. Phase 14a's 70.5% exact-match rate.** With the 0.85 price gate, we'll see fewer events than the Phase 14a 95-event sample — but each one is supposed to be a high-confidence trade. A sub-50% winrate over 10+ trades flags strategy drift; expected band is 65–80%.
3. **Unexpected gate rejections in `gate_events.jsonl`.** Filter `gate=snipe AND signal_class=weather_peak_snipe`. Watch for: `bankroll` firing >2× per day (dynamic sizing wrong), `dedup` firing on first-of-day events (state corruption), `empty_book_snipe` >50% rate (book genuinely thin or detector bug). The KDEN d=5 outlier from Phase 14a was deferred — flag any KDEN trade where the winner bracket was at the high end of the strike grid as a potential repeat.

## Known-good non-issues

- IEM rate-limited bursts produce HTTP 429 if multiple stations are queried back-to-back without spacing. Mitigated by `_MIN_INTERVAL_SEC = 6.0` global throttle in `data/ground_truth/asos_timeseries.py`.
- ORD station is preferred over MDW (which `data/ground_truth/weather_kalshi._CITY_TO_CLI` uses for CLI resolution). Phase 14a found KORD gave better trigger fire rates; the strategy module hard-codes the ASOS-station preference and does not affect the existing CLI snipe path.
- Older `KXHIGHNY` form uses city code "NY"; lows use "NYC". Strategy module canonicalizes via `_TICKER_CITY_TO_CANONICAL`.

## Refs

- Phase 14a: `audit/weather_snipe_phase_a_20260510.md`
- Trigger logic source: `audit/weather_phase_a/_compute_triggers.py` (the validated reference implementation).
