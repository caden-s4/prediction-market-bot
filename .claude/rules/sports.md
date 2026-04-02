# Sports Pipeline Rules

Applies to all files in `data/sports/`.

## Pipeline Execution (called every cycle from executor.py)
1. `refresh_if_stale()` — polls ESPN, populates `_game_snapshots` dict keyed by ESPN game ID
2. `run_shock_detection()` — compares prev/current state, populates shock cache
3. `check_for_new_finals()` — dispatches resolution-lag background threads

## GT Source Priority
- SportsLiveSource: fires ONLY in final period (Q4 NBA/NFL, H2 NCAAB) with confidence ≥0.85. Returns None (not tradeable=False) for non-final-period games — router falls through to SportsDataSource
- SportsDataSource: pre-game/fallback, lower confidence (0.65), tradeable=False for pre-game and non-final-period in-progress games

## Shock Confidence Tiers (shock_detector.py `_score_confidence`)
- 0.92: shock ≥0.25, final period, <120s remaining
- 0.85: shock ≥0.15, final period, <300s remaining
- 0.78: shock ≥0.12, final period (logged only, below trade gate)
- 0.00: not in final period — never cached, never traded

## Market Matcher
- Fast path: regex `_GAME_MARKET_RE` parses KXNBAGAME/KXNCAAMBGAME IDs directly
- Team concat format: `MEMDET` = MEM + DET, yes suffix = `-DET`
- Fallback: title text extraction with "vs"/"at" separators
- Fuzzy matching threshold: 0.72

## _find_game_snapshot() Matching
- Iterates ALL snapshots, filters by sport, substring-matches team names
- Score threshold: ≥4 (BOTH home and away teams must substring-match ESPN names)
- Each team match = +2; partial word matches = +1 each
- Does NOT try swapped home/away orderings — match_market() is expected to return the correct ordering

## ESPN Endpoints
- NBA: `site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`
- NCAAB: `site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard`
- NFL: currently returns 400 (off-season), harmless error

## Known Issues
- NFL 400 error every cycle — off-season, ignore
- NCAAB alias coverage: only top 68 programs. Mid-major teams may fail fuzzy match
