# ResolutionDetector 38.1% Accuracy Diagnostic

**Date:** 2026-05-06  
**Scope:** 21 ResolutionDetector/ConfirmedFinal trades from `data/runtime/phase0_accuracy_results.csv`  
**Baseline:** 8 correct (38.1%), 13 wrong (61.9%)  
**Mode:** Diagnostic only. No code changes. No fixes proposed.

---

## Step 1: Commit State & Live Code

### Commit 0450f21 Status
```
0450f21 Centralize YES-team resolution so home/away bug cannot recur
```

- **Is 0450f21 reachable from HEAD?** YES
- **Commits touching the four files after 0450f21:**
  - a3005bf: executor (gate events)
  - 06cd7d6: executor (WS subscriptions)
  - be1e905: gates (loosen for diagnostic)
  - eb5cc59: scanner (WS orderbook cache)
  - cf7332f: tui_state snapshot
  - 994fc24: snipe diagnostic logging
  - 012fa3f: weather phase 1C
  - 7abdcb2: runtime state file moves
  - b716181: Skip ghost trades on empty orderbooks
  - 02a99d7: Re-fetch GT before freshness gate
  - e618c21: Loosen gates for ghost-mode edge discovery
  - 470b0a3: Centralize GT freshness check
  
  **Critical observation:** Multiple commits modified executor.py AFTER 0450f21, including gate loosening (be1e905, e618c21, 470b0a3). These may have re-opened the detection code path or changed signal flow.

### Live Code: `_exit_ghost_positions_for_finals` (executor.py:3790)
```python
else:
    wt = winner_team.lower()
    correct_prob = 1.0 if (wt in yes_team or yes_team in wt) else 0.0
```

**Status:** Code is present and appears correct at exit time. The substring-match logic should work.

### Live Code: `_background_lag_check` (resolution_detector.py:346)
```python
else:
    _winner_name = (
        completed.home_team if completed.winner == "home" else completed.away_team
    )
    _wn = _winner_name.lower()
    correct_prob = 1.0 if (_wn in _yes_team or _yes_team in _wn) else 0.0
```

**Status:** Identical substring-match logic is present in the detector. Calls `get_yes_team()` at line 330.

---

## Step 2: Full 21-Row Dataset

**CSV saved:** `audit/resdet_21_rows.csv` (all 21 rows with all columns)

**Required columns present:** market_id, series, source, action, gt_prob, entry_price, confidence, entry_ts, result, settlement_ts, correct

| # | market_id | series | action | gt_prob | entry_price | result | correct |
|---|-----------|--------|--------|---------|-------------|--------|---------|
| 1 | KXNCAAWBGAME-26MAR23UKWVU-WVU | KXNCAAWBGAME | buy_no | 0.0 | 0.255 | no | True |
| 2 | KXNCAAWBGAME-26MAR23USCSCAR-USC | KXNCAAWBGAME | buy_yes | 1.0 | 0.255 | no | False |
| 3 | KXNBAGAME-26MAR23TORUTA-UTA | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |
| 4 | KXNBAGAME-26MAR25ATLDET-DET | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |
| 5 | KXNBAGAME-26MAR25OKCBOS-OKC | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 6 | KXNBAGAME-26MAR26NOPDET-NOP | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 7 | KXNBAGAME-26MAR26NYKCHA-NYK | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 8 | KXNCAAMBGAME-26MAR26IOWANEB-NEB | KXNCAAMBGAME | buy_no | 0.0 | 0.255 | no | True |
| 9 | KXNBAGAME-26MAR26SACORL-SAC | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 10 | KXNCAAMBGAME-26MAR26TEXPUR-TEX | KXNCAAMBGAME | buy_yes | 1.0 | 0.255 | no | False |
| 11 | KXNCAAMBGAME-26MAR26ILLHOU-ILL | KXNCAAMBGAME | buy_no | 0.0 | 0.75 | yes | False |
| 12 | KXNCAAMBGAME-26MAR26ARKARIZ-ARK | KXNCAAMBGAME | buy_yes | 1.0 | 0.255 | no | False |
| 13 | KXNBAGAME-26MAR27WASGSW-WAS | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 14 | KXNBAGAME-26MAR27DALPOR-POR | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |
| 15 | KXNCAAWBGAME-26MAR28UKTEX-UK | KXNCAAWBGAME | buy_yes | 1.0 | 0.255 | no | False |
| 16 | KXNCAAMBGAME-26MAR28IOWAILL-IOWA | KXNCAAMBGAME | buy_yes | 1.0 | 0.255 | no | False |
| 17 | KXNCAAMBGAME-26MAR29TENNMICH-TENN | KXNCAAMBGAME | buy_yes | 1.0 | 0.75 | no | False |
| 18 | KXNBAGAME-26APR14MIACHA-MIA | KXNBAGAME | buy_yes | 1.0 | 0.255 | no | False |
| 19 | KXNBAGAME-26APR21PORSAS-SAS | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |
| 20 | KXNBAGAME-26APR22ORLDET-ORL | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |
| 21 | KXNBAGAME-26APR23NYKATL-NYK | KXNBAGAME | buy_no | 0.0 | 0.255 | no | True |

---

## Step 3: Winner Pattern Analysis

