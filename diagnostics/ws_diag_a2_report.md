# Phase WS-Diag-A2 — close metadata capture results

Mode: read-only diagnostic following a single-line instrumentation change. No fix proposed.

## Change applied

Single block edited at `data/markets/kalshi_ws.py:351–363`, diff `+6/-2`:

```
                close_code = getattr(exc, "code", None)
                close_reason = getattr(exc, "reason", None)
                rcvd = getattr(exc, "rcvd", None)
                sent = getattr(exc, "sent", None)
                logger.warning(
                    "Kalshi WS connection lost (%s) code=%s reason=%r rcvd=%r sent=%r, retrying in %.0fs",
                    type(exc).__name__, close_code, close_reason, rcvd, sent, backoff,
                )
```

Exception types caught (lines 351–355) unchanged. No other modifications.

- `websockets` version: **16.0** (has both `.code`/`.reason` and `.rcvd`/`.sent` Close-object attributes)
- `python -m py_compile data/markets/kalshi_ws.py`: pass
- `ruff` and `mypy` are not installed in this Python env; no `pyproject.toml`/`setup.cfg` lint config in repo. Bytecode compile is the available static check.
- Format sanity check with `None` args produced one clean line, no `%` placeholders unfilled.

## Restart confirmation

- Old bot PID: **2552** (seen during A2 prep at 11:27 PST)
- New bot PID: **11328** (observed during A2 capture at ~11:38 PST)
- First post-restart log line attributable to the new process: **2026-06-01 11:32:06** — `"KalshiWebSocket background thread started"` (`logs/bot.log:27824`). This precedes Sunny's stated 11:33 PST restart by ~54 s; the source of the discrepancy is presumed clock-rounding in Sunny's report. T0 is treated as **11:32:06 PST** for the purposes of this report (earliest evidence in the log) with the 20-minute target window extended to T0 + 20 min = **11:52:06 PST**, conservatively read out to 11:54:00 to allow logging flush.

## Capture window

- Log file: `logs/bot.log` (single file; no rotation occurred during capture)
- Window: 2026-06-01 **11:32:06 PST → 11:54:00 PST** (~21 m 54 s)
- Lines emitted by `data.markets.kalshi_ws` in the window: 6, of which:
  - 1 × `KalshiWebSocket background thread started`
  - 1 × `Connecting to Kalshi WS …`
  - 1 × `Kalshi WS connected`
  - 3 × `Kalshi WS error (code=7): Unknown subscription ID` (server-side application error, **not** a close event; emitted by the `type=error` path at `kalshi_ws.py:498–502`, not the close-side handler at lines 351–363)
- **Disconnect lines (`Kalshi WS connection lost …`) in the window: 0**
- Disconnect lines in old format: 0
- Disconnect lines in new format (`code=…`): 0
- Reconnect events (`Connecting to Kalshi WS …` after the initial 11:32:06): 0
- `Kalshi WS rejected connection …` (InvalidStatusCode path): 0

Comparison to A1 baseline: the prior 30-hour window had 13,349 disconnects at a sustained 7.34/min. A1 projection for a 21 m 54 s window: **~161 disconnects**. Observed: **0**.

For the same period the bot continued normal operation: 974 log lines were emitted between 11:33 and 11:53, including market scans, GT fetches, trade lifecycle events, sync_subscriptions activity (the new ack-driven state machine fired — the `WS session subscribed` line from `kalshi_ws.py:553` appeared at 11:32:41, which had **zero hits** in the A1 window across 13,348 reconnects). The connection is live, used, and exchanging frames — it simply has not closed.

## Close metadata results

**No samples.** The instrumented warning at `kalshi_ws.py:360–363` was reached zero times in the capture window because no `OSError` / `ConnectionClosed` / `WebSocketException` was raised. Distributions of `code`, `reason`, `rcvd`, `sent` cannot be computed from N=0.

The only Kalshi-side numeric code captured in the window comes from a different log path: three `Kalshi WS error (code=7): Unknown subscription ID` lines at 11:48:27, 11:50:24, 11:52:23. These are msg.code values inside an application-level `type=error` frame, not WebSocket close codes. They are logged from line 502 (the WS-Fix-A handler), not from the new lines 360–363, and they do **not** trigger a close — the connection survives them. This is not the metadata the phase set out to capture.

## Code interpretation

Not applicable for this window. Reference table preserved for future phases that do capture close codes:

| Close code | RFC 6455 / Kalshi meaning |
|-----------:|---------------------------|
| 1000       | Normal close              |
| 1001       | Going away                |
| 1002       | Protocol error            |
| 1006       | Abnormal close (no close frame received) |
| 1008       | Policy violation          |
| 1011       | Server error              |
| 4xxx       | Application-specific (Kalshi-defined; numeric values seen in `msg.code` of `type=error` frames include 4 "Subscription IDs required" and 7 "Unknown subscription ID" per A1 context) |
| other      | Standard RFC 6455 or unknown |

## Sample size note

- Target: ≥100 disconnects
- Observed: **0 disconnects** in 21 m 54 s
- Shortfall: 100 / 100 (complete shortfall — no samples at all)
- Pre-restart rate (A1): 7.34/min, sustained for >30 h, 13,348 samples
- Post-restart rate (this window): **0/min** over 21 m 54 s

The disappearance of the bug correlates exactly with the restart. Possible causes (listed without ranking — additional evidence required to discriminate):

- Server-side state for the prior client connection was poisoned; new client session avoids whatever triggered the close-loop on the old client.
- Server-side behavior changed coincidentally between 11:27 and 11:32 (Kalshi-side deploy, rate-limit reset, or config rollback).
- The old client accumulated subscription state that intersected with a Kalshi-side bug; fresh subscription set (now 644 → 649 markets) does not.
- The old client had a session-scoped credential issue that fresh handshake resolved.

None of these can be confirmed from the available evidence.

## Verdict

**(d) Inconclusive — the bug did not reproduce during the post-restart capture window. Close metadata could not be sampled because zero close events occurred. The instrumentation is in place and will capture metadata on the first future disconnect, if any.**

What would resolve this:

1. Keep the instrumentation in place across the next 24–48 h of normal bot operation. If the loop re-emerges, the first disconnect produces a populated `code=` / `reason=` line and the underlying timer can be identified.
2. If the bot runs cleanly for 48 h with no disconnects, the WS-Fix-B commit (`87892d4`) is presumptively the fix — the prior loop appears to have been state-machine-driven on the client side (cumulative subscription divergence triggering server-side close), and WS-Fix-B's server-authoritative state model breaks the cycle. The instrumentation should be retained as a regression sentinel; if the loop reappears, the close metadata identifies the cause without another diagnostic round.
3. Out-of-scope tangents observed in the window — three `code=7 Unknown subscription ID` application errors at 11:48–11:52, and `refresh book sources: ws=0 rest=…` showing the scanner is not yet trusting WS books (all reads fall back to REST) — are noted for separate scoping. The code=7 errors do not close the connection but suggest the unsubscribe state machine still has a race; they are precisely the bug class WS-Fix-B targeted, so this is a residual edge case, not a regression. Not investigated further per phase scope.
