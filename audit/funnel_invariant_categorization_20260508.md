# Phase 12 — gate_funnel invariant_violation categorization

Date: 2026-05-09
Scope: `scripts/gate_funnel.py` only.

## Problem

Pre-fix, all 6,191 `invariant_violation` events in the 2700m window collapsed
to a single `(none)` reason because the funnel script reads `e.get("reason")`
and Phase 11 invariants emit the reason code via the `decision` field
(`reason` is `null`).

Sample row (verbatim from `data/runtime/gate_events.jsonl`):

```
{"schema_version": 1, "ts": "2026-05-08T03:59:01.848Z",
 "ticker": "KXINX-26MAY08H1600-T6850",
 "gate": "invariant_violation",
 "decision": "ws_rest_mid_disagreement",
 "reason": null,
 "cycle_id": null, "platform": "kalshi",
 "extra": {"ws_mid": 0.010..., "rest_mid": 0.255, "delta": -0.245}}
```

Other gates (`scanner_reject`, etc.) follow the documented schema:
`decision` is the verb (`reject`/`accept`), `reason` is the descriptive code.
Only `invariant_violation` deviates.

## Fix

In the per-event aggregation loop, when `gate == "invariant_violation"`
fall back to `decision` if `reason` is null. One-gate carve-out, three lines
of code, no other behavior changed.

```python
if gate == "invariant_violation":
    reason = e.get("reason") or e.get("decision") or "(none)"
else:
    reason = e.get("reason") or "(none)"
```

No bot logic touched. No emit code touched. No threshold or gate changed.

## Verification — full funnel rerun (`--since 2700m`)

```
58 unparseable line(s) skipped

Gate funnel - last 2700m (2026-05-08T02:40:49Z to 2026-05-09T23:40:49Z)

scanner_reject                598,998 events
  category                            564,910 (94.3%)
  hours                               18,098 (3.0%)
  financial_bracket_disabled          15,990 (2.7%)

gt_routing                    16,186 events
  source_returned_none                14,178 (87.6%)
  source_not_tradeable                 2,008 (12.4%)

confidence                    28 events
  source_below_gate                       28 (100.0%)

executor_pretrade             287 events
  large_divergence_extreme_market        220 (76.7%)
  gt_stale_at_entry                       66 (23.0%)
  empty_book_ghost                         1 (0.3%)

snipe                         3,972 events
  no_signal                            3,931 (99.0%)
  dedup                                   27 (0.7%)
  bankroll                                14 (0.4%)

invariant_violation           6,192 events
  ws_rest_mid_disagreement             5,061 (81.7%)
  implausible_gap                      1,131 (18.3%)
```

invariant_violation is now broken into the Phase 11 reasons.
`kalshi_mid_out_of_range` count = 0 (range check is sane —
no out-of-band mids in this window).

Unparseable count = 58, unchanged from the earlier pull.

## Verification — per-ticker detail (`--gate invariant_violation --detail`)

```
58 unparseable line(s) skipped

Gate funnel - last 2700m (2026-05-08T02:41:09Z to 2026-05-09T23:41:09Z)

invariant_violation           6,192 events
  ws_rest_mid_disagreement             5,061 (81.7%)
    KXWTI-26MAY08-T92.99                                124x
    KXWTI-26MAY08-T97.99                                90x
    KXGOLDW-26MAY0817-T4713.99                          80x
    KXSILVERW-26MAY0817-T79.99                          78x
    KXWTI-26MAY08-T93.99                                76x
    ... and 1354 more ticker(s)
    sample extra: ws_mid=0.295, rest_mid=0.19999999999999998, delta=0.095
  implausible_gap                      1,131 (18.3%)
    KXMVECROSSCATEGORY-S20268E149EBD55B-6180587743F     296x
    KXMVECROSSCATEGORY-S20268E149EBD55B-6FC96D978D3     294x
    KXMVECROSSCATEGORY-S2026B1EAC50197B-96DDA2AFAE3     266x
    KXAAAGASD-26MAY09-4.520                             163x
    KXAAAGASD-26MAY09-4.510                             50x
    ... and 9 more ticker(s)
    sample extra: market_price=0.75, gt_prob=0.02, gap=0.73
```

## Reads

- **ws_rest_mid_disagreement** dominates (5,061 = 81.7%) and is widely
  diffuse: top ticker `KXWTI-26MAY08-T92.99` only 124x, long tail of
  1,354+ distinct tickers. Concentration is in **commodity bracket
  series** (KXWTI / KXGOLDW / KXSILVERW). Sample delta ~0.095 — WS and
  REST mids genuinely disagree at the ~10c level on these books.
- **implausible_gap** (1,131 = 18.3%) is concentrated: top 5 tickers
  account for 1,069 of 1,131 events; only 14 distinct tickers total.
  KXMVECROSSCATEGORY (584 combined) and KXAAAGASD (213+) dominate.
  Sample: market=0.75, gt=0.02, gap=0.73 — these are extreme gaps,
  consistent with the `large_divergence_extreme_market` rejections seen
  in `executor_pretrade` (220 events). Likely overlap between the two
  populations.
- **kalshi_mid_out_of_range**: 0 events. Range check is healthy — no
  mids landing outside [0,1].

## Files changed

- `scripts/gate_funnel.py` — 5-line carve-out in aggregation loop
- `audit/funnel_invariant_categorization_20260508.md` — this doc
