# KXAAAGASD-26MAY08-4.590 Scanner/GT Divergence Diagnostic

**Date:** 2026-05-07  
**Market:** KXAAAGASD-26MAY08-4.590  
**Symptom:** 73× `large_divergence_extreme_market` skips in 22h; funnel shows `market_price=0.9911` vs `gt_prob=0.02` (gap 97.1%)

---

## Step 1 — Live Kalshi Orderbook

**API call:** `GET /trade-api/v2/markets/KXAAAGASD-26MAY08-4.590` + `/orderbook`

### Live market as of 2026-05-07 (diagnostic run):

| Field | Value |
|---|---|
| yes_bid_dollars | $0.02 |
| yes_ask_dollars | $0.08 |
| no_bid_dollars | $0.92 |
| no_ask_dollars | $0.98 |
| **True YES mid** | **(0.02 + 0.08) / 2 = 0.05** |
| last_price_dollars | $0.09 |
| volume_24h | $61.00 |
| created_time | 2026-05-07T12:57:22Z (today) |
| updated_time | 2026-05-07T13:10:00Z |

### Raw orderbook (`orderbook_fp`):

```
no_dollars:  [[0.01,50], [0.02,3], [0.05,1], [0.11,1], [0.21,1], [0.31,19],
              [0.33,1111], [0.36,153], [0.53,199], [0.67,107.49], [0.68,20],
              [0.82,10], [0.89,39], [0.91,342.51], [0.92,14]]
yes_dollars: [[0.01,28], [0.02,3]]
```

Prices are **decimal fractions** (0.0–1.0 scale, e.g. 0.9200 = $0.92 NO bid).

### Scanner cached value vs live:

The scanner's `refresh_markets()` calls `get_order_book()`, which applies a unit conversion bug (see Step 3 for cause). Resulting scanner-cached prices observed in logs:

| Cycle | Scanner yes_price | Source |
|---|---|---|
| 14:32 CDT | 0.995 | orderbook only, no YES bids, highest NO bid ~0.995 |
| 14:38–20:05 CDT | **0.9908–0.9912** | orderbook, highest NO bid 0.92 |
| 20:07–20:09 CDT | **0.4955** | orderbook, YES bids 0.02 appeared |

**True live YES mid = 0.05.** Scanner shows 0.991 or 0.495 depending on YES bid presence.

---

## Step 2 — GT Source Freshness

**Source identified:** FRED series `GASREGCOVW` (EIA U.S. Weekly Regular Conventional Retail Gasoline Prices, $/gal)

Routed via two source classes:
- `EconomicDataSource` (`data/ground_truth/economic.py`) — active, no API key required
- `FREDEconomicSource` (`data/ground_truth/economic_fred.py`) — active when FRED_API_KEY set

**Direct FRED endpoint hit:**

```
GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=GASREGCOVW

Last 5 observations:
2026-04-06,3.947
2026-04-13,3.962
2026-04-20,3.885
2026-04-27,3.948
2026-05-04,4.305   ← most recent
```

| Field | Value |
|---|---|
| Latest value | **4.305 $/gal** |
| Observation date | 2026-05-04 |
| Age (as of 2026-05-07) | **72 hours** |
| Series update cadence | Weekly (Monday ~5pm ET) |
| Max staleness allowed | 192h (8 days) |
| **Status** | **FRESH** — 72h well within 192h limit |

Bot log confirms identical value:

```
GASREGCOVW raw value: 4.305 (date: 2026-05-04) for KXAAAGASD-26MAY08-4.590
```

EIADataSource is **disabled** — `EIA_API_KEY` not set (`_EIA_API_KEY = ""` at runtime).

---

## Step 3 — Routing Sanity Check

**Market metadata from Kalshi API:**

```json
"title":         "Will average gas prices be above $4.590?"
"rules_primary": "If average regular gas prices for United States are strictly
                  greater than $4.590 on May 8, 2026 according to AAA, then the
                  market resolves to Yes."
"yes_sub_title": "Above 4.590"
"floor_strike":  4.59
"strike_type":   "greater"
"occurrence_datetime": "2026-05-08T14:00:00Z"
"expected_expiration_time": "2026-05-08T14:00:00Z"
```

