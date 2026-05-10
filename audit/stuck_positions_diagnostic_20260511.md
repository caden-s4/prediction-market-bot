# Phase 15a — Stuck weather position diagnostic (READ-ONLY)

**Date:** 2026-05-10
**Scope:** Verify three claims from the prior diagnostic against actual
runtime behavior. No code changes. No fix proposed.

## Subject

`data/runtime/ghost_positions.json` holds 21 ghost positions. 19 are 11–14h
past their `resolution_date_iso` but still tracked as open. All 19 are
legacy `weather_snipe` entries on `KXLOWT*` / `KXHIGHT*` 26MAY09 markets
entered between 05:35 and 07:45 UTC today. The other 2
(`KXAAAGASD-26MAY11-*`) are still 8h from resolution and are not in scope.

## Step 1 — `_get_current_price` return values on stuck tickers

Tested 3 representative stuck tickers against the live Kalshi REST endpoint
(same path `_get_current_price` uses when WS is unavailable):

| ticker | `get_order_book().best_yes_bid` | `best_yes_ask` | `mid_price` | `_get_current_price` returns | `get_market().status` | `get_market().result` |
|---|---|---|---|---|---|---|
| `KXLOWTOKC-26MAY09-T56` | `None` | `None` | `None` | **None** | `finalized` | `no` |
| `KXHIGHTHOU-26MAY09-T88` | `None` | `None` | `None` | **None** | `finalized` | `yes` |
| `KXLOWTSEA-26MAY09-T50` | `None` | `None` | `None` | **None** | `finalized` | `no` |

The order book is empty for all three (both bid and ask are `None`), so
`ob.mid_price` is `None` and `_get_current_price` returns `None`. **The
prior claim about the decay monitor filter is verified.**

But `client.get_market()` returns `status="finalized"` and the actual
binary outcome `result in {"yes", "no"}` for the same tickers. **The
settlement outcome is readily queryable from Kalshi REST — it just isn't
queried at runtime for non-sports markets.**

## Step 2 — Decay monitor Pass 2 filter (`executor.py:3748-3768`)

```
3747         # ── Pass 2: decay monitor (remaining positions only) ─────────────────
3748         open_positions = []
3749         for rec in self._positions.values():
3750             current_price = self._get_current_price(rec.market)
3751             if current_price is None:
3752                 continue
3753             _gt_published_at = None
3754             ...
3756             open_positions.append(OpenResolutionPosition(...))
3770         decisions = self._decay.evaluate(open_positions)
```

The filter is unconditional: any position where `_get_current_price` returns
`None` is excluded from the decay monitor's input. **It is silent — there
is no log line when a position is filtered out.** A position with no
orderbook is invisible to every downstream rule, including the
"`hours_left < 15min`" approach-exit rule
(`decay_monitor.py:263`).

Other code paths that handle past-resolution markets:

| Path | File:line | Scope |
|---|---|---|
| Startup load expiry | `executor.py:4363` | Drops positions on bot startup if `hours_to_resolution <= 0` |
| Sports game-final exit | `executor.py:3802–3876` (`_exit_ghost_positions_for_finals`) | Settles ghost positions when ESPN reports a confirmed FINAL — sports only |
| Auto-adopt skip | `executor.py:4634` | Refuses to adopt an exchange position whose market already resolved |
| Tier registry evict | `tier_registry.py:281` | Removes expired markets from the scan registry (not positions) |
| Decay monitor approach exit | `decay_monitor.py:263` | Requires `current_price != None` to ever run (Pass 2 filter is upstream) |

**There is no runtime, weather-aware settlement-exit path.** The sports
path is the only "exit at the correct settled outcome" wiring in the
runtime loop.

## Step 3 — Kalshi settlement query path

Settlement data is reachable via `KalshiClient.get_market(ticker)` →
`raw["status"]` and `raw["result"]`. This is used in scripts:

- `scripts/phase0_accuracy.py:109-112` — accepts only `status=="finalized" and result in ("yes","no")`
- `scripts/backtest.py:95-98`, `scripts/probe_kalshi_hist.py:95-98`,
  `scripts/analyze_kalshi_hist.py:76-79` — same pattern
- `scripts/phase1b_weather_validation.py` — caches into
  `data/runtime/kalshi_settled_cache.json`
- `scripts/shadow_analysis.py:117` — `_SETTLED_STATUSES = {"finalized", "settled", "closed", "resolved"}`

**No runtime path (executor / bot / decay_monitor / scanner) reads `status`
or `result` from Kalshi for settlement-driven exits.** The only runtime
settlement wiring is the sports/ESPN one in Step 2's table.

So the bot has the API capability and a pattern (the sports path), but
neither is wired into runtime exit for weather/financial markets.

## Step 4 — Startup cleanup (`executor.py:4363-4372`) — P&L behavior

