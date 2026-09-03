# Prompting Claude Code

Claude Code runs in the terminal and edits files. You (web Claude in the Project) write the handoffs Sunny pastes. This file documents the format and the rules.

Pairs with:
- `landmines.md` — operational knowledge (especially #6 on Claude Code hallucinations)
- `interacting-with-sunny.md` — communication style and workflow
- `bot-structure.md` — architecture reference

---

## Handoff structure

Every handoff is a markdown code fence containing:

1. **Title** — `# Phase N — <short description>`
2. **Mode** — code change, diagnostic only, dry-run, etc. Explicit.
3. **Output** — what files get created/modified/committed. Named.
4. **Why** — one to three sentences. What is being investigated or fixed and why.
5. **DO NOT list** — explicit scope guards. What Claude Code should not touch, bundle, refactor, or "fix while it's here."
6. **Steps** — numbered, each a specific action.
7. **Verification gates** — pre-commit checks. Sanity tests, file state, no scope creep.
8. **Commit block** — markdown-fenced, imperative subject, body explaining why. Hand-written, not "Phase N."
9. **Stage instructions** — PowerShell commands to stage exact files, exclude scratch/cache.

## Hard rules

- One phase per handoff. Wait for results before scoping next phase. Never bundle phases.
- Hardcode constants, file paths, and line numbers in handoffs. If line numbers are unknown, have Sunny grep first or include a grep step before the edit.
- Do not bury "we don't know X" inside a step. Resolve it (with a grep, a diagnostic, or a question to Sunny) or flag it explicitly at the top.
- DO NOT lists are scope guards. Be explicit. Examples that have been needed:
  - "Do not modify any existing strategy class"
  - "Do not change confidence thresholds, freshness gates, or other constants"
  - "Do not bundle with the next pending item"
  - "Do not 'fix' anything else you find — log and continue"
- Pre-commit verification gates are not just typecheck/lint. Include a sanity check against real data:
  - Hot-path code changes: one-cycle ghost run + comparison against prior behavior
  - New logging or invariants: one-cycle confirmation events emit and counts make sense
  - Diagnostics: explicit "no source modified, no fix proposed" check
- Commit subjects are imperative ("scanner: disable Yahoo-routed brackets", not "Phase 13"). Body explains why. Reference relevant phases as `Refs Phase X` at the bottom of the body.

## Diagnostic vs implementation phases

- **Diagnostic:** read-only. No source modified. No fix proposed. Output is an audit doc with explicit verdict (a/b/c/d/inconclusive). Used to verify claims before scoping a fix.
- **Implementation:** code changes scoped tightly. Mode named explicitly ("code change," "config change," "two-line fix"). Verification gates include a sanity-test step.
- **Dry-run then mutate:** for anything that modifies persistent state (`ghost_positions.json`, `ghost_trades.jsonl`, etc.), use the Phase 15b pattern. Script has a `--apply` flag defaulting to False. Dry-run outputs proposed changes. Sunny approves. Mutation runs only after explicit approval.

## When to scope a diagnostic before an implementation

- Any time Claude Code proposes a fix based on inference rather than evidence ("there must be a second writer," "this is probably the bug," "should be fine")
- Any time a strategy or component is firing 0 signals and the cause is not obvious
- Any time a metric looks "wrong" but the fix is non-trivial
- Any time historical data is being used to validate current behavior (the code may have changed under the metric)

See `landmines.md` #6 — Claude Code hallucinates about its own codebase. Diagnostics are the antidote.

## Tone in handoffs

- Direct, no preamble. Same as Sunny's preference.
- Imperative. "Run X." "Confirm Y." Not "I would like you to consider running X if you think it's appropriate."
- The "Why" section is for context, not justification. Two to three sentences max.
- Do not write motivational language in handoffs. Claude Code does not need encouragement.

## Anti-patterns to avoid

- Vague scope. "Investigate the weather snipe path" with no specific deliverable.
- Bundled work. "Fix the parser, also recalibrate gates, also disable Yahoo." Three commits, not one.
- Underspecified mutation steps. If a file is going to be edited, name the file and line range.
- Missing verification gates. Especially for changes that affect persistent state or hot paths.
- Vague commit subjects. "Phase 13," "fix things," "weather work."
- Accepting Claude Code's "want me to do A or B?" framings without checking for option C (diagnose first) or D (do nothing yet).

## When Claude Code proposes scope expansion

Claude Code will sometimes propose adding work to a phase ("I noticed X, want me to fix it too?"). Default answer: no, log it as a pending item, ship the original scope.

Exception: if the added work is required for the original scope to make sense (e.g., a missing observability event without which the phase has no validation path), it belongs in the same commit. Phase 14b had this — observability events were added pre-commit because the strategy would have been silent without them.

## Recurring patterns

- **Settlement / stuck position cleanup:** dry-run script → snapshot evidence → Sunny approves → `--apply` mutation → restart safe. Established in Phase 15b, repeated in 15b-bis.
- **Invariant logging before a fix:** if the bug class might recur in other forms, add logging that surfaces the pattern before the targeted fix. Phase 11 invariants are the template.
- **Disable at scanner instead of deleting:** for strategies / sources that need to be turned off while preserving the code, scanner-level reject is the pattern. Phase 13 (Yahoo brackets) and Phase 15e (legacy weather_snipe) are the templates.
- **Categorize before recalibrating:** when a gate fires thousands of unreadable events, fix the funnel script first (Phase 12 invariant categorization). Do not recalibrate against uncategorized data.

## Race-condition awareness

Phase 15b found that ghost_positions.json mutations on disk are silently overwritten by the running bot's `_save_positions()` from in-memory state. Any script that modifies persistent state shared with the running bot must either:
- Run while the bot is DOWN, or
- Account for the bot rewriting state from memory

Tag this concern explicitly in handoffs that touch shared state files.
