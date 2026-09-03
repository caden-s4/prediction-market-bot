# Strategy Audit — Trade-Producing Pipelines

**Read-only inventory.** Descriptive only — no kill/keep verdicts.
Forensic claims cite `file:line`; reasoning-from-architecture claims are labeled `[INFERENCE]`.

Signal-volume counts cite `audit/gate_funnel_monday_20260511.txt` (a 45-hour window: 2026-05-09T23:06Z → 2026-05-11T20:06Z) and direct greps over `data/runtime/gate_events.jsonl` (2,372,705 lines as of this audit).

---

## Trade-producing entry points (exhaustive set)

There are exactly two functions in the codebase that book a position (live or ghost):

| Entry method | File:line | Source of signals it accepts |
|---|---|---|
| `_try_execute(GapSignal)` | `resolution/executor.py:2286` | Information signals from `_fetch_info_signals()` (GT-router-driven) and `GapSignal` objects from `GapDetector.run_cross_platform_scan()` |
| `place_snipe_trade(Market, SnipeSignal)` | `resolution/executor.py:3140` | `SnipeSignal` / `WeatherPeakSnipeSignal` from the two strategies in `strategies/` |

Both are invoked from `resolution/scanner.py` and `bot.py`'s scan loop; no other call sites place orders.

Dispatch surfaces:
- Per-market snipe: `_dispatch_weather_snipe()` at `resolution/scanner.py:253`, called per Kalshi market at `scanner.py:619` (suppressed when `DISABLE_LEGACY_WEATHER_SNIPE` is true at `scanner.py:606`).
- Batch peak-snipe: `_dispatch_weather_peak_snipe_batch()` at `resolution/scanner.py:178`, called once per cycle at `scanner.py:699`.
- Cross-platform fuzzy scan: `executor.py:1107` and `executor.py:1421` call `self._gap_detector.run_cross_platform_scan(...)`, then those signals flow into `_try_execute`.
- Information signals: `_fetch_info_signals()` (called at `executor.py:1166`) iterates active markets and invokes `GroundTruthRouter.fetch()` (`data/ground_truth/router.py:341`) per market.

The router's source list (`router.py:_build_default_sources`, lines 139–160) is the authoritative inventory for the GT-driven pipelines.

---

## 1. Information signal — `FinancialDataSource`

### What it trades
- Kalshi financial bracket markets: NQ=F / NDX (KXNASDAQ100*), ES=F / SPX (KXSPX*), GC=F (KXGOLD*), CL=F (KXWTI/KXWTIW), NG=F (KXNATGAS*), SI=F (KXSILVER*), ^TNX / yields, and Yahoo-quoted forex.
- Brent crude (`KXBRENTD`, `KXBRENTW`) is explicitly excluded — `financial.py:225-238` (`FINANCIAL_EXCLUDED_SERIES`), because CL=F is the wrong instrument for Brent.
- Active-volume evidence (from `gate_events.jsonl`, sources that ran fetch()): Yahoo Finance/GC=F 1,327; Yahoo Finance/ES=F 1,272; Yahoo Finance/^TNX 520; Yahoo Finance/CL=F 355 (extra fields on `gt_routing` events).
- Scanner inclusion: financial bracket prefixes are admitted via `client.get_financial_bracket_markets()` supplement at `scanner.py:666`. The 45h funnel shows 40,900 `scanner_reject:financial_bracket_disabled` events (mostly KXWTI daily; rollover block — see `CLAUDE.md` completed fix).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: live spot/futures price from Yahoo (or Twelve Data) updates faster than Kalshi bracket prices reprice, especially deep-OTM/ITM brackets that mechanically should be ~0 or ~1. The "free money" thesis is that Kalshi makers leave mispriced wings on the orderbook.
- [INFERENCE] What needs to be true: (a) Yahoo quote freshness must be tight enough that the underlying really has moved; (b) Kalshi orderbook depth on the wings must be real, not a phantom resting order; (c) the symbol Yahoo serves must be the same instrument Kalshi settles against.

### GT source(s) and behavior
- `FinancialDataSource` (`data/ground_truth/financial.py:355`).
- API priority: Alpha Vantage if `ALPHA_VANTAGE_KEY` → Twelve Data if `TWELVEDATA_API_KEY`/`TWELVE_API_KEY` and symbol not in `_TD_FREE_TIER_BLOCKED` (`financial.py:76-90`) → Yahoo Finance `query1.finance.yahoo.com/v8/finance/chart` (`financial.py:44`). All primary bot symbols are in the TD free-tier blocked set, so production path is Yahoo (`financial.py:379-388`).
- Cadence: module-level price cache `_PRICE_CACHE` with 60s success TTL, 30s failure TTL (`financial.py:266-268`). Quote staleness window is `GT_FRESHNESS_SECONDS = 300` (`data/ground_truth/base.py:31`, loosened from 60 per the in-file comment).
- Confidence: time-decayed from 1.0 with floor `_TIME_CONF_FLOOR = 0.65` (Yahoo, `financial.py:312`) or `_TD_TIME_CONF_FLOOR = 0.85` (Twelve Data, `financial.py:313`). Yahoo spatial cap raised to 0.65 to match `MIN_CONFIDENCE_THRESHOLD` (`financial.py:306-312`).
- `data_published_at` is set from `regularMarketTime` in the Yahoo response (`financial.py:969-985`), enabling freshness checks downstream.
- Landmine #1 (`mds/landmines.md`): Yahoo CL=F is structurally ~604s stale during pit hours (0/240 samples cleared a 300s gate). Quoted upstream.

### Pipeline flow (operational)
- Scanner inclusion: bulk paginated fetch + `get_financial_bracket_markets()` supplement at `scanner.py:666-694`. Hours filter widened to 72h for bracket prefixes (CLAUDE.md completed fix).
- GT routing: `FinancialDataSource.can_handle()` checks for instrument keywords / Kalshi prefixes in market text after excluding the keyword/series blocklists (`financial.py:407+`).
- Gap detector: standard information-signal path via `GapDetector` (`resolution/gap_detector.py`); `SLIPPAGE_BUFFER = 0.01` (loosened from 0.03, `gap_detector.py:50`).
- Confidence gate: two-dimensional `ConfidenceScorer` (`resolution/confidence.py`); `CONFIDENCE_THRESHOLD = 0.30` default in code with `MIN_CONFIDENCE_THRESHOLD` env override (`confidence.py:70`). Note: `landmines.md` #8 documents that this was loosened from 0.45 (originally 0.80).
- Executor entry: `_try_execute` (`executor.py:2286`). Series exposure cap 15% live / 50% ghost (executor.md, `executor.py` series-cap branch).
- Exit behavior: financial hard-stop loop (`executor.py`, financial source-aware), decay monitor, resolution exit. See `executor.md:Don't Touch`.

