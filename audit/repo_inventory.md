# Repository Inventory

Generated 2026-05-11. Factual snapshot only.

---

## 1. Directory tree (2 levels deep)

Excluded: `__pycache__/`, `.git/`, `node_modules/`, `audit/`, `data/runtime/`.

```
prediction_market_bot/
├── .claude/
│   ├── CLAUDE.md
│   ├── agents/
│   │   ├── gate-auditor.md
│   │   └── log-diagnoser.md
│   ├── rules/
│   │   ├── executor.md
│   │   ├── kalshi.md
│   │   └── sports.md
│   ├── scheduled_tasks.lock
│   └── settings.local.json
├── .codex/
│   └── environments/
├── .env
├── .env.example
├── .gitignore
├── .pytest_cache/
├── .tracecov/
├── AGENTS.md
├── CLAUDE.md
├── RUNME.py
├── SETUP.txt
├── archive.zip
├── bot.py
├── config/
│   ├── __init__.py
│   └── signal_testing.py
├── crypto.zip
├── data/
│   ├── __init__.py
│   ├── calibration.db
│   ├── events.db
│   ├── exclusions.json
│   ├── ground_truth/
│   ├── historical/
│   ├── markets/
│   ├── release_calendar.py
│   ├── sports/
│   └── state.json
├── diagnostics/
│   ├── _backfill_phase2.py
│   ├── archive/
│   ├── generate_report.py
│   └── latest_report.txt
├── launch_tui.ps1
├── legacy/
│   ├── __init__.py
│   ├── adapters/
│   ├── backtest/
│   ├── data/
│   ├── execution/
│   ├── live_trading.py
│   ├── meta/
│   ├── pipeline/
│   ├── risk/
│   └── signals/
├── logs/
│   ├── bot.log
│   ├── bot.log.1
│   ├── bot.log.2
│   ├── bot.log.3
│   ├── bot_run_tmp.log
│   └── snipe_smoke.log
├── main.py
├── maker/                                (empty)
├── monitoring/
│   ├── __init__.py
│   ├── alerts.py
│   ├── event_db.py
│   ├── gate_events.py
│   ├── gate_names.py
│   └── tui_state.py
├── prediction_market_bot_roadmap.docx
├── requirements.txt
├── resolution/
│   ├── __init__.py
│   ├── confidence.py
│   ├── decay_monitor.py
│   ├── executor.py
│   ├── gap_detector.py
│   ├── priority.py
│   ├── scanner.py
│   ├── signal_stats.py
│   └── tier_registry.py
├── scripts/
│   ├── _inspect_weather_resolution.py
│   ├── _inventory_weather.py
│   ├── _phase0_diagnostic.py
│   ├── analyze_kalshi_hist.py
│   ├── backtest.py
│   ├── diag.py
│   ├── gate_events_tail.py
│   ├── gate_funnel.py
│   ├── latest.pdf
│   ├── phase0_accuracy.py
│   ├── phase1b_weather_validation.py
│   ├── probe_kalshi_hist.py
│   ├── run.py
│   ├── runthis.py
│   ├── scratch/
│   ├── shadow_analysis.py
│   └── stations.pdf
├── shared/
│   ├── __init__.py
│   ├── bankroll.py
│   ├── exclusion_list.py
│   ├── fee_cache.py
│   └── paper_log.py
├── strategies/
│   ├── __init__.py
│   ├── weather_peak_snipe.py
│   └── weather_snipe.py
├── tests/
│   ├── _cli_cache_lax_final.txt
│   ├── conftest.py
│   ├── test_confidence.py
│   ├── test_config.py
│   ├── test_exclusions_and_fees.py
│   ├── test_executor_snipe.py
│   ├── test_gap_detector.py
│   ├── test_live_game_monitor.py
│   ├── test_market_core.py
│   ├── test_risk_and_state.py
│   ├── test_router.py
│   ├── test_scanner_weather_snipe_dispatch.py
│   ├── test_tier_registry.py
│   ├── test_tui_state.py
│   ├── test_weather_cli.py
│   ├── test_weather_kalshi.py
│   ├── test_weather_peak_snipe.py
│   └── test_weather_snipe.py
├── trading-bot-project-instructions.md
├── tui.py
├── utils/
│   ├── __init__.py
│   ├── logger.py
│   └── storage.py
└── venv/                                  (Python virtual environment, not expanded)
```

---

## 2. Top-level Python modules

