# Phase D-WS — Kalshi WebSocket Subscription Lifecycle Audit

Read-only forensic audit of `data/markets/kalshi_ws.py` and its call
sites in `bot.py`, `resolution/executor.py`, and `resolution/scanner.py`.
Anchored on `Kalshi WS error (code=?): {'code': 4, 'msg': 'Subscription
IDs required'}` firing 1,080× in the active `logs/bot.log` slice
(2026-05-19 08:43–14:42, ~5.8h) with `ws-share` consistently 0–46 of
50–150 markets per refresh.

No source modified. No fix proposed.

---

## TL;DR

The original Tier 3 #10 framing ("empty-batch unsubscribe responses,
harmless protocol noise") is **refuted by current evidence**. The
errors are not noise; they are 1:1 server rejections of every
unsubscribe frame the client sends.

- **`_send_unsubscribe` sends `params.market_ticker` (singular);
  Kalshi WS v2 expects `params.sids` (subscription-ID array)**
  (`data/markets/kalshi_ws.py:427-438`). The bug was acknowledged in
  commit `6d07f39` (2026-05-05) as a deferred fix and never landed.
- **Forensic 1:1 correlation in current log:** 1,082 unsubscribe
  frames sent → 1,080 `code=4` errors logged (2-error delta = frames
  still in flight at log-slice end).
- The server rejects each unsubscribe but **silently keeps the
  subscription bound** (no further book activity from the client
  invalidates it). Inbound `orderbook_snapshot` lines keep flowing
  (2,691 in the log) regardless of how many code=4 errors fire.
- **Low ws-share (0–17/50 T1) is a *separate* problem, not caused
  by code=4.** 2,151 of 2,691 snapshots arrive with `0 bids, 0 asks`
  (80%). The scanner's `<30s update-age` gate at
  `resolution/scanner.py:501` then excludes any subscribed market
  that hasn't ticked recently. Both effects → REST fallback. This
  was already diagnosed verbatim in reverted commit `ee9a101`:
  *"T2 cycle shows 0/150 WS hits despite 1135 markets subscribed
  and snapshot-confirmed because most of them haven't received a
  delta in the last 30 seconds."*
- The error handler at `data/markets/kalshi_ws.py:452-454` reads
  `data.get("code", "?")` but the server wraps errors under `msg.*`,
  so the WARNING line always shows `code=?` and the actual code lives
  in the body — a logging bug that masked the diagnosis.

Verdict ranking (Part 7): unsubscribe-format bug is **High-confidence,
forensic**. Low ws-share is **High-confidence, multi-cause** (empty
books + illiquid-tick-rate vs. <30s gate). The two are independent.

---

## Part 1 — Connection lifecycle

### 1.1 Instantiation and start

- `bot.py:71-75` — `KalshiWebSocket(api_key, api_secret)` constructed
  once per bot process when `config.kalshi.enabled`. `.start()` called
  immediately.
- `data/markets/kalshi_ws.py:136-145` — `start()` launches a single
  daemon thread named `"kalshi-ws"`. Idempotent via `self._started`
  flag.

### 1.2 URL and auth

- URL: `wss://api.elections.kalshi.com/trade-api/ws/v2`
  (`data/markets/kalshi_ws.py:104`, overridable via `base_url=` kwarg).
- Auth: RSA-PSS/SHA-256 signed handshake mirroring REST. Headers
  computed in `_sign_ws()` at `data/markets/kalshi_ws.py:264-289`:
  `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
  over `ts_ms + "GET" + "/trade-api/ws/v2"`.
- No handshake messages beyond the HTTP/WS upgrade; subscriptions
  follow over the established connection.

### 1.3 Lifecycle ownership

- Single long-lived background thread (`data/markets/kalshi_ws.py:141-145`)
  running `asyncio.new_event_loop()` in `_run_loop()`
  (`data/markets/kalshi_ws.py:293-302`).
- Outer connect-loop at `_ws_loop()` (`data/markets/kalshi_ws.py:304-350`):
  - `websockets.connect(..., ping_interval=20, ping_timeout=10,
    close_timeout=5)` (`data/markets/kalshi_ws.py:314-320`).
  - On success: logs `"Kalshi WS connected"`, resets backoff, calls
    `_resubscribe_all(ws)`, then `_run_connection(ws)`.
  - On `InvalidStatusCode` / `OSError` / `ConnectionClosed` /
    `WebSocketException` / generic `Exception`: logs WARNING/EXC and
    falls through to backoff (`data/markets/kalshi_ws.py:330-347`).
  - Backoff: exponential `1.0 → 2.0 → 4.0 → 8.0 → 16.0 → 30.0` seconds,
    capped (`data/markets/kalshi_ws.py:97-98, 349-350`).

### 1.4 Reconnect re-subscribe

- `_resubscribe_all` (`data/markets/kalshi_ws.py:352-361`) snapshots
  `self._subscribed` under `_sub_lock`, then iterates 100-ticker chunks
  through `_send_subscribe`. Logs `"Re-subscribed to %d tickers after
  reconnect"`.