### Signal volume
- `gate_events.jsonl` distribution of `source_name` in `extra` fields (sources whose fetch claimed a market): Yahoo Finance/GC=F 1,327, /ES=F 1,272, /^TNX 520, /CL=F 355.
- The 45h gate_funnel shows 8,449 `invariant_violation` events; the top tickers are `KXSILVERD-*`, `KXCOPPERD-*`, `KXNATGASD-*`, `KXAAAGASD-*`, `KXAAAGASW-*` with `implausible_gap` and `ws_rest_mid_disagreement` (gate_funnel_monday_20260511_detail.txt:122-138). These are bracket signals nuked at the invariant layer, not the confidence gate.
- Top funnel rejection for financial-bracket pipeline: `invariant_violation:implausible_gap` (3,173, 37.6% of invariants) and `executor_pretrade:large_divergence_extreme_market` (918, 99.6% of pretrade gate — but this last one is dominated by KXAAAGASW/KXAAAGASD which route to gas, not financial; see EconomicDataSource section).

### Code references
- Source class: `data/ground_truth/financial.py:355`
- Provider priority: `financial.py:379-392`
- Price cache: `financial.py:266-268, 750-792`
- Symbol routing: `financial.py:94-138` (Twelve Data map), `142+` (instrument map)
- Excluded series: `financial.py:225-238`
- Tests: `tests/test_executor_snipe.py` (indirect), no dedicated `test_financial.py` in the test inventory (see `audit/repo_inventory.md:125-143`).

### Known issues
- Landmine #1: stale Yahoo data is the dominant risk class. Yahoo CL=F 0/240 within a 300s gate. Phase 0b accuracy numbers (CL=F 80.5%, NQ=F 98%) are dead (landmine #2).
- `KXBRENTD`/`KXBRENTW` block is load-bearing — landmine on a wrong-instrument class (`financial.py:230-238`). [INFERENCE] If a Brent GT source is added, this needs review.
- All primary symbols blocked on Twelve Data free tier — production path is Yahoo for everything (`financial.py:76-90, 379-388`).

---

## 2. Information signal — `EconomicDataSource` (legacy FRED CSV)

### What it trades
- FRED/BLS legacy series matched by keyword: CPI, GDP, unemployment, jobless claims, Fed funds rate, PPI, PCE, retail sales, housing starts, nonfarm payrolls, trade balance, ISM PMI, AAA gas (mapped via `GASREGCOVW`) — `economic.py:78-120`.
- Kalshi tickers: KXAAAGASW (weekly gas), KXAAAGASD (daily gas), CPI/PPI/UNRATE event markets, KXFEDDECISION, KXJOBSREPORT-style series.
- Active-volume evidence (`gate_events.jsonl`): `FRED/GASREGCOVW` 1,103 routed lookups.
- 45h funnel: `executor_pretrade:large_divergence_extreme_market` 918 events, dominated by `KXAAAGASW-26MAY11-4.360` (556x), `KXAAAGASD-26MAY11-4.490` (322x) — i.e. AAA gas brackets where the FRED `GASREGCOVW` value is reported as `gt_prob=0.02` against `market_price=0.99` (gate_funnel_monday_20260511_detail.txt:64). **This is the wrong-instrument class flagged in landmines.md (new entry; see Cross-cutting observations below).**

### Claimed edge (conceptual)
- [INFERENCE] Edge claim for FRED-resolved markets (CPI, jobs): right after the BLS/FRED release, the bot's mechanical mapping resolves the question faster than Kalshi can settle. `data/release_calendar.py` provides the three-window state machine (pre_release → hold → hunt) so the bot only fires during the post-release window.
- [INFERENCE] For AAA gas: claim is that `GASREGCOVW` (EIA Regular Conventional weekly) tracks AAA national avg closely enough to provide a price-prediction edge. Documented spread is $0.10–0.20/gal below AAA (`economic.py:108-111`). This source is intended as fallback when `EIA_API_KEY` is unset.

### GT source(s) and behavior
- `EconomicDataSource` (`data/ground_truth/economic.py`).
- API: FRED `fredgraph.csv` shortcut (no key) for legacy series, plus FRED API (`_FRED_API_BASE`) for release-calendar checks if `FRED_API_KEY` set (`economic.py:33-46`).
- Cadence: module-level `_FRED_CACHE` TTL 300s (`economic.py:41-42`).
- Confidence: 0.95 for published data, 0.0 before release (per docstring `economic.py:14`).
- Per-series staleness windows: `_SERIES_STALENESS` map (`economic.py:56-75`) — DFF 24/72h, GASREGCOVW 24/192h (8-day max), ICSA 24/168h, monthly series 24/744h.
- `data_published_at` set from FRED observation date; freshness validated against the staleness map before returning a result.

### Pipeline flow (operational)
- Scanner inclusion: standard bulk fetch + sports/bracket supplements.
- GT routing: `EconomicDataSource.can_handle()` keyword-matches `_INDICATOR_MAP` in `market.question + market.tags` (`economic.py:78-120`).
- Gap detector / confidence gate: standard `_try_execute` path.
- Executor entry: `_try_execute` (`executor.py:2286`). The `large_divergence_extreme_market` check at `executor.py:2587` fires for AAA gas brackets where mechanical gt_prob comes back at 0.02 against a 0.99 mid (gate event extras: `gap_pct=97.0, market_price=0.99, gt_prob=0.02`).
- Exit behavior: standard decay monitor; FRED-source results don't have a `data_published_at` for freshness multiplier purposes when the GT path skips it (per `data/ground_truth/base.py:85-95` — `is_fresh()` returns True if `data_published_at` is None).

### Signal volume
- 45h: 1,103 `FRED/GASREGCOVW` claims (mostly LARGE_DIVERGENCE → blocked by `executor_pretrade` gate). KXAAAGASW-26MAY11-4.360 alone is 556× LARGE_DIVERGENCE in 45h.
- Where signals die: `executor_pretrade:large_divergence_extreme_market` is the dominant terminator (918 events, 99.6% of that gate). [INFERENCE] These ones are dying not because the data is wrong per se but because the wrong source is claiming the market (GASREGCOVW vs AAA national avg).

### Code references
- Source class: `data/ground_truth/economic.py`
- FRED cache: `economic.py:33-46`
- Indicator map: `economic.py:78-120`
- Staleness map: `economic.py:56-75`
- AAA-gas misroute caveat: `economic.py:99-120` (in-source comments)

### Known issues
- Landmines.md entry (new) — wrong-instrument class: GASREGCOVW used for AAA Friday/daily markets. KXAAAGASW-26MAY11 LARGE_DIVERGENCE block is the dominant `executor_pretrade` signal in the 45h window. Investigation surfaced this audit's necessity.
- Bracket-style gas markets (KXAAAGASD, daily) have a `lag_days` mismatch — daily Kalshi resolution vs weekly Monday EIA release. [INFERENCE] Source produces a fresh value Monday for a Friday/daily question; staleness rule passes because the data IS fresh, but the value is for the wrong day.

---

## 3. Information signal — `EIADataSource`