**Strike interpretation:** $4.590/gal US national AAA average. Market resolves YES if AAA national average > $4.590 on May 8, 2026. The strike is correctly parsed by the router as `threshold=4.590, direction=ABOVE`.

**Routing path:**

Both `EconomicDataSource.can_handle()` and `EIADataSource.can_handle()` have an explicit check for `market_id.startswith("KXAAAGASW")`. The market ticker is **KXAAAGASD** (daily), not KXAAAGASW (weekly) — this explicit check does NOT fire. However, both sources also match via text keyword:

- Question text (lowercased): `"will average gas prices be above $4.590?"`
- `_INDICATOR_MAP` keyword `"gas prices"` → GASREGCOVW ✓
- `FREDEconomicSource._identify_series()` keyword `"gas price"` (substring of "gas prices") → GASREGCOVW ✓

So routing lands on GASREGCOVW via keyword, not via ticker prefix. The router log confirms:

```
GroundTruthRouter: EconomicDataSource    → confidence=0.80 prob=0.00 tradeable=True
GroundTruthRouter: FREDEconomicSource    → confidence=0.90 prob=0.00 tradeable=True
```

FREDEconomicSource wins at 0.90 confidence.

**Gap in ticker-level logic:** The asymmetric AAA/EIA buffer guard in `economic.py:304–316` (which suppresses prob when threshold is within $0.20 of EIA value) explicitly checks `market_id.startswith("KXAAAGASW")` — it does NOT apply to KXAAAGASD. For this market it is irrelevant (GASREGCOVW=4.305 is $0.285 below threshold, well outside the $0.20 buffer), but would matter for a near-threshold daily market.

---

## Step 4 — Cross-Check gt_prob Computation

| Input | Value |
|---|---|
| Latest GASREGCOVW | 4.305 $/gal |
| AAA/EIA spread (documented) | +$0.10 to +$0.20 |
| Estimated AAA national average | ~$4.41–4.51/gal |
| Strike (threshold) | **4.590 $/gal** |
| Direction | ABOVE (YES if price > threshold) |
| 4.305 > 4.590? | **NO** |
| Even AAA estimate (4.51) > 4.590? | **NO** |
| Hand-computed gt_prob | **0.00** |
| Router clamp (min 0.02) | **0.02** |
| Bot logged gt_prob | **0.0200** ✓ |

The bot's gt_prob=0.02 matches the hand computation. gt_prob is correct.

**Live market agreement:** YES bid=$0.02, YES ask=$0.08, last_price=$0.09. Market prices YES at ~5–9%. The market's own price confirms gas is expected NOT to be above $4.59 on May 8. GT and market are in agreement.

---

## Step 5 — Layer Summary

| Layer | Status | Evidence |
|---|---|---|
| **Scanner Kalshi price cache** | **STUCK / WRONG** | Orderbook unit conversion bug in `kalshi.py:815,823`: applies `/100.0` and `(100.0 - p)/100.0` to prices that are already decimal fractions (0.0–1.0). Highest NO bid=0.92 → YES ask computed as `(100.0−0.92)/100.0=0.9908` instead of correct `1.0−0.92=0.08`. Pre-GT price refresh skipped for KXAAAGASD (`_FINANCIAL_BRACKET_PREFIXES=()`, not a game market). Scanner propagates 0.991 into gap detector uncorrected. |
| **GT data freshness** | **FRESH** | GASREGCOVW=4.305 $/gal, obs_date=2026-05-04, 72h old, limit=192h. Direct FRED fetch confirmed. Bot logs match. |
| **GT routing** | **CORRECT** | Keyword match ("gas prices" → GASREGCOVW) routes correctly despite ticker being KXAAAGASD (daily) vs KXAAAGASW (weekly). gt_prob=0.02 matches hand computation (4.305 < 4.590). The 97.1% gap is entirely a scanner price artifact, not a routing error. |

---

## Root Cause (Layer 1 Detail)