- **Log evidence: zero reconnects in the active 5.8h log window.**
  `grep -c "Kalshi WS connected\|Re-subscribed"` on
  `logs/bot.log` = 0. The connection established before the log slice
  started has stayed up the entire window.

### 1.5 [INFERENCE] Connection health vs. error volume

The 1,080 `code=4` errors are not symptoms of connection failure —
the receive loop is healthy and dispatching messages normally
(`logs/bot.log:21+` snapshot/delta lines stream continuously between
errors). Errors are application-level rejections of malformed
unsubscribe frames, not transport-level events.

---

## Part 2 — Subscription lifecycle

### 2.1 Subscribe path

| Call site | What it sends | Frame format |
|---|---|---|
| `KalshiWebSocket.subscribe()` (`data/markets/kalshi_ws.py:147-160`) | Diffs against `self._subscribed`, enqueues one `_WsCommand("subscribe", "orderbook_delta", new_tickers)` | n/a (queue only) |
| `_cmd_pump` (`data/markets/kalshi_ws.py:392-410`) | Drains queue every 50ms; subscribe batched 100/chunk | calls `_send_subscribe` |
| `_send_subscribe` (`data/markets/kalshi_ws.py:414-425`) | One frame per chunk of ≤100 tickers | `{"id": next, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": [...]}}` |

Subscribe payload key is **`market_tickers` (plural array)** — correct
per Kalshi WS v2 (confirmed by commit `6d07f39` author note 2026-05-05;
subscribes succeed: 2,691 snapshots received in log window).

### 2.2 Caller path into `subscribe()`

- `resolution/executor.py:884` — `self._kalshi_ws.sync_subscriptions(kalshi_t1_t2)`
  every cycle with all T1+T2 Kalshi markets.
- `resolution/executor.py:1589` — `self._kalshi_ws.subscribe(kalshi_tickers)`
  for current cycle's candidates (subset of T1+T2 typically already
  covered).
- `sync_subscriptions` (`data/markets/kalshi_ws.py:182-201`) computes
  `target - currently` and `currently - target`, then calls
  `subscribe()`/`unsubscribe()` for the diffs.

### 2.3 Unsubscribe path

| Call site | What it sends | Frame format |
|---|---|---|
| `KalshiWebSocket.unsubscribe()` (`data/markets/kalshi_ws.py:162-180`) | Filters against `self._subscribed`, enqueues `_WsCommand("unsubscribe", ...)`, evicts `self._books`/`self._tickers` | n/a (queue only) |
| `_cmd_pump` (`data/markets/kalshi_ws.py:407-410`) | **One frame PER TICKER** (not batched) with 10ms sleep between | calls `_send_unsubscribe(ws, ticker)` per ticker |
| `_send_unsubscribe` (`data/markets/kalshi_ws.py:427-438`) | One frame per ticker | `{"id": next, "cmd": "unsubscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": ticker}}` |

**Unsubscribe payload key is `market_ticker` (singular).** Per
Kalshi WS v2 (and the error message itself), the server expects
`params.sids` — the subscription IDs returned in the prior `subscribed`
/ `ok` acks. The current code path never references `sids`.

### 2.4 Subscription state tracking

- `self._subscribed: Set[str]` (`data/markets/kalshi_ws.py:116`).
  Client-side set of ticker strings.
- Mutated in:
  - `subscribe()` line 153 (`add`)
  - `unsubscribe()` line 168 (`discard`)
  - Read in `_resubscribe_all` line 355, in `sync_subscriptions` line 191.