### What it trades
- KXAAAGASW prefix + gas-price keywords (`eia.py:71-78`).
- Same target ticker class as `EconomicDataSource` GASREGCOVW path, but uses the EIA `EMM_EPM0U` series (all formulations, tracks AAA more closely).

### Claimed edge (conceptual)
- [INFERENCE] Same as EconomicDataSource gas path, but with a tighter underlying. Documented in `eia.py:22-28`: confidence 0.90 when ≤7 days old, EIA all-formulations is closer to AAA than `GASREGCOVW` regular-conventional.

### GT source(s) and behavior
- `EIADataSource` (`data/ground_truth/eia.py:64`).
- API: EIA Open Data v2 (`https://api.eia.gov/v2/petroleum/pri/gnd/data/`) — series `EMM_EPM0U_PTE_NUS_DPG` (`eia.py:46-49`).
- Disabled when `EIA_API_KEY` env var is absent (`can_handle()` returns False — `eia.py:71-78`).
- Cadence: `_EIA_CACHE_TTL = 300s` (`eia.py:53`).
- Confidence: 0.90 if data <24h old, 0.80 up to 168h, None beyond (`eia.py:88-96`).
- Source type `HARD`, source_name `EIA/EPM0` (`eia.py:104-106`).
- Released Mondays ~5 pm ET; published period encoded in API response.

### Pipeline flow (operational)
- Scanner inclusion: standard bulk fetch.
- GT routing: claims KXAAAGASW + gas keywords; runs in parallel with `EconomicDataSource`. Router takes max-confidence — EIA 0.90 wins over economic's 0.95-with-different-prob race [INFERENCE: actual race depends on which prob is returned, and the `is_tradeable` flag].
- Confidence gate, executor entry, exit: same `_try_execute` path.

### Signal volume
- [INFERENCE] Not separately counted in 45h funnel; would appear as `source_name="EIA/EPM0"` in `extra` fields. Grep shows zero EIA-prefixed source names in the 45h sample, suggesting `EIA_API_KEY` is unset on the production bot, so this source is dormant. Verifiable by checking `.env`; not done in this read-only audit.

### Code references
- Source class: `data/ground_truth/eia.py:64`
- API URL/series: `eia.py:46-49`
- can_handle: `eia.py:71-78`

### Known issues
- Same wrong-instrument-class risk as `EconomicDataSource` if applied to daily gas brackets (KXAAAGASD) — weekly EIA release vs daily Kalshi question.
- Source is silently disabled when key absent — no operational signal indicates it's off.

---

## 4. Information signal — `FREDEconomicSource` (JSON observations API)

### What it trades
- FRED JSON observations for: CPI (CPIAUCSL), Core CPI (CPILFESL), 10Y Breakeven (T10YIE), Unemployment (UNRATE), Nonfarm Payrolls (PAYEMS), Fed Funds Rate (DFF / FEDFUNDS), GDP, 30Y Mortgage Rate — see `FRED_SERIES` table (`economic_fred.py:71+`).
- Foreign-economy exclusion (`economic_fred.py:47-60`).

### Claimed edge (conceptual)
- [INFERENCE] Same as `EconomicDataSource` but uses the structured JSON API for cleaner per-series cache TTLs. Edge claim is post-release arbitrage on FRED-resolved markets (CPI Thursday, jobs Friday, FOMC).
- [INFERENCE] Per CLAUDE.md "Completed fixes": FRED release calendar 3-window state machine (`data/release_calendar.py`) is intended to keep this source dark outside its release window so it can't fire on stale CPI for a current-period market. Landmine #1 specifically calls out "FRED nearly burned the bot on stale CPI returned on a current-period market."

### GT source(s) and behavior
- `FREDEconomicSource` (`data/ground_truth/economic_fred.py`).
- API: `https://api.stlouisfed.org/fred/series/observations` (`economic_fred.py:37`). Requires `FRED_API_KEY`; can_handle returns False otherwise (per docstring `economic_fred.py:16-18`).
- Confidence: 0.90 base (`_CONFIDENCE`, `economic_fred.py:42`).
- Per-series cache TTL (`FRED_SERIES["cache_hours"]`): Fed rate 6h, monthly CPI 12h, jobs 6h, GDP 12h (`economic_fred.py:71+`).
- Per-series `lag_days` (BLS publication delay): used for staleness rejection.
- `data_published_at` set from FRED observation date.

### Pipeline flow (operational)
- Scanner inclusion: standard bulk fetch; CPI/jobs/Fed-decision markets appear in `general` / `economics` categories.
- GT routing: keyword matches with US-specific phrasing to avoid foreign-economy false positives.
- Confidence gate, executor entry, exit: standard `_try_execute` path.

### Signal volume
- 45h funnel `gt_routing:source_returned_none`: 10,610 events (80.6% of gt_routing). FRED is one of several sources contributing to this bucket; per-source breakdown not directly counted by `gate_funnel.py`.
- [INFERENCE] Volume should be tightly bounded by FRED release calendar — most cycles are outside any release window. Signals concentrated on CPI Thursday and jobs Friday.

### Code references
- Source class: `data/ground_truth/economic_fred.py`
- Series metadata: `economic_fred.py:71-160`
- Foreign exclusion: `economic_fred.py:47-60`

### Known issues
- Landmine #1: FRED stale-CPI near-miss is the canonical "stale data is the #1 risk class" incident. Release-calendar gate is the mitigation.
- Two FRED-bearing sources exist in the router (`EconomicDataSource` + `FREDEconomicSource`) — both claim CPI/jobs markets. The router runs both and takes the highest-confidence tradeable result. [INFERENCE] On a fresh release this is fine; but they share underlying cache risk and could amplify a single bad fetch.

---

## 5. Information signal — `SportsLiveSource` (ESPN live polling + shock)

### What it trades
- In-progress NBA / NCAAB / NFL game markets matched via `data/sports/market_matcher.py`.
- Activation: market category sports / tagged sport / Kalshi prefix KXNBA, KXNFL, KXNCAAMBGAME, KXNCAAWBGAME (`live_source.py:81-92`).
- Final-period only (Q4 NBA/NFL, H2 NCAAB) — non-final-period games return None (`live_source.py` + sports.md).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: ESPN scoreboard updates faster than Kalshi market makers reprice on a late-game lead change or shock event (Q4 turnover, last-minute 3-pointer). The bot is racing other automated traders to the same data.
- [INFERENCE] What needs to be true: ESPN's update latency < competing-bot latency; Kalshi orderbook on the favored side has thinning depth that's still real (not yet swept).

### GT source(s) and behavior
- `SportsLiveSource` (`data/sports/live_source.py:58`). Registered before `SportsDataSource` so shock signals shadow the slower final-only path (`router.py:152`).
- API: ESPN public endpoints (`site.api.espn.com/apis/site/v2/sports/...`). NBA, NCAAB confirmed working; NFL currently 400s (off-season; sports.md).
- Cadence: `LiveGameMonitor.refresh_if_stale()` called at cycle start; module-level `_SCOREBOARD_CACHE` TTL 90s (`sports.py:172-173`).
- Confidence tiers (`data/sports/shock_detector.py:_score_confidence`):
  - 0.92: shock ≥0.25, final period, <120s remaining
  - 0.85: shock ≥0.15, final period, <300s remaining
  - 0.78: shock ≥0.12, final period — **below 0.80 trade gate, logged only** (`shock_detector.py:42`)
  - 0.00: not in final period — never cached, never traded
