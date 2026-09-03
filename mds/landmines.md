# Landmines — Operational Knowledge

Things Claude (web or Code) and the bot should keep in mind when problem-solving. These are not to-do items. They are characteristics of the system, the tool, and the user that affect judgment.

Both `interacting-with-sunny.md` and `prompting-claude-code.md` reference this file.

---

## System landmines

### 1. Stale data is the #1 risk class

Any new ground-truth source or pricing path needs explicit freshness validation. FRED nearly burned the bot on stale CPI returned on a current-period market. Yahoo CL=F is structurally ~604s stale during pit hours (0/240 samples cleared a 300s gate). TTL cache has lost refreshed prices before.

When a new source is being added or a freshness gate is being touched, verify cadence against the source's actual refresh behavior before trusting it. Do not assume documented refresh rates match reality.

### 2. Phase 0b validation numbers are dead

CL=F 80.5% and NQ=F 98% accuracy were measured before the parser fix and before any freshness gate existed. Both measurements are contaminated with phantom data. Do not cite them when reasoning about strategy edge. Re-validation on clean data is required and is on the pending list.

### 3. Ghost P&L is asymmetric on extreme entries, not just optimistic

Earlier framing said ghost P&L was "optimistic on sub-penny entries." The KXTRUFGAS bleed (06-01, ~$212 realized loss across 13 entries on a single ticker) showed the bug is worse than that. The ghost fill size cap against orderbook depth is not implemented. Sub-penny entries on illiquid markets produce fictional fills — bot fills NO at $0.02 phantom ask while real YES ask is $0.67-0.80, and the immediate mark-to-market shows catastrophic adverse capture, triggering instant stop_loss. The bug pattern: extreme entries are systematically negative in ghost because fictional fills produce real mark-to-market adverse capture. Strategies that enter at extreme prices look worse in ghost than they should — sometimes catastrophically worse. Bleed-Fix-1 (perm-skip on consecutive stop_losses per (ticker, signal_source)) catches the bleed pattern at entry #3, but doesn't fix the underlying ghost-fill realism bug.

### 4. Pre-resolution price convergence affects strategy validation

When evaluating a strategy that holds positions past its trigger time, accuracy degrades as market makers reprice. Phase 14a's 88% trigger-time accuracy is the upper bound; 70.5% Kalshi bracket match is the realistic floor. Live ghost data is what actually settles the question, not historical sniff tests.

### 5. Illiquid orderbooks produce bad mids and bad fills

Thin markets have caused premature hard stops. Any strategy expanding to thin tickers needs to plan for this. Invariant checks catch the worst cases at runtime, but strategy logic should not assume Kalshi market prices are always meaningful. They are meaningful when there is a real orderbook, garbage when there is not.

### 6. Process-memory state evaporates on restart

Cooldowns, in-flight counters, perm-skip dicts before SQLite-3a — all in process memory. Restart wipes them. This is the same bug class as the Phase 15b race condition but inverted: there, on-disk state was modified while in-memory was authoritative. Here, in-memory state is destroyed and there's no on-disk equivalent. Both directions of the in-memory ↔ on-disk gap are buggy.

SQLite-3a partially addressed this: perm-skip counters are now hydrated from SQLite on init and write-through persisted. But cooldowns are still in-memory. Any future state that should survive restart belongs in SQLite, not a process-local dict.

### 7. Diagnostic windows must outlive operational events

A2's first 22-minute window captured zero disconnects because the operational event (restart) preceded the diagnostic. The restart *was* the implicit fix. The original 48h WS watch was destroyed entirely by log rotation: 20 MB cap on a bot producing 5 MB in 63 seconds during reconnect storms meant the entire watch window's logs were unrecoverable. Future diagnostics on suspected fixes should include a "kept-running" arm with retention that outlives the window — observe for ≥48h before declaring a bug closed, and verify retention can actually hold the data.

### 8. Tight close-time distributions are server-side timers, not network failures

WS-Diag-A1 found 79.8% of 13,348 connections dying at exactly 7s ± 0.4s. That's not network behavior — that's a timer firing. Network-layer randomness produces distributions with fat tails. When a network-symptom distribution shows up impossibly clean, the cause is upstream of the network. Inverse: when a distribution has fat tails and outliers (the post-Fix-B code=1006 bursts attributable to internet drops), the cause likely is the network.

