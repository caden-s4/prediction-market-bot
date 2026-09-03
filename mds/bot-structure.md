Bot Structure

Architecture reference for the prediction_market_bot. Pure structure — no operational landmines, no to-do items. Those live in `landmines.md` and chat context.

Inventory snapshot: 2026-06-04 (post-SQLite-3b1). Path: `C:\Users\caden\Desktop\prediction_market_bot`.

---

## Pipeline (execution order)
Scanner → Tier Registry → Priority Scorer → GT Router → Gap Detector →
Confidence Scorer → Executor → Decay Monitor

Each cycle runs the pipeline against the active market pool. Gate events emit at every rejection point to `data/runtime/gate_events.jsonl` for funnel analysis.

### Stage purpose

| Stage | File | Purpose |
|-------|------|---------|
| Scanner | `resolution/scanner.py` | Ingests markets from Kalshi (bulk + per-category + financial-bracket supplements + Polymarket sweep). Applies first-pass filters (excluded, financial_bracket_disabled, category, hours, price). Maintains T1/T2 tier pool. Dispatches snipe candidates. |
| Tier Registry | `resolution/tier_registry.py` | Tracks "sticky" / urgent markets across cycles. Persists to `data/runtime/.tier_sticky.json`. |
| Priority Scorer | `resolution/priority.py` | Orders markets within tiers for the rest of the cycle. |
| GT Router | `data/ground_truth/router.py` | For each market, tries every registered source's `can_handle()`. Returns highest-confidence tradeable result. Emits `gt_routing` events on rejection. |
| Gap Detector | `resolution/gap_detector.py` | Computes `abs(gt_prob - market_price)`. Emits `invariant_violation: implausible_gap` for gaps >0.40. Generates ACTIONABLE signals on real gaps. |
| Confidence Scorer | `resolution/confidence.py` | 2D scoring on source confidence + resolution clarity. Emits `confidence` events on gate failure. |
| Executor | `resolution/executor.py` | Pre-trade gates (empty_book_ghost, perm_skip, gt_stale, large_divergence, series_cap). Places ghost or live orders. Manages position state. |
| Decay Monitor | `resolution/decay_monitor.py` | Monitors open positions for exit conditions (approach-exit, stop-loss, profit target). Filters Pass 2 by non-None price. |

---

## Top-level entry points

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point. Parses args (`--once`, `--info`, `--test-signal`, `--suppress-signal`, `--compare`, `--replay`, `--force-test`, `--log-level`). Starts continuous scan cycles. |
| `bot.py` | `BotCoordinator` for resolution drift arbitrage. Runs the main scan loop, owns shared infrastructure (`FeeCache`, `ExclusionList`, `Bankroll`), persists ghost state. Ghost state save/load routes through dispatcher pattern: `dry_run AND get_db_path().exists()` → SQLite, otherwise → JSON-equivalent legacy path. |
| `tui.py` | Textual-based terminal UI shell. Currently mock data via `MockDataProvider`. Real data integration via `BotCoordinator` pending. |
| `data/runtime/sqlite_store.py` | SQLite data layer for ghost mode state. Per-thread connections (WAL mode), schema creation, atomic CRUD helpers including `close_position_atomic`, `partial_exit_atomic`, `clear_all_ghost_positions_atomic`. Consumed by `bot.py` and `resolution/executor.py` via the dispatcher pattern. JSON methods exist as `_json` suffixed variants until Phase SQLite-3b2 cleanup. |
| `RUNME.py` | One-off probe. Fetches a single Kalshi market and prints rule fields. |

---

