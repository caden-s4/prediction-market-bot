# Prediction Market Trading Bot

This is a Python bot that watches prediction markets on Kalshi and Polymarket. It looks
for markets where the real outcome is already basically decided by some outside data
source (a sports API, a FRED economic release, a price quote) but the market hasn't caught
up yet. When the market price is far enough off from what the data says, and the trade
clears two confidence checks, the bot sizes a bet with fractional Kelly and places an
order. By default it only pretends to trade (ghost mode). It won't touch real money unless
you explicitly turn that on.

## Architecture

`main.py` is the entry point. It handles the CLI and runs the scan loop. `bot.py` has
`BotCoordinator`, which owns everything and runs one cycle at a time. The scanning and
trading code is in `resolution/`, the data fetching is in `data/`, and the shared plumbing
is in `shared/`, `monitoring/`, and `utils/`.

- **`config/`** holds all the settings. They come from a `.env` file through
  `python-dotenv`, and nothing else in the code reads environment variables directly.
  `config/__init__.py` has the config dataclasses (`KalshiConfig`, `PolymarketConfig`,
  `BotConfig`, `MonitoringConfig`, `AppConfig`, `SignalTestSettings`).
  `config/signal_testing.py` has the `VALID_SIGNALS` list that the signal CLI flags check
  against. There is no `config.py` at the top level. The real config is this `config/`
  package.

- **`data/`** is where the bot talks to the outside world.
  - `data/markets/` has the exchange clients: `kalshi.py` and `kalshi_ws.py` for the REST
    API and WebSocket, `polymarket.py` and `polymarket_ws.py` for Polymarket, and a shared
    `base.py`.
  - `data/ground_truth/` has the data sources and the router that picks the right one for a
    given market. `router.py` decides based on keyword and prefix checks and filters out
    novelty markets. The sources include `economic_fred.py` (FRED numbers like CPI,
    payrolls, and rates), `financial.py` (Yahoo Finance and Twelve Data for futures and
    yields), `sports.py`, `federal_register.py`, `congress.py`, `eia.py`,
    `rotten_tomatoes.py`, `cross_platform.py`, and the weather files (`weather_kalshi.py`,
    `weather_cli.py`, `asos_timeseries.py`, `weather_timezones.py`). There's also an older
    `economic.py` sitting next to `economic_fred.py`.
  - `data/sports/` has the live sports code: `live_source.py`, `live_game_monitor.py`,
    `market_matcher.py`, `team_resolver.py`, `win_probability.py`, and the detectors
    (`shock_detector.py`, `panic_detector.py`, `staleness_detector.py`,
    `resolution_detector.py`). There isn't one `pipeline.py` file. Instead, `executor.py`
    runs the sports flow each cycle by calling functions like
    `live_game_monitor.refresh_if_stale()` and `shock_detector.run_shock_detection()`.
  - `data/historical/` has the big Kalshi market dump (`kalshi_markets.jsonl`), some candle
    samples, and the backtest CSV output (see the Backtest section).
  - `data/runtime/` has state the bot saves between runs: `bot_state.db` (SQLite),
    `sqlite_store.py`, the ghost-trade logs, settlement caches, and some validation CSVs.

- **`resolution/`** is the scan-to-trade pipeline. `scanner.py` finds and filters markets
  and lays live orderbook prices over them. `tier_registry.py` sorts markets into
  time-based tiers. `priority.py` ranks them. `gap_detector.py` works out the gap between
  price and ground truth and writes the `[SIGNAL]` log lines. `confidence.py` is the
  two-part gate. `executor.py` is the big one (around 3000 lines) that evaluates markets,
  places orders, and handles ghost trades. `decay_monitor.py` handles early exits and stop
  losses. `signal_stats.py` tracks the per-cycle signal counts.

- **`monitoring/`** has `alerts.py` (Telegram and Discord), `event_db.py` (event storage),
  `gate_events.py` and `gate_names.py` (gate-funnel logging), and `tui_state.py`.