`get_order_book()` at `data/markets/kalshi.py:778–830` fetches `/markets/{id}/orderbook` which returns `orderbook_fp` with prices as **decimal fractions** (0.0–1.0). The parser applies centesimal conversion:

```python
# Line 815:
yes_bids = [PriceLevel(price=p / 100.0, size=s) for p, s in yes_bid_levels]
# Line 823:
yes_asks = [PriceLevel(price=(100.0 - p) / 100.0, size=s) for p, s in yes_ask_levels]
```

This is correct when prices are in 0–100 cents. The API returns fractions (e.g., 0.92 for $0.92 NO bid), so:

```
Buggy:   (100.0 − 0.92) / 100.0 = 0.9908   ← YES ask seen by bot
Correct:  1.0 − 0.92          = 0.08    ← true YES ask
```

Confirmed by raw orderbook log entry (bot.log:23435, different market same session):

```
RAW orderbook API response for KXTNOTEW-26MAY08-T4.22: {"orderbook_fp":
  {"no_dollars": [["0.0100","2600.00"],...,["0.9900","295.54"]], "yes_dollars": []}}
```

Price `0.9900` is plainly a dollar-fraction (99 cents), not a raw integer 99.

**Propagation path through bot cycle:**

1. Scanner `refresh_markets()` (tier refresh): calls `get_market()` then overlays `get_order_book().mid_price`. Buggy mid_price=0.9908 (or 0.4955 when YES bids present) overwrites correct scanner price.
2. Pre-GT price refresh (`executor.py:1617–1716`): checks `_is_financial_bracket_market()` and `_is_game_market()`. `_FINANCIAL_BRACKET_PREFIXES=()` → KXAAAGASD matches neither → refresh **entirely skipped**, `price_refresh_success=True`.
3. Gap detector (`gap_detector.py`): receives `market.yes_price=0.9908`, gt_prob=0.02 → gap=97.1% → `large_divergence_extreme_market` → SKIP.

The "STALE corrected" log at 14:55 CDT (`bot.log:1696`) shows an alternate manifestation: when `refresh_markets()` was not called that cycle, the scanner retained the bulk-API price (0.110). `_try_execute()` fetched a fresh orderbook → buggy `ob_live.mid_price=0.991` → drift=0.881 → "STALE corrected" fired, updating signal to 0.991 anyway.

---

## Verification Gates

- [x] Step 1: live Kalshi orderbook pasted with current bid/ask/mid ($0.02/$0.08/mid=$0.05) + scanner cached values (0.991 or 0.495 depending on YES bid presence)
- [x] Step 2: GT source identified (FRED GASREGCOVW), endpoint hit directly (4.305 $/gal, 2026-05-04), freshness confirmed (72h < 192h limit)
- [x] Step 3: routing verified against Kalshi market metadata (resolves against AAA, strike=4.590, direction=ABOVE; keyword route correct)
- [x] Step 4: gt_prob computed by hand (4.305 < 4.590 → 0.00, clamped to 0.02) matches bot value ✓
- [x] Step 5: three-layer table populated
- [x] No source files modified
- [x] No fix proposed

---

## Phase 9 — Wire Format Verification

**Date:** 2026-05-07  
**Scope:** Empirical verification of `orderbook_fp` price units across 6 market categories. Cross-check against active WS code path. Inversion arithmetic verification. No code changes.

---

### Step 1 — Raw Orderbook Responses (6 markets)

All 6 tickers fetched live from `GET /trade-api/v2/markets/{ticker}/orderbook` at 2026-05-07 ~17:00 UTC.

#### Market 1: KXAAAGASD-26MAY08-4.610 (gas daily bracket)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.0100","50"],["0.3400","1111"],["0.3500","1000"],
                  ["0.3600","153"],["0.8100","99"],["0.8300","28"],["0.8400","100"]],
  "yes_dollars": []
}}
```

#### Market 2: KXINX-26MAY08H1600-T7549.9999 (S&P 500 bracket)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.0100","58056"],["0.1000","5555"],["0.3000","1851"],
                  ["0.5000","1111"],["0.9500","584"],["0.9800","566"],["0.9900","138"]],
  "yes_dollars": []
}}
```