## Directory map
prediction_market_bot/
├── main.py                 CLI entry
├── bot.py                  BotCoordinator + scan loop
├── tui.py                  Textual UI shell
│
├── resolution/             Pipeline stages
│   ├── scanner.py
│   ├── tier_registry.py
│   ├── priority.py
│   ├── gap_detector.py
│   ├── confidence.py
│   ├── executor.py
│   ├── decay_monitor.py
│   └── signal_stats.py
│
├── data/
│   ├── markets/            Kalshi / Polymarket REST + WS clients
│   ├── ground_truth/       GT sources + router
│   ├── sports/             SportsLiveSource, MarketMatcher, ShockDetector, LiveGameMonitor, resolution_detector
│   ├── historical/         One-time scrapes (kalshi_markets.jsonl ~4.7GB, candles)
│   ├── runtime/
│   │   ├── sqlite_store.py SQLite data layer (per-thread, WAL mode)
│   │   ├── bot_state.db    SQLite database (ghost mode source of truth)
│   │   └── ...             other runtime state files
│   ├── release_calendar.py Economic release schedule
│   ├── exclusions.json     Static exclusion list
│   ├── calibration.db / events.db / state.json
│
├── strategies/
│   ├── weather_snipe.py            Legacy T-60 close-window sniper (disabled)
│   └── weather_peak_snipe.py       Phase 14b post-peak monotonic-decline trigger
│
├── monitoring/
│   ├── gate_events.py      log_gate_event() JSONL writer
│   ├── gate_names.py       Gate + reason constants
│   ├── alerts.py
│   ├── event_db.py
│   └── tui_state.py
│
├── shared/
│   ├── bankroll.py         Reserve / release accounting
│   ├── exclusion_list.py   Runtime exclusion logic
│   ├── fee_cache.py
│   └── paper_log.py        Owner of ghost_trades.jsonl
│
├── scripts/
│   ├── gate_funnel.py              Primary diagnostic tool
│   ├── gate_events_tail.py
│   ├── migrate_to_sqlite.py        Dry-run-then-apply JSON→SQLite migration
│   ├── phase0_accuracy.py
│   ├── phase1b_weather_validation.py
│   ├── shadow_analysis.py
│   ├── analyze_kalshi_hist.py
│   ├── probe_kalshi_hist.py
│   ├── backtest.py
│   └── scratch/                    Gitignored one-off scripts
│
├── tests/                  pytest suite
│
├── config/                 Signal testing config
├── diagnostics/            Backfill + report generation
├── legacy/                 Old pipeline (not active)
└── utils/                  logger, storage

---

## Ground truth sources

Routed by `data/ground_truth/router.py:_build_default_sources()` (lines 139–160). The router tries every claimant source and returns the highest-confidence tradeable result.

| Source class (file) | Tickers / topics handled | Freshness |
|---------------------|--------------------------|-----------|
| `SportsLiveSource` (`data/sports/live_source.py`) | In-progress NBA / NCAAB / NFL game markets via `MarketMatcher`; reads `LiveGameMonitor` snapshots + `ShockDetector` cache. Registered before `SportsDataSource` so shock signals shadow ESPN-final results. | ESPN polled by `LiveGameMonitor.refresh_if_stale()` at cycle start (15s budget). `tradeable=False` for pre-game / early-period / stale data. Confidence tiers 0.92 / 0.85 / 0.78. |
| `SportsDataSource` (`data/ground_truth/sports.py`) | NFL, NBA, MLB, NHL, MLS, NCAAF, NCAAB (`KXNCAAMBGAME`), NCAAW (`KXNCAAWBGAME`), soccer (EPL, Champions League, La Liga, Bundesliga, Serie A), golf (PGA / LPGA majors + named tour events), racing. ESPN site API. | Module-level cache, TTL 90s. Confidence 0.95 final, 0.65 in-progress final-period substantial (≥28% edge), 0.65 pre-game with moneyline odds, 0.0 pre-game without odds, None when in-progress but not final period or lead too small. |
| `EconomicDataSource` (`data/ground_truth/economic.py`) | FRED + BLS classic-API: CPI, GDP, unemployment, Fed rate decisions, PPI, PCE, AAA weekly gas (`KXAAAGASW`). Rejects foreign-economy markets. | Confidence 0.95 for published data; 0.0 before release. |
| `EIADataSource` (`data/ground_truth/eia.py`) | EIA Weekly U.S. Regular Conventional Retail Gas series. Matches `KXAAAGASW*` and gas-price keywords. Disabled without `EIA_API_KEY`. | Released Mondays ~5pm ET. Confidence 0.90 if ≤24h old, 0.80 up to 168h, None beyond. |
| `FREDEconomicSource` (`data/ground_truth/economic_fred.py`) | FRED JSON API: CPI (CPIAUCSL), Core CPI (CPILFESL), 10Y Breakeven (T10YIE), Unemployment, Nonfarm Payrolls (PAYEMS), Fed Funds Rate, GDP, 30Y Mortgage Rate. Disabled without `FRED_API_KEY`. Confidence 0.90. | Per-series datetime cache. TTLs match publication frequency. |
| `FinancialDataSource` (`data/ground_truth/financial.py`) | Indices (NQ=F, ES=F, YM=F, ^GSPC, ^RUT, ^VIX), forex (EUR/USD, USD/JPY, GBP/USD, USD/CAD, AUD/USD, USD/CHF), Treasury yields (^TNX, ^FVX, ^IRX, ^TYX), commodities (GC=F, CL=F, NG=F, SI=F). Excludes KXVOTEHUB, KXPRESMENTION, KXMENTION, KXAPPROVAL, KXBRENTD, KXBRENTW, KXMVECROSSCATEGORY. | Provider order: Alpha Vantage → Twelve Data → Yahoo Finance. 60s symbol cache. Yahoo confidence capped at 0.55 beyond 8h to resolution. `GT_FRESHNESS_SECONDS=300` (loosened from 60). |
| `CongressSource` (`data/ground_truth/congress.py`) | Congress.gov v3 API. Bill passage, signed-into-law, vetoed, floor-failed. | Confidence: 0.95 signed/vetoed, 0.85 failed, 0.75 keyword-match, 0.60 passed-one-chamber, 0.50 introduced. None for unresolved. |
| `FederalRegisterSource` (`data/ground_truth/federal_register.py`) | Federal Register API + CourtListener. Skips sports and count series. | Confidence: 0.90 final rule, 0.85 enforcement/consent, 0.75 interim final, 0.50 other notice, None for proposed rules. |
| `RottenTomatoesSource` (`data/ground_truth/rotten_tomatoes.py`) | RT internal search API. | 30-min TTL cache. Confidence: 0.90 ≥40 reviews, 0.80 10-39, 0.60 5-9, None <5. |
| `CrossPlatformSource` (`data/ground_truth/cross_platform.py`) | **Not** a `DataSource` subclass. Paired Kalshi↔Polymarket markets via fuzzy title match. Invoked by `GapDetector.run_cross_platform_scan()`. | Pair cache rebuilt by `build_pairs()`. Confidence = `POLYMARKET_AS_GT_CONFIDENCE × similarity_score`, below the 0.80 auto-trade gate. |