### 9. "0 hits" findings are evidence, not absence of evidence

WS-Diag-A1 had three useful "0 hits" lines: no `subscribed` acks, no `Re-subscribed after reconnect` events, no `code=` logging. Each absence at high sample size proved a different fact. Diagnostic outputs should include explicit "we expected N times, observed 0" sections — absence at scale is a finding.

### 10. Ack-driven state machines need ack-arrival evidence

WS-Fix-B's premise was that `_subscribed` would mirror server state via acks. The diagnostic showed acks were never arriving because connections died first. The fix code was correct; the premise was unverified. When designing state machines that depend on external events, the verification gate must include: "confirm the dependent events actually fire." A correct algorithm depending on impossible events is functionally broken.

### 11. Pre-fix analysis goes stale fast

The original WS "phantom diff" framing was based on code=7 storm data from a 2h19m window in `bot.log.3` that had self-cleared by `bot.log.2`. We almost scoped a fix for a bug that wasn't actively occurring. Pattern: when a diagnostic flags a phenomenon, verify it continues into the most recent log file, not just the one that surfaced it. The most recent data is the relevant data.

### 12. Log-based and gate-events-based diagnostics see different things

Gate funnel won't show WS reconnects or stuck positions. Logs won't show gate-trip counts. They produce non-overlapping views of bot behavior. Treating either as the only ground truth misses things the other captures. For any new diagnostic window, run both.

### 13. Schema and field inventory cannot be inferred from documentation

SQLite-1 step 1 field discovery revealed at least 6 fields missing from the proposed schema (`order_id`, `category`, `tags`, `fill_status`, `limit_price_used`, `market_id`), and 5 fields proposed that don't exist in the actual JSON. The bot-structure.md doc describes the strategy and pipeline well but doesn't enumerate persisted state schemas. For any future schema work, the actual JSON or DB row is the source of truth, not the documentation. Field discovery before schema design is mandatory, not optional.

### 14. Derived state in JSON looks like persisted state

`ghost_state.json` had `total_usd` and `realized_pnl_usd` only. `reserved_usd` and `available_usd` are computed at runtime from open positions on each load — not persisted. Reading the file naively suggests they're stored. Migration would have introduced bogus columns if not caught. When migrating, "what's stored" and "what the application uses" can differ — only the former goes in the schema.

### 15. Field name drift between docs and code is real

JSON uses `ground_truth_prob` and `source_confidence`. Internal docs and discussion often use `gt_prob` and `confidence`. Same semantic field, different names. For schema work specifically, mirror the actual file keys 1:1 — divergent names make round-trip verification fragile and produce subtle data corruption.

### 16. Refactor-on-top-of-bug failure mode

When a bug is identified and a refactor that touches the same code path is also queued, do the bug diagnostic first. Refactoring on top of an unfixed bug makes the bug harder to isolate and the refactor harder to validate. Specific instance avoided: unified booking path was deferred until buy_no side-correctness diagnostics returned.

### 17. Structural enforcement of human-judgment calls is itself a failure mode

The instinct to enforce everything in config (gate constants as YAML, strategy edge-validation as registration gates) creates problems when applied to decisions that legitimately need human flexibility. Distinguish "bug class with no legitimate exception" (instrument-match assertions — KXBRENTD never legitimately routes to CL=F) from "policy with legitimate exceptions" (strategy ship/no-ship judgments). Use blocks for the first, observability/warnings for the second.

### 18. Q2-Q3 conflict in state migration

"Match existing write cadence" (minimize behavior change) and "logical transactions" (atomicity guarantee) are in tension whenever the existing cadence is batched and the transactional unit is finer. SQLite-3a's resolution: Q3 wins, position-close transactions atomically write ghost_state on every exit, even though JSON wrote ghost_state per-cycle. The divergence is a correctness improvement and should be documented intentionally in commit bodies when it occurs. May recur in future state work.

### 19. Cross-platform signals collapse into a single counter key

Bleed-Fix-1's `_consecutive_stop_losses` key is `(ticker, signal_source)`. Cross-platform signals use the literal string `"cross-platform"` as the signal_source, meaning multiple distinct cross-platform matches on the same ticker collapse into one counter. Currently fine because cross-platform is disabled per memory. If re-enabled, multiple distinct cross-platform pairs stop-loss on the same ticker would perm-skip the ticker for *any* cross-platform signal — which may or may not be correct depending on whether you consider cross-platform a single mechanism.

