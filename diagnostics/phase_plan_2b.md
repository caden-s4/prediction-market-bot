# Phase Plan 2B — Decompose the Cycle-Time Fix

Planning-only doc. Read-only. No source modified. No fix proposed at
implementation level.

Reconstructs the intent of the never-landed Phase 2B.4b/2B.4c work
(per reverted commit `ee9a101` and its revert `a173100`), evaluates
whether that intent is still the right approach given the
`phase_d_ws_audit.md` evidence, and decomposes the cycle-time fix
into a sequence of small implementable sub-phases.

Forensic claims cite `file:line` or commit hash. Inferred claims are
tagged `[INFERENCE]` at the start of the sentence.

---

## TL;DR

- The reverted commit `ee9a101` (Phase 2B.4a) tried per-market `seq`
  validation on `_BookEntry`. It was reverted 23 minutes later by
  `a173100` because Kalshi `seq` is a **session-global** counter
  paired with `sid`, not a per-market counter (`a173100` body).
- The revert commit promised a Phase 2B.4b ("session-level seq
  tracking on a clean baseline") and Phase 2B.4c ("rewrites the
  scanner's <30s update-age gate"). **Neither has landed.** Current
  `data/markets/kalshi_ws.py` `_BookEntry` (lines 64–68) has no
  `seq`/`valid` fields, and `resolution/scanner.py:497–519` still
  carries the hardcoded `ws_age < 30.0` gate.
- The sibling Phase D-WS Mechanism A fix (unsubscribe `sids`
  payload + state-authority) DID land in `87892d4` (2026-05-19).
  That work eliminated the 1,080 `code=4` errors / 5.8h but did
  NOT touch ws-share or cycle time.
- The Phase D-WS audit identified TWO independent causes of low
  ws-share (`phase_d_ws_audit.md:330–431`):
  - **Cause 1** (80% of incoming snapshots): empty books on
    arrival (`0 bids, 0 asks`) → `mid_price = None` → REST fallback.
  - **Cause 2** (variable share of remaining snapshots): valid
    non-empty books that haven't received a delta in 30s →
    scanner's <30s gate trips → REST fallback.
- **The reconstructed 2B.4b/2B.4c plan addresses Cause 2 only.**
  Stream-health validity tells the scanner "the stream is up";
  it does not make an empty book non-empty. Treating the prior
  plan as the full fix is incorrect framing.
- Decomposition produces six candidate sub-phases (§4) ordered by
  dependency. The cycle-time fix is genuinely multi-commit work;
  three of the six are mandatory, three are optional / scoped to
  finish the job.

---

## Methodology

Same as the D-WS and D-Pool audits.

- **Forensic** claims (file path + line, commit hash) carry no tag.
- **Inferred** claims (best-guess reasoning from artifacts when the
  artifact is silent or ambiguous) start with `[INFERENCE]`.
- "Reconstructed intent" of 2B.4b/2B.4c is itself almost entirely
  inference — clearly tagged.
- No source file modified; no persistent state modified; bot not run.

---

## Part 1 — Reconstructed intent of 2B.4b / 2B.4c

### 1.1 The reverted commit (Phase 2B.4a)

`ee9a101` (2026-05-05 23:12 PDT) — "kalshi_ws: track snapshot
receipt and seq on _BookEntry".

Added to `_BookEntry`:
- `snapshot_seq: Optional[int]` — seq of the snapshot that
  initialized the book.
- `last_seq: Optional[int]` — seq of the most recent applied delta.
- `valid: bool` — flipped `False` on any seq gap; healed by the
  next valid snapshot.

`_handle_delta` validated `incoming_seq == entry.last_seq + 1` per
market. On mismatch: invalidate the book, drop the delta, log a
WARNING. `get_book` returned `None` for invalid entries.

Stated motivation in the commit body, quoted verbatim:

> *"Foundation for Phase 2B.4c, which will replace scanner's <30s
> update-age gate with a stream-health validity check. The current
> gate fails for illiquid T2 markets that have valid books but no
> recent ticks: T2 cycle shows 0/150 WS hits despite 1135 markets
> subscribed and snapshot-confirmed because most of them haven't
> received a delta in the last 30 seconds. Phase 2B.4b adds
> connection health tracking. Phase 2B.4c wires both into the
> scanner's WS read path."*

### 1.2 The revert (`a173100`)

`a173100` (2026-05-05 23:35 PDT, 23 min after `ee9a101`) — "kalshi_ws:
revert per-market seq tracking from Phase 2B.4a".

Stated reason, verbatim:

> *"The design was wrong by spec: Kalshi's seq is a session-global
> counter paired with sid, not per-market. With 1255 markets
> subscribed, adjacent deltas for one market are separated by
> thousands of seq numbers (other markets' deltas in between), so
> every second delta looked like a gap and books were immediately
> invalidated."*

And the re-scoped promise:

> *"Phase 2B.4b adds session-level seq tracking on a clean baseline.
> Phase 2B.4c rewrites the scanner's <30s update-age gate."*

### 1.3 What 2B.4b was supposed to be

[INFERENCE] After the per-market design failed, 2B.4b was re-scoped
from a generic "connection health tracking" capability to the
specific prerequisite of session-level seq tracking. The two
framings refer to the same goal: produce a stream-health validity
signal that is independent of any single market's tick rate.

[INFERENCE] Implementation shape:
- Single session-global `_session_seq: Optional[int]` on
  `KalshiWebSocket`, paired with a session-global `_session_sid`
  or set of active sids.
- Every incoming delta carries a `seq`. Validate `seq ==
  _session_seq + 1`. On mismatch: log a gap, optionally trigger
  a forced reconnect+resubscribe (which heals via the existing
  `_resubscribe_all` path), bump some `last_session_gap_time`
  counter.
- Expose `is_stream_healthy()` (or `seconds_since_last_session_gap`,
  or `last_seen_session_seq`) so the scanner can ask the WS client
  "is your inbound stream currently healthy?" without coupling to
  any single market's last update timestamp.

[INFERENCE] The "[stream-health validity check]" the revert commit
references is a single binary (or graded) check per cycle, not a
per-market predicate. Phase 2B.4b is the prerequisite that makes
this signal extractable.

### 1.4 What 2B.4c was supposed to be

[INFERENCE] The scanner WS read-path rewrite at `resolution/scanner.py:497–519`:

Current code:
```python
ws_book = None
if self._kalshi_ws is not None and market.platform == "kalshi":
    ws_age = self._kalshi_ws.get_book_age(market.market_id)
    if ws_age is not None and ws_age < 30.0:
        ws_book = self._kalshi_ws.get_book(market.market_id)

if ws_book is not None and ws_book.mid_price is not None:
    # ... use WS price
else:
    # ... REST fallback
```

Reconstructed proposed shape:
```python
ws_book = None
if (self._kalshi_ws is not None
        and market.platform == "kalshi"
        and self._kalshi_ws.is_stream_healthy()):
    ws_book = self._kalshi_ws.get_book(market.market_id)

if ws_book is not None and ws_book.mid_price is not None:
    # ... use WS price
else:
    # ... REST fallback
```

[INFERENCE] The per-market `ws_age` is dropped entirely (the stream
is healthy → trust the snapshot). The `mid_price is not None`
predicate still filters empty books (Cause 1 unaffected).

### 1.5 What 2B.4b/2B.4c did NOT promise

- Did not promise an empty-book filter or empty-snapshot handler.
- Did not promise per-tier freshness thresholds.
- Did not promise reducing subscription footprint.
- Did not promise scanner-side fallback instrumentation.

The revert author's framing was narrow: replace one freshness
heuristic with another. That framing reads as **partially obsolete
in light of the D-WS audit** — see §2.

### 1.6 Recoverability of intent

Honestly:
- Implementation **shape** is reasonably recoverable (above).
- Implementation **details** are not in the artifact. Whether to
  force-reconnect on session-seq gap vs. soft-mark-unhealthy, what
  the unhealthy-window decay looks like, whether `is_stream_healthy()`
  should be per-channel — none of that is in any commit, comment,
  or docstring. Those are open design questions for 2B.4b proper.

---

## Part 2 — Evaluate the prior plan against current evidence

### 2.1 Component map: reconstructed plan vs. current code

| Reconstructed component | Current state | Source |
|---|---|---|
| Per-market seq tracking on `_BookEntry` | Reverted, not present | `data/markets/kalshi_ws.py:64–68` (no seq fields); `a173100` |
| Session-level seq tracking | Not landed | `grep -n "session" data/markets/kalshi_ws.py` → no matches relevant to seq |
| Stream-health accessor on `KalshiWebSocket` | Not present | No `is_stream_healthy` / `last_session_seq` method exists in current `kalshi_ws.py` |
| Scanner read-path uses stream-health | Not landed | `resolution/scanner.py:497–519` still has `ws_age < 30.0` hardcoded |
| Unsubscribe-format / sid-tracking / state authority | Landed | `87892d4` (2026-05-19) — separate work, audit §A |
| Empty-book filtering at receive | Not present | `data/markets/kalshi_ws.py:657–658` caches `_BookEntry` even when `yes_bids=[], yes_asks=[]` |
| Per-tier or per-liquidity freshness gates | Not present | `resolution/scanner.py:501` is a single hardcoded threshold |
| Subscription footprint shrink | Not done | `executor.py:884` syncs all T1+T2 markets unconditionally (~1135–1255 markets per audit) |

### 2.2 Does the reconstructed plan solve the audit's actual problem?

The audit's TL;DR identifies two compounding causes of low ws-share
(`phase_d_ws_audit.md:32–48` and §5):

**Cause 1 — Empty snapshots:** 2,151 of 2,691 snapshots in the 5.8h
log window arrive with `0 bids, 0 asks` (80%). `_handle_snapshot`
caches them as a `_BookEntry`; `OrderBook.mid_price` returns `None`;
scanner falls back to REST. These are mostly KXMVECROSSCATEGORY-*,
KXHYPE15M-*, and similar low-liquidity series.

**Cause 2 — Stale-tick gate:** Non-empty books that received an
initial snapshot but no delta in 30s trip the
`scanner.py:501` gate. The reverted commit's own diagnosis:
*"T2 cycle shows 0/150 WS hits despite 1135 markets subscribed and
snapshot-confirmed because most of them haven't received a delta in
the last 30 seconds."*

The reconstructed 2B.4b/2B.4c plan:
- **Addresses Cause 2 directly.** Replacing the per-market `<30s`
  gate with a stream-health check means a healthy stream + valid
  non-empty snapshot is enough to trust the book.
- **Does NOT address Cause 1.** Empty snapshots will still cache
  empty `_BookEntry`s; `mid_price` will still be `None`; the scanner
  will still fall back to REST. Stream-health doesn't change the
  predicate that empty books fail.

[INFERENCE] If the reconstructed plan landed today as-is, ws-share
would improve for illiquid-but-non-empty T2 markets (NBA/NCAAB
in-game tickers that didn't tick in the last 30s, plus financial
brackets between price-moves) but would NOT improve for the 80% of
snapshots that arrive empty. The dominant REST-fallback share would
shrink only partially.

### 2.3 What was wrong with the prior framing

- It treated the cycle-time problem as a single freshness-gate
  problem. The audit shows it is two independent problems.
- It implicitly trusted the WS subscription footprint of 1135–1255
  markets as correct. The Cause 1 evidence suggests the footprint is
  too wide: subscribing to series that consistently emit empty
  snapshots wastes bandwidth and produces zero usable price data
  for the gate to evaluate.
- It did not propose any instrumentation. Landing 2B.4b/2B.4c
  blind, without per-fallback-reason logging, would make it hard to
  verify the change actually helped.

### 2.4 What is still useful about the prior plan

- Session-level seq tracking is **independently valuable** as a
  general WS-health signal, irrespective of the scanner rewrite. A
  session-seq gap signals real protocol-level loss that no other
  current diagnostic catches. (Current code reads `seq` only in DEBUG
  log lines on ack acks — `data/markets/kalshi_ws.py:560–562`.)
- The framing "stream-health is the right validity signal, not
  per-market tick age" is correct as far as it goes. The flaw is in
  treating it as sufficient on its own.

---

## Part 3 — Alternative approaches

Each presented with pros, cons, and disambiguating evidence. **No
verdict.** Sunny picks.

### 3.1 Approach α — Implement the reconstructed plan

What it is: land 2B.4b (session-seq + stream-health accessor) and
2B.4c (scanner rewrite to use stream-health) more-or-less as the
revert commit author envisioned.

Pros:
- Addresses Cause 2 with a real signal instead of a heuristic.
- The session-seq capability is independently useful for diagnosing
  protocol-level loss.
- Tracks the original author's intent — lowest cognitive cost to pick
  up.

Cons:
- Leaves Cause 1 (80% empty snapshots) untouched. Cycle-time wins
  will be partial.
- "Stream-health" is itself an undefined predicate. [INFERENCE] How
  long after a gap is the stream "unhealthy"? Does any gap count, or
  only sustained gaps? Needs design before it can be implemented.
- Adds a session-seq invariant that may itself have subtle bugs (the
  per-market version got it wrong on the first attempt).

Right choice if: the empty-snapshot problem turns out to be a
fixed-size noise floor, not a dominant contributor to cycle time.
Evidence required: per-fallback-reason instrumentation showing that
empty-book fallbacks are bounded and rare relative to stale-tick
fallbacks (current evidence suggests the opposite, but instrumentation
would settle it).

### 3.2 Approach β — Solve the empty-book half first

What it is: handle Cause 1 explicitly. Two sub-options:
- (β1) Filter at `_handle_snapshot` — if both `yes_bids` and
  `yes_asks` are empty, don't cache a `_BookEntry`. Scanner sees
  `get_book_age() is None` and routes via the existing REST
  fallback **or** is taught to skip the market entirely.
- (β2) Filter at scanner read — distinguish "no entry / stale /
  empty book" and short-circuit empty-book markets without falling
  through to REST.

Pros:
- Targets the **dominant** contributor (80% of snapshots).
- Cheap and isolated: one filter or one branch.
- Independent of seq tracking — can ship alone, doesn't block α.

Cons:
- Doesn't fix the stale-tick half (Cause 2). ws-share won't
  improve for illiquid-but-non-empty T2 markets.
- (β1) hides empty books from observability — might mask a real
  problem (a market that *used* to have a book and went empty is
  worth knowing about).
- (β2) keeps the REST fallback path alive but shorts it for empty
  books — needs careful predicate so we don't miss markets whose
  book is briefly empty during a snapshot/delta race.

Right choice if: the empty-book series are reliably identifiable
(e.g. by series prefix) and skipping them at the subscription level
is acceptable. Evidence required: a 24h sample showing which series
emit empty snapshots, whether any of them ever fill, and whether the
bot trades any of them today.

### 3.3 Approach γ — Punt on WS, optimize REST

What it is: declare WS too unreliable for the current market mix.
Accept REST as the primary path. Optimize REST batching (one
endpoint call per cycle that returns N orderbooks, instead of N
calls). Subscribe to WS only for the markets that benefit
(sports in-game, FRED release windows).

Pros:
- Eliminates the entire problem class. Empty books and stale ticks
  don't matter if WS isn't on the critical path.
- Aligns with the audit's observation that WS subscription state is
  expensive (1255 markets, audit §4.3) for a small ws-share return.
- REST batching is a well-understood optimization with bounded scope.

Cons:
- Kalshi REST has an 8 req/s rate limit (`CLAUDE.md: API Rate
  Limits`). A batched orderbook endpoint may not exist (open question
  §5.2). Without one, this approach degrades to "fewer markets,"
  which is approach δ.
- Throws away the WS work already done (`87892d4`, `e746ff0`,
  etc.). Reversible but wasteful.
- Sports live-source needs sub-second update freshness during
  final-period shock detection. REST cadence at 8 req/s plus bulk
  page latency cannot match WS for in-game ticks. Sports use case
  forces WS to stay alive for *some* subset of markets.

Right choice if: Kalshi has a usable bulk orderbook endpoint, and
the bot's signal-freshness needs are dominated by non-sports paths.
Evidence required: read the Kalshi REST API for a bulk endpoint;
measure sports cycle-latency budget in a final-period game window.

### 3.4 Approach δ — Reduce the subscription footprint

What it is: only subscribe to markets that demonstrably tick or
matter (e.g. only T1, plus sports in-game from a dedicated path).
Drop T2 from the WS subscribe set. Let T2 refresh via REST or via a
parallel bulk orderbook path.

Pros:
- Both halves of the problem (empty snapshots + stale ticks) shrink
  with the footprint. The 80% empty-snapshot share is mostly T2
  and low-liquidity series.
- Smaller subscribe set → ws-share denominator drops → ws-share
  percentage rises naturally.
- Less server-side state churn from sync_subscriptions diffs.

Cons:
- T2 was added to the WS sync for a reason (commit `06cd7d6`
  "executor: sync Kalshi WS subscriptions to T1+T2 each cycle").
  [INFERENCE] The reason was probably to have warm books for T2
  markets that might be sniped or might generate cross-platform
  signals. Dropping T2 from WS may regress that path.
- Need a story for T2 freshness: bulk REST cadence isn't sub-30s
  for 100+ markets at 8 req/s. May require accepting higher latency
  on T2 signals.

Right choice if: T2 signals can tolerate REST latency (most
financial brackets do; sports T2 means "second-to-resolve quarter,"
which is rarely the active path). Evidence required: realized signal
distribution by tier — which tier produces actionable signals today.

### 3.5 Approach ε — Per-tier / per-liquidity freshness thresholds

What it is: replace the single `< 30.0` constant with a per-tier
table: T1 demands `< 5s` ws-age, T2 tolerates `< 60s` or `< 120s`,
T3 tolerates `< 5min` or skips WS. Combine with stream-health from
α if desired.

Pros:
- Aligns freshness gate with the actual signal-freshness need per
  tier. Most T2 markets don't need sub-30s freshness.
- Compatible with α (use per-tier thresholds inside the stream-health
  branch); also compatible with β (filter empty books, then check
  per-tier age).
- Small, isolated change at the scanner read site.

Cons:
- Still a heuristic — the audit's framing is that any per-market age
  heuristic is the wrong shape. Per-tier is a softer version of the
  same flaw.
- Introduces a new constant table that will drift over time.

Right choice if: stream-health turns out to be too coarse a signal
on its own (false-trusts during partial degradation) and we need a
per-market backstop. Evidence required: production data after α to
see whether stream-health alone is correlated with actually-fresh
prices.

### 3.6 Approach ζ — Instrumentation first, decide later

What it is: before any structural fix, instrument the scanner WS
read site to log per-fallback the reason: `no_entry`, `stale_age`,
`empty_book`, `ws_disabled`. Run a 24–48h sample. Decide which
approach to invest in based on the relative weights.

Pros:
- Removes the entire "is this even the right fix?" question. Cheap.
- Belt-and-suspenders: also useful after any structural fix lands
  (to verify the fix worked).
- Already surfaced as a useful follow-up in the D-WS audit (§7.6).

Cons:
- Adds one cycle of "diagnose first, fix later." Sunny's
  pattern (landmines #9: impatient/skip-diagnostics) makes this the
  step likely to get skipped.
- Doesn't itself fix anything.

Right choice if: there's any doubt about which cause dominates. The
audit gives a high-confidence answer (80% empty snapshots), but
real-time per-fallback counts would settle it.

---

## Part 4 — Define the goal precisely

### 4.1 Translating Sunny's goal (iii) — "fix it enough that going live is safe"

Live-safety derives from three signal-freshness needs:

**Need A — Sports final-period shock detection.** Per `sports.md`,
the shock-detector tiers fire at confidence 0.92 with `<120s
remaining` and 0.85 with `<300s remaining`. Signal-to-trade latency
must be well under the remaining-time budget. [INFERENCE] A `<30s`
cycle is the safe bar for the 0.85 tier; `<15s` for the 0.92 tier.

**Need B — FRED release window.** Per `data/release_calendar.py`, the
post-release window is ~15 minutes. Cycle time can be much looser
here — `<60s` is generous. The freshness gate is dominated by FRED's
own publish cadence, not the bot's read cadence.

**Need C — Financial bracket signals.** Per `data/ground_truth/financial.py`
TTL of 60s on Yahoo quotes (audit `strategy_audit.md:46`), the
underlying-price input is itself stale by up to 60s. Cycle time
matching this (~30–60s) is sufficient.

**Acceptance criteria for goal (iii):**

- (iii-1) **Sports cycle p95 < 30s** during any final-period
  in-game window. Hard requirement for live trading on shock signals.
- (iii-2) **General cycle p95 < 60s** during steady-state operation
  (no specific event window).
- (iii-3) **No ws-rest disagreement WARNINGs** that aren't already
  explainable by Yahoo-vs-Kalshi spread. (Tracked at
  `resolution/scanner.py:505` via `_check_ws_rest_agreement` —
  current state mostly silent per audit.)

These are measurable from existing log lines: `cycle complete in
%s` lines plus the existing `ws=%d rest=%d total=%d` line at
`resolution/scanner.py:535–537`.

### 4.2 Translating Sunny's goal (iv) — "make the diagnostic noise stop"

The noise is two log shapes:

**Noise A — `Received orderbook_snapshot for X (0 bids, 0 asks)`.**
2,151 in the 5.8h audit window (`phase_d_ws_audit.md:377–382`).
[INFERENCE] These are INFO-level; not strictly noise unless you're
looking. But they obscure the legitimate snapshots in the log
stream.

**Noise B — `ResolutionScanner: refresh book sources: ws=%d
rest=%d total=%d` with consistently 0/N ws on T1.** Not an error,
but the signal-to-noise on this line is low when ws is
structurally 0.

**Acceptance criteria for goal (iv):**

- (iv-1) **Empty-book snapshot INFO lines < 10% of total snapshot
  INFO lines** in a 24h sample. (From 80% baseline.) Most realistic
  via either an empty-book filter at receive (approach β1) or
  reduced subscription footprint (approach δ).
- (iv-2) **`ws=0 rest=N` lines < 20% of T1 refresh log lines** in
  a 24h sample. (From current ~100% on T1 per audit table at
  `phase_d_ws_audit.md:417–423`.) Requires that *some* T1 markets
  produce usable WS books; today they mostly don't because T1 = imminent-resolution = often
  illiquid or pre-game.
- (iv-3) **No new noise classes introduced.** Any session-seq gap
  log should be WARN-level (real signal) and fire at < 1/cycle in
  steady state.

---

## Part 5 — Decomposition into shippable sub-phases

Each sub-phase below is one focused commit. Each either improves
the situation on its own or is a prerequisite that unblocks the
next piece. Ordered by dependency.

### 5.1 Phase 2B-1 — Per-fallback-reason instrumentation

**Scope.** At `resolution/scanner.py:497–519`, count and log
per-cycle the reason each market took the REST fallback path:
`no_ws_entry` / `stale_age` / `empty_book` / `disabled`. Add to the
existing `ws=%d rest=%d total=%d` log line.

**Acceptance.** A single cycle's log shows the breakdown. After 24h
of runtime, the relative weights of the four reasons can be read
from `logs/bot.log` without grepping individual markets.

**Dependencies.** None.

**Size.** Small (one function, <50 LOC).

**Does NOT.** Does not change any behavior. Pure observability.

**Why first.** The D-WS audit's 80%-empty-snapshot number is a
counter on incoming WS data, not on scanner-side fallbacks. Without
this instrumentation, no sub-phase below can prove it actually
moved the needle.

---

### 5.2 Phase 2B-2 — Empty-snapshot fast-skip at receive

**Scope.** At `data/markets/kalshi_ws.py:657–658`, decide on receive
whether to cache an empty-book snapshot. Two viable shapes (pick one
in the handoff):
- (β1a) Don't cache. `_handle_snapshot` returns early when both
  `yes_bids` and `yes_asks` are empty. Scanner's `get_book_age`
  then returns `None` for those markets, naturally routing through
  REST fallback.
- (β1b) Cache with an `is_empty` flag on `_BookEntry`. Scanner
  reads the flag and short-circuits BOTH the WS price path AND the
  REST fallback (skip the market entirely for the cycle).

**Acceptance.** Empty-book snapshot INFO lines drop below 10% of
total snapshots in a 24h sample (criterion iv-1). For shape β1b,
the per-fallback-reason instrumentation (2B-1) shows `empty_book`
count near zero.

**Dependencies.** Phase 2B-1 (to measure the effect).

**Size.** Small (one function, <30 LOC).

**Does NOT.** Does not change subscription set. Does not address
stale-tick books. Does not introduce session-seq.

---

### 5.3 Phase 2B-3 — Identify and exclude chronically-empty series from WS subscribe set

**Scope.** Using a 24–48h sample of empty-snapshot data (collected
via 2B-1 and/or 2B-2 logging), identify series prefixes that emit
empty books in ≥95% of samples. Add them to a `WS_EMPTY_SERIES`
exclusion list applied in `subscribe()` or at `executor.py:884`
before `sync_subscriptions`.

**Acceptance.** Subscription footprint drops to a number that matches
the count of series the bot actually wants WS prices for (sports +
financial brackets + macro events). Empty-snapshot INFO lines
approach zero (much stronger than 2B-2's < 10%).

**Dependencies.** Phase 2B-1 (to identify the series). Phase 2B-2
(no hard dependency; the two work together — 2B-2 is the runtime
filter, 2B-3 is the upstream filter).

**Size.** Small to medium (config change + sample analysis).

**Does NOT.** Does not change the scanner read path. Does not change
the freshness gate.

---

### 5.4 Phase 2B-4 — Session-level seq tracking + `is_stream_healthy()` accessor (2B.4b proper)

**Scope.** Add to `KalshiWebSocket`:
- A single `_session_seq: Optional[int]` validated on every incoming
  delta. [INFERENCE] May need to be tracked per-`sid` if a session
  carries multiple channel subscriptions with independent counters
  — confirm shape against a 5-minute production WS dump before
  implementing.
- On `seq != _session_seq + 1`: log WARN, bump a
  `_last_session_gap` timestamp; optionally trigger a forced
  reconnect via the existing `_ws_loop` backoff path.
- Public `is_stream_healthy() -> bool` (or equivalent
  `seconds_since_session_gap() -> float`). [INFERENCE] Definition
  TBD in the handoff — likely `_last_session_gap is None or
  (now - _last_session_gap) > 30s`.

**Acceptance.** Tests cover (a) clean delta sequences validate, (b)
single-gap sets `is_stream_healthy() == False` briefly, (c) recovery
heals on the next valid snapshot+delta. Production WARN lines fire
<1/hour in steady state. No scanner behavior changes yet.

**Dependencies.** None on prior 2B sub-phases. Parallel-safe with
2B-1, 2B-2, 2B-3 (different file, no overlap).

**Size.** Medium. The 2B.4a attempt was ~35 LOC but the wrong shape.
The correct session-level version is similar size; the work is in
specifying the predicate (open question §6.1) and testing the gap
detection.

**Does NOT.** Does not change scanner read path. Does not change
freshness gate. Does not change any `_BookEntry` field (the failed
2B.4a path).

---

### 5.5 Phase 2B-5 — Scanner read path uses stream-health (2B.4c proper)

**Scope.** Rewrite `resolution/scanner.py:497–519` to use the new
`is_stream_healthy()` predicate instead of `ws_age < 30.0`.
Predicate shape TBD by 2B-4. Empty-book and missing-entry cases
still route to REST fallback (or, if 2B-2/2B-3 landed, are filtered
upstream).

**Acceptance.** ws-share on T2 in-game sports markets rises from
0–46/150 baseline to >50% during in-game windows. General-cycle
p95 drops to < 60s (criterion iii-2). Sports-cycle p95 < 30s during
final-period games (criterion iii-1) — verifiable by sampling
`cycle complete` lines during the next NBA/NCAAB in-game window.

**Dependencies.** Phase 2B-4 (provides the predicate). [INFERENCE]
Phase 2B-2 strongly recommended — without it, empty books still
dominate the fallback regardless of stream-health.

**Size.** Small (one branch in scanner, plus removal of the
hardcoded constant).

**Does NOT.** Does not introduce per-tier thresholds. Does not
change the REST fallback path itself.

---

### 5.6 Phase 2B-6 (optional) — Per-tier freshness floors

**Scope.** Within the new stream-health-gated branch, add a
per-tier minimum freshness floor: T1 must have `ws_age < 5s`, T2
< 60s, T3 < 5min (or skip WS entirely). Pulls from the new tier
registry classification (`resolution/tier_registry.py`).

**Acceptance.** ws-rest disagreement WARNINGs (`scanner.py:505`)
remain at baseline rate or drop. No regression in cycle time.
Sports cycle stays <30s.

**Dependencies.** Phase 2B-5.

**Size.** Small to medium.

**Does NOT.** Does not change any other path.

**Optionality.** Only worth doing if production data after 2B-5
shows stream-health is too coarse a signal on its own (e.g.
ws-rest disagreement WARNINGs rise after 2B-5). If 2B-5 is clean,
skip this.

---

### 5.7 Parallelism and ordering

```
  2B-1 ─┬─> 2B-2 ─> 2B-3
        └─> 2B-5 (with 2B-4)         (2B-6 optional follow-on)
  2B-4 ─────^
```

- **2B-1 first.** Cheap, unblocks measurement on everything else.
- **2B-2 and 2B-4 can run in parallel** (different files: scanner
  read site vs WS receive; though 2B-2 may also touch
  `_handle_snapshot` in `kalshi_ws.py` — coordinate the merge).
  [INFERENCE] Practically, do 2B-2 first because it's smaller and
  the wins are larger; 2B-4 needs design work first.
- **2B-3 after 2B-1.** Needs the sample data 2B-1 produces.
- **2B-5 requires 2B-4.** Hard dependency.
- **2B-6 is optional and last.**

### 5.8 No sub-phase requires bundling

Every sub-phase above is implementable as one commit with one
structural change. No "must-land-together" cases identified. The
prior `ee9a101` revert was exactly the failure mode of bundling
session-seq with scanner-side consumption in a single commit; the
decomposition above splits the prerequisite (2B-4) from the
consumer (2B-5).

---

## Part 6 — Risks and open questions

### 6.1 Landmines that apply

- **#1 stale-data risk** (`mds/landmines.md:11–15`). Applies to
  Phase 2B-5: any relaxation of the freshness gate must validate
  that the stream-health proxy actually correlates with book
  correctness for illiquid markets. Mitigation: keep the
  `_check_ws_rest_agreement` callout at `scanner.py:505` enabled
  during the rollout window; treat WARNINGs as a regression signal.
- **#5 illiquid orderbooks** (`mds/landmines.md:29–31`). Applies
  to Phase 2B-2: an empty book may transition to non-empty and back
  during a snapshot/delta race; the "empty" filter must distinguish
  "structurally empty" from "transiently between deltas." The most
  conservative shape is: only filter at `_handle_snapshot` (not at
  delta), so a delta arriving on an empty book still gets
  processed.
- **#6 Claude-Code hallucinates** (`mds/landmines.md:37–45`). This
  doc reconstructs intent from a reverted commit body. Most of the
  reconstructed implementation shape is `[INFERENCE]`. Before any
  sub-phase ships, Sunny should validate the reconstructed predicate
  shape against the Kalshi WS v2 docs and against a real production
  WS dump.

### 6.2 Open questions for Sunny

1. **Session-seq shape** — Is Kalshi's session-global `seq` paired
   with `sid` per-subscription, or is it truly a single counter
   across all subscriptions on the same connection? `a173100` body
   says "session-global counter paired with sid" — read literally,
   each sid has its own counter. If so, 2B-4 needs a `Dict[sid,
   int]`, not a single `_session_seq`. **Recommended: capture a 5-minute
   raw WS dump and read the seq pattern before designing 2B-4.**
2. **Empty-book filter shape** — β1a (don't cache) vs β1b
   (cache-with-flag). β1a is simpler; β1b is more observable.
   Sunny's call.
3. **Subscription footprint** — Is the bot willing to subscribe
   to fewer markets if it means higher ws-share? `06cd7d6` added
   T1+T2 sync; would dropping T2 from WS regress an actively-used
   signal path?
4. **Per-tier thresholds (2B-6)** — Worth the complexity, or skip
   unless data demands it?
5. **Stream-health predicate definition** — How long after a seq
   gap is the stream "unhealthy"? Hard window (30s)? Decaying
   confidence? Soft mark with `last_gap_seconds_ago` exposed and
   scanner decides? [INFERENCE] Hard window is simplest; decaying
   confidence is more correct but adds tunables that drift.
6. **Cycle-time target for live trading** — `<30s` sports cycle is
   the reconstructed bar from the sports.md shock tiers. Is that
   right, or does Sunny want tighter (e.g. `<15s` for the 0.92
   tier)?
7. **Scope of "diagnostic noise stop"** — Is the goal silencing
   the `0 bids, 0 asks` INFO lines, or fixing the underlying
   subscription mis-targeting? Both achieve "noise stops" but
   imply different sub-phase priorities (2B-2 alone vs 2B-2 + 2B-3).

### 6.3 What this plan deliberately does not commit to

- A specific approach (α/β/γ/δ/ε/ζ). The decomposition supports
  multiple combinations; the right combination depends on the open
  questions above.
- The exact shape of the session-seq predicate. Design open until
  the Kalshi WS dump answers question 6.2.1.
- A specific cycle-time target. Reconstructed from sports.md and
  release_calendar.py; Sunny can tighten or loosen.

---

## Verification

```
$ test -f diagnostics/phase_plan_2b.md && echo present
present

$ git status --short data/ resolution/ bot.py main.py
(no source modifications)

$ git status --short diagnostics/phase_plan_2b.md
?? diagnostics/phase_plan_2b.md
```

No source modified. No persistent state modified. No fix proposed
at implementation level. Every forensic claim cites file:line or
commit hash. Every inferred claim starts with `[INFERENCE]`. Six
candidate sub-phases produced, each with a scoping stub
(name/scope/acceptance/dependencies/size/does-not). Open questions
explicitly listed.