| File | One-sentence purpose |
|------|----------------------|
| `bot.py` | `BotCoordinator` for resolution drift arbitrage; runs the main scan loop, owns shared infrastructure (`FeeCache`, `ExclusionList`, `Bankroll`), and persists ghost state. |
| `main.py` | CLI entry point; parses args (`--once`, `--info`, `--test-signal`, `--suppress-signal`, `--compare`, `--replay`, `--force-test`, `--log-level`) and starts continuous scan cycles. |
| `tui.py` | Textual-based terminal UI shell (currently mock data via `MockDataProvider`). |
| `RUNME.py` | One-off probe that fetches a single Kalshi market and prints its `rules_primary`, `rules_secondary`, and rule-related raw fields. |

---

## 3. Strategies (`strategies/`)

| Strategy class | Signal output type | Dispatched from |
|----------------|--------------------|-----------------|
| `weather_snipe.SnipeSignal` (built by `evaluate_snipe()`) | Dataclass: `market_id`, `action` (`buy_yes`/`buy_no`), `target_price`, `edge`, `confidence` (0.99), `rationale`, plus diagnostic fields (`gt_prob`, `asos_temp_f`, `bracket_low/high`, `market_mid`). | `resolution/scanner.py:619` calls `_dispatch_weather_snipe(m, self._snipe_callback)` per market. Snipe candidate filter at `scanner.py:300` (shadow). When `DISABLE_LEGACY_WEATHER_SNIPE` is true (`scanner.py:606`), dispatch is suppressed and a `scanner_reject:legacy_weather_snipe_disabled` event is emitted instead. Callback ultimately invokes `resolution/executor.py:place_snipe_trade()` (around line 3128). |
| `weather_peak_snipe.WeatherPeakSnipeSignal` (built by `evaluate_event_signals()`) | Dataclass: same shape as `SnipeSignal` plus `signal_class="weather_peak_snipe"`, `max_risk_usd=5.0`, `trigger_event_id`, `bracket_kind` (`winner`/`adjacent`). Up to 5 signals per trigger event (winner YES + ±1/±2 adjacent NOs). | `resolution/scanner.py:699` calls `_dispatch_weather_peak_snipe_batch(peak_snipe_candidates, self._snipe_callback)` once per cycle (batched, not per-market). Ghost-only — strategy returns `[]` if `dry_run=False`; executor adds defense-in-depth check at `executor.py:3142`. |

---

## 4. Ground truth sources (`data/ground_truth/`)

Routed by `data/ground_truth/router.py:_build_default_sources()` (lines 139–160). The router tries every claimant source and returns the highest-confidence tradeable result.