- **No SID tracking.** The `subscribed` ack handler at
  `data/markets/kalshi_ws.py:462-465` extracts `sid` and logs it; the
  `ok` ack handler at lines 455-461 extracts `sid` and logs at DEBUG.
  Neither persists the sid into `_subscribed` or any other structure.
- **Asymmetric state mutation:** `subscribe()` adds to `_subscribed`
  *before* the server acks, and `unsubscribe()` removes from
  `_subscribed` *before* the server acks/rejects. So when the server
  rejects an unsubscribe with code=4, the client-side state already
  shows the ticker as unsubscribed, but the server-side subscription
  is still active. The next `sync_subscriptions` diff will then re-add
  the ticker to `target - currently` and re-issue a subscribe for an
  already-subscribed market.

[INFERENCE] The server appears tolerant of duplicate subscribes
because no code=4 fires after subscribe waves — only after unsubscribe
waves. To verify, runtime instrumentation would need to log raw `ok`
acks during a sync_subscriptions cycle that contains both adds and
removes.

---

## Part 3 — Message handling

### 3.1 Dispatch

`_process_message` at `data/markets/kalshi_ws.py:442-469` routes by
`data.get("type")`:

| `type` | Handler / behaviour | Line |
|---|---|---|
| `orderbook_snapshot` | `_handle_snapshot` — full book replace, logs INFO | 445-446, 471-519 |
| `orderbook_delta` | `_handle_delta` — incremental level update, no log | 447-448, 521-577 |
| `ticker` | `_handle_ticker` — bid/ask cache update | 449-450, 579-614 |
| `error` | WARNING log `"Kalshi WS error (code=%s): %s"`, no recovery action | 451-454 |
| `ok` | DEBUG log of subscribed_total, no state update | 455-461 |
| `subscribed` | INFO log of channel/sid, no state update | 462-465 |
| `unsubscribed` | INFO log of msg body, no state update | 466-467 |
| (other) | WARNING `"WS unknown msg type=%s"` | 468-469 |

### 3.2 Error path is fire-and-forget

`data/markets/kalshi_ws.py:451-454`:

```python
elif msg_type == "error":
    code = data.get("code", "?")
    message = data.get("msg", data.get("message", ""))
    logger.warning("Kalshi WS error (code=%s): %s", code, message)
```

Notable:
- **`code` is always logged as `"?"`** in production. The actual log
  line is `Kalshi WS error (code=?): {'code': 4, 'msg': 'Subscription
  IDs required'}`. The server nests the error under `msg.*` — i.e.
  `{"type":"error", "msg":{"code":4,"msg":"Subscription IDs
  required"}}` — so `data.get("code", "?")` returns the default and
  the full nested dict ends up in `message`. **This is a logging bug**
  that obscured the original Tier 3 #10 diagnosis. [Surfaced for
  follow-up §8.1.]
- No recovery action: no retry, no subscription-state invalidation,
  no re-subscribe attempt. Errors are observability noise from the
  client's perspective.

### 3.3 Sequence tracking

Per current code, **no per-market or session-level sequence tracking
exists.** Snapshot/delta dispatch validates only the presence of
`market_ticker` and the levels' shape (`data/markets/kalshi_ws.py:489-491,
540-542`). The `seq` field on `ok` acks is logged at DEBUG only
(`data/markets/kalshi_ws.py:460-461`); the `seq` field on
`orderbook_delta` is never read.

Git history (`git log -- data/markets/kalshi_ws.py`):
- `ee9a101` (2026-05-05) — added `last_seq`/`snapshot_seq`/`valid`
  fields to `_BookEntry` with per-market seq gating
- `a173100` (2026-05-05, 23 min later) — **reverted** with note that
  per-market design was wrong by spec ("Kalshi's seq is a session-global
  counter paired with sid, not per-market"). Author committed to a
  Phase 2B.4b/2B.4c follow-up that has not landed.

[INFERENCE] Phase 2B.4b/2B.4c have not been completed — `grep -r` finds
no session-level seq tracking added since. [Surfaced for follow-up §8.3.]

---

## Part 4 — The code=4 mechanism

### 4.1 What triggers code=4

**Forensic: every `_send_unsubscribe` frame.** The 1:1 correlation
in `logs/bot.log` is unambiguous:

```
$ awk -F'|' '/Queued unsubscribe for/ {split($NF,a," "); sum+=a[4]} END {print sum}'
total unsubs queued: 1082