### Weather GT infrastructure (not in router; used directly by snipe strategies)

| File | Purpose |
|------|---------|
| `data/ground_truth/weather_cli.py` | NWS Daily Climatological Report fetcher/parser. `fetch_asos_running_extreme()` returns day's running max/min for a station. |
| `data/ground_truth/weather_kalshi.py` | Kalshi weather ticker parser. Maps city codes to NWS CLI stations. Bracket interpretation `B{N}` → `[N-0.5, N+0.5]`. |
| `data/ground_truth/asos_timeseries.py` | IEM ASOS hourly METAR fetcher. Per-process TTL cache by (station, lookback_hours). Used by peak-snipe monotonic-trend detection. |
| `data/ground_truth/weather_timezones.py` | City → IANA timezone lookup. |

---

## Strategies

| Class | Signal output | Dispatch |
|-------|---------------|----------|
| `weather_snipe.SnipeSignal` (built by `evaluate_snipe()`) | Dataclass: market_id, action (buy_yes/buy_no), target_price, edge, confidence (0.99), rationale, plus diagnostic fields. | `resolution/scanner.py:619` calls `_dispatch_weather_snipe()` per market. When `DISABLE_LEGACY_WEATHER_SNIPE` is true (`scanner.py:606`), dispatch is suppressed and `scanner_reject:legacy_weather_snipe_disabled` emits instead. Callback invokes `executor.place_snipe_trade()` (~line 3128). |
| `weather_peak_snipe.WeatherPeakSnipeSignal` (built by `evaluate_event_signals()`) | Dataclass: same shape as SnipeSignal plus `signal_class="weather_peak_snipe"`, `max_risk_usd=5.0`, `trigger_event_id`, `bracket_kind` (winner/adjacent). Up to 5 signals per trigger event (winner YES + ±1/±2 adjacent NOs). | `resolution/scanner.py:699` calls `_dispatch_weather_peak_snipe_batch()` once per cycle (batched). Ghost-only — returns `[]` if `dry_run=False`; executor adds defense-in-depth at `executor.py:3142`. |

---

## Gate events

All gate decisions emit JSONL to `data/runtime/gate_events.jsonl` via `monitoring/gate_events.py:log_gate_event()`. Schema: `ts, ticker, gate, decision, reason, cycle_id, platform, extra`. Reasons defined in `monitoring/gate_names.py`.

**Note:** `fill_cap` events have been observed in `gate_events.jsonl` but are not yet enumerated below. As of 2026-06-04 the firing path has not been traced. Tier 2 item — verify the constant, the check site, and whether it's the unimplemented orderbook-depth fill cap from landmines #3.

### `scanner_reject`
- `excluded` — `ExclusionList.is_excluded()` (`scanner.py:785`)
- `financial_bracket_disabled` — `DISABLE_FINANCIAL_BRACKETS` flag + `_FINANCIAL_BRACKET_PREFIXES` match (`scanner.py:789`)
- `category` — category in `EXCLUDED_CATEGORIES` or `is_weather_market()` (`scanner.py:791`)
- `hours` — `hours_to_resolution` outside window (`scanner.py:811`)
- `price` — `yes_price` not in (0.0, 1.0) (`scanner.py:817`)
- `legacy_weather_snipe_disabled` — Phase 15e suppression (`scanner.py:611`)
- `economic_bracket_disabled` — economic-bracket disable (e.g. KXAAAGASD per Gas-Disable phase)