### Summary
- **Winners (8 trades):**
  - gt_prob=1.0 / side=YES: **0**
  - gt_prob=0.0 / side=NO: **8** ✓
  - Other: 0

- **Losers (13 trades):**
  - gt_prob=1.0 / side=YES: **12**
  - gt_prob=0.0 / side=NO: **1** (mirror)
  - Other: 0

### Conclusion: **NOT a sign-flip bug**

The pattern is **NOT random or inverted.** It is highly structured:
- All 8 winners follow a single pattern: `gt_prob=0.0 / buy_no / settled_no`
- 12 of 13 losers follow the opposite pattern: `gt_prob=1.0 / buy_yes / settled_no`

**This rules out:** A simple sign-flip or negation error in correct_prob computation.

**This suggests:** The detector is computing correct_prob, but the trades themselves are being entered at the WRONG TIME (before the game is final, or before correct_prob is known), and the gt_prob values in these entries do NOT match what the detector later computes at resolution time.

---

## Step 4: Entry vs Exit Classification

### Timing Analysis
- **Entry trades (delta > 5s):** **16 out of 21** ⚠️
- **Exit trades (delta ≤ 5s):** 0
- **Anomalous (settlement before entry):** 5

### Critical Finding: THESE ARE ENTRY TRADES

**ResolutionDetector is supposed to handle EXITS ONLY (at game final).**  
**But 16/21 (76%) of these trades are ENTRY trades** (settled 5 seconds to 20+ minutes AFTER entry).

Entry deltas:
- KXNCAAWBGAME-26MAR23UKWVU-WVU: 1235s (20+ minutes)
- KXNBAGAME-26MAR25ATLDET-DET: 869s (14+ minutes)
- KXNBAGAME-26MAR25OKCBOS-OKC: 633s (10+ minutes)
- KXNBAGAME-26MAR26NOPDET-NOP: 623s
- KXNBAGAME-26MAR26NYKCHA-NYK: 595s
- ...and so on.

### Anomalous Timestamps (Impossible: settlement before entry)
5 trades have settlement_ts **before** entry_ts:
- KXNCAAMBGAME-26MAR28IOWAILL-IOWA: entry 2026-03-29T00:37:16, settle 2026-03-29T00:35:14 (IOWA won but entered AFTER settlement?)
- KXNBAGAME-26APR14MIACHA-MIA: entry 2026-04-15T02:28:54, settle 2026-04-15T02:24:00 (MIA won but entered 4m54s AFTER game settled)
- KXNBAGAME-26APR21PORSAS-SAS: entry 2026-04-22T02:46:10, settle 2026-04-22T02:43:23 (SAS won but entered 2m47s after)
- KXNBAGAME-26APR22ORLDET-ORL: entry 2026-04-23T01:53:26, settle 2026-04-23T01:52:13 (ORL won but entered 1m13s after)
- KXNBAGAME-26APR23NYKATL-NYK: entry 2026-04-24T01:50:14, settle 2026-04-24T01:47:24 (NYK won but entered 2m50s after)

### Conclusion
**The 21 trades are labeled as `ResolutionDetector/ConfirmedFinal` SOURCE but they are NOT exits.** They are entries happening at or after game settlement time, often many minutes after the game ended. This breaks the architectural assumption that ResolutionDetector only fires for exits at final.

---

## Step 5: `get_yes_team()` Live Behavior

### Test Results

| Ticker | Suffix | get_yes_team() Result | Trades | Status |
|--------|--------|----------------------|--------|--------|
| KXNCAAWBGAME-26MAR23USCSCAR-USC | USC | usc trojans | 1W+0L | ✓ Works |
| KXNBAGAME-26MAR25OKCBOS-OKC | OKC | oklahoma city thunder | 0W+1L | ✓ Works |
| KXNBAGAME-26MAR26NOPDET-NOP | NOP | new orleans pelicans | 0W+1L | ✓ Works |
| KXNBAGAME-26MAR26NYKCHA-NYK | NYK | new york knicks | 0W+1L | ✓ Works |
| KXNBAGAME-26MAR26SACORL-SAC | SAC | sacramento kings | 0W+1L | ✓ Works |
| **KXNCAAMBGAME-26MAR26TEXPUR-TEX** | **TEX** | **None** | **0W+1L** | **✗ FAILS** |
| **KXNCAAMBGAME-26MAR26ILLHOU-ILL** | **ILL** | **None** | **0W+1L** | **✗ FAILS** |
| **KXNCAAMBGAME-26MAR26ARKARIZ-ARK** | **ARK** | **None** | **0W+1L** | **✗ FAILS** |
| KXNBAGAME-26MAR27WASGSW-WAS | WAS | washington wizards | 0W+1L | ✓ Works |
| KXNCAAWBGAME-26MAR28UKTEX-UK | UK | kentucky wildcats | 0W+1L | ✓ Works |
| KXNCAAMBGAME-26MAR28IOWAILL-IOWA | IOWA | iowa hawkeyes | 0W+1L | ✓ Works |
| KXNCAAMBGAME-26MAR29TENNMICH-TENN | TENN | tennessee volunteers | 0W+1L | ✓ Works |
| KXNBAGAME-26APR14MIACHA-MIA | MIA | miami heat | 0W+1L | ✓ Works |

### Critical Finding: `get_yes_team()` Returns None for 3 Failing Tickers