---

## Tool landmines

### 20. Claude Code hallucinates about its own codebase

Treat "I think this is how X works" claims as unverified until grep, log, or file evidence appears. Documented incidents:

- Phase 2 of ResolutionDetector investigation: grep claimed single writer (was correct, but framed with low confidence)
- Phase 3 of same investigation: invented "second writer" hypothesis to explain a contradiction, with no evidence
- Phase 15a: missed that a settlement-query path already exists for sports markets
- WS-Diag-A1: schema field inventory inferred from docs was wrong on at least 11 fields

Verify-before-trust is the standing mitigation. Pattern-match check: if Claude Code's diagnosis "explains" the observations but the evidence is one log line or one assertion, push back and ask for the actual code or data.

### 21. Audit subagents reason from rejection counts, not P&L

A confidence-threshold recommendation derived from "X% of signals are rejected" is the wrong shape. The right shape is "rejected signals had positive realized EV, so the gate is throwing away money." Apply this check to any threshold recommendation: was it derived from realized outcomes, or from gate-trip counts? If the latter, the recommendation is unsupported.

### 22. Discovery steps before structural changes are mandatory

Pattern established across Bleed-Fix-1 (field name discovery), SQLite-1 (schema field discovery), SQLite-3a (call site discovery): any handoff that touches persisted state, gates, or non-trivial code paths must begin with a discovery step that surfaces the actual structure before the code change is scoped. Resolve "we don't know X" before burying it in a step. Discovery returns evidence; design proceeds from evidence.

---

## User-behavior landmines

### 23. Sunny twiddles gate constants when nothing fires

Documented behavior pattern. During low-signal periods, the temptation is to loosen thresholds to "see more signals" rather than diagnose why nothing is firing. Three gates were loosened on 5/5 (CONFIDENCE_THRESHOLD 0.45→0.30, GT freshness 60→300s, CONFIDENCE_GATE_GHOST_CROSS_PLATFORM 0.50→0.30). All need data-justified re-tightening as part of gate recalibration.

Active counter-pressure required: when a gate-loosening is proposed without realized-P&L data justifying it, push back.

### 24. Sunny gets impatient and skips diagnostics

When a strategy could be shipped now vs. validated for 2-3 weeks, the impulse is "ship now and see live." Sometimes this is right (Phase 14b weather peak snipe). Sometimes it's premature (would have been wrong on Phase 10b gate recalibration).

The check: does ghost data exist to validate first, and will skipping it create a class of failure that's hard to recover from? If yes to first, validate. If yes to second, do not skip.

### 25. Late-night and chemically-energized sessions

Adderall and similar focus aids sharpen execution but do not sharpen judgment. More likely to ship code fast, less likely to catch the "wait, this doesn't add up" moment that the verify-before-trust pattern is supposed to catch. Hold the pattern harder, not softer, during these sessions.

"Just one more thing" — after the second consecutive request after a stated stop point, call out the pattern. But also: Claude's session-time intuition is bad. When Sunny says "I've only been at it an hour," trust him over Claude's sense of how long the conversation feels.

### 26. Stop-point pushback is one-way

When Claude calls a stop point and Sunny pushes back with new information ("I've only been at it an hour"), accept the pushback and proceed without re-litigating. The pattern in `interacting-with-sunny.md` is "mention once, do not lecture" — and that extends to not re-calling the same stop point later in the same session.

---

## Procedural reminders (discipline, not bugs)

- Stale data is the #1 risk. Verify, do not assume.
- One structural change per commit. Bundling unrelated changes makes rollback impossible and hides regressions.
- Verify-before-trust on every Claude Code diagnostic.
- Do not recalibrate gates against starvation windows. Saturday data, post-fix-bot data, and short windows do not justify gate changes.
- Tests-with-the-code-they-test go in the same commit.
- Field/schema/call-site discovery before structural changes — every time.
- Dry-run-then-apply for any operation that mutates persistent state.
- Bot must be DOWN before scripts that touch state files. Race condition class is documented but not eliminated everywhere.
- Match real file structure 1:1 in migrations. Don't add speculative fields.
- Both log-based and gate-events-based diagnostics should run together on any new window.