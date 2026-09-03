# Phase 7 — Yahoo CL=F Freshness Profile
**Sampler file:** `audit/yahoo_clf_samples.csv`
**Total rows:** 254  |  **Successful fetches:** 254
**Window:** 2026-05-07 22:56 EDT – 13:00 EDT

## Step 2 — Lag by ET hour

| ET hour bucket | N | min lag | median | p75 | p95 | max | <300s | <600s |
|---|---|---|---|---|---|---|---|---|
| 09:00-10:00 ET | 60 | 600s | 604s | 607s | 614s | 618s | 0% | 0% |
| 10:00-11:00 ET | 60 | 600s | 604s | 607s | 612s | 614s | 0% | 0% |
| 11:00-12:00 ET | 60 | 600s | 604s | 607s | 611s | 612s | 0% | 2% |
| 12:00-13:00 ET | 59 | 601s | 605s | 608s | 613s | 615s | 0% | 0% |
| 13:00-14:00 ET | 1 | 612s | 612s | 612s | 612s | 612s | 0% | 0% |
| 22:00-23:00 ET | 14 | 602s | 606s | 612s | 616s | 616s | 0% | 0% |
| **OVERALL** | 254 | 600s | 604s | 608s | 612s | 618s | 0% | 0% |

## Step 3 — Phase 0b KXWTI trade-hour cross-reference

Phase 0b KXWTI trades: 41

| entry_ts (ET) | ET hour | correct | median lag at hour | pass 300s gate? |
|---|---|---|---|---|
| 2026-03-22 18:17 | 18h | True | no data | unknown |
| 2026-03-22 18:19 | 18h | True | no data | unknown |
| 2026-03-23 19:27 | 19h | False | no data | unknown |
| 2026-03-23 19:38 | 19h | False | no data | unknown |
| 2026-03-24 14:40 | 14h | True | no data | unknown |
| 2026-03-24 14:45 | 14h | True | no data | unknown |
| 2026-03-24 14:47 | 14h | True | no data | unknown |
| 2026-03-24 14:51 | 14h | True | no data | unknown |
| 2026-03-25 14:44 | 14h | False | no data | unknown |
| 2026-03-25 14:49 | 14h | True | no data | unknown |
| 2026-03-26 14:38 | 14h | False | no data | unknown |
| 2026-03-26 14:42 | 14h | True | no data | unknown |
| 2026-03-28 19:06 | 19h | True | no data | unknown |
| 2026-03-28 20:48 | 20h | True | no data | unknown |
| 2026-03-28 20:50 | 20h | True | no data | unknown |
| 2026-03-28 21:18 | 21h | True | no data | unknown |
| 2026-03-29 17:19 | 17h | False | no data | unknown |
| 2026-03-29 19:54 | 19h | True | no data | unknown |
| 2026-03-29 23:12 | 23h | True | no data | unknown |
| 2026-03-30 01:08 | 01h | True | no data | unknown |
| 2026-03-31 22:43 | 22h | False | 606s | NO |
| 2026-04-01 02:10 | 02h | False | no data | unknown |
| 2026-04-01 02:36 | 02h | False | no data | unknown |
| 2026-04-01 21:07 | 21h | True | no data | unknown |
| 2026-04-01 21:17 | 21h | True | no data | unknown |
| 2026-04-01 21:34 | 21h | True | no data | unknown |
| 2026-04-02 00:42 | 00h | True | no data | unknown |
| 2026-04-02 07:27 | 07h | True | no data | unknown |
| 2026-04-02 16:33 | 16h | True | no data | unknown |
| 2026-04-02 18:44 | 18h | True | no data | unknown |
| 2026-04-04 15:32 | 15h | True | no data | unknown |
| 2026-04-04 17:23 | 17h | True | no data | unknown |
| 2026-04-06 02:39 | 02h | True | no data | unknown |
| 2026-04-06 03:47 | 03h | True | no data | unknown |
| 2026-04-06 04:53 | 04h | True | no data | unknown |
| 2026-04-07 18:51 | 18h | True | no data | unknown |
| 2026-04-07 19:33 | 19h | True | no data | unknown |
| 2026-04-07 23:37 | 23h | True | no data | unknown |
| 2026-04-07 23:40 | 23h | True | no data | unknown |
| 2026-04-08 16:30 | 16h | True | no data | unknown |
| 2026-04-08 18:16 | 18h | True | no data | unknown |

**Of 41 Phase 0b KXWTI trades: 0 would pass 300s gate (at median lag for that hour). 0 trades at hours with no sampler data.**

The 8 incorrect trades were at hours with median lag: 19h=no data, 19h=no data, 14h=no data, 14h=no data, 17h=no data, 22h=606s, 02h=no data, 02h=no data.

## Step 4 — Conclusion

Pit-hours (9am-1pm ET) median lag: 604s  |  <300s: 0% of 240 samples

**Verdict: (b) Yahoo is >300s stale even during pit hours.**
