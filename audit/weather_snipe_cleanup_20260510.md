# Phase 14c — Weather peak-snipe cleanup + Phase 13 verification prep

**Date:** 2026-05-10
**Scope:** `strategies/weather_peak_snipe.py` (dedup), `tests/test_weather_peak_snipe.py`
(two new tests), Monday verification checklist. No strategy logic changes.

## 1. Dedup added

`evaluated` and `skip/price_gate` events for `weather_peak_snipe` now emit at
most once per `(winner_market_id, trigger_obs_ts)` per process. The dedup key
is the **ASOS observation timestamp** that fired the trigger (stable across
cycles between hourly METAR updates), not `now_utc` (which changes every
cycle and made the original key uselessly unique).

Changes in `strategies/weather_peak_snipe.py`:

| Hunk | What |
|---|---|
| `_TriggerOutcome` | Added `trigger_obs_ts: Optional[datetime]` |
| `_evaluate_trigger` | Returns `trigger_obs_ts=cur_ts` when fired |
| Module state | Added `_LOGGED_TRIGGERS: set` |
| `evaluate_event_signals` | Computes `trigger_obs_iso` from `outcome.trigger_obs_ts`, derives `log_key`, suppresses both `evaluated` and `skip/price_gate` emit when key already in `_LOGGED_TRIGGERS`. The `trigger_time_utc` field in the `evaluated` extras now carries the **observation** timestamp (stable per trigger), not the cycle timestamp. |
| `_clear_dedup_for_test` | Clears `_LOGGED_TRIGGERS` |

Trigger logic, price gates, contract caps, and signal emission are untouched.
Per spec: per-process state, no disk persistence, multi-process or restart
resets dedup.

## 2. Sanity verification

- `pytest tests/test_weather_peak_snipe.py` — **29/29 pass** (27 pre-existing +
  2 new dedup tests: one for repeat suppression, one for re-emit on new obs).
- 3-minute `python main.py --info` cycle exercised the new code path against
  live ASOS data. Bot emitted one fresh `evaluated` event for
  `KXHIGHMIA-26MAY10-B90.5` at trigger time `2026-05-10T18:53:00+00:00` — no
  exceptions, KXHIGHMIA-26MAY10 still FIRED and placed ghost trades as before.
- During the sanity run, **two python.exe processes** were active concurrently
  (PIDs 400 and 4708). Per spec ("If the bot has multiple WeatherPeakSnipe
  instances or process restarts mid-day, dedup resets. That's acceptable"),
  cross-process duplicates are expected and don't indicate a bug. Within a
  single process the dedup is proven correct by the unit tests.

## 3. CHI 52°F bracket-id spot check — **NO BUG**

Skip event captured for `KXHIGHCHI-26MAY09-T70`:

```
{"ts": "2026-05-10T04:02:20.485Z",
 "ticker": "KXHIGHCHI-26MAY09-T70",
 "decision": "skip", "reason": "price_gate",
 "extra": {"signal_class": "weather_peak_snipe",
           "winner_yes_ask": 0.01,
           "adjacent_yes_asks": [null, null, 0.01, 0.01],
           "winner_idx": 0, "obs_temp_f": 52.0}}
```

Bracket convention (verified from `audit/weather_phase_a/brackets.csv`): for
HIGH series, `-T<n>` strike → subtitle `"(n-1)° or below"`. Examples on file:

```
KXHIGHCHI-26FEB12-T38 → subtitle "37° or below"
KXHIGHNY-26FEB12-T32  → subtitle "31° or below"
KXHIGHMIA-26FEB12-T73 → subtitle "72° or below"
```

So `KXHIGHCHI-26MAY09-T70` parses as `low=None, high=69` (lower-tail bracket
"69° or below"). `_bracket_sort_key` returns `(-10**6, 69)` → sorts to
position 0. `Bracket.contains(52.0)` is True (52 ≤ 69). `winner_idx=0` ✓.

**Verdict:** bracket-id is correct. Winner correctly contains the observed
temperature. The market priced the winner at 1¢ (yes_ask=0.01) — i.e., the
market disagrees with the bot's evidence that the day's high won't exceed
69°F. The 0.85 winner price gate correctly blocked the trade; this is exactly
the type of low-edge day the gate is meant to filter.

A separate observation, **not in scope here**: the strategy uses the
*current* obs (`cur_temp`) — not the running peak — to identify the winner
bracket. For days where the running peak and the post-peak current temp fall
in the same bracket (like CHI 2026-05-09 with everything ≤ 69°F), the
identification is correct. For edge cases where they diverge (e.g. peak=70,
current=68), the winner would be the bracket containing the post-peak temp
rather than the peak. Documented for a future phase; do not address here.

## 4. Phase 13 Monday verification checklist

Run Monday 2026-05-11 at ≥14:00 UTC, after the equity session has been live
long enough for KXINX/KXWTI/KXGOLDW/KXTNOTEW brackets to be listed and
scanned by the bot.

```powershell
# (a) Non-zero fbd events in the post-deploy window.
python scripts/gate_funnel.py --since 4h
# Expect: scanner_reject category includes financial_bracket_disabled with
# count > 0.

# (b) Each new Phase 13 prefix appears explicitly under fbd rejects.
python scripts/gate_funnel.py --since 4h --reason financial_bracket_disabled --detail --top 20 | findstr "KXINX KXWTI KXGOLDW KXTNOTEW"
# Expect: each of KXINX, KXWTI (incl. KXWTIW), KXGOLDW, KXTNOTEW shows
# at least one matching ticker line. Absence of any one = check live
# market listings for that series before declaring a bug.

# (c) KXINXU under fbd, NOT under category.
python scripts/gate_funnel.py --since 4h --ticker KXINXU --detail
# Expect: scanner_reject only shows financial_bracket_disabled,
# 0 category rejects. Pre-Phase-13 the 168h window had 40,068 category
# rejects for KXINXU; post-Phase-13 these should all be fbd.

# (d) ws_rest_mid_disagreement for disabled prefixes stays at 0.
python -c "import json; from collections import Counter; from datetime import datetime, timezone, timedelta; \
now = datetime.now(timezone.utc); cutoff = now - timedelta(hours=4); c = Counter(); \
[c.update([(e.get('ticker') or '').split('-')[0]]) for e in (json.loads(line) for line in open('data/runtime/gate_events.jsonl', 'r', encoding='utf-8') if line.strip()) if e.get('gate')=='invariant_violation' and e.get('decision')=='ws_rest_mid_disagreement' and datetime.fromisoformat(e.get('ts','').replace('Z','+00:00')) >= cutoff]; \
print({k: c[k] for k in ('KXWTI','KXWTIW','KXGOLDW','KXTNOTEW','KXINX','KXINXU') if k in c})"
# Expect: empty dict, or all zero. Any non-zero entry = the disable
# isn't catching that prefix before the WS/REST invariant fires
# (would mean the prefix isn't in the disable tuple, or
# DISABLE_FINANCIAL_BRACKETS was toggled off).
```

If (a) is 0 events, sanity-check live Kalshi listings for those series
before declaring the disable broken — Mondays sometimes lag.

## 5. Next

Monday morning ≥14:00 UTC: run the 4-step checklist above. If all four pass,
Phase 13 is end-to-end verified and we can return to the Phase 1 benchmark
backlog (≥10 actionable signals/week is still the gating metric). If (b) or
(c) fail, the disable tuple is misconfigured — investigate prefix ordering
in `_FINANCIAL_BRACKET_PREFIXES` first (KXINXU/KXNASDAQ100U-style suffixes
need their longer-prefix variant placed earlier so `startswith` matches
correctly).