- Non-shock late-game fallback: prob ≥ 0.85 with confidence 0.65 (`live_source.py:48` and `data/ground_truth/sports.py:9-16` table for the slower path).

### Pipeline flow (operational)
- Scanner inclusion: kalshi sports supplement via `client.get_sports_markets()` (`scanner.py:631-655`). 48h window for game series prefixes (CLAUDE.md kalshi.md: game-market resolution date override = `game_date + 30h`).
- GT routing: `SportsLiveSource.can_handle` (`live_source.py:81`); router tries it before `SportsDataSource`.
- Gap detector: standard info-signal path.
- Confidence gate: shock-derived 0.85 / 0.92 passes the 0.80 floor.
- Executor entry: `_try_execute` (`executor.py:2286`). Newly-final game markets get priority sorting before GT evaluation (`executor.py:1126-1156`).
- Exit behavior: standard decay + resolution-detector exit.

### Signal volume
- 45h `gt_routing:source_returned_none` top: NBA game markets are the dominant ticker class (KXNBAGAME-26MAY11DETCLE-CLE 490x, ...; gate_funnel_monday_20260511_detail.txt:30-37). Sample extra: `none_reasons=['SportsLiveSource: returned None (no relevant data found)', 'SportsDataSource: returned None (no relevant data found)']`. So most cycles return None — typically because the game is not yet in the final period.
- Volume of actual signals not directly counted in current gate_funnel output. [INFERENCE] Shock-signal volume is low — would require a shock magnitude ≥0.15 with <300s remaining; rare per game.

### Code references
- Source class: `data/sports/live_source.py:58`
- Shock detector / confidence tiers: `data/sports/shock_detector.py:40-48`
- Market matcher: `data/sports/market_matcher.py`
- Live game monitor: `data/sports/live_game_monitor.py`
- Tests: `tests/test_live_game_monitor.py`

### Known issues
- sports.md: NFL ESPN 400 error every cycle (off-season; harmless).
- sports.md: NCAAB alias coverage only top 68 programs — mid-major teams may fail fuzzy match.
- Snipe positions store `TradeRecord.signal=None` and lack `_gt_published_at` for decay-monitor freshness reference (executor.py:3157-3166 TODO). [Note: that TODO is on the snipe path, not the gap-signal sports path, but the freshness-reference gap applies to both sports paths.]

---

## 6. Information signal — `SportsDataSource` (ESPN final-only / pre-game odds)

### What it trades
- Same Kalshi sports tickers as `SportsLiveSource` plus broader sport coverage: MLB, NHL, MLS, NCAAF, soccer (EPL, Champions League, La Liga, Bundesliga, Serie A), golf (PGA / LPGA), racing (NASCAR / IndyCar).
- Final-result confidence 0.95, pre-game moneyline confidence 0.65, in-progress final-period substantial-lead 0.65 — see docstring `sports.py:1-25`.

### Claimed edge (conceptual)
- [INFERENCE] Edge claim — pre-game: vig-removed implied probability from moneyline is more accurate than Kalshi's order book before the market is liquid.
- [INFERENCE] Edge claim — final: ESPN reports FINAL faster than Kalshi settles, opening a sub-resolution window for arbitrage on the bracket's last few percentage points.

### GT source(s) and behavior
- `SportsDataSource` (`data/ground_truth/sports.py:176`).
- API: ESPN scoreboard (`https://site.api.espn.com/apis/site/v2/sports/...`).
- Cadence: `_SCOREBOARD_CACHE` TTL 90s (`sports.py:172-173`). `_TIMEOUT = 0.5s` hard cap (`sports.py:124`).
- Confidence map (`sports.py:8-15`):
  - 0.95 game FINAL
  - 0.65 in-progress AND in final period AND substantial lead (≥28% edge from 0.50)
  - 0.65 pre-game with ESPN moneyline odds
  - None in-progress but not final period or lead too small (router falls through)
  - 0.0 pre-game with no odds available
- Time-weighted in-progress formula: `prob = clip(0.5 + lead × 0.03 × time_weight, 0.08, 0.92)` (`sports.py:15-21`).

### Pipeline flow (operational)
- Scanner: same path as SportsLiveSource — sports supplement + standard bulk fetch.
- Routing: registered after `SportsLiveSource`; gets the market when the live source returns None.
- Confidence gate: 0.95 final passes the 0.80 gate; 0.65 in-progress does NOT.
- Executor entry: `_try_execute`.

### Signal volume
- 45h: `ESPN/football/college-football` 183 mentions in `gt_routing` extras (relatively low — NCAAF is largely offseason during this window; the prefix is in_active mostly).
- NBA game tickers dominate `gt_routing:source_returned_none` — both sports sources frequently return None together (sample extra shows both fail).

### Code references
- Source class: `data/ground_truth/sports.py:176`
- ESPN base / cache: `sports.py:123-173`
- Sport detection: `sports.py:_detect_sport` (called from `fetch` at line 204)
- American-odds-to-prob: `sports.py:145-158`

### Known issues
- Final-result auto-trade only works in a tight window before settlement — landmine #4 (pre-resolution price convergence).
- Same NFL off-season 400 as live source.

---

## 7. Information signal — `CongressSource`

### What it trades
- US Congress bill markets: passed/signed/vetoed/defeated.
- Activation: market category politics / legal / government / general AND any of `_BILL_KEYWORDS` (`congress.py:44-48, 57-62`).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: Congress.gov API publishes bill status before Kalshi politics markets reprice. Very narrow opportunity class — only "signed into law" / "vetoed" / "floor-failed" are tradeable; "introduced" / "passed one chamber" return None.

### GT source(s) and behavior
- `CongressSource` (`data/ground_truth/congress.py:51`).
- API: `https://api.congress.gov/v3` (free, no key, `congress.py:36`).
- Timeout: 0.5s hard cap (`congress.py:37`).
- Confidence map (`congress.py:11-20`):
  - 0.95 signed into law / vetoed
  - 0.85 failed on the floor
  - 0.75 keyword-search match
  - 0.60 passed one chamber — prob=None
  - 0.50 introduced — prob=None
- `prob=None` returned for unresolved states → not tradeable.
- No `data_published_at` set [INFERENCE based on code shape; not verified line-by-line in this read].

### Pipeline flow (operational)
- Standard `_try_execute` path.

### Signal volume
- [INFERENCE] Very low. Not visible in the `source_name` extras for this 45h window.

### Code references
- Source class: `data/ground_truth/congress.py:51`
- API endpoint: `congress.py:36`
- Bill-reference regex: `congress.py:77-92`
- Tests: none in test inventory (`audit/repo_inventory.md:125-143`).

