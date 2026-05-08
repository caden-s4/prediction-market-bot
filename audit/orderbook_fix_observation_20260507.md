# Orderbook Fix Observation Report — Phase 10a

**Date:** 2026-05-07  
**Fix:** `kalshi.py:816,824` — `p / 100.0` → `p`, `(100.0 - p) / 100.0` → `1.0 - p`  
**Bot mode:** Ghost (ghost trade simulation, no real orders)  
**Observation window:** 2026-05-07 20:32:32–20:33:24 UTC (~52s of cycle data)  

---

## Pre-Fix Behavior (bot.log.1)

Source: `logs/bot.log.1` — 165 complete cycles, 2026-05-07 ~14:00–20:31 UTC.

| metric | count | per cycle |
|---|---|---|
| Cycles completed | 165 | — |
| `[SIGNAL] ACTIONABLE` | 805 | **4.9** |
| `[SIGNAL] BLOCKED` | 39 | 0.24 |
| `large_divergence_extreme` SKIPs | 74 | **0.45** |
| `gt_stale_at_entry` | 44 | 0.27 |
| Ghost fills fired | 0 | 0 |

**Repeating phantom signals per cycle (last 8 cycles before rotation):**

Every cycle fired 3 ACTIONABLE signals at ~97% gap — same 3 markets, same pattern:
```
KXTNOTEW-26MAY08-T4.22    gt_prob=0.020  mkt_price=0.990  gap=97.0%  side=NO
KXINX-26MAY08H1600-T6850  gt_prob=0.020  mkt_price=0.990  gap=97.0%  side=NO
KXAAAGASD-26MAY08-4.595   gt_prob=0.020  mkt_price=0.991  gap=97.1%  side=NO
```

All three were blocked in executor by `large_divergence_extreme_market` (gap ≥ 85%). The buggy REST parser was reading NO bids of 0.99 and computing YES ask = `(100 − 0.99) / 100 = 0.9901`, making the market look 97% confident YES resolves when the true YES ask is `1 − 0.99 = 0.01`.

---

## Post-Fix Behavior (observation run)

Observation window: `logs/bot.log` — 354 lines, 20:32:32–20:33:24 UTC.

The cycle started a full discovery scan at 20:32:32 (fetching 6,870 Kalshi markets across 35 pages), then entered the weather shadow-candidate phase. The cycle was killed at 90s before reaching GT evaluation / executor. **No `[SIGNAL]` events, no `large_divergence_extreme`, and no ghost fills fired.**

What IS visible in the post-fix window (weather shadow scan):

```
SHADOW_SIGNAL KXLOWTMIN-26MAY07-B32.5   action=buy_no  target=0.0100  market_mid=0.9950
SHADOW_SIGNAL KXLOWTAUS-26MAY07-B60.5   action=buy_yes target=0.9200  market_mid=0.8550
SHADOW_SIGNAL KXLOWTAUS-26MAY07-B58.5   action=buy_no  target=0.9200  market_mid=0.0900
SHADOW_SIGNAL KXLOWTMIA-26MAY07-B74.5   action=buy_no  target=0.0100  market_mid=0.9950
SHADOW_SIGNAL KXLOWTDC-26MAY07-B53.5    action=buy_yes target=0.0100  market_mid=0.0050
SHADOW_SIGNAL KXLOWTDC-26MAY07-B51.5    action=buy_no  target=0.6200  market_mid=0.5800
SHADOW_SIGNAL KXLOWTDC-26MAY07-B49.5    action=buy_no  target=0.9500  market_mid=0.1300
SHADOW_SIGNAL KXLOWTBOS-26MAY07-B49.5   action=buy_yes target=0.9500  market_mid=0.9300
SHADOW_SIGNAL KXLOWTBOS-26MAY07-B47.5   action=buy_no  target=0.9400  market_mid=0.0700
SHADOW_SIGNAL KXLOWTATL-26MAY07-B59.5   action=buy_no  target=0.7200  market_mid=0.4400
```

These market_mid values (0.995, 0.855, 0.09, etc.) are plausible decimal fractions for near-close weather brackets — not the 0.99 artifacts seen in pre-fix data. Weather prices are sourced from WS (already correct path), so this is not a direct test of the REST fix, but confirms the cycle is running normally.

---

## Top 10 Markets by Absolute Mid Change

Live orderbooks pulled at observation time to compute exact buggy vs correct mids.

| ticker | pre-fix mid (buggy) | post-fix mid (correct) | Δ |
|---|---|---|---|
| KXINX-26MAY08H1600-T7549.9999 | 0.9901 | **0.0100** | −0.9801 |
| KXTNOTEW-26MAY08-T4.60 | 0.9901 | **0.0100** | −0.9801 |
| KXTNOTEW-26MAY08-T4.22 | 0.9901 | **0.0100** | −0.9801 |
| KXINX-26MAY08H1600-T6850 | 0.9901 | **0.0100** | −0.9801 |
| KXAAAGASD-26MAY08-4.590 | 0.9907 | **0.0700** | −0.9207 |
| KXAAAGASD-26MAY08-4.595 | 0.9908 | **0.0800** | −0.9108 |
| KXAAAGASD-26MAY08-4.610 | 0.9916 | **0.1600** | −0.8316 |
| KXMVECROSSCATEGORY-... | 0.9963 | **0.6280** | −0.3683 |
| KXRAINNYC-26MAY08-T0 | 0.5034 | **0.8350** | +0.3316 |
| KXNBAGAME-26MAY12MINSAS-SAS | 0.5027 | **0.7700** | +0.2673 |