| Source class (file) | Tickers / topics handled | Freshness characteristics |
|---------------------|--------------------------|---------------------------|
| `SportsLiveSource` (`data/sports/live_source.py`) | In-progress NBA / NCAAB / NFL game markets matched via `MarketMatcher`; reads `LiveGameMonitor` snapshots + `ShockDetector` cache. Registered before `SportsDataSource` so shock signals shadow ESPN-final results. | ESPN polled by `LiveGameMonitor.refresh_if_stale()` at cycle start (15s budget). Returns `tradeable=False` for pre-game / early-period / stale data. Confidence tiers 0.92 / 0.85 / 0.78 (shock_detector `_score_confidence`). |
| `SportsDataSource` (`data/ground_truth/sports.py`) | NFL, NBA, MLB, NHL, MLS, NCAAF, NCAAB (`KXNCAAMBGAME`), NCAAW (`KXNCAAWBGAME`), soccer (EPL, Champions League, La Liga, Bundesliga, Serie A), golf (PGA / LPGA majors + named tour events), racing (`racing/*`). Endpoint: `site.api.espn.com/apis/site/v2/...`. | Module-level `_SCOREBOARD_CACHE`, TTL 90 s. Confidence: 0.95 final, 0.65 in-progress final-period substantial (≥28 % edge), 0.65 pre-game with moneyline odds, 0.0 pre-game without odds, `None` (not tradeable) when in-progress but not final period or lead too small. |
| `EconomicDataSource` (`data/ground_truth/economic.py`) | FRED + BLS classic-API series: CPI, GDP, unemployment, Fed rate decisions, PPI, PCE, AAA weekly gas (`KXAAAGASW`); rejects foreign-economy markets (`_ECON_FOREIGN_INDICATORS`). | Confidence 0.95 for published data; 0.0 before release. Cache and TTL behavior internal to `_fetch_fred_latest()`. |
| `EIADataSource` (`data/ground_truth/eia.py`) | EIA Weekly U.S. Regular Conventional Retail Gas series `EMM_EPMR_PTE_NUS_DPG`; matches `KXAAAGASW*` ticker prefix and gas-price keywords. Disabled (`can_handle()→False`) without `EIA_API_KEY`. | Released Mondays ~5 pm ET. Confidence 0.90 if data ≤24 h old, 0.80 up to 168 h, returns `None` beyond 168 h. |
| `FREDEconomicSource` (`data/ground_truth/economic_fred.py`) | FRED JSON observations API: CPI (CPIAUCSL), Core CPI (CPILFESL), 10Y Breakeven (T10YIE), Unemployment, Nonfarm Payrolls (PAYEMS), Fed Funds Rate, GDP, 30Y Mortgage Rate. Disabled without `FRED_API_KEY`. Confidence 0.90 base. | Per-series datetime cache (single + paired/MoM). Cache TTLs match publication frequency (Fed rate ~6 h, monthly CPI ~12 h). |
| `FinancialDataSource` (`data/ground_truth/financial.py`) | Indices: NQ=F (Nasdaq 100 / NDX), ES=F (S&P 500 / SPX), YM=F (Dow), ^GSPC, ^RUT, ^VIX. Forex: EUR/USD, USD/JPY, GBP/USD, USD/CAD, AUD/USD, USD/CHF. Treasury yields: ^TNX (10Y), ^FVX (5Y), ^IRX (2Y), ^TYX (30Y). Commodities: GC=F (Gold), CL=F (WTI Crude), NG=F (Natural Gas), SI=F (Silver). Excludes `KXVOTEHUB`, `KXPRESMENTION`, `KXMENTION`, `KXAPPROVAL`, `KXBRENTD`, `KXBRENTW`. | Provider order: Alpha Vantage (if `ALPHA_VANTAGE_KEY`) → Twelve Data (if key and symbol not in `_TD_FREE_TIER_BLOCKED`) → Yahoo Finance (`query1.finance.yahoo.com/v8/finance/chart`). Module-level 60 s symbol cache. Yahoo confidence capped at 0.55 beyond 8 h to resolution; 0.85 floor when `SIMULATE_PRO_DATA=true`. Quote staleness window `GT_FRESHNESS_SECONDS=300` (loosened from 60). |
| `CongressSource` (`data/ground_truth/congress.py`) | Congress.gov v3 API; bill passage, signed-into-law, vetoed, floor-failed (categories: politics / legal / government / general matched against `_BILL_KEYWORDS`). | Confidence: 0.95 signed/vetoed, 0.85 failed, 0.75 keyword-match, 0.60 passed-one-chamber, 0.50 introduced. `prob=None` (non-tradeable) for unresolved states. |
| `FederalRegisterSource` (`data/ground_truth/federal_register.py`) | Federal Register API (final rules, enforcement actions, interim rules, proposed rules, presidential documents) + CourtListener (federal court rulings via free public API). Skips sports markets and "or more / or fewer / how many" count series (`KXJUDGECOUNT`, `KXFIREDCOUNT`, etc.). | Confidence: 0.90 final rule, 0.85 enforcement/consent, 0.75 interim final, 0.50 other notice/presidential, `None` (non-tradeable) for proposed rules and guidance docs. |
| `RottenTomatoesSource` (`data/ground_truth/rotten_tomatoes.py`) | RT internal search API; markets mentioning "rotten tomatoes" / "tomatometer" / "rt score" / "tomato score". | 30-min TTL cache (`_RT_CACHE_TTL=1800`). Confidence: 0.90 ≥40 reviews, 0.80 10–39, 0.60 5–9, `None` <5. |
| `CrossPlatformSource` (`data/ground_truth/cross_platform.py`) | NOT a `DataSource` subclass — paired Kalshi↔Polymarket markets via fuzzy title match (`SequenceMatcher` after normalization). Invoked by `GapDetector.run_cross_platform_scan()`, not by the router pipeline. | `_PAIR_CACHE_TTL`-bounded pair cache rebuilt by `build_pairs()`. Confidence = `POLYMARKET_AS_GT_CONFIDENCE × similarity_score` (~0.46–0.78), below the 0.80 auto-trade gate. |

Standalone weather infrastructure not registered in the router (used directly by snipe strategies):