### Known issues
- 0.5s timeout cap means slow Congress.gov calls don't block a cycle but also don't return data — claimed markets may silently fail (`congress.py:64-72` swallows exceptions).

---

## 8. Information signal — `FederalRegisterSource`

### What it trades
- Federal regulatory markets: SEC/FDA/EPA rules, executive orders, court rulings (via CourtListener).
- Activation: question text matches `_REGULATORY_KEYWORDS` ∪ `_COURT_KEYWORDS` ∪ `_SEC_KEYWORDS` AND not in count-style "or more / how many" phrases (`federal_register.py:46-97`).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: Federal Register / CourtListener publishes a final rule or court ruling structured-data before discretionary Kalshi politics markets reprice.

### GT source(s) and behavior
- `FederalRegisterSource` (`data/ground_truth/federal_register.py:69`).
- APIs: Federal Register API (`https://www.federalregister.gov/api/v1`) and CourtListener (`https://www.courtlistener.com/api/rest/v3`) (`federal_register.py:41-42`).
- Timeout: 0.5s hard cap (`federal_register.py:43`).
- Confidence map (`federal_register.py:15-22`):
  - 0.90 Final Rule
  - 0.85 Enforcement Action / Consent Order
  - 0.75 Interim Final Rule
  - 0.50 Other Notice / Presidential Document
  - None Proposed Rule, Guidance Document (non-binding)
- 0.50-source results are NOT tradeable (`base.py:103-110`: `is_tradeable` requires `confidence >= 0.8` AND source_type HARD/REGULATORY).

### Pipeline flow (operational)
- Standard `_try_execute` path.
- `gt_routing:source_not_tradeable` (45h: 2,546 events, 19.4% of gt_routing) has Federal Register sample: `sample extra: source_name=Federal Register API, confidence=0.5` — these are markets where the source claims and fetches but the result is below the trade gate (`gate_funnel_monday_20260511_detail.txt:44-46`).

### Signal volume
- 45h: `Federal Register API` 5,518 appearances in `source_name` extras — the most-claiming source in the dataset.
- The vast majority become `source_not_tradeable` (confidence=0.5) — i.e. claimant returns a result but it's not strong enough to trade.

### Code references
- Source class: `data/ground_truth/federal_register.py:69`
- Keyword sets: `federal_register.py:45-67`
- Count-phrase exclusion: `federal_register.py:83-87`

### Known issues
- Claims a large fraction of markets and almost never produces a tradeable result. [INFERENCE] This shape — high claim rate, near-zero trade rate — means it absorbs router CPU per cycle without contributing P&L. The `can_handle()` gates were already tightened (see in-source comments at `federal_register.py:75-97`).
- No `data_published_at` likely set [INFERENCE based on docstring shape].

---

## 9. Information signal — `RottenTomatoesSource`

### What it trades
- Movie Tomatometer markets: questions mentioning "rotten tomatoes" / "tomatometer" / "rt score" / "tomato score" (`rotten_tomatoes.py:59-63`).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: RT score changes with each new review; the bot can read the live score before Kalshi makers reprice. Edge is highest in the first 1–3 days after release while reviews are accreting fast.

### GT source(s) and behavior
- `RottenTomatoesSource` (`data/ground_truth/rotten_tomatoes.py:51`).
- API: RT private search API (`https://www.rottentomatoes.com/api/private/v2.0/movies`, `rotten_tomatoes.py:35-39`).
- Cache: 30-min TTL (`rotten_tomatoes.py:45`).
- Confidence by review count (`rotten_tomatoes.py:13-17`):
  - 0.90 ≥40 reviews
  - 0.80 10–39
  - 0.60 5–9 — below 0.80 trade gate
  - None <5

### Pipeline flow (operational)
- Standard `_try_execute` path.

### Signal volume
- Not seen in the 45h source_name distribution — low volume in this window.

### Code references
- Source class: `data/ground_truth/rotten_tomatoes.py:51`
- API URL / headers: `rotten_tomatoes.py:35-39`

### Known issues
- Uses an undocumented private RT API — landmine #1 (verify-before-trust on unverified third-party feeds).

---

## 10. Cross-platform fuzzy gap — `CrossPlatformSource`

### What it trades
- Paired Kalshi↔Polymarket markets matched via fuzzy title (SequenceMatcher after normalization).
- Trades the Kalshi side only (Polymarket assumed more efficient; `cross_platform.py:14-17`).

### Claimed edge (conceptual)
- [INFERENCE] Edge claim, stated in module docstring: "Polymarket generally has more sophisticated traders than Kalshi, making Kalshi the better market to trade against when they diverge."
- [INFERENCE] What needs to be true: title-similarity threshold (0.60) correctly identifies the same underlying event; the price gap is real (not a structural difference like settlement-time mismatch); Kalshi orderbook depth is real on the underpriced side.

### GT source(s) and behavior
- `CrossPlatformSource` (`data/ground_truth/cross_platform.py:95`).
- **Not** a `DataSource` subclass — does NOT participate in `GroundTruthRouter.fetch()` (`cross_platform.py:96-107`).
- Invoked by `GapDetector.run_cross_platform_scan()` (`resolution/gap_detector.py:351`), called from `executor.py:1107` and `1421`.
- Pair-cache rebuild every 8h (`_PAIR_CACHE_TTL`, `cross_platform.py:48`); fuzzy signal cache also 8h (`gap_detector.py:_fuzzy_signal_cache_ttl`).
- Confidence: `POLYMARKET_AS_GT_CONFIDENCE (0.78) × similarity_score (0.60–1.0)` → ~0.47–0.78. **Always below the 0.80 trade gate by design.**

### Pipeline flow (operational)
- Scanner inclusion: same as everything else — both platforms' markets are fetched normally.
- Pairing: `build_pairs()` runs once per 8h on the full T1+T2+T3 Kalshi×Polymarket cross product (`gap_detector.py:1092-1104`).
- Per-cycle: fuzzy signal cache returned (`gap_detector.py:375-385`); on miss, gap evaluated for each pair using `SLIPPAGE_BUFFER` (1%).
- Confidence gate: confidence_scorer estimates source confidence from gap size when `signal.ground_truth_result` lacks a source (`confidence.py:22-26`).
- Executor entry: `_try_execute`.
- The depth-ratio liquidity penalty (`gap_detector.py:71` and `confidence.py:25`) applies here, since cross-platform pairs often pair an illiquid Kalshi side with a deep Polymarket.

### Signal volume
- Not directly broken out in gate_funnel. [INFERENCE] Cross-platform signals show up in `_try_execute` with no `source_name` set or with `Polymarket/cross-platform` source name (`gap_detector.py:435`). The `Candidate` source_name (5 events in 45h) and the small unbroken-out remainder are candidates here.

