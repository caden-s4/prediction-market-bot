# Sports Loss Attribution — Diagnostic Report

Data: `data/runtime/ghost_trades.jsonl` as of 2026-05-11
Method: per-entry pairing (each entry → first exit on that market after entry ts)
No source code modified. No fix proposed.

---

## Section 1 — Data quality sanity check

**Sports prefixes touched:** KXNBAGAME (340 records), KXNCAAWBGAME (230),
KXNCAAMBGAME (53), KXMLBSTGAME (1)

| Bucket                            | Count |
|-----------------------------------|------:|
| Sports entry records              |   278 |
| Sports exit records               |   244 |
| Distinct sports market_ids        |    74 |
| Strict clean pairs (1e, 1x)       |    17 |
| Per-entry trade rows              |   245 |
| Markets with multi entries        |    29 |
| Markets with multi exits          |    28 |
| Orphan exits (no entry)           |     0 |
| Open entries (no exit yet)        |    33 |

Per-entry pairing (N=245) matches the user's "~243 paired sports trades" figure.

### **Critical reframing of the "95% loss rate" headline**

The headline is misleading. Decomposing the 245 trade rows by pnl sign:

| pnl bucket                         | Count | %      |
|------------------------------------|------:|-------:|
| pnl > 0 (true wins)                |     5 |  2.0%  |
| pnl < 0 (true losses)              |     7 |  2.9%  |
| **pnl == 0 (unfilled_timeout)**    | **233** | **95.1%** |

**Among trades that actually FILLED (pnl != 0):**
- N = 12
- Wins = 5, Losses = 7
- Fill-only WR = **41.7 %**

The "~2% win rate" arithmetic counts the 233 unfilled timeouts as non-wins.
Sports is not losing 95% of trades — it is generating signals that
**never fill** 95% of the time, then losing 7 of the 12 that do fill.

Sample size for the actual loss attribution is **n=7**, which is statistically
thin. Treat the percentages below as a fact pattern, not a confidence interval.

---

## Section 2 — Per-source breakdown (per-entry pairing, N=245)

| source                                          |   N | wins | WR%   | sum_pnl | mean_pnl |
|-------------------------------------------------|----:|-----:|------:|--------:|---------:|
| ESPN/basketball/nba                             |  98 |    0 |  0.0% |  -273.86 |   -2.79 |
| ESPN/basketball/womens-college-basketball       |  83 |    0 |  0.0% |     0.00 |    0.00 |
| ResolutionDetector/ConfirmedFinal               |  51 |    0 |  0.0% |     0.00 |    0.00 |
| SportsLiveSource/Shock                          |  12 |    4 | 33.3% | 53222.32 |  4435.19 |
| SportsLiveSource/LateGame                       |   1 |    1 |100.0% |  4435.93 |  4435.93 |

Exit-reason distribution per source:
- `ESPN/basketball/nba`: 93 unfilled_timeout, 5 game_final (the 5 losses)
- `ESPN/basketball/womens-college-basketball`: 83 unfilled_timeout (0 ever filled)
- `ResolutionDetector/ConfirmedFinal`: 51 unfilled_timeout (0 ever filled)
- `SportsLiveSource/Shock`: 7 game_final (4W/2L+1?), 5 unfilled_timeout
- `SportsLiveSource/LateGame`: 1 game_final (1W)

**Observation A:** Two sources (`ResolutionDetector/ConfirmedFinal` and
`ESPN/basketball/womens-college-basketball`) account for 134/245 entries (55%)
and never produced a single filled trade. They are pure noise on the system —
generating signals that are stale by the time the order is placed.

**Observation B:** `SportsLiveSource/Shock` is the only meaningful winner.

---

## Section 3 — Wrong-side test on losing fills

Settlement side derived from ghost trade `exit_price`:
- `exit_price == 1.0` → YES side resolved (YES team won)
- `exit_price == 0.0` → NO side resolved (NO team won)