```
4347                 rd = datetime.fromisoformat(saved["resolution_date_iso"])
4352                 market = Market(
4353                     market_id=saved["market_id"],
...
4361                 )
4362
4363                 if market.hours_to_resolution <= 0:
4364                     logger.info(
4365                         "ResolutionBot: skipping expired position %s "
4366                         "(market already resolved)", mid,
4367                     )
4368                     if self._dry_run:
4369                         expired_count += 1
4370                     else:
4371                         skipped += 1
4372                     continue
```

The cleanup:
- Does **not** add the position to `self._positions`
- Does **not** call `_exit_position(...)`
- Does **not** write to `paper_trades.jsonl`
- Does **not** call `_bankroll.reserve` or `release`
- Does **not** query Kalshi for the actual outcome
- Does **not** mark any P&L (zero or otherwise)
- Just logs a one-line `INFO`, bumps a counter, and moves on

When `_save_positions()` runs later, the position is no longer in
`self._positions`, so it disappears from `ghost_positions.json` too.
**The trade outcome is silently dropped — no W/L attribution, no $ recorded.**
For ghost mode this means paper P&L stats are systematically blind to any
trade that survives the bot from open through resolution without ever
hitting an in-cycle exit (which is exactly the failure mode we're seeing
now).

## Step 5 — Git blame for intent

| Hunk | Commit | Date | Subject (paraphrased) |
|---|---|---|---|
| Startup-cleanup core (4363–4367, 4372) | `9755e1d6` | 2026-02-23 | "Persist open positions to disk so bot survives restarts and crashes" |
| Startup-cleanup ghost counter split (4368–4371, 4406–4411) | `b7cb05a1` | 2026-03-13 | "Fix stale ghost positions blocking series cap + ghost mode cap override" |
| Decay-monitor Pass 2 filter (3748–3752) | `20b70fea` | 2026-02-21 | "Reconfigure bot: dual-strategy maker rebate + resolution drift arbitrage" |

`9755e1d6`'s body is explicit about intent:

> Before this change: Ctrl-C or any crash left positions orphaned on the
> exchange with no record in the bot. On restart the bankroll showed the
> reserved capital as available, potentially leading to over-sized new
> positions while the old ones stayed open.
>
> After: state.json always contains the canonical set of open positions.
> Restart reads it back; expired or irrecoverable entries are pruned.

So the cleanup was scoped to **restart hygiene**, not settlement. It was
meant to drop "irrecoverable entries" on load, not to be the canonical
exit path for resolved positions. The expected normal-path exit was always
through the decay monitor.

`b7cb05a1`'s body confirms the same intent — the change was purely a
logging/counter cosmetic ("get their own log line… instead of being lumped
into the generic skipped/expired counter"), not a settlement-design
change.

`20b70fea` is the original implementation of the dual-strategy executor.
The `if current_price is None: continue` predates every weather strategy
file. **No later commit ever modified the filter** — the absence is not a
revert or a deferred TODO; it's just that the runtime exit path never
needed to handle "live market with no current price" because the original
strategies (FRED brackets, financial brackets) had liquid order books
through resolution. Weather snipes break that implicit invariant: at the
final-60-min snipe price (0.01 or 0.99) the market is already thin, and
after settlement Kalshi tears the book down completely.

**Verdict on intent:** the current behavior is accidental, not by design.
The startup-only cleanup was never meant to substitute for an exit path,
and no commit explicitly chose to skip settlement-driven exits for
non-sports markets. The gap is "weather strategies were added after the
runtime exit assumptions were baked in, and nothing extended the exit path
to cover them."

## Step 6 — Actual ghost P&L impact

Computed hypothetical realized P&L for the 19 stuck positions using the
live `get_market().result` from Kalshi (settle value 1.0 if `yes`, 0.0 if
`no`), applied via Kalshi contract economics
(`(settle − entry) × size/entry` for buy_yes;
 `(entry − settle) × size/(1−entry)` for buy_no):

| ticker | act | entry | size | result | hyp P&L |
|---|---|---|---|---|---|
| KXLOWTCHI-26MAY09-B48.5 | buy_yes | 0.8900 | 54.00 | yes | **+6.67** |
| KXLOWTLAX-26MAY09-B56.5 | buy_no | 0.8700 | 58.72 | yes | −58.72 |
| KXLOWTOKC-26MAY09-B53.5 | buy_no | 0.9100 | 58.75 | yes | −58.75 |
| KXLOWTSEA-26MAY09-B47.5 | buy_no | 0.9100 | 58.75 | yes | −58.75 |
| KXLOWTOKC-26MAY09-T56 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTOKC-26MAY09-T83 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTOKC-26MAY09-B83.5 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXLOWTNOLA-26MAY09-T67 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXLOWTNOLA-26MAY09-B69.5 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTSATX-26MAY09-T90 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXHIGHTSATX-26MAY09-T83 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTMIN-26MAY09-T60 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTMIN-26MAY09-B62.5 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXHIGHTHOU-26MAY09-T88 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXHIGHTHOU-26MAY09-T81 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTDAL-26MAY09-T84 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXHIGHTDAL-26MAY09-B90.5 | buy_no | 0.9900 | 58.80 | yes | −58.80 |
| KXLOWTSEA-26MAY09-T50 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| KXLOWTLAX-26MAY09-B58.5 | buy_yes | 0.0100 | 58.80 | no | −58.80 |
| **Total** | | | **$1,112.22** | | **−$1,051.55** |

Wins: 1 / 19. Losses: 18 / 19. Hypothetical realized P&L if settled now:
**−$1,051.55** of $1,112.22 notional (≈ 94.5% loss rate, ≈ 94.6% notional
burn).

Right-now P&L impact:
- **TUI / paper summary:** the 19 positions show as open. Their unrealized
  P&L would be computed against `current_price = None`, so most P&L
  displays will either show $0, "?", or NaN. They distort
  "open positions" counts but don't poison `paper_trades.jsonl` (no
  EXIT/log line was ever written).