### Code references
- Source class: `data/ground_truth/cross_platform.py:95`
- Pair builder: `cross_platform.py:128-178`
- Run-cross-platform-scan: `resolution/gap_detector.py:351`
- Loosened confidence gate landmine: per `mds/landmines.md` #8, `CONFIDENCE_GATE_GHOST_CROSS_PLATFORM` was loosened 0.50→0.30 on 5/5 and needs re-tightening.

### Known issues
- Landmine #8: `CONFIDENCE_GATE_GHOST_CROSS_PLATFORM` is currently loose (0.30); cross-platform was one of the three gates loosened on 5/5. Re-tightening is documented as needed.
- Pairs are below the auto-trade gate by design — these are ghost signals for human review until pair accuracy is validated (`cross_platform.py:11-13`).
- Title similarity at 0.60 produces phantom pairs on weak matches; the source logs top-10 candidates per unmatched market for tuning (`cross_platform.py:159-164`).

---

## 11. Named strategy — `weather_peak_snipe` (Phase 14b v1, ghost-only)

### What it trades
- Bracket markets for daily HIGH and LOW temperatures in 4 cities: NYC (KNYC), Chicago (KORD), Miami (KMIA), Denver (KDEN) — `weather_peak_snipe.py:96-101`.
- Series: KXHIGH<CITY>, KXHIGHT<CITY>, KXLOWT<CITY> (prefix-evolution handled, `weather_peak_snipe.py:96-122`).
- Up to 5 signals per (series, event_date) per day: winner YES + ±1/±2 adjacent NO.

### Claimed edge (conceptual)
- [INFERENCE] Edge claim, documented in module docstring + audit/weather_snipe_v1_phase14b.md: after the climatological peak (14:00 local HIGH, 07:00 local LOW), if a 30-min monotonic post-peak trend is observed and current obs is ≥1°F past the running extremum, the day's bracket is effectively determined. The strategy buys the winner at ≥$0.85 and the ±2 adjacent NOs at ≤$0.15.
- [INFERENCE] What needs to be true: ASOS observations don't reverse meaningfully after the trigger; Kalshi has not yet repriced the bracket to near-certainty.
- Phase 14a's 88% trigger-time accuracy is the upper bound; landmine #4 notes 70.5% Kalshi bracket match as a realistic floor.

### GT source(s) and behavior
- **Not router-driven.** Uses `data/ground_truth/asos_timeseries.py:fetch_asos_timeseries()` directly (`weather_peak_snipe.py:719-723`).
- ASOS = IEM hourly METAR feed; per-process TTL cache by `(station, lookback_hours)`.
- Cadence: ASOS publishes hourly; cache prevents over-fetch.
- Confidence: `SNIPE_CONFIDENCE = 0.95` (`weather_peak_snipe.py:69`).
- DECISIVE_PROB: 0.99 (mirrors legacy weather_snipe; `weather_peak_snipe.py:77`).

### Pipeline flow (operational)
- Scanner inclusion: `is_peak_snipe_candidate()` filter (`weather_peak_snipe.py:726`), called per market at `scanner.py:620`.
- Batch dispatch: `_dispatch_weather_peak_snipe_batch()` at `scanner.py:178`, called at `scanner.py:699`. Hard-coded `dry_run=True` (Phase 14b v1).
- Strategy internal gates:
  - Trigger window: local hour ≥ peak_hour + 1 (`weather_peak_snipe.py:263-270`)
  - Trigger conditions A–D: see docstring lines 13–20.
  - Winner YES price gate: ≥ 0.85 (`weather_peak_snipe.py:60, 388`)
  - Adjacent YES price gate: ≤ 0.15 (`weather_peak_snipe.py:61, 413`)
  - Risk caps: $5/bracket, 6 contracts/event (`weather_peak_snipe.py:65-66, 446-479`)
- Executor entry: `place_snipe_trade` (`executor.py:3140`). Ghost-only guard at `executor.py:3182` rejects with `reason="ghost_only"` if `signal_class == "weather_peak_snipe"` and not dry-run.
- Exit behavior: standard decay monitor, but `TradeRecord.signal=None` for snipe entries means `_gt_published_at=None` (per the TODO at `executor.py:3157-3166`) so decay monitor freshness reference is absent.

### Signal volume
- 45h: `signal_class=weather_peak_snipe` appears 183 times in gate_events (grep). Specifically:
  - `snipe:(none)` decision="evaluated" — 47 events for peak class (gate_funnel_monday_20260511_detail.txt:89-97, sample extra `signal_class=weather_peak_snipe`).
  - `snipe:price_gate` — 30 events (peak-snipe trigger fired but bracket prices didn't pass; sample `winner_yes_ask=0.01, adjacent_yes_asks=[None,None,0.01,1.0]` — i.e. trigger fired in a market already at 0.01 where there's no edge).
- Top funnel rejection: `snipe:no_signal` (4,259, 96.4% of snipe gate) — most cycles, the trigger conditions aren't met. This includes weather_snipe shadow rejections.

### Code references
- Strategy: `strategies/weather_peak_snipe.py`
- Dispatcher: `resolution/scanner.py:178` (batch), `scanner.py:699` (per-cycle call)
- Executor entry: `resolution/executor.py:3140` (with ghost-only guard at line 3182)
- ASOS feed: `data/ground_truth/asos_timeseries.py`
- Tests: `tests/test_weather_peak_snipe.py`
- Audit notes: `audit/weather_snipe_v1_phase14b.md`, `audit/weather_snipe_phase_a_20260510.md`

### Known issues
- Landmine #3: ghost fill cap against orderbook depth is not implemented. Sub-penny entries (≤$0.05) — common on adjacent NO buys when winner is at 0.99 — produce unrealistically large ghost fills. **Open Issues in CLAUDE.md confirms: "Ghost fill size cap against orderbook depth: NOT IMPLEMENTED."**
- Landmine #4: pre-resolution convergence eats the 88%→70.5% accuracy delta.
- Ghost-only by spec; `dry_run=True` hard-coded in `_dispatch_weather_peak_snipe_batch` (`scanner.py:211`) plus executor defense-in-depth at `executor.py:3182`.
- `TradeRecord.signal=None` for snipe entries (executor.py:3157-3166 TODO) reduces decay-monitor and per-class exit attribution.

---

## 12. Named strategy — `weather_snipe` (legacy, disabled)

### What it trades
- All weather bracket markets (~30+ cities) in the final 60 minutes before close (`weather_snipe.py:30`, `_WEATHER_SNIPE_WINDOW`).
- Per-market trigger: today's running max/min temperature locks the bracket outcome.

### Claimed edge (conceptual)
- [INFERENCE] Edge claim: in the final hour, the day's max/min temperature is effectively settled (no significant remaining movement), so the bracket question is determined. Trade the YES side at ≤0.97 (or NO side at ≥0.03) for near-decisive contracts.
- Phase 1B / 14a accuracy ~88% at trigger time (per audit history); landmine #4 floor 70.5%.