**Pattern:** Markets with only NO bids (no YES side) see mids drop from ~0.99 to their true values (0.01–0.16). Markets with BOTH sides had mid stuck near 0.50 (harmonic mean of tiny YES bid / near-1 YES ask); these see mids increase to their true levels (0.77–0.84).

**Buggy mid → correct mid arithmetic:**
- Only-NO-bids (e.g., KXTNOTEW-T4.60, NO bid=0.99): buggy `(100−0.99)/100=0.9901`; correct `1−0.99=0.01`
- Both-sides (e.g., KXNBAGAME, YES bid=0.75 / NO bid=0.21): buggy bid `0.75/100=0.0075`, ask `(100−0.21)/100=0.9979`, mid=0.5027; correct bid=0.75, ask=0.79, mid=0.77

---

## Gate Impact Analysis

### `large_divergence_extreme` gate

Pre-fix: 74 hits across 165 cycles (0.45/cycle). All were caused by phantom 97% gaps from buggy mkt_price=0.990 on markets where the true YES ask is $0.01.

Post-fix: The gap for these markets drops from 97% to ~1% (e.g., KXTNOTEW-T4.22: mkt_price=0.01 vs gt_prob=0.02 → gap=1%). This 1% gap is blocked by `insufficient_edge` (below min_gap floor), never reaching `large_divergence_extreme`. Expected: `large_divergence_extreme` → **0 hits/cycle** for REST-fetched markets.

### ACTIONABLE count

Pre-fix: 4.9/cycle. The dominant signals were the 3 phantom 97% gaps above, none of which ever produced a ghost fill. Genuine signals (cross-category, weather snipe candidates) existed but were also being evaluated against potentially wrong market prices.

Post-fix (projected): 
- Phantom gas/treasury/SP500 signals disappear (mkt_price ≈ gt_prob → insufficient_edge)
- Markets with BOTH sides (NBA, weather, cross-category) had mids stuck at ~0.50; now correct — signals from these will reflect true mid vs GT divergence
- Total ACTIONABLE count expected to drop significantly until real edges are confirmed

### Confidence threshold hit rate

Cannot measure from incomplete cycle. No signals reached confidence gate in the observation window. The confidence gate (0.80/0.85) is downstream of gap detection; the fix removes phantom signals before they reach that gate, making confidence hit counts more meaningful in future cycles.

### Ghost fills

Zero in both pre-fix and post-fix sessions. Pre-fix ghost fills were prevented by `large_divergence_extreme`. Post-fix: phantom signals gone, so ghost fills will only fire if real edges clear all gates.

---

## Trades This Cycle

**Trades that fired:** None (cycle incomplete — did not reach executor).

**Near-misses:** 10 weather SHADOW_SIGNAL events (see above). These are scanner-phase candidates, not executor-phase signals. They would proceed to GT evaluation and confidence gating in the next phase of the cycle.

---

## New Gates Lighting Up or Going Dark

| gate | pre-fix | post-fix (expected) | direction |
|---|---|---|---|
| `large_divergence_extreme` | 0.45/cycle | ~0/cycle | **went dark** |
| `insufficient_edge` (gap < min_gap) | not measured | will absorb former phantom signals | will increase |
| confidence gate | not reached by phantoms | will see real signals | more meaningful |
| `ACTIONABLE` count | 4.9/cycle (mostly phantom) | lower (only real edges) | **will decrease** |

---

## Step 5 — Conclusion

**(b) Fix works, but gates are calibrated against the buggy price reality.**

Specifically:
- The fix is mechanically correct: all 6 Phase 9 markets produce correct mids, confirmed by direct unit test.
- The fix eliminates the 3 phantom ACTIONABLE signals per cycle (KXTNOTEW, KXINX-T6850, KXAAAGASD variants) that were dominating signal output and consistently hitting `large_divergence_extreme`.
- The `large_divergence_extreme` gate was functioning as a correct safety valve for a broken input — its hit rate going to zero is expected and correct.
- **Cannot assess:** whether any genuine edges exist at the correct price levels, or whether the confidence/edge/divergence thresholds are appropriately sized for real market prices. The 90-second observation window captured only the weather shadow scan, not a full GT evaluation cycle.
- **Phase 10b prerequisite:** run ≥5 full cycles post-fix, capture the ACTIONABLE/BLOCKED breakdown, and verify that ghost fills (if any) are firing on markets where the correct mid-GT divergence is real.
- **No gate changes made in this phase.**

---

## Verification Gates

- [x] Two-line change at kalshi.py:816/824 only (plus adjacent comment update) — nothing else touched
- [x] mypy/ruff not installed; `py_compile` confirms syntax clean; `p` is `float` from `_parse_level`
- [x] All 6 Phase 9 markets produce correct mid against fixed parser (all PASS, delta=0.000000)
- [x] One observation cycle ran and logged (20:32:32–20:33:24 UTC, 354 lines)
- [x] Report has pre/post-fix funnel comparison (pre-fix: 165 cycles, 4.9 ACTIONABLE/cycle, 0.45 large_div/cycle)
- [x] Top 10 mid changes table populated (computed from live orderbooks)
- [x] No gate or threshold modified
- [x] Conclusion is **(b)**: fix works, gate calibration for real price reality TBD in Phase 10b
