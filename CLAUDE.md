# Prediction Market Trading Bot

Python bot on Windows. Trades Kalshi (live), reads Polymarket (read-only). Scans markets, computes ground truth probabilities from external data, detects price gaps, executes when edge exists.

## Architecture (execution order)
- Scanner (`resolution/scanner.py`): discovers Kalshi markets, filters by liquidity/time, overlays live orderbook prices
- Tier Registry (`resolution/tier_registry.py`): T1/T2/T3 time-based classification, `mark_urgent()`
- Priority Scorer (`resolution/priority.py`): ranks by freshness, volume, bracket proximity, staleness
- GT Router (`data/ground_truth/router.py`): routes markets to correct ground truth source
- Gap Detector (`resolution/gap_detector.py`): price-vs-truth divergence, `[SIGNAL]` logging, min_gap thresholds
- Confidence Scorer (`resolution/confidence.py`): source confidence × resolution clarity, threshold 0.80
- Executor (`resolution/executor.py`): GT evaluation, signal generation, order placement, ghost trades (~3000 LOC)
- Decay Monitor: tracks signal staleness, triggers exits

## Ground truth sources
- **FRED** (`data/ground_truth/economic_fred.py`): CPI (CPIAUCSL, CPILFESL), jobs (PAYEMS)
- **Financial** (`data/ground_truth/financial.py`): Yahoo Finance / Twelve Data (ES=F, NQ=F, CL=F, GC=F, ^TNX)
- **Sports** (`data/sports/live_source.py`): ESPN in-game win probability, final-period only (Q4 NBA/NFL, H2 NCAAB)
- **Sports fallback** (`data/ground_truth/sports.py`): SportsDataSource, pre-game/non-final, lower confidence
- **Release calendar** (`data/release_calendar.py`): FRED release windows (pre_release → hold → hunt)

## Module map
```
main.py                              — entry, CLI args (--info, --force-test)
bot.py                               — BotCoordinator, main scan loop
config/__init__.py                   — real config (NOT config.py)
resolution/executor.py               — core trading logic, GT eval, ghost trades
resolution/scanner.py                — market discovery, refresh_markets()
resolution/tier_registry.py          — T1/T2/T3 tiers
resolution/priority.py               — PriorityScorer
resolution/gap_detector.py           — gap calculation, [SIGNAL] logging
resolution/confidence.py             — two-dimensional confidence scorer
resolution/orderbook_monitor.py      — orderbook depth checks
resolution/signal_stats.py           — signal tracking
data/ground_truth/router.py          — GT source routing
data/ground_truth/economic_fred.py   — FRED API
data/ground_truth/financial.py       — Yahoo Finance / Twelve Data
data/ground_truth/sports.py          — SportsDataSource
data/sports/live_source.py           — SportsLiveSource (ESPN in-game)
data/sports/live_game_monitor.py     — ESPN polling, refresh_if_stale()
data/sports/pipeline.py              — SportsSignalPipeline
data/sports/market_matcher.py        — team name extraction, 750+ aliases
data/sports/shock_detector.py        — run_shock_detection()
data/sports/resolution_detector.py   — check_for_new_finals()
data/release_calendar.py             — FRED release calendar
data/markets/kalshi.py               — Kalshi API client, _parse_market()
data/markets/kalshi_ws.py            — Kalshi WebSocket
shared/bankroll.py                   — bankroll management, Kelly sizing
shared/paper_log.py                  — PaperTradeLog, JSONL ghost trade history
shared/fee_cache.py                  — fee lookups
shared/exclusion_list.py             — market exclusions
utils/logger.py                      — logging config
signals/engine.py                    — signal engine
signals/cross_exchange.py            — cross-exchange signals
monitoring/alerts.py                 — alerting
monitoring/event_db.py               — event storage
```

## Current state
- Mode: ghost mode default, live on explicit sessions only
- Phase 1 benchmarks not yet met:
  - [ ] ≥10 actionable signals/week
  - [ ] Paper win rate ≥60% over 30 trades
  - [ ] Average edge ≥3% after fees
  - [ ] Signal-to-trade latency <30s
  - [ ] Zero crashes M-F full week
  - [ ] Can explain every paper trade

## Open issues
- Ghost fill size cap against orderbook depth: NOT IMPLEMENTED
- NFL ESPN 400 error every cycle (off-season, harmless but noisy)
- Freshness scoring: `created_time` in market.raw but parser returns 0 fresh (low priority)

## Do not do
- Do not trust Kalshi/FRED data without checking timestamp freshness
- Do not modify executor without diffing ghost behavior against prior commit
- Do not add dependencies without checking existing utils first
- Do not add unbounded individual Kalshi API fetches (rate limit: 8 req/s token bucket)
- Do not touch illiquidity filter logic, confidence thresholds (0.80/0.85), decay monitor, or live persistence paths
- Do not revert any completed fix listed below

## Kalshi market ID formats
- Financial brackets: `KXNASDAQ100U-26MAR13H1600-T24399.99` (series-date-strike)
- NBA games: `KXNBAGAME-26MAR15INDMIL-MIL` (series-dateTeams-yesTeam)
- NCAAB games: `KXNCAAMBGAME-26MAR14WISMICH-MICH`
- WTI daily: `KXWTI-26MAR13-T80.99` (rollover blocked)
- WTI weekly: `KXWTIW-26MAR13-T97.99` (rollover exempt)
- Novelty/mention: `KXNBAMENTION-*`, `KXNCAABMENTION-*` (auto-excluded)

## Testing
- `python main.py --info` — normal ghost mode
- `python main.py --force-test` — bypasses safety guards (ghost only, blocks if LIVE_TRADING=true)
- `bank 500` — set virtual bankroll
- `paper` — ghost trade daily summary
- `ghost-clear` — wipe ghost positions
- `p` — open positions
- `history` — resolved trades

## Completed fixes (do NOT revert)
- CL=F rollover block, WTI weekly exempt
- NQ=F/ES=F rollover 25% size reduction
- Executor secondary gap check cap
- FRED forward-looking market date check
- Priority scorer 4 dimensions
- FRED release calendar 3-window state machine
- Permanent no-source skip (graduates after 3 fast-skip cycles)
- Signal logging (`[SIGNAL] ACTIONABLE/BLOCKED`)
- Paper trading system (ghost_positions.json + paper_trades.jsonl)
- Ghost mode series cap 50% (vs 15% live)
- `--force-test` flag
- Sports tickers, resolution date override (game_date + 30h), 48h scan window
- Sports pipeline wiring in executor
- Trump approval misroute fix
- Stale ghost position expiry on load
- Scanner stale price fix (live orderbook mid_price overlay)
- Financial bracket discovery via series_ticker parameter
- Scanner 72h filter for financial brackets
- Financial bracket prefix list (KXGOLDD/W, KXSILVERD/W, KXCOPPERD, KXINX)
- Pre-GT price refresh before illiquidity check
- Token bucket rate limiter (8 req/s)