### GT source(s) and behavior
- Uses `data/ground_truth/weather_cli.py:fetch_asos_running_extreme()` for the day's running max/min (`weather_snipe.py:22`).
- NWS CLI Daily Climatological Report — not the IEM ASOS hourly feed used by peak-snipe.
- Min observation count: 6 (`weather_snipe.py:32`).
- Confidence: `_SNIPE_CONFIDENCE = 0.99` (`weather_snipe.py:36`).

### Pipeline flow (operational)
- Scanner inclusion: per-market dispatch at `scanner.py:619`.
- **Disabled flag**: `DISABLE_LEGACY_WEATHER_SNIPE` is true → dispatch is suppressed and `scanner_reject:legacy_weather_snipe_disabled` is emitted instead (`scanner.py:606-617`). See `audit/legacy_weather_snipe_disable_20260511.md`.
- Shadow window (60–240 min before close): logs SHADOW_SIGNAL / SHADOW_REJECT but never dispatches (`scanner.py:300-348`).
- Internal gates: `_YES_FULLY_PRICED = 0.97`, `_NO_FULLY_PRICED = 0.03` (`weather_snipe.py:37-38`); decisive-outcome decider with safety margins (`weather_snipe.py:133-161`).
- Executor entry (when not disabled): `place_snipe_trade` (`executor.py:3140`) with `signal_class="weather_snipe"` default at `executor.py:3172`.

### Signal volume
- 45h: 116 `signal_class=weather_snipe` events in gate_events (grep).
- Top funnel rejections include `snipe:no_signal` 4,259 (shadow rejects + real rejects, sample extra `shadow=True, minutes_to_close=68`).
- Other relevant: `snipe:bankroll` 48, `snipe:dedup` 17, `snipe:empty_book_snipe` 10, `snipe:series_cap` 5 — all show `signal_class=weather_snipe` in sample extras.

### Code references
- Strategy: `strategies/weather_snipe.py`
- Per-market dispatcher: `resolution/scanner.py:253`
- Disable flag: `scanner.py:606`
- Disable audit: `audit/legacy_weather_snipe_disable_20260511.md`
- Inventory comparison vs peak: `audit/legacy_weather_snipe_inventory_20260510.md`
- Executor entry: `resolution/executor.py:3140`
- Tests: `tests/test_weather_snipe.py`, `tests/test_scanner_weather_snipe_dispatch.py`

### Known issues
- Disabled as of phase 15e. The strategy module still loads and is callable directly (per `audit/legacy_weather_snipe_disable_20260511.md:69-72`); only the scanner dispatch is gated.
- `signal_class=weather_snipe` still appears in 116 gate events in this 45h window — these are the `scanner_reject:legacy_weather_snipe_disabled` rejections (and a handful of shadow-mode evaluations that pre-date the flag flip). [INFERENCE] Re-enabling would resurrect this volume but the prefix overlap with peak-snipe (HIGH/LOW × NYC/CHI/MIA/DEN) means peak-snipe already covers the same trigger-class for the 4 priority cities; re-enabling would only add the other ~26 cities.

---

## Cross-cutting observations

### Wrong-instrument class (per landmines.md #1 + new entry)

Three documented or implicit wrong-instrument situations exist:

1. **`KXBRENTD` / `KXBRENTW` → CL=F (WTI)** — explicitly excluded at `financial.py:225-238`. CL=F and Brent have a $3–8 spread that moves independently. Phase 0b 44.4% accuracy (below coin-flip) drove the block. Status: blocked by series exclusion until a Brent feed is wired.

2. **`KXAAAGAS*` → FRED `GASREGCOVW` (EIA Regular Conventional)** — claimed by `EconomicDataSource` at `economic.py:117-120`. AAA spread documented as $0.10–0.20/gal in `economic.py:108-111`. The 45h funnel shows this is the dominant `executor_pretrade:large_divergence_extreme_market` trigger (KXAAAGASW-26MAY11-4.360 556×, KXAAAGASD-26MAY11-4.490 322×). Status: blocked at runtime by LARGE_DIVERGENCE gate, but the source is still selected and the cycle CPU is still spent fetching/computing. The investigation that prompted this audit surfaced this.

3. **`KXAAAGASD` (daily) → weekly EIA/FRED gas series** — same `GASREGCOVW` mechanic applied to a daily question. [INFERENCE] Even if the source were the right instrument, the cadence mismatch (weekly value for a daily question) is a structural wrong-instrument. Status: implicit via the same LARGE_DIVERGENCE block; no series exclusion.

4. **`KXNATGAS*` natural gas brackets** — appear repeatedly in `invariant_violation:implausible_gap` (e.g. KXNATGASD-26MAY1117-T3 413× implausible_gap in 45h, plus the only `confidence:source_below_gate` event class). [INFERENCE] `FinancialDataSource` routes NG=F here; check whether the Yahoo NG=F front-month vs Kalshi natural-gas series-of-record is the same instrument (continuous front-month vs settlement-month).

### Shared gate logic vs custom gates

Information-signal pipelines (sources 1–9) share:
- `ConfidenceScorer` 0.80 / 0.85 gate (`resolution/confidence.py:70`) — currently loosened to 0.30 default; `MIN_CONFIDENCE_THRESHOLD` env override; landmine #8.
- `GT_FRESHNESS_SECONDS = 300` (`data/ground_truth/base.py:31`) — loosened from 60; landmine #1.
- `SLIPPAGE_BUFFER = 0.01` (`gap_detector.py:50`) — loosened from 0.03.
- `LARGE_DIVERGENCE` gate at gap >40% (`router.py:_validate_result` lines 541-577).
- `MIN_HOURS_TO_RESOLUTION = 0.25` (`gap_detector.py:53`).
- Series exposure cap 15% live / 50% ghost (`executor.md`).
- `_try_execute` empty-book guards, perm-skip counters, depth-ratio liquidity penalty.

Snipe pipelines (10–11 = peak + legacy) share:
- `place_snipe_trade` entry with its own empty-book guard (`executor.py:3140+`), distinct from `_try_execute`.
- Snipe price-gate behavior is *per-strategy* (legacy: 0.97/0.03; peak: 0.85 winner / 0.15 adjacents).
- DECISIVE_PROB = 0.99 in both.
- Snipe `TradeRecord.signal=None` (executor.py:3157-3166 TODO) — both lose decay-monitor freshness reference and exit attribution.

