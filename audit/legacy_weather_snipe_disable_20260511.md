# Phase 15e — Disable legacy weather_snipe at scanner

**Date:** 2026-05-11
**Mode:** Config change. No logic edits. Bot remained DOWN throughout. No restart.

## Evidence summary (cumulative across Phase 15b + 15b-bis)

| Metric | Value |
|---|---|
| Legacy weather_snipe positions settled | 27 |
| Wins | 1 |
| Losses | 26 |
| Win rate | 3.7% |
| Realized P&L | −$1,521.19 |

Strategy fires paired bets at near-max premium on both sides of a bracket
structure, guaranteeing losses on most resolutions. Paired-bet logic itself
was not investigated (out of scope for this disable). No edge thesis remains
intact.

## Change

`DISABLE_LEGACY_WEATHER_SNIPE` flag gates the per-market dispatch call inside
the scanner's kalshi scan loop. When True, the dispatcher is skipped and a
`scanner_reject / legacy_weather_snipe_disabled` gate event is emitted instead
— but only for markets that would have actually entered the real or shadow
dispatch window, to keep the funnel signal-bearing rather than logging every
kalshi market.

### Files

- `resolution/scanner.py:125` — `DISABLE_LEGACY_WEATHER_SNIPE: bool = True` constant
- `resolution/scanner.py:606-619` — dispatch-site gate at the scan loop
- `monitoring/gate_names.py:18` — `REASON_LEGACY_WEATHER_SNIPE_DISABLED` constant

### Pattern

Matches the existing `DISABLE_FINANCIAL_BRACKETS` template (scanner.py:108,
Phase 13). One-flag gate at the scanner; `scanner_reject` gate event for
funnel visibility post-restart.

## weather_peak_snipe unaffected

Phase 14b (`strategies/weather_peak_snipe.py`) is a separate dispatcher:

- Legacy dispatch: `_dispatch_weather_snipe()` (scanner.py:246) — gated by flag
- Peak dispatch: `_dispatch_weather_peak_snipe_batch()` (scanner.py:171) — unchanged

Peak candidates are collected via `is_peak_snipe_candidate(m.market_id)` on
line 615, which sits OUTSIDE the `DISABLE_LEGACY_WEATHER_SNIPE` if/else
branch, and flushed via batch dispatch at line 691. Confirmed:

- `is_peak_snipe_candidate` targets `KXHIGHNY / KXHIGHCHI / KXHIGHMIA /
  KXHIGHDEN / KXLOWTNYC / KXLOWTCHI / KXLOWTMIA / KXLOWTDEN` only
- `_dispatch_weather_snipe` uses `_WEATHER_SERIES_TICKERS` (all ~30+ cities)
- The two code paths share no state

`tests/test_weather_peak_snipe.py` (29 tests) passes unchanged.

## Test results

```
tests/test_weather_snipe.py ........................ 17 passed
tests/test_weather_peak_snipe.py ..................  29 passed
tests/test_scanner_weather_snipe_dispatch.py .......  18 passed
============================= 64 passed in 0.19s ==============================
```

`_dispatch_weather_snipe` is still imported and works when called directly —
the disable is at the caller, so unit tests of the dispatcher itself remain
valid. No test edits were needed.

## Expected funnel signal after restart

Post-restart, the gate funnel should show a new `scanner_reject` reason:

```
scanner_reject / legacy_weather_snipe_disabled    N events
```

Count scales with how many weather markets sit in the 0-240 min window each
cycle (real + shadow). Compare to pre-disable funnel runs where the strategy
produced `[SIGNAL]` logs and live placements instead.

## Re-enable path

If the strategy is ever re-validated:

1. Diagnose the paired-bet bug — why both `buy_yes` and `buy_no` fire on the
   same bracket at near-max premium. Likely in `_decide_outcome` or
   `evaluate_snipe`'s observation aggregation.
2. Re-validate the edge thesis. The legacy strategy assumed ASOS observations
   inside the bracket window would already imply YES; the settlement data
   shows that assumption is wrong in practice.
3. Flip `DISABLE_LEGACY_WEATHER_SNIPE = False`.
4. Run a short shadow-only window first to confirm signal direction matches
   resolution direction before re-enabling live placement.

## Refs

- Phase 14d — `audit/legacy_weather_snipe_inventory_20260510.md` (strategy inventory)
- Phase 15b — `audit/settle_stuck_applied_20260511.md` (19 stuck positions settled)
- Phase 15b-bis — `audit/settle_stuck_8_applied_20260511.md` (8 additional positions settled)
- Phase 13 — `audit/yahoo_disable_phase13.md` (precedent disable pattern)
