# Phase 7 — Yahoo CL=F Freshness Profile
**Status: PENDING PIT-HOUR DATA** — sampler scheduled for 2026-05-08 06:00 local (09:00 ET)
**Mode:** Diagnostic only. No code changes.

---

## Setup

**Sampler:** `scripts/scratch/yahoo_clf_sampler.py` — fetches `regularMarketTime` every 60s
**Analysis:** `scripts/scratch/analyze_clf_samples.py` — produces table + Phase 0b cross-reference
**Output CSV:** `audit/yahoo_clf_samples.csv` (append mode; survives restarts)
**Task Scheduler entry:** `YahooCLFSampler_Phase7` — fires 2026-05-08 06:00 local (09:00 ET),
runs for 240 min (10:00 local / 13:00 ET). Logon required.

---

## Overnight baseline (2026-05-07 22:56–22:59 ET, 14 samples)

These are samples from the test runs during script setup. Overnight session, low volume.

| ET hour bucket | N | min lag | median | p75 | p95 | max | <300s | <600s |
|---|---|---|---|---|---|---|---|---|
| 22:00–23:00 ET | 14 | 602s | 606s | 612s | 616s | 616s | 0% | 0% |

**Interpretation:** Yahoo consistently delivers CL=F data ~10 minutes behind wall-clock during
the overnight session (10pm ET). Zero samples cleared the 300s gate, zero cleared 600s.
This aligns with Phase 6's single-point measurement of 605s at 22:49 ET.

---

## Step 2 — Pit-hour lag table (PENDING)

To be populated after sampler run on 2026-05-08 09:00–13:00 ET.

Run the analysis script after 1pm ET:
```
python scripts/scratch/analyze_clf_samples.py > audit/yahoo_clf_freshness_profile_20260507.md
```

Expected columns: ET hour | N | min | median | p75 | p95 | max | <300s | <600s

---

## Step 3 — Phase 0b KXWTI cross-reference (PENDING)

41 KXWTI trades in Phase 0b. Hour distribution:
- 14h ET (2pm): 6 trades (all daytime, highest liquidity)
- 18h ET (6pm): 5 trades (post-pit, electronic)
- 19h ET (7pm): 3 trades (electronic)
- 20h ET (8pm): 2 trades
- 21h ET (9pm): 1 trade
- 23h ET (11pm): 1 trade
- 01h ET (1am): 1 trade
- 02h ET (2am): 3 trades
- 03h–07h ET: 3 trades
- 15h–17h ET: 3 trades
- 22h ET: 1 trade (this hour sampled: median 606s → would NOT pass 300s gate)

Cross-reference pending pit-hour sampling. The 22h trade already confirmed blocked.

---

## Step 4 — Conclusion (PENDING)

Will be resolved to (a), (b), or (c) after pit-hour data is collected:

- **(a)** Yahoo serves CL=F fresh enough during pit hours to clear 300s → per-source/hour-aware gating
- **(b)** Yahoo is always >300s stale, even during pit hours → real-time source or kill CL=F
- **(c)** Yahoo borderline during pit hours (median 200–450s) → wider gate or off-hours filter

---

## Verification gates

- [x] Sampler built and tested (14 overnight samples confirm CSV format correct)
- [x] Task Scheduler entry created for 2026-05-08 06:00 local (09:00 ET)
- [ ] Sampler ran ≥4 hours covering pit hours — **PENDING (fires 06:00 local tomorrow)**
- [ ] Hour-bucketed lag table populated
- [ ] Phase 0b trade hours cross-referenced
- [ ] Conclusion identifies (a), (b), or (c) explicitly
- [x] No source files modified
- [x] No fix proposed
- [x] `audit/yahoo_clf_samples.csv` saved
