# Phase WS-Diag-A1 — Kalshi WS ConnectionClosedError loop diagnosis

Mode: read-only. No source modified. No fix proposed.

## Window

Log files read:
- `logs/bot.log.2` — 39,276 lines — 2026-05-31 05:00:37 → 2026-05-31 16:42:49 (~11h45m)
- `logs/bot.log.1` — 38,839 lines — 2026-05-31 16:42:56 → 2026-06-01 03:56:43 (~11h14m)
- `logs/bot.log`   — 26,980 lines — 2026-06-01 03:56:43 → 2026-06-01 11:18:32 (~7h22m)

Log files NOT read (per task scope):
- `logs/bot.log.3` — pre-fix data (code=7 storm self-cleared 04:09 May 31); referenced only for context that the prior code=7 bug is closed.

Total window: **~30h 18m**, 105,095 log lines.

## WS-Fix-B identification

Commit: **`87892d4`** — *"kalshi_ws: fix unsubscribe payload and state authority"*
Author: Caden Sun <cadensun2018@gmail.com>
Date:   2026-05-19 15:12:48 -0700

Files touched:
- `data/markets/kalshi_ws.py` (+221 / −39 effective via 221 insertions, replaces unsubscribe path)
- `tests/test_kalshi_ws.py` (+246 / 16 new cases)

Per commit message, WS-Fix-B bundles four related changes:
1. SID tracking (ticker→sid map populated from `ok`/`subscribed` acks)
2. Unsubscribe payload format (`params.sids` array, not `params.market_ticker`)
3. Batched unsubscribe (100/frame, mirrors subscribe)
4. Ack-driven `_subscribed` mutation (server-authoritative state)

Companion observability fix `e22bb02` ("kalshi_ws: read error code from correct nested field", 2026-05-19 14:57:30) is labelled **WS-Fix-A** in its commit message; both landed the same afternoon.

No other commits touched `data/markets/kalshi_ws.py` between `87892d4` and the current HEAD `b33f79d`.

## Current exception logging

File: `data/markets/kalshi_ws.py`. Reconnect outer-loop exception handler at lines **351–359**:

```
351   except (
352       OSError,
353       websockets.exceptions.ConnectionClosed,
354       websockets.exceptions.WebSocketException,
355   ) as exc:
356       logger.warning(
357           "Kalshi WS connection lost (%s), retrying in %.0fs",
358           type(exc).__name__, backoff,
359       )
```

Fields logged on close: **only `type(exc).__name__` and the backoff value.**

Fields NOT logged that the `websockets.exceptions.ConnectionClosedError` exception object exposes:
- `exc.code` — server close code (numeric, e.g. 1000/1001/1006/4000+)
- `exc.reason` — server close reason string
- `exc.rcvd` — `Close` frame received from peer (code + reason)
- `exc.sent` — `Close` frame sent to peer
- `exc.rcvd_then_sent` — boolean indicating which side initiated

No other catch site for `ConnectionClosedError` exists in the file. `_recv_loop` (line 399) and `_cmd_pump` (line 415) propagate the exception up to `_run_connection` → `_ws_loop`, where it is caught at lines 351–359.

No DEBUG-level logging of outgoing frames is enabled in current bot runs (the `WS subscribe sent` / `WS unsubscribe sent` lines at `kalshi_ws.py:450,485` are at `logger.debug` and produce zero hits in the window).

## Connection-lifetime distribution

Pairing rule: each `Kalshi WS connected` line paired with the next chronological `Kalshi WS connection lost` line (across all three files, flattened in time order).

| Metric            | Value     |
|-------------------|-----------|
| pairs             | **13,348** |
| mean              | 6.876 s   |
| std dev           | 0.443 s   |
| min               | 5 s       |
| max               | 10 s      |
| p10               | 6 s       |
| p25               | 7 s       |
| p50               | **7 s**   |
| p75               | 7 s       |
| p90               | 7 s       |
| p99               | 8 s       |

Histogram, 1-second buckets, 0–30 s (`>30s` is the overflow bucket):