$ grep -c "Subscription IDs required" logs/bot.log
1080
```

(The 2-error delta is timing: the last batch's frames are still in
flight at log-slice end.)

Each unsubscribe `_WsCommand` is fanned out one frame per ticker by
`_cmd_pump` lines 407-410:

```python
elif cmd.action == "unsubscribe":
    for ticker in cmd.tickers:
        await self._send_unsubscribe(ws, ticker)
        await asyncio.sleep(0.01)
```

So a 14-ticker unsubscribe queue (08:46:37) → 14 frames sent → 14
code=4 errors (`logs/bot.log:90-108`). An 83-ticker unsubscribe queue
(09:00:14) → 83 frames → 83 errors (`logs/bot.log:659-741`).

### 4.2 Why the frame is malformed

`data/markets/kalshi_ws.py:427-438` sends:

```python
{"id": ..., "cmd": "unsubscribe",
 "params": {"channels": ["orderbook_delta"],
            "market_ticker": ticker}}
```

Per Kalshi WS v2 (commit `6d07f39` author note 2026-05-05:
*"Unsubscribe format bug (returns 'Subscription IDs required' error)
... separate fix in upcoming phases"*), unsubscribe takes `sids`:

```
{"id": ..., "cmd": "unsubscribe",
 "params": {"sids": [<sid1>, <sid2>, ...]}}
```

[INFERENCE] The exact correct payload (`sids` plural array vs. `sid`
singular, plus whether `channels` is required) is not in any code
comment or docstring inside this repo. The error message
"Subscription IDs required" plus the prior commit's framing strongly
suggest `sids`, but verifying the canonical shape requires the Kalshi
WS v2 docs themselves. [Open question for Sunny §9.1.]

The client doesn't track sids — see §2.4 — so even if `_send_unsubscribe`
were rewritten to use `sids`, it would need a sid→ticker map populated
from `ok`/`subscribed` acks first.

### 4.3 Why bursts of 8-10 at cycle start

The baseline diagnostic noted "bursts of 8-10 at cycle start". The
mechanism, forensically:

- `executor.py:872-889` calls `sync_subscriptions(kalshi_t1_t2)` once
  per cycle in the `kalshi_ws_sync` phase.
- `sync_subscriptions` computes `removed = currently - target`. As
  T1/T2 churn between cycles (markets expire, tier boundaries shift),
  `removed` is typically 1–20 per cycle.
- Each removed ticker → one `_send_unsubscribe` frame → one code=4.
- Burst size = unsubscribe count for the cycle.

Larger bursts (14 at 08:46:37, 83 at 09:00:14) coincide with the end
of `discovery_scan` (every 8–10 cycles per
`logs/bot.log` timing), when the registry is rebuilt and many T1/T2
entries get evicted at once. Smaller bursts (1 per cycle, the steady
state) coincide with single tier churn.

The "bursts" pattern is therefore not unique to bot startup or
reconnect — it's just sync churn. It is NOT batched into one frame
because `_cmd_pump` deliberately sends one frame per ticker for
unsubscribes (see §2.3).

---

## Part 5 — Why ws-share stays low

### 5.1 ws-share computation

`resolution/scanner.py:497-519`:

```python
ws_book = None
if self._kalshi_ws is not None and market.platform == "kalshi":
    ws_age = self._kalshi_ws.get_book_age(market.market_id)
    if ws_age is not None and ws_age < 30.0:
        ws_book = self._kalshi_ws.get_book(market.market_id)

if ws_book is not None and ws_book.mid_price is not None:
    _check_ws_rest_agreement(...)
    fresh.yes_price = ws_book.mid_price
    with _counter_lock:
        _ws_hits[0] += 1
else:
    try:
        ob = client.get_order_book(market.market_id)
        if ob is not None and ob.mid_price is not None:
            fresh.yes_price = ob.mid_price
    except Exception:
        pass
    if market.platform == "kalshi":
        with _counter_lock:
            _rest_fallbacks[0] += 1