- **`shared/`** has `bankroll.py` (capital, Kelly sizing, and the daily halt),
  `fee_cache.py`, `exclusion_list.py`, and `paper_log.py` (the ghost-trade JSONL log).

- **`utils/`** has `logger.py` for logging setup and `storage.py`.

- **`strategies/`** has two standalone weather strategies, `weather_peak_snipe.py` and
  `weather_snipe.py`. Its `__init__.py` is empty.

- **`legacy/`** is old code left over from a migration that isn't finished. It has its own
  `execution/`, `pipeline/`, `adapters/`, `backtest/`, `data/`, `meta/`, and `risk/`
  folders plus `live_trading.py`. None of the current work runs from here.

One thing worth knowing: there are no `execution/`, `pipeline/`, or `signals/` folders at
the top level. Those names only show up under `legacy/`. The execution logic lives in
`resolution/executor.py`, the sports flow is driven by `executor.py` calling the
`data/sports/` files directly, and the signal handling is spread across
`config/signal_testing.py`, `resolution/gap_detector.py`, and `resolution/signal_stats.py`.

## Status

The bot is still in the validation and ghost-trading stage. It is not running live.

- **Ghost mode is the default.** `BotConfig.dry_run` is `True` unless you set
  `LIVE_TRADING=true` in `.env`. In ghost mode every trade is fake. It gets tracked in
  `data/runtime/` and the ghost-trade logs, and no real orders go out.
- **Kalshi starts on the `demo` environment** (`KALSHI_ENV=demo`). Demo markets are 7 to 30
  or more days out. You need `KALSHI_ENV=prod` to trade live.
- **Polymarket is off by default** (`POLYMARKET_ENABLED=false`). When it's on, it can run
  in a public read-only mode with no credentials.
- **The confidence gate is turned down right now.** `MIN_CONFIDENCE_THRESHOLD` defaults to
  **0.45** in `config/__init__.py`, down from 0.80. That was done on purpose to let more
  possible edges through while we're still exploring in ghost mode. Just know the gate is a
  lot looser than the 0.80 the docs talk about, so put it back to 0.80 before going live.

## Backtest

`scripts/analyze_kalshi_hist.py` reads through the big Kalshi dump
(`data/historical/kalshi_markets.jsonl`, about 4.7 GB) and, for each category of market,
counts how often markets that ended at extreme prices (final price at or above 0.85, or at
or below 0.15) actually resolved the way the price suggested. It only counts finalized
binary markets that had real volume, and it skips team sports, individual sports, parlays,
esports, and spread markets. The result goes to `data/historical/upset_rates.csv`.

- After all the filtering, it looked at **60,218 finalized markets across 345 categories**.
- **Extreme prices are very accurate.** In the normal categories, markets priced at or
  above 0.85 resolved the favored way 94 to 100% of the time, and markets priced at or
  below 0.15 resolved against 97 to 100% of the time. The big upsets are mostly in crypto
  tails (for example `KXXRP` at about 24% upsets when priced high), novelty and mention
  markets, and small hockey markets.
- **Just buying the favorite loses money after fees.** The other file,
  `data/historical/backtest_results.csv`, has the per-contract profit and loss after fees,
  and most categories come out break-even to slightly negative once you count fees. Only a
  few (like `KXSOLD`, `KXXRPD`, `KXINXU`, `KXNASDAQ100U`) actually clear the fees. So being
  well-priced isn't the same as having an edge. The whole point of the bot is to find
  markets that are wrong compared to a live data source, not just markets that are
  confidently priced.

## Running it

You need Python 3.11 or newer.

1. Install the dependencies:

   ```
   pip install -r requirements.txt
   ```