- **Bankroll:** capital is still reserved for them — $1,112.22 of ghost
  bankroll sits unavailable. New trades can still come in (ghost mode
  doesn't enforce hard balance), but bankroll metrics underreport
  available cash.
- **Win-rate / capture metrics:** undercount. The 18 losses are not in
  any closed-trade ledger. Any "win rate over N closed trades" computed
  from `paper_trades.jsonl` is biased UP.
- **Resolves on next bot restart:** at startup, `_load_positions` (Step 4)
  will silently drop all 19 with no P&L attribution — the loss disappears
  rather than being booked. So the longer the bot stays up, the more
  accurate the open-positions display is *as-of-the-moment*, but the worse
  the eventual P&L ledger gap is.

The selection-bias caveat: these 19 are the *survivors* of a much larger
trade flow. Trades that won had their YES price drift toward 1.0 (or NO
price drift toward 0.0) and the decay monitor's "early exit" rule fired
while the book was still alive. The losers are the ones where the price
stayed flat or moved the wrong way, so the book stayed thin, the decay
monitor never fired, and they stuck around past resolution. **The 18/19
loss rate is a survivorship artifact** — but it also means every "stuck"
position is a loser the bot never realized.

## Step 7 — Verdict

**Closest fit: (a) with one significant correction.**

Verified:
- ✅ Decay monitor Pass 2 skips positions where `_get_current_price`
  returns `None` (silent, no log).
- ✅ `_get_current_price` returns `None` for post-resolution Kalshi
  markets because the orderbook is torn down (bid and ask both null).
- ✅ Startup-only cleanup at `executor.py:4363` is the only path that
  removes past-resolution positions, and it does so with **zero P&L
  attribution** (silent drop, no `paper_trades.jsonl` entry).
- ✅ No runtime path queries Kalshi settlement (`status` / `result`) for
  exit purposes for weather/financial markets.

Correction to the prior diagnostic:
- ⚠️ A **settlement-driven runtime exit path does exist** —
  `_exit_ghost_positions_for_finals` at `executor.py:3802` — but **it is
  scoped to sports only**. It consumes `(market_id, winner_team)` tuples
  from `data.sports.resolution_detector.drain_ghost_exits` (ESPN-driven),
  not from Kalshi. Weather has no analogue. The Kalshi REST endpoint
  *does* return settlement data and is used by analysis scripts; no
  runtime caller wires it in.

So the design space for a fix is not "build a settlement query from
scratch" — the data and an exit-at-settlement pattern both exist
already. The gap is wiring a Kalshi-driven (vs. ESPN-driven) settlement
poller into the runtime, scoped to non-sports markets.

Intent question (option (d)): no. Git blame shows the startup cleanup
was added for restart hygiene, and the Pass 2 filter predates weather
strategies. The current behavior is not a deliberate design choice —
it's an unmaintained gap that became visible only when overnight weather
snipes started filling.

No fix proposed in this phase per the spec.

## Verification gates

- [x] `_get_current_price` behavior tested on 3 stuck tickers with actual
      return values pasted (all `None`)
- [x] `executor.py:3748-3768` code pasted with surrounding context
- [x] Settlement query path searched and reported (exists in REST client
      + analysis scripts; **no runtime caller** for weather/financial; sports
      has its own ESPN-driven path)
- [x] `executor.py:4363-4372` cleanup code pasted; P&L marking behavior
      identified as **silent drop, zero attribution**
- [x] Git blame output for both relevant code sections, with commit
      bodies quoted for intent
- [x] Actual ghost P&L state for stuck positions reported (−$1,051.55
      hypothetical across 18 losses / 1 win, 94.5% loss rate; capital
      $1,112.22 still reserved)
- [x] Verdict (a)/(b)/(c)/(d)/(e) explicit — **closest to (a)**, with a
      correction noting the sports settlement path that prior diagnostic
      missed
- [x] No source file modified
- [x] No fix proposed
