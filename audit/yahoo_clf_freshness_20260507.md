# Phase 6 — Yahoo CL=F Freshness Verification
**Date:** 2026-05-07 (run at 2026-05-08T02:49 UTC)
**Mode:** Diagnostic only. No code changes.

---

## Step 1 — Direct Yahoo fetch (no cache)

```
Now (UTC):                  2026-05-08T02:49:44+00:00
Last price:                 96.44
Last 1-min bar (UTC):       2026-05-08T02:39:00+00:00
Staleness of last bar:      644.9s
info.regularMarketTime:     1778207980
regularMarketTime (UTC):    2026-05-08T02:39:40+00:00
regularMarketTime (ET):     2026-05-07T22:39:40-04:00
regularMarketTime staleness: 604.9s
info.regularMarketPrice:    96.42
```

Yahoo's own `regularMarketTime` field, returned in a live HTTP response with no caching
intermediary, reports a timestamp **604.9 seconds (10m 5s) in the past**. The 1-minute
bar history confirms the same lag: last bar at 22:39 ET, fetch at 22:49 ET.

---

## Step 2 — Bot's cached value

**Cache location:** `data/ground_truth/financial.py`, module-level `_PRICE_CACHE` dict  
**Cache TTL:** `_CACHE_TTL = 60s` (success), `_FAILURE_CACHE_TTL = 30s` (failure)

The cache stores a 4-tuple: `(fetched_at, price, source_key, quote_ts)`

- `fetched_at` = `time.monotonic()` at fetch time → at most 60s old (working correctly)
- `quote_ts` = `datetime.fromtimestamp(regularMarketTime, tz=UTC)` → inherits Yahoo's lag

The bot's OWN cache is never more than 60s stale (correct behavior — it refetches on every
cycle). But each refetch stores a `quote_ts` that is already ~605s behind real time, because
Yahoo's `regularMarketTime` field is itself ~10 minutes old.

**Freshness gate:** `GT_FRESHNESS_SECONDS = 300` (loosened from 60 in a prior diagnostic).  
`gt.gt_age_seconds()` computes `now - quote_ts`. Every fresh Yahoo pull produces a `quote_ts`
that is ~605s old → gate fires on every cycle regardless of how often we refetch.

---

## Step 3 — Comparison

**Outcome: (a) — Yahoo is genuinely lagged.**

The bot's 60s TTL cache is working correctly. The problem is upstream: Yahoo Finance delivers
CL=F futures data with an inherent ~10-minute delay (604–645s in this measurement). Every
time the bot refetches (every 60s), Yahoo hands back data whose `regularMarketTime` is
already ~10 minutes old. There is no third-layer caching issue.

The chain:
```
Yahoo API → regularMarketTime = 22:39:40 ET (604s old at fetch time)
Bot cache  → quote_ts = datetime(22:39:40 UTC)      [stored correctly]
Executor   → gt_age_seconds() = now - quote_ts = ~605s
Gate       → GT_FRESHNESS_SECONDS = 300 → is_fresh() = False → BLOCKED
```

---

## Step 4 — Market hours check

```
Now (ET):        2026-05-07T22:50 (Thursday night)
CL=F trading:    YES — overnight session (Sun 6pm – Fri 5pm ET, break 5pm–6pm ET)
In break:        No
Saturday closed: No
Sunday pre-open: No
```

CL=F is actively trading in the CME Globex overnight session. The ~10-minute Yahoo lag is
NOT caused by being outside trading hours. Yahoo Finance simply applies a ~10-minute delay
to futures quotes regardless of whether the market is open.

---

## Step 5 — Conclusion

**Outcome (a) is confirmed.** Yahoo Finance serves CL=F futures data with an inherent
~10-minute delay (measured at 604–645s on a live weeknight overnight session). The bot's
60s refetch cycle and `_PRICE_CACHE` are working correctly — each refetch pulls the "latest"
Yahoo data, but that data is already ~600s old before it enters the cache. The
`GT_FRESHNESS_SECONDS = 300` threshold was already loosened from 60s for this reason but
remains too tight for Yahoo's futures latency floor.

**Next phase should investigate:** whether to raise `GT_FRESHNESS_SECONDS` to 900s for
Yahoo-sourced futures signals specifically (since Yahoo's delay is structural, not a
transient outage), or to wire in a real-time futures source (Twelve Data paid tier) for
CL=F so that `quote_ts` reflects actual trade time. Raising the threshold across the board
risks accepting stale data for other sources (FRED, sports) that correctly use
`data_published_at=None` and are already gated separately.

---

## Verification gates

- [x] Yahoo direct fetch output pasted with timestamps
- [x] Bot's cached value described with structure and timestamp origin
- [x] Comparison resolves to (a) explicitly — Yahoo lag, not our cache
- [x] Market hours check done — CL=F actively trading, lag is structural
- [x] No source file modified
- [x] No fix proposed