```

A market counts as ws-served iff **all** of the following:

1. `self._kalshi_ws is not None` (always true when Kalshi enabled).
2. `market.platform == "kalshi"`.
3. `get_book_age(market_id)` returns non-`None` (i.e. a `_BookEntry`
   exists in `self._books` for that ticker — requires that a snapshot
   has arrived).
4. `age < 30.0` seconds since `entry.last_updated`. `last_updated` is
   set only when a snapshot arrives (`data/markets/kalshi_ws.py:514`)
   or a delta is applied (`data/markets/kalshi_ws.py:577`).
5. `get_book().mid_price is not None` — requires at least one bid
   AND one ask in `OrderBook`.

### 5.2 Why most markets fail this gate

**Cause 1 (forensic): empty books on snapshot arrival.**

```
$ grep -c "Received orderbook_snapshot" logs/bot.log
2691
$ grep -c "0 bids, 0 asks"  logs/bot.log
2151
```

80% of incoming snapshots have `0 bids, 0 asks` — these are
KXMVECROSSCATEGORY-*, KXHYPE15M-*, and other low-liquidity series
that subscribe successfully but have no resting book. `OrderBook(...,
yes_bids=[], yes_asks=[])` → `mid_price` is `None` → fallback to REST.

**Cause 2 (forensic + commit ee9a101 author note): illiquid markets
that don't tick within 30s.**

`ee9a101` (2026-05-05, reverted): *"T2 cycle shows 0/150 WS hits
despite 1135 markets subscribed and snapshot-confirmed because most
of them haven't received a delta in the last 30 seconds."*

A market that received a non-empty initial snapshot but no deltas
since has `last_updated` from the snapshot time. After 30s without
a delta the gate at scanner.py:501 trips → fallback to REST. Most
T1/T2 markets satisfy this (they're not actively trading every 30s).

**Cause 3: not caused by code=4.** The 1,080 code=4 errors do not
affect subscription state for subscribed markets — those that arrived
via successful `_send_subscribe` keep streaming. Unsubscribe failures
just leave server-side bindings that the client thinks are gone, but
the client never queries those tickers anyway (it removed them from
`self._subscribed`, and scanner only queries by `market_id`).

[INFERENCE] Concretely: a market that goes T2→T3 demotion will get
a (failed) unsubscribe, then the server keeps sending snapshots/deltas
that the client ignores (no `_BookEntry` to apply deltas to per
`data/markets/kalshi_ws.py:556-559`). This is wasted bandwidth, but
does not corrupt ws-share for tickers that *are* in `_subscribed`.

### 5.3 ws-share log evidence

Sampled from `logs/bot.log`:

| Time | T1 ws/rest/total | T2 ws/rest/total |
|---|---|---|
| 08:45:00 / 08:45:32 | 0/50/50 | 46/104/150 |
| 08:46:49 / 08:47:26 | 0/50/50 | 4/146/150 |
| 08:48:43 / 08:49:21 | 0/50/50 | 0/150/150 |
| 08:50:39 / 08:51:12 | 0/50/50 | 38/112/150 |
| 09:00:16 / 09:00:51 | 0/8/8 | 20/130/150 |

Pattern: T1 ws-share is structurally near 0 (T1 = imminent-resolution
markets, often illiquid or in pre-game state where book is empty);
T2 ws-share is variable 0–46 (some recent ticks during NBA/NCAAB
in-game). Both align with the empty-book + stale-tick story, not
with subscription failures.

---

## Part 6 — Historical context

### 6.1 Prior WS fixes (`git log --since="60 days ago" -- data/markets/kalshi_ws.py`)

| Commit | Date | Fix |
|---|---|---|
| `16aaceb` | (earlier) | Add: KalshiWebSocket initial client |
| `f6051cc` | (earlier) | Integrate: orderbook cache with REST fallback in executor |
| `6ffc9a6` | 2026-04-05 | Fix: WS command queue drain (subscribe was never sent) |
| `0a346ef` | 2026-04-05 | Fix: per-ticker subscribe `market_ticker` singular, silence ok spam, fix subscribed parsing |
| `3ec172c` | (earlier) | Add: `sync_subscriptions` |
| `06cd7d6` | (earlier) | executor: sync Kalshi WS subscriptions to T1+T2 each cycle |
| `6d07f39` | 2026-05-05 | **Fix: subscribe param name `market_ticker` → `market_tickers`** (plural) — explicitly defers the unsubscribe-format bug |
| `11e92a4` / `257c5a0` | 2026-05-05 | TEMP probe to log raw WS messages, then revert |
| `e746ff0` | 2026-05-05 | **Fix: orderbook handlers to match v2 wire format** (msg.* nesting, dollar-decimal pricing) — fixes the "silently discarding all incoming data" bug |
| `ee9a101` | 2026-05-05 | Add: per-market seq tracking on `_BookEntry` |
| `a173100` | 2026-05-05 | **Revert** per-market seq tracking — design wrong by spec (seq is session-global) |
| `beea41c` | 2026-05-07 | Add: hot-path numeric invariants |

### 6.2 Mapping to user-mem hints

- *"silently discarding all incoming data due to a message format
  mismatch (msg.* nesting, dollar-decimal pricing)"* → fixed by
  `e746ff0`. Current code uses `data.get("msg") or {}` and reads
  `market_ticker` from there (`data/markets/kalshi_ws.py:488-489,
  538-539`). **Fix is intact.**
- *"Monotonic subscription leak"* → likely refers to either (a)
  `_msg_id` lock added in `0a346ef` (was `int(time.time()*1000)`,
  now monotonic + thread-safe — `data/markets/kalshi_ws.py:131-132,
  227-231`) or (b) the per-market seq design that was tried and
  reverted in `ee9a101`/`a173100`. Current code uses `_next_id()`
  for `id`; no per-market seq. **Fix (a) is intact; (b) was
  abandoned.**
- *"Broken unsubscribe format"* → **explicitly deferred in commit
  `6d07f39` body and never fixed.** Current `_send_unsubscribe`
  still emits `params.market_ticker`. This is the active bug.

### 6.3 Original Tier 3 #10 framing context

The original diagnosis ("empty-batch unsubscribe responses, harmless
protocol noise, not subscription failures") is inconsistent with the
current code path and current log evidence:

- **Not based on earlier code:** the `_send_unsubscribe` shape has
  not changed since `0a346ef` (2026-04-05) — that commit established
  per-ticker unsubscribe with `market_ticker` singular and it
  remains the same today.
- **Plausibly based on a different log signature:** the current
  WARNING line shows `code=?` due to the logging bug at
  `data/markets/kalshi_ws.py:452` (§3.2). An earlier reader who saw
  `code=?` and an empty/short message body may have assumed empty-
  batch noise. The full body `{'code': 4, 'msg': 'Subscription IDs
  required'}` only becomes visible by reading the `message` field
  carefully, which the WARNING line *does* include but is easy to
  glance past.
- **Or, the original framing was based on the absence of crash /
  reconnect symptoms** — true that errors don't crash the connection
  (§1.5), but the framing then over-generalized to "harmless" when
  the symptom is per-cycle protocol churn at the volume of unsubscribed
  tickers.

The May 5 commit body explicitly identifies the bug as real and
unfixed. **The Tier 3 #10 diagnosis was an under-investigation, not
a regression.**

### 6.4 Regression check

- The unsubscribe-format bug is NOT a regression: it is an
  unaddressed leftover from the April→May 2026 WS hardening pass.
  `6d07f39` (2026-05-05) explicitly defers it.
- Per-market seq tracking is not a regression: the revert
  (`a173100`) was correct (per-market seq was wrong by spec).
- The orderbook silent-discard bug (`e746ff0`) is fixed and current
  code still uses the correct `msg.*` reads.

---

## Part 7 — Verdict

Two distinct mechanisms, both well-supported by evidence and
independent of each other.

### 7.1 Mechanism A — Unsubscribe payload format bug (code=4 source)

**Confidence: HIGH (forensic, multi-source).**

- What it is: `_send_unsubscribe` at `data/markets/kalshi_ws.py:427-438`
  sends `params.market_ticker` (singular ticker string); Kalshi WS v2
  expects `params.sids` (subscription-ID array).
- Code evidence:
  - `data/markets/kalshi_ws.py:432-435` shows the malformed payload.
  - No SID tracking exists (§2.4) — the `sid` field in `ok` /
    `subscribed` acks is logged then discarded (`data/markets/kalshi_ws.py:460,
    463`).
  - Author commit `6d07f39` (2026-05-05) explicitly acknowledges the
    bug as deferred.
- Log evidence:
  - 1:1 correlation: 1,082 unsubscribe frames sent ↔ 1,080 code=4
    errors logged (§4.1).
  - Burst sizes match unsubscribe counts (§4.3, e.g. 14→14 at
    08:46:37 and 83→83 at 09:00:14).
  - No code=4 fires after subscribe waves — only after unsubscribe
    waves.
- Contradictory evidence: none in the active log slice. Subscribe
  flow is healthy (2,691 snapshots received), so the error is
  unambiguously bound to the unsubscribe-only path.
- What would distinguish: nothing further needed for the *root cause*.
  Verifying the exact correct payload (`sids` array? `sid` singular?
  with or without `channels`?) requires consulting Kalshi WS v2 docs
  or a test subscribe→capture-sid→unsubscribe-with-sids cycle.

### 7.2 Mechanism B — ws-share stays low (subscribe success ≠ usable book)

**Confidence: HIGH (forensic, multi-cause).**

- What it is: ws-share at 0–46/150 is the result of two compounding
  filters at the scanner's WS check, NOT subscription failure.
- Code evidence:
  - `resolution/scanner.py:497-519` — book usable iff (snapshot
    received) AND (age <30s) AND (mid_price not None).
  - Empty-book handling at `data/markets/kalshi_ws.py:506-512` creates
    a `_BookEntry` even when `yes_bids`/`yes_asks` are both empty;
    `OrderBook.mid_price` then returns `None`.
- Log evidence:
  - 2,151 / 2,691 snapshots (80%) arrive with `0 bids, 0 asks`.
  - Commit `ee9a101` author note (2026-05-05): *"T2 cycle shows
    0/150 WS hits despite 1135 markets subscribed and
    snapshot-confirmed because most of them haven't received a delta
    in the last 30 seconds."*
- Contradictory evidence: none. When books *are* fresh and non-empty
  (NBA/NCAAB in-game ticks), ws-share moves up to ~46/150 (08:45:32
  T2 sample) — consistent with the gate working as designed for
  tickers that actually tick.
- What would distinguish: instrument the scanner to log, per
  fallback, *why* the WS path was skipped (no entry / age expired /
  empty book) to confirm relative weights. Out-of-scope per phase
  brief.

### 7.3 Mechanism C — connection-level instability (RULED OUT)

**Confidence: HIGH negative.**

- Zero reconnects in the 5.8h log window (`grep -c "Kalshi WS
  connected\|Re-subscribed"` = 0).
- Receive loop processes 2,691 snapshots without interruption between
  error bursts.
- No `_ws_loop` exception lines (`grep -c "Unexpected error in WS
  loop"` = 0).

The connection is stable. code=4 errors are application-level rejects,
not transport events.

### 7.4 Mechanism D — error handler silently swallowing recoverable failures (PARTIAL)

**Confidence: MEDIUM — true but consequence-bounded.**

- `data/markets/kalshi_ws.py:451-454` does no recovery, no retry, no
  state invalidation.
- For code=4 *specifically*, no recovery is appropriate (the request
  was malformed; retrying with the same payload would fail again).
- For *other* error codes (not observed in current log), the handler
  is equally silent — this is a generic robustness gap, not a cause
  of the current code=4 symptom.

### 7.5 Ranking

| # | Mechanism | Evidence weight | Causal link to code=4 | Causal link to low ws-share |
|---|---|---|---|---|
| A | Unsubscribe `market_ticker` vs. `sids` | **HIGH (forensic 1:1)** | Direct | None |
| B | Empty books + 30s stale-tick gate | **HIGH (forensic 80% + commit note)** | None | Direct |
| C | Connection instability | **HIGH negative** | Ruled out | Ruled out |
| D | Generic error-swallow handler | MEDIUM | Bounded (no recovery is correct for code=4) | None |

The Tier 3 #10 finding (code=4 noise) is **Mechanism A** and is a real
bug. The Tier 3 #11 finding (low ws-share / cycle time over budget) is
**Mechanism B** and is a separate, deeper architectural problem (the
<30s tick-freshness gate is the wrong validity signal for illiquid
T2 markets, per the abandoned Phase 2B.4b/2B.4c).

### 7.6 Runtime instrumentation needed?

For Mechanism A: not needed for diagnosis. Needed only to verify the
*correct* unsubscribe payload before implementing a fix. Two
approaches:

1. Log raw `subscribed` and `ok` ack bodies to extract the sid format,
   then experimentally send `params.sids: [<sid>]` vs.
   `params.sid: <sid>` and observe which gets accepted.
2. Consult Kalshi WS v2 docs (external).

For Mechanism B: instrumentation would help **scope** the fix — log
per-fallback the reason (no-entry / stale-age / empty-book). Useful
input for the deferred Phase 2B.4c. Not needed for diagnosis itself.

---

## 8. Adjacent bugs surfaced (out of scope — do NOT investigate or fix)

### 8.1 Error logger reads wrong fields → `code=?` always

`data/markets/kalshi_ws.py:451-454` reads `data.get("code", "?")` and
`data.get("msg", data.get("message", ""))` but Kalshi WS v2 error
frames nest as `{"type":"error", "msg":{"code":N,"msg":STR}}`. The
top-level `code` field doesn't exist, so the log always shows `code=?`
and the entire nested dict ends up in `message`. This is what masked
the Tier 3 #10 diagnosis.

### 8.2 Asymmetric sub/unsub state mutation

`subscribe()` and `unsubscribe()` update `self._subscribed` *before*
the server acks. If the server rejects, the client-side state
diverges from server state. Next `sync_subscriptions` will then
re-issue based on the bad assumption. Symptom under current
unsubscribe-bug: server-side subscriptions accumulate; client thinks
they're cleared.

### 8.3 Phase 2B.4b/2B.4c deferred indefinitely

Commit `ee9a101` (reverted by `a173100`) explicitly committed to
session-level seq tracking (2B.4b) and a scanner WS read-path rewrite
(2B.4c). Neither has landed (no later commits add either). Without
2B.4c, ws-share will stay structurally low for illiquid markets even
if Mechanism A is fixed.

### 8.4 Unsubscribe batch-shape asymmetry

`_cmd_pump` at `data/markets/kalshi_ws.py:404-410`:
- Subscribe: 100 tickers per frame (batched).
- Unsubscribe: **1 ticker per frame** with 10ms sleep.

An 83-ticker unsubscribe takes ~830ms during cycle start, blocking
the cmd pump loop. Aligning unsubscribe to also batch would be
trivial — needs to land alongside the sids fix because the API
shape for batched unsubscribe is the same `sids: [...]` array.

### 8.5 `polymarket_ws.py` has a same-name `sync_subscriptions`

(`data/markets/polymarket_ws.py:106`.) Not in scope for this audit.
Mention only because a "fix WS sync" patch could accidentally touch
both files; they should be reasoned about separately.

---

## 9. Open questions for Sunny

### 9.1 Correct unsubscribe payload shape

Kalshi WS v2's exact unsubscribe param name and value type — `sids`
array? `sid` singular? With or without `channels`? — is not
documented in this repo. The error message and commit history say
"Subscription IDs required" / "sids" but the canonical doc would
nail down the shape before any fix lands.

### 9.2 Phase 2B.4 — abandoned or deprioritized?

`ee9a101`/`a173100` committed to a Phase 2B.4b (session-level seq
tracking) and 2B.4c (scanner WS read-path rewrite). Were these
abandoned outright, or just deprioritized behind other phases?
Without 2B.4c, fixing Mechanism A alone will eliminate code=4 spam
but **will not improve ws-share** — the cycle-time problem (Tier 3
#11) will persist.

### 9.3 Tier 3 #10 — reopen or new issue?

The audit shows the original "harmless protocol noise" diagnosis was
incomplete (Mechanism A is real). Whether to reopen Tier 3 #10 in
place, or close it with a pointer to a new issue covering both A
and the prerequisite §8.1 logging fix, is a process call.

### 9.4 API-key / endpoint changes in the relevant window?

Baseline shows code=4 at very steady per-cycle rate (1 per cycle in
steady state). If anything changed at the Kalshi end (key rotation,
endpoint version bump, different auth mode) on 2026-05-05 or later
that might explain why this only became visible recently, that
would inform priority.

---

## Verification

```
$ git status diagnostics/phase_d_ws_audit.md
?? diagnostics/phase_d_ws_audit.md

$ git diff data/markets/kalshi_ws.py | wc -l
0

$ git diff resolution/scanner.py | wc -l
0

$ git diff resolution/executor.py | wc -l
0

$ git diff bot.py | wc -l
0
```

No source modified. No persistent state modified. No fixes proposed.
All forensic claims cite `file:line` and all inferences are tagged
`[INFERENCE]`.