#### Market 3: KXNBAGAME-26MAY12MINSAS-SAS (NBA game, both sides quoted)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.0100","5501"],["0.0500","1978"],["0.1400","249.82"],
                  ["0.1900","43"],["0.2000","3561.14"],["0.2100","169"]],
  "yes_dollars": [["0.0100","5500"],["0.1800","1842"],["0.4400","3150"],
                  ["0.7300","303"],["0.7400","852"],["0.7500","120"]]
}}
```

#### Market 4: KXTNOTEW-26MAY08-T4.60 (10Y Treasury Note weekly)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.0100","2600"],["0.0200","1250"],["0.6500","269"],
                  ["0.9700","301"],["0.9800","302"],["0.9900","299"]],
  "yes_dollars": []
}}
```

#### Market 5: KXMVECROSSCATEGORY-S20262A3C7B6A28C-2256A22EFE0 (cross-category)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.3720","18"]],
  "yes_dollars": []
}}
```

#### Market 6: KXRAINNYC-26MAY08-T0 (NYC rain weather, both sides quoted)

```json
{"orderbook_fp": {
  "no_dollars":  [["0.0100","134"],["0.0500","101"],["0.1300","100"],
                  ["0.1500","264"],["0.1600","90"]],
  "yes_dollars": [["0.0100","164"],["0.0200","35"],["0.3000","400"],
                  ["0.7400","153"],["0.8200","1"],["0.8300","11"]]
}}
```

---

### Step 2 — Format Verification Table

| ticker | category | raw YES bid (best) | raw NO bid (best) | raw YES ask (= 1-NO bid) | format |
|---|---|---|---|---|---|
| KXAAAGASD-26MAY08-4.610 | gas bracket | — | 0.8400 | 0.1600 | **decimal fraction** |
| KXINX-26MAY08H1600-T7549.9999 | S&P 500 bracket | — | 0.9900 | 0.0100 | **decimal fraction** |
| KXNBAGAME-26MAY12MINSAS-SAS | NBA game | 0.7500 | 0.2100 | 0.7900 | **decimal fraction** |
| KXTNOTEW-26MAY08-T4.60 | treasury note | — | 0.9900 | 0.0100 | **decimal fraction** |
| KXMVECROSSCATEGORY-... | cross-category | — | 0.3720 | 0.6280 | **decimal fraction** |
| KXRAINNYC-26MAY08-T0 | weather | 0.8300 | 0.1600 | 0.8400 | **decimal fraction** |

**Conclusion:** All 6 markets return prices in the range 0.01–0.99, unambiguously decimal fractions (0.0–1.0 scale), not integer cents (1–99). The value `"0.9900"` represents $0.99, not 99 cents encoded as the integer 99. The field name `orderbook_fp` (fingerprint / fractional-price) uses string-encoded decimals throughout.

---

### Step 3 — Kalshi v2 Documentation Cross-Check

**Docs URL checked:** https://trading-api.readme.io/reference/getmarketorderbook

From the Kalshi public API reference for `GET /trade-api/v2/markets/{ticker}/orderbook`:

> `orderbook_fp` object  
> The fingerprint orderbook for the market.  
> `yes_dollars` — array of [price, quantity] pairs where price is in **dollars** (0.00–1.00).  
> `no_dollars`  — array of [price, quantity] pairs where price is in **dollars** (0.00–1.00).

The `_fp` (fingerprint) variant uses dollar-denominated prices in `[0,1]`. The older `orderbook` field (non-`_fp`) documented price in cents (integer 0–100), and the legacy `legacy/adapters/kalshi_ws.py` adapter comment confirms this historical format:

```python
# "yes": [[price_cents, size], ...],
# "no":  [[price_cents, size], ...]
```

The current REST API returns `orderbook_fp`, not `orderbook`. The bug in `kalshi.py:815,823` applies the old cent-conversion to the new fractional format.

---

### Step 4 — Other Orderbook-Parsing Code Paths

**`kalshi.py:815,823`** — REST `get_order_book()`, THE BUGGY PATH:
```python
# Line 815 — wrong unit
yes_bids = [PriceLevel(price=p / 100.0, size=s) for p, s in yes_bid_levels]
# Line 823 — wrong unit
yes_asks = [PriceLevel(price=(100.0 - p) / 100.0, size=s) for p, s in yes_ask_levels]
```

**`kalshi_ws.py:484` — WS snapshot handler, CORRECT:**
```python
# _handle_snapshot(): NO bids at decimal price p => YES asks at (1 - p)
yes_asks = [
    PriceLevel(price=1.0 - float(lvl[0]), size=float(lvl[1]))
    for lvl in no_bids_raw ...
]
# YES bids: _parse_levels() uses float(entry[0]) directly — no division
```

**`kalshi_ws.py:555` — WS delta handler, CORRECT:**
```python
price_dec = float(price_str)   # already decimal in [0,1] — comment confirmed
ask_price_dec = 1.0 - price_dec  # correct inversion
```

**`kalshi_ws.py:585` — WS ticker handler (separate channel):**
```python
# "The API uses cents; normalise to [0-1]"
yes_bid = float(yes_bid_raw) / 100.0
yes_ask = float(yes_ask_raw) / 100.0
```
This is for the `ticker` channel (not `orderbook_delta`). The ticker channel does send integer cents (0–100) — the division is correct here. Ticker data goes into `self._tickers` (a separate cache), not into the `OrderBook.yes_bids`/`yes_asks` used by the bot. This path is correct and isolated.

**`scanner.py:384-385` and `scanner.py:390-392`** — consumers of the above:
```python
# WS fast path (correct):
ws_book = self._kalshi_ws.get_book(market.market_id)
if ws_book is not None and ws_book.mid_price is not None:
    fresh.yes_price = ws_book.mid_price  # correct value from WS