```
 0s:      0
 1s:      0
 2s:      0
 3s:      0
 4s:      0
 5s:      1
 6s:   2191  ################################
 7s:  10654  ################################################################################
 8s:    461  ######
 9s:     40
10s:      1
11s+:     0
>30s:     0
```

**79.8 % of connections die at exactly 7 s; 16.4 % at 6 s; 3.5 % at 8 s; 0.3 % outside this band.** Zero outliers above 10 s in 13,348 pairs.

Inter-disconnect gap (consecutive `lost`→`lost`): mean 8.171 s, median 8 s, p99 9 s, min 7 s, max 11 s. The reconnect + reconnect-backoff = 1 s sleep + a re-connect that itself lives ~7 s.

Total reconnect events ≈ 13,349 across ~30h 18m → **~7.34 disconnects/min sustained**. Exception class: 100 % `ConnectionClosedError` (no `OSError`, no `WebSocketException`, no `InvalidStatusCode`).

## Periodicity findings

**Connection lifetime is the dominant pattern.** Connections die at a near-fixed offset from connect, not at any wall-clock time. The 7 s median ± 0.4 s std is tighter than what a network-failure or congestion explanation would produce.

Disconnects by hour-of-day (UTC of log timestamp):

| Hour | Count | Hour | Count |
|------|-------|------|-------|
| 00 | 437 | 12 | 444 |
| 01 | 438 | 13 | 444 |
| 02 | 438 | 14 | 440 |
| 03 | 440 | 15 | 438 |
| 04 | 438 | 16 | 440 |
| **05** | **888** | 17 | 439 |
| **06** | **890** | 18 | 439 |
| **07** | **883** | 19 | 439 |
| **08** | **888** | 20 | 435 |
| **09** | **878** | 21 | 437 |
| **10** | **881** | 22 | 437 |
| 11 | 581 | 23 | 437 |

The hours 05–10 show ~2× the rate — this is a **coverage artifact, not real periodicity**: those clock-hours appear twice in the window (once from `bot.log.2` on 2026-05-31 and once from `bot.log` on 2026-06-01). Normalised by coverage, the rate is uniform at ~440 disconnects/hour ≈ 7.33/min — identical to the headline rate above.

Disconnects by minute-of-hour and second-of-minute are uniformly distributed (top buckets within 5 % of each other). **No alignment to clock minute or second.**

**No gaps where the connection survived longer than the median by more than 3 s.** The max observed lifetime in 13,348 connections is 10 s. The bot has not had a single connection live past 10 s for the entire 30-hour window.

## Pre-disconnect context

Subscription-related events that appear at INFO level in the window:

| Event source | Count in `bot.log` (7h22m) |
|-------------|---------------------------|
| `Queued subscribe for N tickers` (`kalshi_ws.py:170`) | 28 |
| `sync_subscriptions added=N removed=M total=K` (`kalshi_ws.py:215`) | 226 |
| `WS session subscribed: channel=...` (`kalshi_ws.py:553`) | **0** |
| `Re-subscribed to N tickers after reconnect` (`kalshi_ws.py:384`) | **0** |

Observations:
- The cycle's `sync_subscriptions` call runs ~30 times/hour (every ~2 minutes — Tier-1 cycle cadence), and 28 of those resulted in queued subscribe frames. Disconnects (~440/hr) are **~15×** more frequent than subscribe attempts. The vast majority of disconnects therefore occur in cycles with **no outgoing subscribe/unsubscribe traffic** queued.
- `WS session subscribed` would log on receipt of a server `subscribed` ack. **Zero hits** across the window means the server never sends one — either it does not exist in v2, or the bot disconnects before any are received.
- `Re-subscribed to N tickers after reconnect` (`_resubscribe_all`, kalshi_ws.py:368–384) logs only when `tickers` is non-empty (line 379 short-circuits on empty). **Zero hits** across 13,348 reconnects means `_subscribed | _subscribing_in_flight` is empty at every reconnect entry. Per the WS-Fix-B logic at lines 374–376, ack-driven mutation requires an `ok` or `subscribed` ack before `_subscribed` is populated; if connections die at 7s before any ack lands, the set stays empty across the reconnect.
- No DEBUG-level outgoing-frame logs are emitted (the bot runs at INFO).
- The cycle-level events surrounding disconnects (sampled in `_ws_diag_a1_out.txt` lines 178–217) show no consistent trigger — disconnects co-occur with `gt_fetch_loop_inner`, `cycle_summary_write`, GroundTruthRouter results, etc. No event class precedes >50 % of disconnects.