| File | Purpose |
|------|---------|
| `data/ground_truth/weather_cli.py` | NWS Daily Climatological Report (CLI) fetcher/parser; `fetch_asos_running_extreme()` returns the day's running max/min for a given station. |
| `data/ground_truth/weather_kalshi.py` | Kalshi weather ticker parser (`parse_weather_ticker()`); maps city codes to NWS CLI stations; bracket interpretation `B{N}` → `[N-0.5, N+0.5]`. |
| `data/ground_truth/asos_timeseries.py` | IEM ASOS hourly METAR temperature timeseries fetcher (per-process TTL cache by `(station, lookback_hours)`); used by peak-snipe monotonic-trend detection. |
| `data/ground_truth/weather_timezones.py` | `CITY_TZ_MAP` city → IANA timezone lookup. |

---

## 5. Gates emitted via `monitoring/gate_events.py`

Identifiers and reason codes are defined in `monitoring/gate_names.py`. `log_gate_event()` writes one JSON line per call to `data/runtime/gate_events.jsonl` (schema_version 1; thread-safe append).

### `scanner_reject` — `GATE_SCANNER_REJECT`
Reasons emitted (`reason=` field):
- `excluded` — `ExclusionList.is_excluded()` true (`scanner.py:_reject_reason:785`)
- `financial_bracket_disabled` — `DISABLE_FINANCIAL_BRACKETS` flag + `_FINANCIAL_BRACKET_PREFIXES` match (`scanner.py:789`)
- `category` — category in `EXCLUDED_CATEGORIES` or `is_weather_market()` (`scanner.py:791`)
- `hours` — `hours_to_resolution` outside `(0, effective_window]` (48 h game, 72 h financial bracket, else `window_hours`) (`scanner.py:811`)
- `price` — `yes_price` not strictly in `(0.0, 1.0)` (`scanner.py:817`)
- `legacy_weather_snipe_disabled` — phase 15e suppression for would-be weather-snipe candidates when `DISABLE_LEGACY_WEATHER_SNIPE` true (`scanner.py:611`)

Call sites: `scanner.py:589` (general bulk fetch), `scanner.py:613` (legacy weather snipe), `scanner.py:641` (sports supplement), `scanner.py:676` (financial bracket supplement), `scanner.py:718` (per-category Polymarket), `scanner.py:755` (Polymarket near-term sweep).

### `gt_routing` — `GATE_GT_ROUTING`
Reasons:
- `no_source_matched` — no registered source's `can_handle()` returned True (`router.py:360`)
- `source_not_tradeable` — best candidate had `is_tradeable=False` (`router.py:438`)
- `source_returned_none` — every claimant source returned `None` from `fetch()` (`router.py:455`)

Call sites: `router.py:356, 434, 451`.

### `confidence` — `GATE_CONFIDENCE`
Reasons (defined; emitted set listed below):
- `direction_ambiguous` — `ground_truth.directional_confidence == "ambiguous"` (`confidence.py:203`)
- `source_below_gate` — only source confidence below `_threshold` (`confidence.py:312` → reason)
- `clarity_below_gate` — only resolution-clarity below `_threshold` (`confidence.py:314`)
- `both_below_gate` — both dimensions below `_threshold` (`confidence.py:310`)
- `freshness_below_gate` — defined in `gate_names.py:27` (`REASON_FRESHNESS_BELOW_GATE`); no current call site emits it.

Call sites: `confidence.py:199` (ambiguous), `confidence.py:315` (gate-fail composite).

### `executor_pretrade` — `GATE_EXECUTOR_PRETRADE`
Reasons:
- `empty_book_ghost` — empty order book on game/financial-bracket market in dry-run (`executor.py:2340`)
- `perm_skip_confidence_failures` — 3rd consecutive confidence failure for same market (`executor.py:2430`)
- `gt_stale_at_entry` — GT failed `is_fresh(GT_FRESHNESS_SECONDS)` even after re-fetch (`executor.py:2486`)
- `large_divergence_extreme_market` — large divergence flag against extreme (~0/1) market price (`executor.py:2591`)
- `series_cap` — per-series exposure would exceed `MAX_SERIES_EXPOSURE_FRACTION_GHOST` (50 %) or `MAX_SERIES_EXPOSURE_FRACTION` (15 %) (`executor.py:2859`)
- `bankroll` — defined; emitted via the snipe path at `executor.py:3299` rather than pretrade.
- `dedup` — defined; emitted via the snipe path at `executor.py:3165`.

Call sites: `executor.py:2336, 2426, 2482, 2587, 2855`.