`kalshi.get_market()` could not be invoked for independent verification —
`KalshiClient.__init__` requires api_key/api_secret. Recommend a separate task
to wire a read-only verification path; until then, exit_price is the only
settlement signal we have. (Note: at game settlement, Kalshi snaps YES/NO
to 1.0/0.0 so this is essentially authoritative for ghost mode.)

### Classification of the 7 losing fills

| Label                  | Count | %     |
|------------------------|------:|------:|
| **INVERTED**           |   **7** | **100%** |
| CORRECT-SIDE-LOST      |     0 |   0%  |
| UNSETTLED              |     0 |   0%  |
| GET_YES_TEAM_FAILED    |     0 |   0%  |

| source                | INVERTED | other |
|-----------------------|---------:|------:|
| ESPN/basketball/nba   |        5 |     0 |
| SportsLiveSource      |        2 |     0 |

Every single loss in the dataset shows the same shape:
**bot bet `buy_no` while gt_prob ≈ 0 (high confidence YES will lose), and
the YES team actually won.**

---

## Section 4 — Layer attribution

For each losing trade, the bot's `action` is coherent with its `gt_prob`:
gt_prob ≈ 0 → bet on NO is the correct gap_detector decision given that
input. So Layers B (signal generator) and C (action logging) are exonerated.

Aggregate layer breakdown across all 7 inverted losses:
- **Layer A (GT/source direction disagreed with settlement): 7 / 7 (100%)**
- Layer B (signal inversion): 0
- Layer C (logging mismatch): 0
- Layer D (team_resolver): not separately decidable without testing team_resolver
  against ESPN home/away on these specific market_ids

### Sample table

| entry_ts (UTC)            | market_id                          | source            | action  | gt_prob | settled (exit_px) | yes_team                  | pnl    |
|---------------------------|------------------------------------|-------------------|---------|--------:|------------------:|---------------------------|-------:|
| 2026-03-22 23:12:52       | KXNBAGAME-26MAR22PORDEN-POR        | ESPN/basketball/nba | buy_no  |    0.00 |   yes (1.0)       | portland trail blazers    | -57.00 |
| 2026-03-23 01:50:28       | KXNBAGAME-26MAR22WASNYK-WAS        | ESPN/basketball/nba | buy_no  |    0.00 |   yes (1.0)       | washington wizards        | -57.00 |
| 2026-03-23 01:52:06       | KXNBAGAME-26MAR22WASNYK-WAS        | ESPN/basketball/nba | buy_no  |    0.00 |   yes (1.0)       | washington wizards        | -57.00 |
| 2026-03-25 01:13:57       | KXNBAGAME-26MAR24SACCHA-SAC        | ESPN/basketball/nba | buy_no  |    0.02 |   yes (1.0)       | sacramento kings          | -54.28 |
| 2026-03-25 02:43:07       | KXNBAGAME-26MAR24ORLCLE-ORL        | ESPN/basketball/nba | buy_no  |    0.02 |   yes (1.0)       | orlando magic             | -48.58 |
| 2026-03-26 04:25:32       | KXNBAGAME-26MAR25HOUMIN-MIN        | SportsLiveSource/Shock | buy_no |    0.05 |   yes (1.0)       | minnesota timberwolves    | -43.93 |
| 2026-03-26 04:26:30       | KXNBAGAME-26MAR25HOUMIN-MIN        | SportsLiveSource/Shock | buy_no |    0.05 |   yes (1.0)       | minnesota timberwolves    | -43.93 |

### Two pieces of corroborating evidence

**(1) ESPN/basketball/nba entries are 100% asymmetric in fill rate.**
Per-entry fill breakdown for this source:

| (action, gt_prob)         | entries | filled | wins | losses |
|---------------------------|--------:|-------:|-----:|-------:|
| buy_no  @ gt_prob = 0.02  |      91 |      2 |    0 |      2 |
| buy_no  @ gt_prob = 0.00  |       3 |      3 |    0 |      3 |
| buy_yes @ gt_prob = 0.98  |       3 |      0 |    0 |      0 |
| buy_yes @ gt_prob = 0.852 |       1 |      0 |    0 |      0 |

