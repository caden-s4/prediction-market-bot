# Phase 14a — Weather snipe historical sniff test

**Date:** 2026-05-10
**Mode:** read-only audit. No live bot code touched.
**Window:** 2026-01-15 → 2026-02-14 (31 days), of which 2026-01-31 → 2026-02-13 (14 days) overlap with the Kalshi `kalshi_markets.jsonl` snapshot.

## Context

Weather snipe is firing 199 events over the last 3-hour funnel and producing 0 signals (all `no_signal`). Before adding new trigger logic, we test the proposed thesis against history:

> Fire on weather bracket markets when (a) clock is ≥1 hour past the climatological peak hour for the city, (b) ASOS observations show monotonic decline ≥30 min (≤1°F bounce tolerance), (c) Kalshi market price is ≥85¢ for the winner-side bracket OR ≤15¢ for adjacent-±2 brackets. Trade winner bracket + ±2 adjacent brackets.

This audit answers: **would the trigger have been right?**

## Step 1 — City selection

Scanned all 1.56M rows of `kalshi_markets.jsonl`. Found **33 weather-prefix series**, of which 19 are tied at the top tier (78 markets / 13 events each). Volume and open-interest fields are zero in every snapshot row — the file is a one-time scrape, not a live tick log — so volume-based ranking among the tied series is not possible from this data. All 19 ties are equivalent on the metric available.

**Selected 4 cities** for diverse climates and reliable ASOS coverage:

| City | High series | Low series | ASOS station | Rationale |
|---|---|---|---|---|
| New York | KXHIGHNY | KXLOWTNYC | **KNYC** (Central Park) | standard Kalshi reference station |
| Chicago | KXHIGHCHI | KXLOWTCHI | **KORD** (O'Hare) | continental, reliable airport |
| Miami | KXHIGHMIA | KXLOWTMIA | **KMIA** (Miami Intl) | tropical contrast |
| Denver | KXHIGHDEN | KXLOWTDEN | **KDEN** (Denver Intl) | high-altitude continental |

Excluded LAX/SFO/SEA per spec note (marine layer / microclimate); excluded AUS/PHIL/NOLA/LV as climate-redundant with picks.

**Naming evolution:** older series use `KXHIGH<CITY>` for NY/CHI/MIA/DEN/AUS/LAX/PHIL; newer use `KXHIGHT<CITY>` for SFO/SEA/LV/NOLA/BOS/DC. Lows are uniformly `KXLOWT<CITY>`. Phase B will need to handle both forms.

Top-tier series with same 78/13 count, by name:

```
KXHIGHNY KXHIGHCHI KXHIGHMIA KXHIGHDEN KXHIGHAUS KXHIGHLAX KXHIGHPHIL
KXHIGHTNOLA KXHIGHTSFO KXHIGHTLV KXHIGHTSEA
KXLOWTNYC KXLOWTCHI KXLOWTMIA KXLOWTDEN KXLOWTAUS KXLOWTLAX KXLOWTPHIL
```

Smaller series: KXHIGHTDC (60), KXHIGHTATL/PHX/MIN (54), KXHIGHTBOS (48), KXHIGHTHOU/OKC/DAL/SATX (12).

## Step 2 — ASOS pull

Pulled 31 days of routine + special METAR temperature observations (`tmpf`, `report_type=3,4`) from IEM `mesonet.agron.iastate.edu`.

| Station | Total obs | Avg/day | Saved file |
|---|---|---|---|
| KNYC | 743 | 24.0 | asos_KNYC.csv |
| KORD | 1,036 | 33.4 | asos_KORD.csv |
| KMIA | 802 | 25.9 | asos_KMIA.csv |
| KDEN | 842 | 27.2 | asos_KDEN.csv |

**Cadence caveat:** IEM's basic ASOS endpoint returns hourly METARs (HH:51) plus specials, not the 5-min product. Trigger semantics in Step 3 were adapted to this cadence (see below).

## Step 3 — Trigger simulation

Empirical climatological peak hours derived from data (mode of hour-of-day-extremum):

| Station | High peak hour (local) | Low peak hour (local) |
|---|---|---|
| KNYC | 13 | 9 |
| KORD | 13 | 7 |
| KMIA | 13 | 7 |
| KDEN | 13 | 11 |

The KDEN low at 11:00 and KNYC low at 09:00 reflect winter cold-front patterns — the daily minimum often arrives well after sunrise as a cold front passes.

**Trigger logic (adapted to hourly cadence):**

1. Local clock-hour ≥ peak_hour + 1
2. Running max (high) / running min (low) was set ≥30 min ago
3. Current obs is below running extremum by > 1°F (clearly past peak)
4. Post-peak monotonicity: between running extremum and now, no upward bounce exceeding 1°F above the post-peak running minimum (and symmetric for lows)

The original spec calls for "≥30 min monotonic decline" with literal 5-min cadence. With hourly METARs, this becomes "extremum was set in a prior hour and no obs since has come back within 1°F bounce tolerance." Phase B with denser data could fire faster and validate fewer false-bounces.

**Fire rates (252 trigger checks across 4 stations × 2 kinds × 31 days):**

| Station | Fired | Total | Rate |
|---|---|---|---|
| KNYC | 55 | 62 | 88.7% |
| KORD | 55 | 64 | 85.9% |
| KMIA | 56 | 62 | 90.3% |
| KDEN | 50 | 64 | 78.1% |
| **AGG** | **216** | **252** | **85.7%** |

Non-firing days (14% aggregate) are days with no clear post-peak decline — typically late warming days where the daily extremum is set near midnight, leaving no post-peak observation window before the resolution boundary.

## Step 4 — Trigger accuracy (settled vs. trigger-time observation)

Per fired trigger: `delta = settled_extremum − observed_at_trigger`. Sign convention:
- Highs: `delta > 0` means the daily max was rallied AFTER the trigger fired (miscall — late rally).
- Lows: `delta < 0` means the daily min was set AFTER the trigger fired (miscall — late cooling).

| Station | exact (Δ=0) | within ±1°F | off >1°F |
|---|---|---|---|
| KNYC | 87.3% | 89.1% | 10.9% |
| KORD | 87.3% | 89.1% | 10.9% |
| KMIA | 91.1% | 91.1% | 8.9% |
| KDEN | 86.0% | 90.0% | 10.0% |
| **AGG** | **88.0%** | **89.8%** | **10.2%** |

**Aggregate delta histogram (signed °F, n=216):**

```
   <= -5 : ############ (12)
   -5..-3: ## (2)
   -3..-1: ##### (5)
    -1..0: #### (4)
        0: ############################################################# (190)
     0..1: 0
     1..3: ## (2)
     3..5: # (1)
```

**Stats:** mean signed Δ = −0.70°F, mean |Δ| = 0.77°F, p90 |Δ| = 2.0°F, max |Δ| = 17.0°F.

The tail is asymmetric on the negative side: late-day cold-front passages drove the daily low further down hours after the morning trigger had fired. Highs are clean — the late-day rally tail (2 events at 1–3°F, 1 at 3–5°F) is small. **Late cooling on lows is the primary failure mode.**

## Step 5 — Kalshi bracket cross-reference

For the 14-day Kalshi-overlap window, found 95 fired triggers with a matching Kalshi event (out of 96 attempts; 1 had no Kalshi event recorded). Computed the bracket index of `observed_at_trigger` (`pick_idx`) and the actual winning bracket (`winner_idx`). All Kalshi events have exactly 1 winner among 6 brackets.

**Exact-match (pick_idx == winner_idx):**

| Station | Match | Total | Rate |
|---|---|---|---|
| KNYC | 23 | 25 | **92.0%** |
| KORD | 15 | 25 | 60.0% |
| KMIA | 14 | 23 | 60.9% |
| KDEN | 15 | 22 | 68.2% |
| **AGG** | **67** | **95** | **70.5%** |

**Bracket-distance distribution `|pick_idx − winner_idx|`:**

| Station | n | d=0 | d=1 | d=2 | d=3 | d=4 | d=5 | in-band (≤2) |
|---|---|---|---|---|---|---|---|---|
| KNYC | 25 | 23 | 2 | 0 | 0 | 0 | 0 | 25/25 = **100.0%** |
| KORD | 25 | 15 | 8 | 2 | 0 | 0 | 0 | 25/25 = **100.0%** |
| KMIA | 23 | 14 | 7 | 0 | 2 | 0 | 0 | 21/23 = 91.3% |
| KDEN | 22 | 15 | 6 | 0 | 0 | 0 | 1 | 21/22 = 95.5% |
| **AGG** | **95** | **67** | **23** | **2** | **2** | **0** | **1** | **92/95 = 96.8%** |

**Headline:** the proposed trade band (winner pick + ±2 adjacents = 5 of 6 brackets) **contained the actual winning bracket 96.8% of the time.** Three whiffs:
- KMIA d=3 (×2): late-day temperature shifts in Miami.
- KDEN d=5 (×1): a single event where pick was at one end and winner at the opposite — likely a strong cold-front evening that flipped the entire distribution after the morning trigger.

## Step 6 — Price feasibility check

`candles_sample.jsonl` (207 markets) and `candles_targeted.jsonl` (360 markets) cover **zero weather brackets**. Distinct prefixes in candle data: `KXINXU`, `KXNASDAQ100U`, `KXNCAABMENTION`, `KXSOLE/D`, `KXXRPD`, `KXNBAMENTION`, `KXBTC/D`, `KXATPCHALLENGERMATCH`, `KXETH/D`, `KXCS2TOTALMAPS`, `KXNBATOTAL`, `KXNCAAMBTOTAL` — no `KXHIGH*` or `KXLOWT*`.

**This is a hard data gap.** The proposed price gate (≥85¢ winner / ≤15¢ adjacents) cannot be validated from existing data. Phase B will need to add forward-only continuous logging of weather-bracket prices to determine whether the gate fires realistically given live market dynamics.

## Step 7 — Frequency

| Station | Days | Fires | Fires/day |
|---|---|---|---|
| KNYC | 31 | 55 | 1.77 |
| KORD | 32 | 55 | 1.72 |
| KMIA | 31 | 56 | 1.81 |
| KDEN | 32 | 50 | 1.56 |
| **AGG** | **32** | **216** | **6.75 across 4 cities** |

If every fire results in the proposed 5-bracket trade (winner + ±2), upper bound is **~34 contracts/day across the 4-city set**. Real Phase B fire count will be lower because the price gate (Step 6, unvalidated) will filter most events.

## Step 8 — Verdict

**Trigger logic is sound (96.8% in-band, 70.5% exact match, ~1.7 fires/city/day) and the proposed 5-bracket trade structure tolerates the observed miss distribution comfortably. However, the price gate (≥85¢ winner / ≤15¢ adjacents) cannot be validated from existing candle data — weather brackets have zero candle coverage. Recommend Phase B with a logging-first sub-phase: deploy continuous price logging on weather brackets for 2–3 weeks before activating the trigger, to confirm the price gate fires realistically and to characterize the late-day-cold-front failure mode that drives the heavy negative tail on lows.**

## Caveats

- **Window is mid-winter (Jan–Feb).** Diurnal patterns differ in summer (later peaks, longer afternoons). Phase B logging needs to span more than one season.
- **Sample size for Step 5 is small** (95 cross-referenced triggers from a 14-day Kalshi snapshot). Aggregate stats will tighten with more data.
- **Hourly METAR cadence** in this audit. Real Phase B with denser ASOS or Kalshi-internal weather feeds would have finer trigger control and could likely improve over the 96.8% in-band figure.
- **The KDEN d=5 outlier** is concerning if it generalizes — a single-event whiff that puts the entire trade band on the wrong side. Phase B should track these and add a secondary safeguard (e.g., disallow trigger when a sharp synoptic-scale cold front is forecast).
- **Lows are riskier than highs.** The negative-Δ tail (12 events at ≤−5°F) is concentrated on lows. Phase B should consider gating low triggers more conservatively, or restricting initial deployment to highs only.

## Outputs

- `audit/weather_phase_a/weather_prefixes.csv` — all 33 weather prefixes by market count
- `audit/weather_phase_a/weather_examples.json` — sample rows per prefix
- `audit/weather_phase_a/asos_K{NYC,ORD,MIA,DEN}.csv` — 31-day ASOS pulls
- `audit/weather_phase_a/triggers_K{NYC,ORD,MIA,DEN}.csv` — per-day trigger sim per station
- `audit/weather_phase_a/triggers_summary.csv` — combined trigger sim across stations
- `audit/weather_phase_a/brackets.csv` — Kalshi bracket bounds for the 8 series × 13 dates
- `audit/weather_phase_a/accuracy_by_trigger.csv` — full per-trigger accuracy + bracket distance
- `audit/weather_phase_a/accuracy_summary.txt` — formatted accuracy summary