What the bot does immediately before each disconnect, given the above, is **whatever cycle work happened to be running**. No subscribe/unsubscribe traffic was on the wire in the typical case.

## Close metadata availability

**No.**

Pattern search across all three log files (`105,095` lines):
- `code=` near a `kalshi_ws` line: **0 hits**
- `reason=` near a `kalshi_ws` line: **0 hits**

`code=` exists in the log format in general (used by WS-Fix-A's error-message handler at `kalshi_ws.py:502`, logging Kalshi server-side application errors received as `type=error` messages), but **none fire in this window** because the connection dies before any application-level error frames arrive. The close-side handler at lines 351–359 does not read `exc.code` or `exc.reason`, so server-supplied close metadata is discarded entirely.

## Verdict

**(c) Server close code/reason is NOT logged — Phase A2 instrumentation is required to determine cause.**

The current exception handler logs only the exception class name. `ConnectionClosedError` from the `websockets` library carries `code` (per RFC 6455 close code, including Kalshi-specific 4xxx codes if the server emits them) and `reason` attributes that are not read. Without adding those two fields to the warning, the report cannot distinguish:

- A server-side 4xxx close (e.g., auth/subscription/protocol violation specific to Kalshi v2)
- A 1006 abnormal close (no close frame received — typical of TCP/TLS layer death)
- A 1000/1001 normal/going-away close (server cycling clients)
- A 4xxx code carrying a structured reason ("rate limit", "session expired", etc.)

The 7 s ± 0.4 s lifetime is too tight to be network-layer randomness; it is most consistent with a server-imposed timeout (auth/session/heartbeat at the protocol layer) — but the close code is the only artifact that can confirm which. Phase A2 should add `getattr(exc, 'code', None)` and `getattr(exc, 'reason', None)` (and optionally `exc.rcvd` / `exc.sent` repr) to the warning at lines 356–359, restart the bot, and capture ≥100 fresh disconnects.

## Out-of-scope items observed

Logged here so they're not lost. None were investigated beyond noting their existence.

1. **`Re-subscribed to N tickers after reconnect` fires 0 times across 13,348 reconnects.** Either `_subscribed`/`_subscribing_in_flight` are both empty at reconnect (ack-driven mutation never completes in the 7 s lifetime), or there is a separate state-clear path. Worth verifying in Phase A2.

2. **`WS session subscribed` fires 0 times across the window.** Server may not send `subscribed`-type acks in v2, or the bot disconnects before any are received. The `ok` path (DEBUG level, not visible at INFO) may be the actual ack channel — if so, the `subscribed` branch at `kalshi_ws.py:508–511` is dead code given the current server behavior.

3. **Cycle-time regression: `cycle took 59953ms — severely over Tier-1 interval (15s)`** observed in `bot.log` (line 28, 2026-06-01 03:56:45). Not part of this scope. The WS reconnect loop may be a contributor but is not, by itself, sufficient evidence to scope a cycle-time fix here.

4. **WS-Fix-B's vaccine claim ("WS subscription state becomes server-authoritative")** depends on ack receipt. If the 7 s lifetime is short enough that acks never arrive, the WS-Fix-B logic is functionally unreached on this server. This is a hypothesis worth checking in Phase A2 once close metadata is captured.

5. **`websockets` library version is not pinned in this report.** `ConnectionClosedError` attribute names (`.code` / `.reason` vs `.rcvd.code` / `.rcvd.reason`) differ between `websockets` ≥10 and ≤9. Phase A2 instrumentation should `getattr`-guard or check the installed version.