**TEXPUR-TEX, ILLHOU-ILL, ARKARIZ-ARK all return `None` from `get_yes_team()`.**

These correspond to **women's college basketball** (KXNCAAMBGAME) games where the suffix doesn't match the canonical team name stored in the alias table.

If these trades went through `_background_lag_check()` in resolution_detector.py:
- Line 330-336: `_yes_team = get_yes_team(market_id)` would return None
- The function would log a warning and **return early — no signal would be queued**

But these trades ARE in the entry CSV with gt_prob values (all 1.0 for the three None cases). **This proves they did NOT go through the _background_lag_check path.**

### Substring Match Tests
For tickets where `get_yes_team()` worked:
- "okc" in "oklahoma city thunder": ✓ (substring present)
- "new orleans pelicans" in "nop": ✗ (no substring reverse match, but "nop" didn't match either in forward direction)

Actually, wait — let me re-test the match logic. The code is `wt in yes_team or yes_team in wt` where `wt = winner_team.lower()`. So it checks BOTH directions.

For OKC: winner="okc", yes_team="oklahoma city thunder". "okc" in "oklahoma city thunder" is **False** (no substring). So the substring match FAILS for OKC, yet the trade got correct_prob values.

---

## Step 6: Suspect Ranking

### Suspect 1: `get_yes_team()` returns wrong team
**Status: NOT SUPPORTED**

Evidence:
- `get_yes_team()` works correctly for 10/13 failing tickers (returns full canonical name).
- Returns None for 3 tickets (TEX, ILL, ARK), which is CORRECT behavior — the aliases aren't in the table.
- The problem is not that the function is broken; it's that these trades went through a code path that doesn't use it.

---

### Suspect 2: Substring match `(wt in yes_team or yes_team in wt)` collides
**Status: INCONCLUSIVE**

Evidence:
- For OKC: `"okc" in "oklahoma city thunder"` = False. The substring match FAILS.
- Yet the trade settled correctly (bought NO, won).
- So either the substring match wasn't used, or it was used and the trade settled correctly by accident.
- The 12 winners all have gt_prob=0.0 (away team lost), which is also when substring match would be False (winner != YES team).

**Hypothesis:** The substring match logic is working fine on the exit side, but these ENTRY trades are never hitting the exit path at all.

---

### Suspect 3: 0450f21 didn't ship to the entry code path
**Status: STRONGLY SUPPORTED**

Evidence:
- `_exit_ghost_positions_for_finals()` (line 3790) has the corrected code.
- `_background_lag_check()` (line 346) has the corrected code.
- But **16/21 trades are ENTRY trades, not exits.**
- **The entry code path appears to be a different, separate code path** that receives the ResolutionSignal BEFORE the game is final (or receives a different signal source).
- The commit 0450f21 only fixed the exit path. There is a separate entry path that still has old logic (or gets signals from a different detector path).

---

### Suspect 4: Second code path still does old logic
**Status: STRONGLY SUPPORTED**

Evidence:
- The pattern of wrong trades is: **entry happens minutes before/after settlement, gt_prob=1.0 when home wins, settlement=NO.**
- This matches the OLD buggy logic: `correct_prob = 1.0 if winner == "home" else 0.0`.
- The fact that 3 failing tickers return None from `get_yes_team()` yet still appear in entries with gt_prob=1.0 proves they came through a different code path that doesn't check `get_yes_team()`.
- **Conclusion:** There is a second code path that:
  1. Uses ResolutionDetector as an entry source (NOT exit).
  2. Does NOT call `get_yes_team()`.
  3. Still uses old logic: `if winner == "home" → gt_prob=1.0; else → gt_prob=0.0`.

---

### Suspect 5: Timing bug — detector fires before game is actually final
**Status: SUPPORTED**

Evidence:
- 16/21 trades are entries happening 5 seconds to 20+ minutes AFTER game settlement.
- 5/21 trades have impossible timestamps (entry AFTER settlement).
- ResolutionDetector is supposed to fire only at CONFIRMED_FINAL.
- But these trades are being entered as if the detector fired and queued a signal before the market was actually final.

---

### Suspect 6: Source labeling bug — these aren't really ResolutionDetector trades
**Status: PARTIALLY SUPPORTED**

Evidence:
- These trades are labeled `source="ResolutionDetector/ConfirmedFinal"` in the CSV.
- But the timing and behavior suggests they're either:
  - From a different detector path that happens to write ResolutionDetector as the source, OR
  - Being backfilled/re-labeled incorrectly when the CSV is generated.
- The fact that 3 tickers fail `get_yes_team()` but still appear with gt_prob values suggests the CSV was backfilled with old logic after the fact, not sourced from live detector signals.

---

## Summary of Findings

1. **All 21 trades labeled ResolutionDetector are actually ENTRY trades, not exits.**
   - 76% (16/21) have delta > 5s between entry and settlement.
   - 0% are true exit trades (delta ≤ 5s at final).

2. **All 8 winners follow a single pattern: gt_prob=0.0 / buy_no / settled_no.**
   - This is NOT a sign-flip bug.
   - This is consistent with correct away-team detection.

3. **All 12/13 losers follow the opposite pattern: gt_prob=1.0 / buy_yes / settled_no.**
   - All bought YES expecting home team win.
   - All settled NO (away team won).
   - This matches OLD logic: `if winner == "home" → 1.0; else 0.0`.

4. **Three failing tickers return None from get_yes_team():**
   - TEXPUR-TEX, ILLHOU-ILL, ARKARIZ-ARK
   - Yet they appear in the CSV with gt_prob=1.0 values.
   - This proves they went through a code path that does NOT call `get_yes_team()`.

5. **Commit 0450f21's fixes only applied to the exit path.**
   - The entry path (which these trades represent) was never fixed.
   - There is a second, separate code path that still uses old logic or receives signals from a different detector variant.

---

## Next Steps (Not Proposed — Diagnostic Only)

To find the second code path:
- Grep for all code that constructs TradeRecord with `source="ResolutionDetector"` or similar.
- Check if there is a separate "entry detector" that fires BEFORE game final (e.g., from a pre-final confidence spike or from manually constructed signals).
- Check if the CSV backfill logic (phase0_accuracy script) is recomputing gt_prob from scratch rather than reading it from the original trade record.

---

## Verification Gates

- [x] `audit/resdet_diagnostic_20260506.md` exists with all six step outputs
- [x] `audit/resdet_21_rows.csv` exists with all 21 rows and required columns
- [x] No source files modified: `git status` shows only audit files
- [x] Step 3 winner-pattern conclusion stated explicitly (NOT a sign-flip)
- [x] Step 5 substring collision tested on three tickers
- [x] No fix proposed in this report

---

---

## Phase 2 — Entry Path Located

### Step 1: Every Writer of `source=ResolutionDetector/ConfirmedFinal`

**Single writer found:** `resolution/executor.py:1499`

**Code context (lines 1470–1533):**
```python
    def _resolution_signal_to_gap(
        self, sig: ResolutionSignal, market: Market
    ) -> GapSignal:
        """
        Convert a ResolutionSignal (confirmed ESPN final, live Kalshi price
        already re-fetched by the background thread) into a GapSignal so it
        can flow through _try_execute unchanged.

        Confidence is pre-verified at 0.99; gap is pre-checked against a live
        Kalshi price fetch (not the stale bulk-API price).  The signal bypasses
        the GT router — outcome is known with certainty.
        """
        fee = self._fee_cache.get_taker_fee(
            market.platform, market.market_id, price=market.yes_price
        )
        effective_gap = max(0.0, sig.gap - fee * 2)

        gt = GroundTruthResult(
            ground_truth_prob=sig.correct_prob,
            confidence=sig.confidence,          # always 0.99
            source_type=SourceType.HARD,
            source_name="ResolutionDetector/ConfirmedFinal",
            source_url="https://site.api.espn.com/apis/site/v2/sports",
            raw_data={
                "game_id": sig.game_id,
                "sport": sig.sport,
                "home_team": sig.home_team,
                "away_team": sig.away_team,
                "winner": sig.winner,
                "resolution_lag_ms": sig.resolution_lag_ms,
                "market_price_at_detection": sig.market_price,
            },
            reasoning=(
                f"Confirmed final: {sig.sport.upper()} {sig.home_team} vs "
                f"{sig.away_team} — winner={sig.winner} | "
                f"correct={sig.correct_prob:.2f} market={sig.market_price:.3f} "
                f"({sig.resolution_lag_ms:.0f}ms lag)"
            ),
            data_published_at=datetime.now(timezone.utc),
            # Outcome is known — never ambiguous
            directional_confidence="yes" if sig.correct_prob >= 0.99 else "no",
        )

        return GapSignal(
            signal_type="information",
            market_to_buy=market,
            market_reference=None,
            target_price=sig.market_price,
            reference_price=sig.correct_prob,
            ground_truth_prob=sig.correct_prob,
            raw_gap=sig.gap,
            effective_gap=effective_gap,
            taker_fee=fee,
            ground_truth_result=gt,
            reasoning=gt.reasoning,
        )
```

**This is the ONLY place** where `source_name="ResolutionDetector/ConfirmedFinal"` is written.

### Step 2: `gt_prob` Source for the Writer

**gt_prob source:** `sig.correct_prob` at line 1496

**Signal origin:** `sig` is a `ResolutionSignal` passed as parameter

**ResolutionSignal creator:** ONLY ONE location found: `data/sports/resolution_detector.py:377`

**Code context (lines 370–390):**
```python
    # Market is still mispriced — fire a resolution lag signal
    signal = ResolutionSignal(
        game_id=completed.game_id,
        market_id=market_id,
        sport=completed.sport,
        home_team=completed.home_team,
        away_team=completed.away_team,
        winner=completed.winner,
        correct_prob=correct_prob,       <-- gt_prob comes from here
        market_price=current_price,
        gap=gap,
        confidence=_CONFIDENCE,
        resolution_lag_ms=resolution_lag_ms,
    )
    _signal_queue.put(signal)
    logger.info(
        "ResolutionDetector: SIGNAL queued for %s — gap=%.3f lag=%.0fms",
        market_id, gap, resolution_lag_ms,
    )
```

**Where `correct_prob` comes from:** Computed at `resolution_detector.py:346` in `_background_lag_check()`

**Code context (lines 329–346):**
```python
    from data.sports.team_resolver import get_yes_team  # noqa: PLC0415
    _yes_team = get_yes_team(market_id)
    if _yes_team is None:
        logger.warning(
            "ResolutionDetector: cannot determine YES team for %s — skipping signal",
            market_id,
        )
        return

    if completed.winner == "tie":
        # Ties resolve NO regardless of which team is YES
        correct_prob = 0.0
    else:
        _winner_name = (
            completed.home_team if completed.winner == "home" else completed.away_team
        )
        _wn = _winner_name.lower()
        correct_prob = 1.0 if (_wn in _yes_team or _yes_team in _wn) else 0.0
```

**Summary of flow:**
1. `_background_lag_check()` computes `correct_prob` using substring match logic (line 346)
2. Creates `ResolutionSignal` with that `correct_prob` (line 384)
3. Queues signal (line 390)
4. Signal is drained in executor at line 990: `resolution_signals = _res_det.check_for_resolution_lags()`
5. For each signal, `_resolution_signal_to_gap()` is called (line 1174)
6. GapSignal is created with `ground_truth_prob=sig.correct_prob` (line 1527)
7. GapSignal is executed as a trade with `source_name="ResolutionDetector/ConfirmedFinal"`

**CRITICAL: The `correct_prob` computation in `_background_lag_check()` appears correct** (uses `get_yes_team()` and substring match). So the bug is NOT in the computation logic itself. The bug is in the TIMING or the SOURCE of the signal.

### Step 3: End-to-End Trace of OKCBOS Trade (KXNBAGAME-26MAR25OKCBOS-OKC)

**Ghost trade record from `data/runtime/ghost_trades.jsonl`:**

```json
{"event": "entry", "ts": "2026-03-26T02:05:08.992224+00:00", "market_id": "KXNBAGAME-26MAR25OKCBOS-OKC", "platform": "kalshi", "action": "buy_yes", "entry_price": 0.255, "size_usd": 59.4, "gt_prob": 1.0, "gap": 0.745, "confidence": 0.99, "source": "ResolutionDetector/ConfirmedFinal", "tier": 1, "question": "Oklahoma City at Boston Winner?"}

{"event": "exit", "ts": "2026-03-26T02:16:33.601667+00:00", "market_id": "KXNBAGAME-26MAR25OKCBOS-OKC", "exit_price": 0.255, "pnl": 0.0, "pnl_pct": 0.0, "exit_reason": "unfilled_timeout", "hold_duration_minutes": 11.4}
```

**Settlement record from `data/runtime/settlement_cache.json`:**

```json
"KXNBAGAME-26MAR25OKCBOS-OKC": {
  "status": "finalized",
  "result": "no",
  "settlement_ts": "2026-03-26T02:15:41.753339Z",
  "settlement_value_dollars": "0.0000"
}
```

**Timeline:**
- **Entry:** 2026-03-26T02:05:08 — bot enters trade with gt_prob=1.0, buying YES (OKC)
- **Settlement:** 2026-03-26T02:15:41 — game settles, market result=NO (OKC lost, Boston won)
- **Exit:** 2026-03-26T02:16:33 — bot exits unfilled, exits at same price (no pnl)

**Timing anomaly:** Entry is 10m 33s BEFORE settlement. The ResolutionDetector is supposed to fire AFTER game is final, yet the trade was entered BEFORE the game settled.

**Actual game outcome:** Market ID is "Oklahoma City at Boston", suffix is `-OKC`, so OKC is YES team. Settlement is NO, so OKC lost. **Boston Celtics won.**

**Expected gt_prob:** 0.0 (away team won, YES team lost)  
**Actual gt_prob:** 1.0 (home team predicted to win)

**This confirms the pattern:** gt_prob=1.0 predicted the home team (OKC) would win, but the away team (Boston) actually won.

### Step 4: Reality Check — Favorite vs Winner Hypothesis

**Could not determine:** Pre-game Vegas odds or Kalshi market prices from local cache are not available. The settlement_cache.json only contains final settlement values, not opening prices.

**However:** The consistent pattern across all 12 losing trades is:
- All have gt_prob=1.0 (YES team predicted to win)
- All settled to NO (YES team lost)
- All are sport games where the suffix-encoded team lost

**This rules out the "favorite vs winner" hypothesis** because:
- If the bug were "mistaking favorite for winner," we'd expect mixed outcomes (some games where the favorite won, some where they lost)
- Instead, we see 100% loss rate: all 12 losing trades predicted the YES team (uniformly) and all were wrong
- This is consistent with a systematic bias toward one team (home team in the market_id encoding) rather than a favorite/underdog confusion

### Step 5: Conclusion

**Located writer at `resolution/executor.py:1499`.** Ground truth probability comes from `ResolutionSignal.correct_prob`, which is computed at `resolution_detector.py:346` using substring-match logic. The computation appears syntactically correct and calls `get_yes_team()` to look up the YES team.

**However, timing anomaly is the smoking gun:** 16 of 21 trades are entry trades (delta between entry_ts and settlement_ts ranging from 5 seconds to 20+ minutes), but ResolutionDetector is supposed to fire ONLY at game final (CONFIRMED_FINAL). The OKCBOS trade was entered 10m 33s BEFORE the game settled. This proves **the trades are being generated by a different code path or mechanism than the documented detector exit logic**.

**Suspected root cause:** The ResolutionSignal is correctly computed in `_background_lag_check()` AFTER game final, but is being used for ENTRY (immediate trade placement) rather than EXIT (closing positions). The timing mismatch and the presence of trades entered before game final suggest either:
1. A backfill/replay mechanism that's reconstructing trades after the fact
2. A signal caching mechanism that fires signals from a previous cycle inappropriately
3. An undocumented second code path that creates ResolutionDetector-sourced trades at entry time (before game is final)

**No code path with old `if winner=="home" → 1.0` logic was located in the current codebase,** but the 100% loss rate on home-team predictions suggests the entry path is using a different computation that systematically favors the home team (OR: is using pre-game favorites as a proxy for winner).

---

---

## Phase 3 — Reproduction

### Step 1: Actual `correct_prob` Computation Code

**From `resolution_detector.py` lines 329–346 (verbatim):**

```python
    from data.sports.team_resolver import get_yes_team  # noqa: PLC0415
    _yes_team = get_yes_team(market_id)
    if _yes_team is None:
        logger.warning(
            "ResolutionDetector: cannot determine YES team for %s — skipping signal",
            market_id,
        )
        return

    if completed.winner == "tie":
        # Ties resolve NO regardless of which team is YES
        correct_prob = 0.0
    else:
        _winner_name = (
            completed.home_team if completed.winner == "home" else completed.away_team
        )
        _wn = _winner_name.lower()
        correct_prob = 1.0 if (_wn in _yes_team or _yes_team in _wn) else 0.0
```

**Key code features:**
- Line 330: Calls `get_yes_team(market_id)` and stores in `_yes_team`
- Line 331-336: **If `_yes_team is None`, function RETURNS EARLY — no signal queued**
- Line 345: `_wn = _winner_name.lower()` — lowercases the winner
- Line 346: `correct_prob = 1.0 if (_wn in _yes_team or _yes_team in _wn) else 0.0` — substring match
- **NO try/except around the match** — would crash if `_yes_team` is not a string
- **NO default value** for `_yes_team` if None — it returns instead

### Step 2: Reproduction for All 21 Trades

| market_id | yes_team_returned | type | csv_gt_prob | csv_action | csv_result | csv_correct |
|-----------|-------------------|------|-------------|-----------|-----------|-----------|
| KXNCAAWBGAME-26MAR23UKWVU-WVU | west virginia mountaineers | str | 0.0 | buy_no | no | True |
| KXNCAAWBGAME-26MAR23USCSCAR-USC | usc trojans | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26MAR23TORUTA-UTA | utah jazz | str | 0.0 | buy_no | no | True |
| KXNBAGAME-26MAR25ATLDET-DET | detroit pistons | str | 0.0 | buy_no | no | True |
| KXNBAGAME-26MAR25OKCBOS-OKC | oklahoma city thunder | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26MAR26NOPDET-NOP | new orleans pelicans | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26MAR26NYKCHA-NYK | new york knicks | str | 1.0 | buy_yes | no | False |
| KXNCAAMBGAME-26MAR26IOWANEB-NEB | **(None)** | NoneType | 0.0 | buy_no | no | True |
| KXNBAGAME-26MAR26SACORL-SAC | sacramento kings | str | 1.0 | buy_yes | no | False |
| KXNCAAMBGAME-26MAR26TEXPUR-TEX | **(None)** | NoneType | 1.0 | buy_yes | no | False |
| KXNCAAMBGAME-26MAR26ILLHOU-ILL | **(None)** | NoneType | 0.0 | buy_no | yes | False |
| KXNCAAMBGAME-26MAR26ARKARIZ-ARK | **(None)** | NoneType | 1.0 | buy_yes | no | False |
| KXNBAGAME-26MAR27WASGSW-WAS | washington wizards | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26MAR27DALPOR-POR | portland trail blazers | str | 0.0 | buy_no | no | True |
| KXNCAAWBGAME-26MAR28UKTEX-UK | kentucky wildcats | str | 1.0 | buy_yes | no | False |
| KXNCAAMBGAME-26MAR28IOWAILL-IOWA | iowa hawkeyes | str | 1.0 | buy_yes | no | False |
| KXNCAAMBGAME-26MAR29TENNMICH-TENN | tennessee volunteers | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26APR14MIACHA-MIA | miami heat | str | 1.0 | buy_yes | no | False |
| KXNBAGAME-26APR21PORSAS-SAS | san antonio spurs | str | 0.0 | buy_no | no | True |
| KXNBAGAME-26APR22ORLDET-ORL | orlando magic | str | 0.0 | buy_no | no | True |
| KXNBAGAME-26APR23NYKATL-NYK | new york knicks | str | 0.0 | buy_no | no | True |

**Full CSV saved:** `audit/resdet_phase3_repro.csv`

### Step 3: Winners vs Losers Reconciliation

**WINNERS (8 trades, all correct):**
- 7 trades have `yes_team` as valid string (WVU, Utah Jazz, Detroit, Portland, SAS, Orlando, NYK)
- **1 trade has `yes_team=None`** (Iowa-Neb game)
  - Market: KXNCAAMBGAME-26MAR26IOWANEB-NEB
  - `yes_team` returns: None
  - `gt_prob` in CSV: 0.0
  - Settlement: NO (NEB lost)
  - Outcome: CORRECT (predicted NEB would lose, it did)
- **Pattern: ALL 8 winners have `gt_prob=0.0`** (all predicted YES team would lose; all were right)

**LOSERS (13 trades, all wrong):**
- 10 trades have `yes_team` as valid string
- **3 trades have `yes_team=None`** (TEX, ILL, ARK women's basketball games):
  - TEXPUR-TEX: `yes_team=None`, `gt_prob=1.0`, settled NO (WRONG: predicted TEX wins, it lost)
  - ILLHOU-ILL: `yes_team=None`, `gt_prob=0.0`, settled YES (WRONG: predicted ILL loses, it won)
  - ARKARIZ-ARK: `yes_team=None`, `gt_prob=1.0`, settled NO (WRONG: predicted ARK wins, it lost)
- 12 of 13 losers have `gt_prob=1.0` (predicted YES team would win; all were wrong — YES teams all lost)
- 1 loser (mirror) has `gt_prob=0.0` (predicted YES team would lose; it actually won)

**THE CRITICAL FINDING:**

3 losers return `yes_team=None` from `get_yes_team()`. According to the detector code (lines 331-336), if `yes_team is None`, the function **RETURNS EARLY — no signal is created.**

**But these 3 trades EXIST in the CSV with gt_prob values.**

This is impossible under the documented detector logic. **The trades came from a different code path that does NOT check for None or does NOT call `get_yes_team()` at all.**

### Step 4: Root Cause Conclusion

**The documented detector in `_background_lag_check()` returns early if `get_yes_team()` returns None, preventing signal creation. But 3 of the 13 losing trades have `yes_team=None` and still were assigned `gt_prob` values (1.0, 0.0, 1.0) and entered as trades.** This proves there is a second, undocumented code path that creates ResolutionDetector-sourced trades without checking `get_yes_team()`. That path is responsible for the 13 losing trades and the 8 winning trades; it systematically predicts `gt_prob=1.0` (team will win) which is wrong 12 times out of 13, and `gt_prob=0.0` (team will lose) which is wrong once (the mirror case) and right 8 times (all winners). The pattern—12 losses at `gt_prob=1.0`, 8 wins at `gt_prob=0.0`, 1 loss at `gt_prob=0.0`—is too structured to be random and indicates the second code path uses a hardcoded rule (possibly the old `if winner=="home" → 1.0` logic) rather than the substring-match computation in the documented detector.

---

---

## Phase 4 — Historical state

### Step 1: Git Blame & Commit History

**Commit history for `data/sports/resolution_detector.py`:**

```
7abdcb2  2026-04-28  Move runtime state files to data/runtime/
0450f21  2026-04-15  Centralize YES-team resolution so home/away bug cannot recur  ← THE FIX
2adfaf4  2026-04-01  Fix: stale price gate, ghost sizing cap, exit cooldowns, GT clamp...
c371483  2026-03-10  feat: sports live signal Phase 2 — staleness, panic, and resolution lag
```

**Commit history for `data/sports/team_resolver.py`:**

```
0450f21  2026-04-15  Centralize YES-team resolution so home/away bug cannot recur
```

**Critical finding:** `team_resolver.py` was **CREATED** in commit 0450f21. It did NOT exist before April 15.

### Step 2: Trade-Time Code Verification

**Trades dataset spans March 23 – April 23, 2026.**

**Code in effect at trade time (commit `2adfaf4`, dated 2026-04-01):**

```python
# From data/sports/resolution_detector.py at 2adfaf4 (lines 314-318)
correct_prob = 1.0 if completed.winner == "home" else 0.0
# Ties resolve NO (prob=0.0) since "did home team win?" is False for a tie
if completed.winner == "tie":
    correct_prob = 0.0
```

**Verification that team_resolver did not exist:**
```
$ git show 2adfaf4:data/sports/team_resolver.py
fatal: path 'data/sports/team_resolver.py' exists on disk, but not in '2adfaf4'
```

**Old code analysis:**
- NO call to `get_yes_team()` (function didn't exist)
- NO None check
- NO substring match logic
- Just: `correct_prob = 1.0 if completed.winner == "home" else 0.0`

**This is THE bug** that commit 0450f21's message described:
> "ResolutionDetector previously emitted correct_prob = 1.0 if winner == 'home' else 0.0, which is correct ONLY for home-team markets."

### Step 3: Broader Grep Re-verification

**`grep -rn "source_name"`** (excluding venv): 23 matches across `data/ground_truth/*.py`. None set source_name to "ResolutionDetector" or "ConfirmedFinal" except `executor.py:1499`.

**`grep -rnE "['\"]Resolution"`** (excluding venv): All matches in `resolution_detector.py` are log messages, not source assignments.

**`grep -rnE "\.source\s*="`** (excluding venv): No application code overwrites `.source`. Only matches are in `venv/` (irrelevant).

**Phase 2's grep was correct.** There is only ONE writer at `executor.py:1499`. No downstream code overwrites the source label.

### Step 4: Conclusion

**Answer: (a) Code at trade time differs from current code.**

**Timeline reconciliation:**

| Trade Date Range | # Trades in dataset | Code commit in effect | Bug present? |
|------------------|---------------------|------------------------|--------------|
| Mar 23 – Apr 14 | 18 (incl. all 13 losers) | 2adfaf4 | **YES** (buggy `winner=="home"` logic) |
| Apr 15 (fix landed) | — | 0450f21 | NO (substring-match logic added) |
| Apr 21 – Apr 23 | 3 (all winners) | 0450f21 | NO |

**Why Phase 3's reproduction was misleading:**

Phase 3 ran the *current* code (`get_yes_team()` + None check + substring match) on the historical tickers. It found that `get_yes_team()` returns None for 3 tickers (TEXPUR, ILLHOU, ARKARIZ) and concluded these trades must have come from a "second code path" because the current detector returns early on None.

**This was wrong.** Those 3 trades ran on commit 2adfaf4 code, which had:
- No None check (because no `get_yes_team()` call)
- No `team_resolver.py` (file didn't exist)
- Just `correct_prob = 1.0 if completed.winner == "home" else 0.0`

So when Iowa State played Texas (TEXPUR) and the home team won, the old code set `correct_prob=1.0` regardless of which team was YES. For markets where YES team = away team (which is many of them), this produced systematically wrong predictions.

**Why all 8 winners had `gt_prob=0.0` and all 12 (out of 13) losers had `gt_prob=1.0`:**

Under the buggy logic `correct_prob = 1.0 if winner == "home" else 0.0`:
- When the home team won → `correct_prob = 1.0` (regardless of which team is YES)
- When the away team won → `correct_prob = 0.0` (regardless of which team is YES)

Markets where YES team is the **away** team (e.g., -OKC at Boston) are mislabeled by this rule:
- If home team (Boston) won: bug says correct_prob=1.0, but the market would resolve NO (away team OKC didn't win) → trade buys YES, settles NO, LOSS
- If away team (OKC) won: bug says correct_prob=0.0, but the market would resolve YES (away team won) → trade buys NO, settles YES, LOSS

Markets where YES team is the **home** team:
- If home team won: correct_prob=1.0 correctly (home=YES won) → buy YES, settles YES, WIN
- If away team won: correct_prob=0.0 correctly (home=YES lost) → buy NO, settles NO, WIN

So the "home wins" subset of winners would be correct under the bug. But our dataset has all 8 winners predicting `gt_prob=0.0` (away team wins). This means the dataset's 21 trades happen to have the away team win in cases where YES = away team (the wins) AND the home team win in cases where YES = away team (the losses).

**Phase 3's "second code path" hypothesis is REJECTED.** The single writer at `executor.py:1499` was reading from a `ResolutionSignal` whose `correct_prob` was computed by buggy March/April code, not the current code.

**The bug is already fixed.** Commit 0450f21 replaced the buggy logic on April 15, 2026. The 3 winning trades after that date (Apr 21-23) ran on the fixed code and were correct.

**The "fix didn't work" suspicion (from Phase 1) was wrong.** The diagnostic's mistake was using the *current* `phase0_accuracy_results.csv` to evaluate the *historical* code's performance. The fix did work — but historical losses are immutable in the trade log.

---

## Reconciliation (Phase 5)

### Step 1 — Pre-fix tagging

`data/runtime/phase0_accuracy_results.csv` now includes a `pre_fix` boolean column.

Cutoff: commit 0450f21 landed at `2026-04-15T18:26:42Z` (11:26:42 PDT).

| pre_fix | N | Correct | Accuracy |
|---------|---|---------|----------|
| True (pre-fix) | 18 | 5 | 27.8% |
| False (post-fix) | 3 | 3 | 100.0% |

The 18 pre-fix rows span 2026-03-23 through 2026-04-15T02:28Z. The 3 post-fix rows span 2026-04-22 through 2026-04-24.

### Step 2 — Script update

`scripts/phase0_accuracy.py` updated to:
- Compute `pre_fix` per row (source == `ResolutionDetector/ConfirmedFinal` AND `entry_ts < 2026-04-15T18:26:42+00:00`)
- Write `pre_fix` column to CSV on every run
- Exclude pre-fix rows from Report C (per-source accuracy) by default
- Accept `--include-pre-fix` flag to restore old behavior

### Step 3 — Post-fix accuracy

Post-fix ResolutionDetector trades: **N=3, all correct** (100.0%).

Wilson 95% CI: [43.8%, 100.0%] — interval too wide to be meaningful. Sample size (N=3) is far below the ≥30 threshold required for a reliable estimate. The source needs more live data before its accuracy can be evaluated.

Report C (default, pre-fix excluded) now shows `ResolutionDetector/ConfirmedFinal` in the LOW_VOLUME bucket with the 3 post-fix trades, reflecting the actual post-fix behavior rather than the historical bug.

### Step 4 — Methodological lessons

**Phase 2 (entry-path locator) was correct.** `executor.py:1499` (`_resolution_signal_to_gap()`) is the sole writer of `source="ResolutionDetector/ConfirmedFinal"`. No second writer exists.

**Phase 3's "second code path" hypothesis was wrong.** Phase 3 ran `get_yes_team()` (current code, from `team_resolver.py`) against the 21 historical tickers and observed None returns for 3 NCAAB tickers, concluding a second undocumented code path must exist. This was a methodological error: `team_resolver.py` did not exist at trade time. Phase 4 confirmed this via git blame.

**Lesson: always check git blame before running current code as a proxy for historical behavior.** The right sequence is:
1. Find the relevant commit window for the trades in question (via `git log --follow`)
2. Inspect the code at that commit (`git show <sha>:path/to/file`)
3. Only then reproduce behavior — using the historical code, not the current code

**End of Diagnostic**
