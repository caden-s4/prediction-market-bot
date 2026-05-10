# Phase 13 — Disable Yahoo-routed financial brackets at scanner

Date: 2026-05-09
Scope: `resolution/scanner.py` — `_FINANCIAL_BRACKET_PREFIXES` only.

## Why

Phase 7 measured Yahoo CL=F at 604s median lag during pit hours, 0/240 samples
under 300s. Phase 0b validation numbers (CL=F 80.5%, NQ=F 98%) were measured
without a freshness gate and before the parser fix — not actionable. Yahoo
data is structurally too stale for bracket trading at any reasonable
freshness threshold. Disabling at the scanner short-circuits these markets
before any GT routing, signal, or invariant work fires on them.

This also eliminates most of the 5,061 `ws_rest_mid_disagreement`
invariant_violation events from Phase 12 (concentrated in KXWTI / KXGOLDW
brackets — see Phase 12 funnel detail).

## Prefixes added

Each was verified Yahoo-routed via `_INSTRUMENT_MAP` keyword matching in
`data/ground_truth/financial.py:142–192`, with the resolved Twelve Data
symbol present in `_TD_FREE_TIER_BLOCKED` (so on the free tier, fetches
fall through to Yahoo Finance with no other path).

| Prefix     | Yahoo symbol | Mapped via | TD blocked  |
|------------|-------------|----------------------|-----|
| `KXINX`    | `ES=F`      | `s&p / spx`          | yes (`SPX`) |
| `KXWTI`    | `CL=F`      | `wti / crude oil`    | yes (`CL1!`) |
| `KXGOLDW`  | `GC=F`      | `gold`               | yes (`GC`, `GC1!`) |
| `KXTNOTEW` | `^TNX`      | `10-year treasury / tnote` | yes (`TNX`) |

Pre-existing entries (unchanged): `KXNASDAQ100U`, `KXNASDAQ100`, `KXGOLDD`,
`KXTNOTED`.

### Notes

- **`KXWTI` covers both daily (`KXWTI-...`) and weekly (`KXWTIW-...`)** by
  prefix match. Both resolve against Yahoo CL=F and share the same
  staleness profile, so a single prefix is the right call here. The
  rollover-week exemption in `financial.py:582–590` becomes inactive for
  KXWTIW under this disable, which is consistent with the broader
  Yahoo-staleness intent (rollover safety doesn't fix stale quotes).
- **`KXSILVERW` was checked and NOT added.** Silver has no entry in
  `_INSTRUMENT_MAP`, so KXSILVER markets do not currently route to
  `FinancialDataSource` at all. Per "Do not disable any series that isn't
  explicitly Yahoo-routed for futures," it stays out. (Note: if a `silver`
  keyword is added later, KXSILVERW should be reconsidered.)
- **`KXBRENTD` / `KXBRENTW` remain blocked separately** in
  `FINANCIAL_EXCLUDED_SERIES` (`financial.py:236-237`) for a different
  reason (wrong-instrument misroute, not freshness). Comment at
  `scanner.py:101` explicitly forbids putting them in
  `_FINANCIAL_BRACKET_PREFIXES`. Untouched here.

## Verification

### Synthetic prefix-match test

Loaded the modified scanner module and exercised `startswith` on
representative IDs. All assertions pass:

```
DISABLE_FINANCIAL_BRACKETS = True
prefixes: ('KXNASDAQ100U', 'KXNASDAQ100', 'KXGOLDD', 'KXGOLDW',
           'KXTNOTED', 'KXTNOTEW', 'KXINX', 'KXWTI')

  disabled=True   KXINX-26MAY11H1600-T6850
  disabled=True   KXWTI-26MAY11-T92.99
  disabled=True   KXWTIW-26MAY15-T92.99       <- weekly also covered
  disabled=True   KXGOLDW-26MAY1517-T4713.99
  disabled=True   KXTNOTEW-26MAY15-T4.30
  disabled=True   KXNASDAQ100-26MAY11H1600-B26450    (regression check)
  disabled=True   KXNASDAQ100U-26MAY11H1600-T24399.99 (regression check)
  disabled=True   KXGOLDD-26MAY1117-T4713.99         (regression check)
  disabled=True   KXTNOTED-26MAY11-T4.30             (regression check)
  disabled=False  KXSILVERW-26MAY1517-T79.99         (not Yahoo-routed)
  disabled=False  KXBRENTD-26MAY11-T80.99            (handled elsewhere)
  disabled=False  KXNBAGAME-26MAY11LALBOS-LAL        (game market)
```

### Sanity-cycle reject count

Ran `python main.py --info` for ~75s on 2026-05-09 ~23:50 UTC (Saturday
after US market close). Funnel for the run window:

```
scanner_reject                16,643 events  (--since 60m)
  category                            16,132 (96.9%)
  hours                                  511 (3.1%)
```

`financial_bracket_disabled` count = **0** for the run window.
Reason: today is Saturday, ~7h after equity close. Latest
`financial_bracket_disabled` event in the JSONL is 2026-05-08T19:58 for
`KXNASDAQ100-26MAY08H1600-*`. Today's KXINX/KXWTI/KXGOLDW close brackets
already resolved earlier; Monday brackets aren't listed yet. The end-to-end
gate event verification will fire naturally on the next weekday session.

The synthetic startswith test above is sufficient evidence that the disable
list takes effect when matching markets do appear.

### mypy / ruff

mypy and ruff aren't installed in this Windows env (`No module named mypy`,
`No module named ruff`). Byte-compile via `py_compile.compile`: clean.
No new style or typing constructs introduced — only string entries appended
to an existing tuple.

## Files changed

- `resolution/scanner.py` — 4 new entries in `_FINANCIAL_BRACKET_PREFIXES`
- `audit/yahoo_disable_phase13.md` — this doc

## Future re-enable

Re-enable depends on a real-time GT source (Twelve Data $79/mo Grow plan or
equivalent) so freshness clears the executor gate. Twelve Data unblocks all
four symbols (SPX, CL1!, GC1!/GC, TNX) on the paid tier. Until then, leave
disabled. **Out of scope here.**