Call sites: `scanner.py:589, 613, 641, 676, 718, 755`.

### `gt_routing`
- `no_source_matched` — no source claimed (`router.py:360`)
- `source_not_tradeable` — best candidate had `is_tradeable=False` (`router.py:438`)
- `source_returned_none` — every source returned None (`router.py:455`)

### `confidence`
- `direction_ambiguous` — GT directional confidence ambiguous (`confidence.py:203`)
- `source_below_gate` — source confidence below threshold (`confidence.py:312`)
- `clarity_below_gate` — clarity below threshold (`confidence.py:314`)
- `both_below_gate` — both below threshold (`confidence.py:310`)
- `freshness_below_gate` — defined but no current emit site

### `executor_pretrade`
- `empty_book_ghost` — empty book on game/financial-bracket in dry-run (`executor.py:2340`)
- `perm_skip_confidence_failures` — 3rd consecutive confidence failure (`executor.py:2430`)
- `perm_skip_stop_losses` — 2+ consecutive stop_losses on same (ticker, signal_source) pair. Counter at `executor.py:680` (`_consecutive_stop_losses` dict, hydrated from SQLite on init), check at `executor.py:2368`. Bleed-Fix-1, commit 99e77bb.
- `gt_stale_at_entry` — GT failed `is_fresh()` after re-fetch (`executor.py:2486`)
- `large_divergence_extreme_market` — large divergence against extreme market price (`executor.py:2591`)
- `series_cap` — per-series exposure cap (`executor.py:2859`)
- `bankroll` — emitted via snipe path (`executor.py:3299`)
- `dedup` — emitted via snipe path (`executor.py:3165`)

### `snipe`
- `no_signal` — shadow evaluator returned None (`scanner.py:323`)
- `ghost_only` — peak-snipe signal when `_dry_run=False` (`executor.py:3147`)
- `dedup` — market already in `_positions` (`executor.py:3165`)
- `empty_book_snipe` — no live book during placement (`executor.py:3187, 3208`)
- `series_cap` — per-series cap during snipe (`executor.py:3283`)
- `bankroll` — `_bankroll.reserve()` failed (`executor.py:3299`)
- `evaluated` (decision="evaluated", reason=None) — peak-snipe trigger fired (`weather_peak_snipe.py:638`)
- `price_gate` (decision="skip") — peak-snipe trigger fired, no bracket passed 0.85/0.15 gates (`weather_peak_snipe.py:680`)

### `invariant_violation` (literal string, not in `gate_names.py`)
Decisions emit instead of reasons:
- `kalshi_mid_out_of_range` — Kalshi mid not in [0.0, 1.0] (`data/markets/kalshi.py:43`)
- `ws_rest_mid_disagreement` — WS mid vs REST mid >0.05 apart (`scanner.py:51`)
- `implausible_gap` — raw |gt_prob - market_price| >0.40 (`gap_detector.py:225`)
- Untagged emits from `data/markets/kalshi_ws.py:42`

---

## Persistent state (`data/runtime/`)