Cross-platform (10) is structurally different:
- Confidence by design below the 0.80 gate (POLYMARKET_AS_GT_CONFIDENCE × similarity = max 0.78).
- Independent gate `CONFIDENCE_GATE_GHOST_CROSS_PLATFORM` (loosened 0.50→0.30 on 5/5, landmine #8).

### Sub-penny / extreme-price sensitivity (landmine #3)

Pipelines that enter at extreme prices and are most affected by the missing ghost-fill cap:
- **`weather_peak_snipe` adjacent NO buys** at ≤$0.15 (often $0.01–0.05 in practice — see `KXHIGHCHI-26MAY10-T59` price_gate event extra: `winner_yes_ask=0.01, adjacent_yes_asks=[None,None,0.01,1.0]`).
- **`weather_snipe` (legacy)** at gates 0.97 YES / 0.03 NO (sub-penny on the NO leg).
- **Financial bracket wing brackets** at 0.02 / 0.98 entries (where `LARGE_DIVERGENCE` clamp lives — `router.py:518-528`).

Per landmine #3 and CLAUDE.md open issues: "Ghost P&L is optimistic on sub-penny entries… Sub-penny strategies look better in ghost than they will live." Affects every pipeline above.

### Pipelines using stale data (landmine #1)

- `FinancialDataSource` Yahoo CL=F: structurally ~604s stale during pit hours; 0/240 cleared 300s gate. Source-level.
- `FREDEconomicSource` historical CPI: near-miss documented in landmines.md.
- `GT_FRESHNESS_SECONDS=300` (`base.py:31`) is the only freshness floor; sources with no `data_published_at` are always "fresh" per `base.py:85-95`.

### Gate constants that span multiple pipelines

| Constant | Value | Location | Spans |
|---|---|---|---|
| `CONFIDENCE_THRESHOLD` | 0.30 (was 0.45, originally 0.80) | `resolution/confidence.py:70` | All info-signal pipelines |
| `MARGINAL_THRESHOLD` | 0.85 | `resolution/confidence.py:71` | All info-signal pipelines |
| `GT_FRESHNESS_SECONDS` | 300 (was 60) | `data/ground_truth/base.py:31` | All info-signal pipelines |
| `SLIPPAGE_BUFFER` | 0.01 (was 0.03) | `resolution/gap_detector.py:50` | All info-signal + cross-platform |
| `MIN_HOURS_TO_RESOLUTION` | 0.25 | `resolution/gap_detector.py:53` | All info-signal + cross-platform |
| `MAX_SERIES_EXPOSURE_FRACTION_GHOST` | 0.50 | executor.py | All pipelines |
| `MAX_SERIES_EXPOSURE_FRACTION` | 0.15 | executor.py | All pipelines (live) |
| `MAX_SIGNALS_PER_SOURCE_ACTION` | (per code) | executor.py near `_dedup_signals` | Info-signal dedup |
| `WINNER_YES_PRICE_GATE` / `ADJACENT_YES_PRICE_GATE` | 0.85 / 0.15 | `strategies/weather_peak_snipe.py:60-61` | weather_peak_snipe only |
| `_YES_FULLY_PRICED` / `_NO_FULLY_PRICED` | 0.97 / 0.03 | `strategies/weather_snipe.py:37-38` | weather_snipe only |

---

## Coverage check

### GT sources in router (`router.py:139-160`) — all documented

- ✅ `SportsLiveSource` → Section 5
- ✅ `SportsDataSource` → Section 6
- ✅ `EconomicDataSource` → Section 2
- ✅ `EIADataSource` → Section 3
- ✅ `FREDEconomicSource` → Section 4
- ✅ `FinancialDataSource` → Section 1
- ✅ `CongressSource` → Section 7
- ✅ `FederalRegisterSource` → Section 8
- ✅ `RottenTomatoesSource` → Section 9

### Non-router GT sources — documented

- ✅ `CrossPlatformSource` (invoked by `GapDetector.run_cross_platform_scan`) → Section 10
- ✅ ASOS feed (`asos_timeseries.py`) — used by weather_peak_snipe → Section 11
- ✅ NWS CLI feed (`weather_cli.py`) — used by weather_snipe → Section 12
- Note: `weather_kalshi.py` and `weather_timezones.py` are parsing infrastructure shared by both snipe strategies; not strategies in their own right (see `audit/repo_inventory.md:192-199`).

### Named strategies in `strategies/`

- ✅ `weather_peak_snipe.py` → Section 11
- ✅ `weather_snipe.py` (disabled) → Section 12
- No other modules in `strategies/` (per `audit/repo_inventory.md:121-124`).

### Executor entry methods — traced

- ✅ `_try_execute` (`executor.py:2286`) — info signals via `_fetch_info_signals` + fuzzy signals via `gap_detector.run_cross_platform_scan`.
- ✅ `place_snipe_trade` (`executor.py:3140`) — both snipe strategies via scanner dispatchers and the `snipe_callback` plumbing.
- No other order-placing methods in the executor (grep confirms `_dispatch_*`, `place_*_trade`, `_try_execute` only).

### Dispatch surfaces

- ✅ `_dispatch_weather_snipe` (`scanner.py:253`) — feeds `place_snipe_trade`.
- ✅ `_dispatch_weather_peak_snipe_batch` (`scanner.py:178`) — feeds `place_snipe_trade`.
- ✅ `_fetch_info_signals` (executor.py — referenced at line 1166) — feeds `_try_execute`.
- ✅ `run_cross_platform_scan` (`gap_detector.py:351`) — feeds `_try_execute`.

### Pipelines that produce trades but aren't otherwise covered

None identified. Grep for `place_` and `_try_execute` and `snipe_callback` shows no additional surfaces.

---

## Open questions for Sunny

1. **EIA source live or dormant?** Zero `EIA/EPM0` source_name appearances in the 45h gate_events window suggest `EIA_API_KEY` is unset in production, making `EIADataSource` dead code on this bot. Confirm — if intended off, that's fine; if intended on, the key needs to be set.

2. **`FREDEconomicSource` vs `EconomicDataSource` overlap.** Both claim CPI/jobs/Fed-decision markets and run in parallel. Per `router.py:374-422` the router takes max-confidence among tradeable sources. [INFERENCE] On a fresh release both will agree, but [INFERENCE] their shared FRED upstream means a single bad fetch can score 0.95 twice. Is the duplication intentional belt-and-suspenders, or an accident of incremental migration?

3. **AAA daily gas (`KXAAAGASD`)** — daily question, weekly underlying. Even with the LARGE_DIVERGENCE gate blocking, the source is still chosen and CPU spent. Should this series be excluded outright, similar to `KXBRENTD`?

4. **`KXNATGASD` natural gas** — 413× `implausible_gap` invariant in 45h. Verify the NG=F Yahoo symbol is the same series Kalshi settles against (continuous front-month vs settlement-month). If not, this is a fourth wrong-instrument class.

5. **Cross-platform pair quality at 0.60 similarity.** Threshold loosened from default — is there a recent ghost-trade audit of pair-match accuracy? Without it the loosening is unsupported (landmine #7).

6. **TradeRecord.signal=None on snipe entries** (`executor.py:3157-3166` TODO, audited 2026-04-30). Affects per-class P&L attribution for both weather strategies. Marked as a pre-live blocker — still pre-live, so still acceptable, but per-class P&L analysis should be aware.

7. **Re-tightening loosened gates** — landmine #8 names three gates that were loosened on 5/5 (CONFIDENCE_THRESHOLD 0.45→0.30, GT freshness 60→300s, CONFIDENCE_GATE_GHOST_CROSS_PLATFORM 0.50→0.30). All marked as needing data-justified re-tightening. Status of each?