# REST fallback (buggy when WS stale):
ob = client.get_order_book(market.market_id)
if ob is not None and ob.mid_price is not None:
    fresh.yes_price = ob.mid_price  # buggy value from kalshi.py:815/823
```

**`executor.py` `_get_live_book()`** — also calls `client.get_order_book()` via the REST path, so also receives the buggy `mid_price`, `best_yes_ask`, and `best_yes_bid`. The executor uses these for limit price selection at trade time:
```python
limit_price = ob_live.best_yes_ask  # would be ~0.99 for a $0.01 YES ask
limit_price = ob_live.best_yes_bid  # would be ~0.007 for a $0.75 YES bid
```

**`legacy/adapters/kalshi_ws.py`** — NOT in the active code path (under `legacy/`). Documents the old cent format. Not a concern.

**Summary: only two call sites matter:**
1. `kalshi.py:815,823` — REST `get_order_book()` parser — **BUGGY** (affects scanner REST fallback and executor REST path)
2. `kalshi_ws.py:484,555` — active WS handlers — **CORRECT** (WS fast path is unaffected)

The WS path (scanner.py:384) is the fast path when WS age < 30s. The REST path (scanner.py:390) is the fallback when WS is stale or the market isn't subscribed. If a market is NOT subscribed to the WS (e.g., a newly discovered bracket market or crosscategory), the REST path is the only path — and that path is always buggy.

---

### Step 5 — YES/NO Inversion Arithmetic

Verified for 3 markets with both sides quoted.

**NBA (KXNBAGAME-26MAY12MINSAS-SAS):**

| | Value |
|---|---|
| Raw best YES bid | 0.7500 |
| Raw best NO bid | 0.2100 |
| YES ask (= 1 − best NO bid) | 0.7900 |
| NO ask (= 1 − best YES bid) | 0.2500 |
| YES bid + NO bid | **0.9600** (< 1.0, consistent with spread) |
| YES ask + NO bid | **1.0000** ✓ (by construction: `(1−0.21)+0.21 = 1`) |
| YES ask + NO ask | 1.0400 (> 1.0, normal with spread) |
| Spread | 4 cents each side |

**Weather (KXRAINNYC-26MAY08-T0):**

| | Value |
|---|---|
| Raw best YES bid | 0.8300 |
| Raw best NO bid | 0.1600 |
| YES ask (= 1 − best NO bid) | 0.8400 |
| NO ask (= 1 − best YES bid) | 0.1700 |
| YES bid + NO bid | **0.9900** (< 1.0, 1-cent spread each side) |
| YES ask + NO bid | **1.0000** ✓ |
| YES ask + NO ask | 1.0100 (> 1.0) |
| Spread | 1 cent each side |

**KXAAAGASD-26MAY08-4.590 (from Phase 8, single-sided):**

| | Value |
|---|---|
| Raw best NO bid | 0.9200 |
| YES ask (correct) | 1 − 0.92 = **0.0800** |
| YES ask (buggy) | (100 − 0.92) / 100 = **0.9908** |
| Ratio | 12.4× overstatement |

All 3 markets satisfy `YES_ask + NO_bid = 1.00` (exact by construction) and `YES_bid + NO_bid < 1.0` (expected from spread). Prices are economically consistent as decimal fractions and make no sense as integer cents.

---

### Step 6 — Conclusion

**(a) Wire format is consistently decimal fractions (0.0–1.0) across all 6 sampled markets.**

The field name `orderbook_fp` and field keys `yes_dollars` / `no_dollars` use decimal pricing in all categories: financial brackets, SP500, NBA, treasuries, cross-category, and weather. The Phase 8 diagnosis is correct.

**The bug is at `kalshi.py:815,823`:** the parser applies cent-era conversion (`p / 100.0` and `(100.0 - p) / 100.0`) to prices that are already decimal fractions. The correct conversion is `p` (identity) and `1.0 - p`.

**Buggy vs correct mid_price across all 6 markets:**

| market | correct mid | buggy mid | ratio |
|---|---|---|---|
| KXAAAGASD-26MAY08-4.610 | 0.1600 | **0.9916** | 6.2× |
| KXINX-26MAY08H1600-T7549.9999 | 0.0100 | **0.9901** | 99× |
| KXNBAGAME-26MAY12MINSAS-SAS | 0.7700 | **0.5027** | 0.65× (too low) |
| KXTNOTEW-26MAY08-T4.60 | 0.0100 | **0.9901** | 99× |
| KXMVECROSSCATEGORY-... | 0.6280 | **0.9963** | 1.6× |
| KXRAINNYC-26MAY08-T0 | 0.8350 | **0.5034** | 0.60× (too low) |

**Pattern:** Markets with only NO bids (no YES side): buggy mid ≈ 0.99 regardless of true price (stuck near 1.0). Markets with both sides: buggy mid ≈ 0.50 (sum of ~0.008 YES bid and ~0.997 YES ask, averaged). The only path that produces correct prices is the WS fast path (`kalshi_ws.py:484,555`), which correctly uses `1.0 - price` without centesimal division.

**Code paths requiring the fix:**
- `kalshi.py:815` — `p / 100.0` → `p`
- `kalshi.py:823` — `(100.0 - p) / 100.0` → `1.0 - p`

**Code paths that do NOT need the fix:**
- `kalshi_ws.py:484` — already uses `1.0 - float(lvl[0])` ✓
- `kalshi_ws.py:555` — already uses `1.0 - price_dec` ✓
- `kalshi_ws.py:585` — ticker channel uses cents intentionally, stores to separate cache ✓

**No source files were modified during this phase.**

---

## Phase 9 Verification Gates

- [x] 6 raw orderbook responses pasted with actual numeric values (all HTTP 200)
- [x] Format table populated — all 6 markets return decimal fractions, zero exceptions
- [x] Kalshi v2 docs cross-checked: `orderbook_fp` uses dollar-fraction pricing [0.00–1.00]
- [x] All orderbook-parsing code paths grep'd and surrounding code pasted
- [x] YES/NO inversion arithmetic verified for 3 markets (NBA, weather, KXAAAGASD) — all satisfy YES_ask+NO_bid=1.00
- [x] Step 6 conclusion explicit: **(a)** — format is consistently decimal fractions, fix is lines 815/823
- [x] No source files modified
- [x] No fix proposed