Every NBA filled trade is a buy_no, every NBA filled trade lost, and the
buy_yes side (which sports.py says "YES won") never fills — so we have no
disconfirming sample on that side. The data shape supports Layer A inversion,
but cannot fully rule out "sports.py is correctly predicting favorites that
happen to be losing this sample."

**(2) gt_prob = 0.00 exactly (not 0.02).**
`data/ground_truth/sports.py:527` clamps the NBA final-game path to
`0.98 if yes_won else 0.02` and `:571` clamps the in-progress path to
`[0.08, 0.92]`. There is no documented sports.py code path that produces
`gt_prob == 0.00 exactly` for an NBA market. Yet 9 entries record exactly
0.00. This is a smoking gun for a code path or default that doesn't honor
the documented floor — worth investigating but **out of scope for this
diagnostic.**

**(3) Double-fire on the same market.**
- `KXNBAGAME-26MAR22WASNYK-WAS` — two entries 98 seconds apart, both buy_no,
  both lost identically.
- `KXNBAGAME-26MAR25HOUMIN-MIN` — two entries 58 seconds apart, both buy_no,
  both lost identically.
This is a separate observability issue (signal deduplication failing under
some condition), independent of the direction bug.

---

## Section 5 — Verdict

- (i) Total clean-pair sports losses (pnl < 0): **7**
- (ii) **INVERTED: 7 (100 %)** — every single loss is on the wrong side
- (iii) CORRECT-SIDE-LOST: 0 (0 %)
- (iv) UNSETTLED: 0 (0 %)
- (v) Per-source: 5 NBA `ESPN/basketball/nba`, 2 `SportsLiveSource/Shock`
- (vi) Layer: 100 % Layer A (GT-vs-settlement direction disagreement)

### Hypothesis verdict

**(a) INVERTED dominates AND attribution points to one consistent layer**
(Layer A — GT source direction is wrong relative to settlement on every
losing fill). High-confidence fault locus identified for follow-up: **the
direction with which `data/ground_truth/sports.py` (and possibly downstream
team→YES mapping consumed by SportsLiveSource) translates a final-game
winner into `ground_truth_prob`.**

### Critical caveats — DO NOT yet commit to a fix without these

1. **Sample size is 7.** The fact pattern is unanimous but statistical power
   is low. Phase0 records (`audit/phase0_*.txt`) report
   `ESPN/basketball/nba` at 72.7% accuracy on N=22 settled markets — that
   data set is inconsistent with the 0% win rate seen on actual filled
   trades. The two need to be reconciled before scoping a fix.

2. **Layer A vs Layer D is not separated.** This diagnostic cannot tell
   apart "sports.py computes `yes_won` incorrectly" from "team_resolver
   returns the wrong YES team to begin with." Both would manifest
   identically here.

3. **`gt_prob == 0.0` exact** does not match any documented sports.py code
   path. There is an additional code path (or default initialization)
   producing 9 NBA and 3 NCAAM entries at exactly 0.0. Find it before
   touching the final-game logic.

4. **`kalshi.get_market()` independent verification failed** — settlement
   is currently derived solely from ghost-log `exit_price`. Wire an
   authenticated read-only path before any production-relevant action.

5. **Asymmetric fill rate hides the disconfirming half of the test.** Of
   ESPN/basketball/nba entries, every `buy_no` filled (eventually) but
   no `buy_yes` ever filled. We cannot observationally distinguish "sports
   is systematically inverted" from "sports is correctly predicting
   home-team blowouts but the home team keeps losing late-season." The
   investigation must look at whether `_get_yes_team(market) in winner_lower`
   gives the correct boolean on these 7 specific market_ids before any fix
   surface is decided.

### Observability gaps to close before deeper investigation

- 28 multi-exit markets — should be ≤1 settlement event per market
- 233 unfilled_timeouts — bot is firing signals that never make it to fills
- `kalshi.get_market()` constructor blocks programmatic settlement verification
- Double-fires within 60-120 s on the same market without dedup
- Phase0 NBA accuracy (72.7%) conflicts with live fill loss rate (0%) — one
  of the two measurements is misleading