2. Set up your environment file:

   ```
   cp .env.example .env
   ```

   At the very least, fill in `KALSHI_API_KEY` and `KALSHI_API_SECRET`, since Kalshi is on
   by default. Leave `LIVE_TRADING=false` and `KALSHI_ENV=demo` while you're testing. On
   demo, set `RESOLUTION_WINDOW_HOURS` to 168 or higher so the scanner actually finds
   markets. `SETUP.txt` has the full walkthrough for getting credentials, but note that its
   Section 5 file list is out of date (see Known limitations).

3. Run one dry-run cycle to make sure things work:

   ```
   python main.py --once --log-level DEBUG
   ```

4. Run it continuously in ghost mode:

   ```
   python main.py --log-level INFO
   ```

### CLI flags

General:

- `--once` runs a single scan cycle and exits.
- `--info` shows INFO-level logs on the console. This is the default now, but the flag
  still works.
- `--log-level {DEBUG,INFO,WARNING,ERROR}` sets how chatty the console is (default INFO).
- `--log-file PATH` also writes the logs to a file.
- `--names` prints the full market name and details for every trade.
- `--force-test` turns off the illiquidity filter and drops the gap threshold to 1% so you
  can test the pipeline. It's ghost-only and won't start if `LIVE_TRADING=true`.

Signal testing:

- `--test-signal SIGNAL` runs only that one signal source. You can pass it more than once.
  Valid values are `financial`, `fred`, `sports_shock`, `sports_staleness`,
  `sports_panic`, `sports_resolution`, and `cross_platform`.
- `--suppress-signal SIGNAL` turns off one signal source. Can be passed more than once.
- `--min-confidence FLOAT` overrides the confidence gate for test mode.
- `--min-gap FLOAT` overrides the minimum gap threshold for test mode.
- `--compare SIGNAL_A SIGNAL_B` runs two signal setups side by side and logs the
  differences.
- `--replay LOG_FILE` re-runs the signal evaluation from a saved verbose log, without
  making any live API calls.

If you run it in a real terminal, there's also a little command listener you can type into
while it runs: `p`/`positions`, `sig`/`signals`, `pairs [N]`, `s`/`scan`, `history`/`hist`,
`paper [N]`, `fred-check`, `bank <amount>`, `clear`, `ghost-reset`, and `help`.

## Known limitations

- **`legacy/` is only half migrated out.** It still has old `execution/`, `pipeline/`,
  `risk/`, and other folders plus `live_trading.py`. It's kept around for reference and
  isn't part of the current flow, so if you see a module in there that looks like a
  duplicate, treat it as the old version.
- **Novelty markets can get sent to FRED by mistake.** The FRED source's `can_handle()`
  just checks whether a market's question or tags contain lowercased words like `cpi`,
  `inflation`, `gdp`, or `gas price`. So a novelty or "what will X mention" market that
  happens to include one of those words can get pulled toward FRED. `router.py` tries to
  catch this with a novelty filter (ticker prefixes like `KX*MENTION` and a "what will X
  say/mention" regex), but the keyword matching is still a place where things can go to the
  wrong source.
- **Polymarket is read-only in practice.** It's off by default and mainly used for
  cross-platform comparison. The live trading path is Kalshi.
- **`SETUP.txt` Section 5 is out of date.** Its file list points at things that don't match
  the current tree anymore (a top-level `config.py`, `data/ground_truth/economic.py` as the
  FRED source, old `federal_register.py` and `decay_monitor.py` paths). Use the
  Architecture section above as the real map. The rest of `SETUP.txt`, the credential setup
  and the `.env` variables, is still correct.

This bot is still being validated and isn't ready for production. Don't run it with
`LIVE_TRADING=true` until the confidence gate is back at 0.80 and it's passed the
paper-trading checklist.

## Development

A lot of this was built with agent tooling. `CLAUDE.md` in the repo root covers the
architecture, the Kalshi market ID formats, the current benchmarks, open issues, and a list
of fixes not to undo. The `.claude/` folder has the working rules (`.claude/CLAUDE.md` and
`.claude/rules/`) and the agent definitions used while building this. Read those before you
change `resolution/executor.py` or anything in the pricing or ground-truth logic.
