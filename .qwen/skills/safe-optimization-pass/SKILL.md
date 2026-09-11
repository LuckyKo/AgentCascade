---
name: safe-optimization-pass
description: Discipline for a performance-optimization pass over recently-committed code — read the real code, enumerate candidate wins, gate each against a named correctness invariant, and treat "no safe win" as a valid outcome rather than forcing a cosmetic diff.
source: auto-generated
version: "1.0.0"
triggers:
  - "optimization pass"
  - "performance optimization"
  - "optimize hot path"
  - "safe performance win"
  - "perf without breaking correctness"
  - "no behavior change beyond performance"
generated_by: ponytail
generated_from_task: "Optimization pass on three committed fixes (backend frame dedup + frontend invalidation gating) for a sub-agent turn-end streaming burst — find SAFE high-value perf wins without breaking reviewed correctness guarantees; 'no safe win' is a valid outcome."
---

## Goal

Run a performance-optimization pass over recently-committed code and ship only changes that are provably safe AND measurable — while treating "there is no safe high-value win" as a legitimate, reportable result instead of inventing a change to look busy.

## When this applies

The task says things like: *"find SAFE performance wins in/around these changed paths WITHOUT breaking correctness"*, *"no behavior change beyond performance"*, *"if you find NO safe high-value optimization that's a valid outcome — say so rather than forcing a change."* The code was already carefully reviewed for CORRECTNESS; your job is PERFORMANCE only, on the hot path.

## Procedure

### Step 1 — Read the ACTUAL current code, never trust the summary
The task brief describes intent but not exact lines. For every changed region:
- `git show <sha> -- <file>` to see exactly what each commit changed (loop body, post-loop block, handler).
- Then `read_file` the CURRENT file at those line ranges — the working tree may differ from the diff.
- Trace the real flow end-to-end: find the helper it calls (`grep "def broadcast_stream_update"`), read ITS body, and determine what work happens on each branch (e.g. the *suppressed* early-return path vs the *broadcast* path). A candidate "wasted work" often turns out to be an O(1) early return.

### Step 2 — Establish a green baseline BEFORE touching anything
- `git diff --stat -- <target files>` → confirm clean (you start from HEAD, not on top of someone else's uncommitted edits).
- Run the named test suites up front so you can distinguish "I broke it" from "pre-existing failure." Note which failures the task told you to ignore (e.g. a timing-sensitive render-cadence test) and confirm the load-bearing tests pass.

### Step 3 — Enumerate candidate wins, then gate EACH against a NAMED invariant
For every opportunity, write down: (a) before/after, (b) which numbered Hard Constraint / correctness guarantee it must preserve, (c) is the win real or cosmetic? Common traps that DISQUALIFY a candidate:
- **"Recompute len() / O(1) thing"** — `len(list)` is O(1); recomputing it costs nothing. Not a win.
- **"Wasted work before the gate"** — if the expensive call is already inside `if need_final:` and skipped when False, there is no wasted work to remove. Confirm, don't assume.
- **"Make helper X cheaper/skippable"** — read X first. If it's already O(1)/cheap AND it's a designated correctness update (not perf), removing it changes behavior → disallowed by "no behavior change."
- **"Short-circuit the render/loop on this frame"** — the killer question: *can I PROVE no visible change is missed?* A cheap gate usually needs to compare every field that can change across every panel/element — which is itself an O(n) walk, costing as much as the work it would skip. If you can't prove equivalence against the "no stale UI" invariant, SKIP it.

### Step 4 — Apply only clearly-safe + high-value changes; otherwise ship nothing
- Per the rules: shortest working diff wins, but ONLY once you understand the problem. A small change in the wrong place is a second bug.
- If every candidate fails the gate, make ZERO edits. Leave the tree clean. Do not add abstractions, caches, or "clever" rewrites to justify the pass.

### Step 5 — Verify & report honestly
- Syntax-check anything you changed (`python -c "import ast;ast.parse(open(f).read())"` / `node --check`).
- Re-run the baseline suites; confirm no new failures.
- Report: every change (file:line, before/after) + which invariant each preserves; every opportunity SKIPPED + why; syntax results; test results. If zero changes, say so plainly — "these fixes are already performant" is a complete answer.

## Tips

- **The most valuable output of an optimization pass can be the absence of code.** Forcing a cosmetic diff to look productive is how you introduce the very bug the reviewed fixes were meant to prevent.
- **Cite the invariant, not vibes.** "This is safe" is weak; "safe because `need_final` still covers all four delivery cases and I only touched an O(1) line" is strong.
- **Distinguish pre-existing test failures from regressions** before panicking — re-run the exact command and compare to the baseline you took in Step 2.
- **Windows host gotcha:** `tail`/pipes may not exist in the shell; run commands plainly or use `__status` on background shells.
- **Don't optimize a path that's already been optimized by the commit under review** — often the "expensive" thing (e.g. a full DOM rebuild sentinel) was ALREADY removed by the fix; what remains is a cheap early-out that no-ops when nothing changed.