### `snipe` — `GATE_SNIPE`
Reasons:
- `no_signal` — shadow-mode evaluator returned `None` for a weather-snipe candidate (`scanner.py:323`)
- `ghost_only` — `weather_peak_snipe` signal arriving when `_dry_run=False` (`executor.py:3147`)
- `dedup` — market already in `_positions` (`executor.py:3165`)
- `empty_book_snipe` — no live book or no side price during snipe placement (`executor.py:3187, 3208`)
- `series_cap` — per-series exposure cap during snipe (`executor.py:3283`)
- `bankroll` — `_bankroll.reserve()` failed (`executor.py:3299`)
- `evaluated` (decision="evaluated", reason=`None`) — peak-snipe trigger fired (`weather_peak_snipe.py:638`)
- `price_gate` (decision="skip") — peak-snipe trigger fired but no bracket passed `WINNER_YES_PRICE_GATE=0.85` / `ADJACENT_YES_PRICE_GATE=0.15` (`weather_peak_snipe.py:680`)

Call sites: `scanner.py:321`, `executor.py:3145, 3163, 3185, 3206, 3281, 3297`, `strategies/weather_peak_snipe.py:635, 678`.

### `invariant_violation` (literal string, not in `gate_names.py`)
Decisions emitted instead of reasons:
- `kalshi_mid_out_of_range` — Kalshi mid not in `[0.0, 1.0]` (`data/markets/kalshi.py:43`)
- `ws_rest_mid_disagreement` — WS mid vs REST mid `> 0.05` apart (`resolution/scanner.py:51`)
- `implausible_gap` — raw |gt_prob - market_price| `> 0.40` (`resolution/gap_detector.py:225`)
- `<unspecified>` from `data/markets/kalshi_ws.py:42` (called inside the WS client; same shape).

---

## 6. Files in `data/runtime/` (state-on-disk)

| Path | Persists |
|------|----------|
| `data/runtime/.tier_sticky.json` | List of market IDs flagged sticky/urgent by `TierRegistry`; loaded at startup, written immediately on change (`resolution/tier_registry.py:39, 318, 340`). |
| `data/runtime/cli_validation_cache.json` | NWS CLI reports keyed by `station:date`, populated by `scripts/phase1b_weather_validation.py`. |
| `data/runtime/cycle_test.log` | Older cycle-test log file (no active writers in the source tree). |
| `data/runtime/dispatched_finals.json` | ESPN game IDs whose final-result threads have already been dispatched, so `data/sports/resolution_detector.py:68` does not re-fire. |
| `data/runtime/gate_events.jsonl` | One JSON line per `log_gate_event()` call (schema_version 1: `ts`, `ticker`, `gate`, `decision`, `reason`, `cycle_id`, `platform`, `extra`); written by `monitoring/gate_events.py:17`. |
| `data/runtime/ghost_positions.json` | Open ghost-trade positions (separate from live positions). Loaded/saved by `resolution/executor.py:_GHOST_POSITIONS_FILE` at line 4244; cleared by `ghost-clear`. |
| `data/runtime/ghost_state.json` | Virtual bankroll snapshot for ghost mode (`saved_at`, `total_usd`, `realized_pnl_usd`); written/loaded by `bot.py:42, 439, 454`. |
| `data/runtime/ghost_trades.jsonl` | Append-only ghost-trade journal. `event="entry"` records (`market_id`, `platform`, `action`, `entry_price`, `size_usd`, `gt_prob`, `gap`, `confidence`, `source`, `tier`, `question`) and `event="exit"` records (`exit_price`, `pnl`, `pnl_pct`, `exit_reason`, `hold_duration_minutes`, `exit_was_decisive_gt`). Owner: `shared/paper_log.py:PaperTradeLog`. |
| `data/runtime/ghost_trades.jsonl.bak.20260409-141509` | Backup snapshot of `ghost_trades.jsonl` taken by `diagnostics/_backfill_phase2.py:154`. |
| `data/runtime/kalshi_settled_cache.json` | Settled Kalshi market results keyed by ticker; populated by `scripts/phase1b_weather_validation.py:37`. |
| `data/runtime/phase0_accuracy_results.csv` | Output of `scripts/phase0_accuracy.py:39` — per-trade settlement accuracy. |
| `data/runtime/phase1b_weather_validation.csv` | Output of `scripts/phase1b_weather_validation.py:39` — per-bracket weather validation. |
| `data/runtime/report_output.txt` | Older diagnostic report output (no active writers in the source tree). |
| `data/runtime/settlement_cache.json` | Kalshi settlement-result cache used by `scripts/phase0_accuracy.py:38` to skip redundant API calls on re-run. |
| `data/runtime/tui_state.json` | Per-cycle TUI snapshot (atomic write via tmp + rename); written by `monitoring/tui_state.py:65` and `bot.py:483` at end of every cycle. |
