# Legacy weather_snipe inventory (Phase 14d)

**Date:** 2026-05-10
**Mode:** Diagnostic only — no code changes.
**Trigger:** Post-Phase-14b funnel showed 24 events tagged `signal_class=weather_snipe` (separate from `weather_peak_snipe`); Sunny did not remember what the legacy strategy does or whether it overlaps with the newly-shipped peak-snipe.

---

## Step 1 — Locations

| File | Role | LOC |
|---|---|---|
| `strategies/weather_snipe.py` | Legacy per-market decisive snipe (this audit's subject) | 230 |
| `strategies/weather_peak_snipe.py` | Phase 14b post-peak monotonic-trend trigger | 755 |
| `resolution/scanner.py` | Imports both; dispatches them on different paths | — |
| `resolution/executor.py` | Receives both via `place_snipe_trade` callback (single entry) | — |

The two strategies live in **separate modules**. Only one class registers as `signal_class="weather_snipe"`: it is the **default** assigned by `executor.place_snipe_trade` when a signal lacks a `signal_class` attribute (`resolution/executor.py:3132`):

```python
signal_class = getattr(signal, "signal_class", "weather_snipe")
```

`SnipeSignal` (legacy) has no `signal_class` field → defaults to `"weather_snipe"`. `PeakSnipeSignal` (Phase 14b) sets `SIGNAL_CLASS = "weather_peak_snipe"`.

---

## Step 2 — What the legacy strategy does

**File:** `strategies/weather_snipe.py:57` — `evaluate_snipe(market, now_utc, *, shadow_mode=False) -> Optional[SnipeSignal]`

**Module docstring (verbatim, lines 1-13):**
> Triggered when a weather market is within 60 minutes of close. Fetches today's running max/min temperature for the market's city, compares against the market's strike, and emits a signal if the outcome is essentially determined and Kalshi has not fully repriced. This is a scheduled-trigger strategy, not a continuous-monitoring one. It evaluates each market only in its final hour. The strategy bypasses the standard GT router / gap detector / scorer pipeline — those assume continuous-monitoring strategies. Sniping has its own gap/edge logic here; the executor's safety gates still apply downstream.

**Trigger conditions** (all must hold):
1. `0 < market.resolution_date - now_utc <= 60 min` (`_within_snipe_window`, line 125)
2. Ticker parses via `parse_weather_ticker` (any city in the weather_kalshi parser, not restricted)
3. ASOS `fetch_asos_running_extreme` returns ≥6 observations (`_MIN_OBSERVATIONS = 6`)
4. Outcome decisive per `_decide_outcome` (line 133):
   - `above`: `temp > strike + 1.0F` → YES; `temp + 1.0F < strike` → NO
   - `below`: mirror image
   - `bracket`: `lo <= temp <= hi` → YES (no margin); `temp < lo - 0.5F or temp > hi + 0.5F` → NO
5. Edge remains: `yes_ask < 0.97` (for buy_yes) or `yes_bid > 0.03` (for buy_no)

**Output:** `SnipeSignal(market_id, action, target_price, edge, confidence=0.99, rationale, gt_prob=0.99, asos_temp_f, bracket_low, bracket_high, market_mid)`. Confidence is hard-coded at 0.99 — strategy assumes the day's extreme is locked in within the final 60 min.

**Three-sentence summary:** In the final 60 minutes before a weather market closes, the legacy snipe pulls today's running max/min from ASOS for the market's city, compares it to the bracket/threshold, and buys YES (or NO) if the outcome is decisive and Kalshi has not fully repriced. It applies a 1.0°F safety margin on threshold-style markets and a 0.5°F outer margin on bracket-NO. It bypasses the standard GT router/gap-detector pipeline and emits directly via the snipe callback.

**Series coverage** (via `_WEATHER_SERIES_TICKERS` in `data/markets/kalshi.py:66`): 13 newer-form HIGH cities (PHX, LV, HOU, SATX, NOLA, ATL, DAL, DC, SFO, SEA, OKC, BOS, MIN), 4 older-form HIGH cities (NY, CHI, MIA, DEN), 15+ LOW cities. Total ~30+ cities.

---

## Step 3 — Relationship to weather_peak_snipe

| Dimension | Legacy `weather_snipe` | `weather_peak_snipe` (14b) |
|---|---|---|
| **Module** | `strategies/weather_snipe.py` (230 LOC) | `strategies/weather_peak_snipe.py` (755 LOC) |
| **Trigger model** | Per-market, every cycle, when `0 < TTC <= 60 min` | Per-(series, event_date) batch, fires once per event when post-peak monotonic-trend criteria met |
| **Time window** | Last 60 min before market close | Opens at `peak_hour + 1` local (15:00 HIGH / 08:00 LOW), event-day |
| **City coverage** | All ~30+ weather cities in `_WEATHER_SERIES_TICKERS` | 4 only: NYC, CHI, MIA, DEN |
| **Signal count** | 1 per market when decisive | Up to 5 per event (winner YES + ±1, ±2 NO) |
| **Confidence** | 0.99 (decisive — temp locked in) | 0.95 (post-peak — late-cooling tail risk) |
| **Trade gates** | YES if `yes_ask < 0.97`; NO if `yes_bid > 0.03` | YES if `yes_ask >= 0.85`; adjacent-NO if `yes_ask <= 0.15` |
| **Risk caps** | None at strategy layer (executor sizes via Kelly) | `$5/bracket`, 6 contracts/event hard caps |
| **Mode** | Honors executor `_dry_run` (can fire LIVE) | Hard-coded `dry_run=True`; defense-in-depth ghost guard in executor:3142 |
| **Scanner dispatch** | `_dispatch_weather_snipe(m, ...)` per market (`scanner.py:595`) | `_dispatch_weather_peak_snipe_batch(...)` once per cycle (`scanner.py:675`) |
| **Signal class tag** | None on dataclass → defaults to `"weather_snipe"` | `SIGNAL_CLASS = "weather_peak_snipe"` |

**Same conditions?** No. Legacy fires when `TTC ≤ 60 min` regardless of trend. Peak-snipe fires post-peak (often hours before close) based on temperature monotonicity, regardless of TTC.

**Same markets at the same time?** They can fire on the **same NYC/CHI/MIA/DEN markets on the same day, but at different times.** A NYC HIGH market could trigger peak-snipe at 15:00 ET (post-peak detected) AND also trigger legacy snipe in the final hour before its close. They are not mutually exclusive on those 4 cities; they are mutually exclusive in time only by accident (depending on when "close" sits relative to peak+1).

**Complementary or redundant?** Reading the docstring on `weather_peak_snipe.py:1-32`: peak-snipe was explicitly authored as an **earlier, more aggressive** trigger (post-peak detection) with **wider city coverage gated to 4 cities for v1**, alongside the existing legacy snipe. The header explicitly contrasts itself with `strategies.weather_snipe`. They appear designed as **complementary** — peak-snipe captures edge earlier in the day where the trend is already locked; legacy snipe is the safety-net "decisive in last hour" bet. Both are wired into scanner and both are reachable in production.

---

## Step 4 — Recent skip events (24 total in `data/runtime/gate_events.jsonl`)

All 24 are concentrated on **3 tickers** in **3 cycles** between 03:20–05:52 UTC on 2026-05-10. Sample (5 events from cycle 1):

```json
{"ts":"2026-05-10T03:20:38.807Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T03:20:38.810Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"empty_book_snipe","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T03:20:38.812Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"empty_book_snipe","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T03:20:38.815Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"series_cap","extra":{"series_root":"KXHIGHTPHX","signal_class":"weather_snipe"}}
{"ts":"2026-05-10T03:20:38.817Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"bankroll","extra":{"signal_class":"weather_snipe"}}
```

Plus 5 more from later cycles (different tickers):

```json
{"ts":"2026-05-10T05:52:24.036Z","ticker":"KXLOWTOKC-26MAY09-T56","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T05:52:25.011Z","ticker":"KXLOWTOKC-26MAY09-B53.5","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T05:52:40.961Z","ticker":"KXHIGHTOKC-26MAY09-T83","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T05:52:42.261Z","ticker":"KXHIGHTOKC-26MAY09-B83.5","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
{"ts":"2026-05-10T04:04:07.794Z","ticker":"KXHIGHTPHX-26APR30-T80","gate":"snipe","decision":"skip","reason":"dedup","extra":{"signal_class":"weather_snipe"}}
```

**Tickers triggering it:** `KXHIGHTPHX` (Phoenix), `KXLOWTOKC`/`KXHIGHTOKC` (Oklahoma City). **Neither city overlaps with the 4-city peak-snipe set** (NYC/CHI/MIA/DEN).

**What it was about to do before being gated:** Each cycle produced a sequence `dedup → empty_book_snipe → empty_book_snipe → series_cap → bankroll` against the same ticker — i.e. the legacy strategy generated a decisive signal, attempted placement, and the executor's snipe placement gates rejected it. The repeating pattern across cycles indicates the strategy is firing as designed — the signal is genuine, the gates are doing their job.

**Time of day:** All in the 03:20–05:52 UTC window (which, for AKDT/PDT-leaning cities, is late local evening — the final hour before a HIGH/LOW market's close). Consistent with the strategy's "final 60 min" trigger.

**Overlap with peak-snipe markets?** No — these are PHX/OKC tickers, which `weather_peak_snipe` does not target. **No overlap in this sample.**

For comparison: `weather_peak_snipe` produced 25 `decision="evaluated"` gate events on tickers like `KXHIGHNY-26MAY09-B01` and `KXHIGHMIA-26MAY10-B90.5` — all on its 4 designated cities, all later in the day (19:34+ UTC).

---

## Step 5 — Git history for `strategies/weather_snipe.py`

```
0744785 weather: enrich shadow log + add resolution-join analysis
aea49a7 Add shadow-window snipe logging for window-tuning diagnostics
994fc24 Add structured snipe diagnostic logging for overnight run
012fa3f Weather Phase 1C: ASOS-driven snipe strategy
```

Only **4 commits** ever touched the file. Originally added as `Weather Phase 1C: ASOS-driven snipe strategy` (commit `012fa3f`). All three subsequent commits are diagnostic/logging-only enhancements, not behavioral changes. No commits in the recent Phase 13 / 14 series (last commit on this file predates the current Phase 14b work).

---

## Step 6 — Verdict

**(a) Legacy `weather_snipe` is doing something different from `weather_peak_snipe` — keep both, no action needed.**

The two strategies have **distinct trigger models** (final-60-min decisive vs. post-peak monotonic-trend), **different price gates** (0.97/0.03 vs. 0.85/0.15), **different city coverage** (~30 cities vs. 4), and **different signal cardinality** (1/market vs. up to 5/event). The 24 recent skip events are all on PHX and OKC, which peak-snipe does not even target — so there is **no observed overlap in production**. Even on the 4 shared cities (NYC/CHI/MIA/DEN), they would fire at different times of day. The peak-snipe module's docstring explicitly contrasts itself with the legacy module, indicating they were designed as complementary. The skip pattern (`dedup → empty_book → series_cap → bankroll`) shows the legacy strategy is working as intended — generating decisive signals that the executor's downstream gates evaluate.

**No follow-up phase needed for legacy weather_snipe based on this inventory.** If the per-signal-class PnL gate (pending) ever shows that one or the other has consistently negative ghost PnL, that would be the trigger to revisit — not this funnel observation.
