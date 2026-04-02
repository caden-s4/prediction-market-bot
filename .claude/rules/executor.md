# executor.py Rules

This is the bot's most complex file (~3000 lines). Changes here can break trading.

## Critical Code Paths (in execution order)
1. `run_once()` — main cycle: sports refresh → tier refresh → GT evaluation → signals → execution
2. Sports pipeline calls: `refresh_if_stale()`, `run_shock_detection()`, `check_for_new_finals()` — wrapped in try/except, must never crash the cycle
3. GT evaluation loop: iterates candidate markets, calls router, checks illiquidity, computes gaps
4. Illiquidity filters: series-level (bracket markets only) and per-market (yes_price ≈ 0.50)
5. Game market price refresh: must run BEFORE GT fetch and BEFORE illiquidity check
6. Signal deduplication: max 2 per source+direction bucket
7. Confidence gate: both dimensions must be ≥ 0.80
8. Order book check: verifies book isn't empty before placing orders (bypassed in --force-test)
9. Series exposure cap: 15% live, 50% ghost mode
10. `_place_order()` → ghost trade path when dry_run=True

## Ghost Trading
- Ghost positions in same `_positions` dict as live, distinguished by `order_id.startswith("ghost_")`
- Persist to `ghost_positions.json` (separate from live positions)
- Paper log: `paper_trades.jsonl` via `PaperTradeLog`
- Full exit logic runs on ghost positions (decay monitor, hard stops)

## Game Market Prefixes
```python
_GAME_PREFIXES = ("KXNBAGAME", "KXNCAAMBGAME", "KXNFLGAME")
```

## Don't Touch
- The illiquidity filter logic itself — it correctly blocks dead markets
- The confidence scorer thresholds (0.80 / 0.85)
- The decay monitor or financial hard stop
- Live trading persistence paths
