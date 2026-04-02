# Prediction Market Trading Bot

## What This Is
Python bot on Windows trading prediction markets on Kalshi (live) and Polymarket (read-only). Scans markets, computes ground truth probabilities from external data, detects price gaps, and executes trades when edge exists.

## Architecture Pipeline
Scanner → Tier Registry → Priority Scorer → Ground Truth Router → Gap Detector → Confidence Scorer → Executor → Decay Monitor

## Key Files
- `main.py` — entry, CLI args (`--info`, `--force-test`)
- `bot.py` — BotCoordinator, main scan loop
- `resolution/executor.py` — core trading logic, GT evaluation, ghost trades (~3000 lines, most complex file)
- `resolution/scanner.py` — market discovery, `refresh_markets()`
- `resolution/tier_registry.py` — T1/T2/T3 time-based tiers, `mark_urgent()`
- `resolution/priority.py` — PriorityScorer (freshness, volume, bracket proximity, staleness)
- `resolution/gap_detector.py` — gap calculation, `[SIGNAL]` logging, min_gap thresholds
- `resolution/confidence.py` — two-dimensional scorer (source confidence × resolution clarity), threshold 0.80
- `data/ground_truth/router.py` — routes markets to GT sources
- `data/ground_truth/economic_fred.py` — FRED API (CPIAUCSL, CPILFESL, PAYEMS)
- `data/ground_truth/financial.py` — Yahoo Finance / Twelve Data (ES=F, NQ=F, CL=F, GC=F, ^TNX)
- `data/sports/live_source.py` — SportsLiveSource, in-game win probability from ESPN
- `data/sports/live_game_monitor.py` — ESPN polling, `refresh_if_stale()` called each cycle
- `data/sports/pipeline.py` — SportsSignalPipeline (staleness, panic, resolution detectors)
- `data/sports/market_matcher.py` — team name extraction from market IDs, 750+ aliases
- `data/sports/shock_detector.py` — `run_shock_detection()` called each cycle
- `data/sports/resolution_detector.py` — `check_for_new_finals()` called each cycle
- `data/release_calendar.py` — FRED release calendar with pre/hold/hunt windows
- `data/markets/kalshi.py` — Kalshi API client, `_parse_market()`, dynamic attributes
- `shared/bankroll.py` — bankroll management, Kelly sizing
- `shared/paper_log.py` — PaperTradeLog, JSONL append-only ghost trade history

## Active Signal Sources
- **FRED** (CPI, jobs): Works. 28 covered + 20 actionable signals on Mar 10.
- **Financial** (ES=F, NQ=F, ^TNX, GC=F, CL=F): 259 Kalshi bracket markets discovered (KXNASDAQ100, KXINX, KXWTI, KXBRENTD, KXGOLD, KXSILVER, KXTNOTED, KXAAAGASW).
- **Sports NBA**: KXNBAGAME + KXNBAPTS markets, ESPN data pipeline active.
- **Sports NCAAB**: KXNCAAMBGAME + KXNCAAMBTOTAL markets, same pipeline as NBA.

## Known Active Bugs / In-Progress
- **NFL ESPN error** — `400 Bad Request` on NFL scoreboard endpoint every cycle. NFL is off-season, harmless but noisy.
- **Freshness scoring** — `created_time` exists in market.raw but parser returns 0 fresh. Low priority.

## Completed Fixes (Do NOT Revert)
- Fix 12: CL=F rollover block (tradeable=False during rollover)
- Fix 13: NQ=F/ES=F rollover 25% size reduction
- Fix 17: executor secondary gap check cap
- Fix 18: FRED forward-looking market date check
- Priority scorer with 4 dimensions (freshness, volume, bracket proximity, staleness)
- FRED release calendar with 3-window state machine (pre_release → hold → hunt)
- Permanent no-source skip (graduates after 3 fast-skip cycles)
- Signal logging (`[SIGNAL] ACTIONABLE/BLOCKED` in gap_detector.py)
- Paper trading system (ghost_positions.json + paper_trades.jsonl)
- Ghost mode series cap 50% (vs 15% live) for validation
- `--force-test` flag (bypasses illiquidity + gap threshold + orderbook check, ghost only)
- Sports tickers: KXNBAGAME, KXNCAAMBGAME added to scanner
- Sports resolution date: game_date + 30h override for game markets
- Sports scan window: 48h for game markets (vs 24h default)
- Sports pipeline wiring: refresh_if_stale(), run_shock_detection(), check_for_new_finals() in executor
- WTI weekly (KXWTIW) exempted from CL=F rollover block
- Trump approval misroute fix (exclusion keywords + magnitude check)
- Stale ghost position expiry on load
- Scanner stale price fix: refresh_markets() now overlays live order book mid_price
- Financial bracket discovery: get_financial_bracket_markets() via series_ticker parameter
- Scanner hours filter extended to 72h for financial brackets
- Financial bracket prefix list: KXGOLDD/W, KXSILVERD/W, KXCOPPERD, KXINX added
- Pre-GT price refresh reordered to run BEFORE illiquidity check

## Kalshi Market ID Formats
- Financial brackets: `KXNASDAQ100U-26MAR13H1600-T24399.99` (series-date-strike)
- NBA games: `KXNBAGAME-26MAR15INDMIL-MIL` (series-dateTeams-yesTeam)
- NCAAB games: `KXNCAAMBGAME-26MAR14WISMICH-MICH`
- WTI daily: `KXWTI-26MAR13-T80.99` (rollover blocked)
- WTI weekly: `KXWTIW-26MAR13-T97.99` (rollover exempt)

## Critical Rules
- executor.py is ~3000 lines — most complex file, changes here can break trading
- Don't touch: illiquidity filter logic, confidence scorer thresholds (0.80/0.85), decay monitor, live trading persistence paths
- Real config is `config/__init__.py`, not `config.py`
- Ghost positions distinguished by `order_id.startswith("ghost_")`
- Game market prefixes: `KXNBAGAME`, `KXNCAAMBGAME`, `KXNFLGAME`

## Testing
- `python main.py --info` — normal ghost mode
- `python main.py --force-test` — bypasses safety guards (ghost only)
- `bank 500` — set virtual bankroll in ghost mode
- `paper` — show ghost trade daily summary
- `ghost-clear` — wipe ghost positions
- `p` — show open positions
- `history` — show resolved trades

## Communication Style
Brutally honest, skip preamble, give precise self-contained answers. When diagnosing, identify the specific problem and give the exact fix.