| File | Persists |
|------|----------|
| `bot_state.db` | SQLite database, source of truth for ghost mode state. Three tables. `ghost_positions` columns mirror legacy JSON 1:1: ticker, market_id, platform, action, entry_price, size_usd, ground_truth_prob, source_confidence, entry_time, order_id, resolution_date_iso, question, category, tags_json, fill_status, limit_price_used, created_at. `ghost_state` is single-row: total_usd, realized_pnl_usd, last_updated. `perm_skip_counters` is Bleed-Fix-1 counter persistence: ticker, signal_source, count, last_updated. Per-thread connections via `sqlite_store.get_connection()`. WAL mode enabled. Created 2026-06-04 via Phase SQLite-3b1 migration. |
| `bot_state.db-wal` / `bot_state.db-shm` | SQLite WAL mode sidecar files. Auto-managed by SQLite. Do not edit, do not commit. |
| `ghost_positions.json.migrated_20260604` | Renamed snapshot from pre-SQLite era. Preserved for forensics, never read by the bot. Safe to archive or delete after stability is confirmed. |
| `ghost_state.json.migrated_20260604` | Same as above. |
| `.tier_sticky.json` | Market IDs flagged sticky/urgent by `TierRegistry`. Written immediately on change (`tier_registry.py:39, 318, 340`). |
| `cli_validation_cache.json` | NWS CLI reports keyed by station:date. Populated by `phase1b_weather_validation.py`. |
| `dispatched_finals.json` | ESPN game IDs whose final-result threads already dispatched. `resolution_detector.py:68` reads to avoid re-fire. |
| `gate_events.jsonl` | One JSON line per `log_gate_event()` call. Schema_version 1. Append-only. |
| `ghost_trades.jsonl` | Append-only ghost-trade journal. Owner: `shared/paper_log.py:PaperTradeLog`. Stays as JSONL (append-only, crash-safe; access pattern is wrong for SQLite). |
| `kalshi_settled_cache.json` | Settled Kalshi results keyed by ticker. Populated by `phase1b_weather_validation.py`. |
| `phase0_accuracy_results.csv` | Per-trade settlement accuracy output. |
| `phase1b_weather_validation.csv` | Per-bracket weather validation output. |
| `settlement_cache.json` | Kalshi settlement-result cache used by `phase0_accuracy.py`. |
| `tui_state.json` | Per-cycle TUI snapshot. Atomic write via tmp + rename. Written by `tui_state.py:65` and `bot.py:483` at end of every cycle. |

---

## Diagnostic tools

| Tool | Purpose |
|------|---------|
| `scripts/gate_funnel.py` | Primary diagnostic. Aggregates `gate_events.jsonl` by gate → reason → ticker. Flags: `--since <duration>`, `--gate <name>`, `--reason <name>`, `--ticker <prefix>`, `--detail`. |
| `scripts/migrate_to_sqlite.py` | Dry-run-then-apply migration script. JSON state → SQLite. `--apply` flag, refuses to overwrite existing DB, rolls back temp DB on round-trip failure. |
| `scripts/gate_events_tail.py` | Real-time tail of gate events with filters. |
| `scripts/phase0_accuracy.py` | Per-trade settlement accuracy by source / signal class. |
| `scripts/phase1b_weather_validation.py` | Per-bracket weather validation against ASOS + Kalshi settlements. |
| `scripts/shadow_analysis.py` | Joins shadow-logged weather signals against Kalshi resolution outcomes. |
| `scripts/probe_kalshi_hist.py` | Backfills market history from Kalshi `/candlesticks` endpoint. |
| `scripts/analyze_kalshi_hist.py` | Reads `data/historical/kalshi_markets.jsonl` for settlement analysis. |
| `scripts/backtest.py` | Strategy backtest harness. |

---

## Modes

| Mode | Behavior |
|------|----------|
| Ghost | Default. No real orders. All entries write to `ghost_trades.jsonl` with `event=entry` records. Bankroll is virtual via `bot_state.db` ghost_state table. State persistence routes through SQLite dispatcher. |
| Live | Real Kalshi orders. Enabled only on explicit sessions. Currently unused — bot is ghost-only. State persists via `self._state` StateStore, unchanged by SQLite migration. |

---

## Configuration constants (current values)

| Constant | Value | Location |
|----------|-------|----------|
| `GT_FRESHNESS_SECONDS` | 300 (loosened from 60 on 5/5) | financial.py / executor.py |
| `CONFIDENCE_THRESHOLD` | 0.30 (loosened from 0.45 on 5/5) | confidence.py |
| `CONFIDENCE_GATE_GHOST_CROSS_PLATFORM` | 0.30 (loosened from 0.50 on 5/5) | confidence.py |
| `MAX_SERIES_EXPOSURE_FRACTION` | 0.15 (live) | executor.py |
| `MAX_SERIES_EXPOSURE_FRACTION_GHOST` | 0.50 (ghost) | executor.py |
| `WINNER_YES_PRICE_GATE` | 0.85 (peak snipe winner bracket) | weather_peak_snipe.py |
| `ADJACENT_YES_PRICE_GATE` | 0.15 (peak snipe adjacent brackets) | weather_peak_snipe.py |
| `_MAX_BYTES` | 150 * 1024 * 1024 (150 MB) | utils/logger.py (LogRetention-1, commit f92ca58) |
| `_BACKUP_COUNT` | 20 (21 total files including bot.log; ~3.15 GB cap) | utils/logger.py |
| `_STOP_LOSS_PERM_SKIP_THRESHOLD` | 2 | resolution/executor.py:169 (Bleed-Fix-1, commit 99e77bb) |

---

## See also

- `landmines.md` — operational risks and known classes of bug
- `interacting-with-sunny.md` — communication style
- `prompting-claude-code.md` — handoff format and rules