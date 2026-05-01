# Universal Rules

## Coding discipline
- Phased tasking only. Write Phase 1, wait for results, then Phase 2. Never both in one message.
- For files >500 lines, use chunked reads (view specific line ranges).
- For files >2000 lines, assume single read is incomplete.
- Always end tasks with: type check, lint, fix all errors.
- Fix root causes. Do not apply minimal fixes. Standardize architecture.
- For multi-file tasks spanning >5 files, split into subagents.
- Before refactoring any file >300 LOC: remove unused imports, dead code, debug logs as a separate phase first.
- Max 5 files per phase. Verify after each phase, stop and wait for confirmation.
- Re-read a file before every edit. Re-read after every edit to confirm. Max 3 edits per file between verifications.
- Remove imports/variables/functions that YOUR changes made unused. Don't touch pre-existing dead code.
- Every changed line must trace directly to the user's request. No drive-by improvements.

## Verification
- Never claim a library/platform is "discontinued" or "restructured" without live verification.
- After edits, diff against prior behavior when executor or pricing logic is touched.
- Never report success without: type check passes, lint passes, no new errors introduced.
- For renames or signature changes, search separately for: direct calls, type references, string literals, dynamic imports, re-exports, test files.
- Large tool outputs may truncate. If results seem incomplete, re-run with narrower scope.

## Communication
- Direct, no preamble. American units. Windows commands by default.
- No sycophancy. Tell me when I'm wrong. Confirm location before editing.
- No sign-off, no filler, no meta-commentary. Execute first, explain only if asked.
- If uncertain, state assumptions and ask. If multiple interpretations exist, present them.
- If a simpler approach exists, say so. Push back when warranted.

## Simplicity
- Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked. No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If 200 lines could be 50, rewrite it.

## Reliability
- Do not allow crashes from missing external data. Prefer degraded operation over failure.
- Match existing code style, even if you'd do it differently.
- Do not make unrelated changes. Do not refactor beyond current phase scope unless necessary for correctness.
- State any required out-of-scope fixes before making them.
