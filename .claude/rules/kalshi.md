# Kalshi Market Data Rules

Applies to `data/markets/kalshi.py`.

## Market ID Formats
- Financial brackets: `KXNASDAQ100U-26MAR13H1600-T24399.99` — `_bracket_prefix()` returns series prefix
- NBA games: `KXNBAGAME-26MAR15INDMIL-MIL` — NOT a bracket, `_bracket_prefix()` returns None
- NCAAB games: `KXNCAAMBGAME-26MAR14WISMICH-MICH` — same, not a bracket

## Sports Game Market Resolution Date Override
Game markets use `game_date + timedelta(hours=30)` because Kalshi sets `close_time` to settlement window (2 weeks out), not game time. Scanner uses 48h window for game series prefixes.

## Dynamic Attributes Extracted in _parse_market()
`last_price`, `volume_24h`, `liquidity`, `yes_ask`, `yes_bid`, `yes_ask_size`, `yes_bid_size`, `updated_time`, `created_time`, `open_time`

## Sports Series Tickers
Scanner searches: `KXNBAGAME`, `KXNCAAMBGAME` (confirmed), plus old `KXNBA`, `KXNCAAB` (props/futures)

## Exclusion Patterns
`KXNBAMENTION`, `KXNCAABMENTION`, `KXENTMENTION`, `KXWBCMENTION` → auto-excluded as novelty_prop

## API Rate Limits
Individual `get_market()` calls cost 1 request each. Bulk `get_markets()` is paginated (200/page). Don't add unbounded individual fetches — keep targeted to game markets at stale 0.50 only.
