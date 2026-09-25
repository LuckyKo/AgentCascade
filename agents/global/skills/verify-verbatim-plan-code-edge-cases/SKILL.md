---
name: verify-verbatim-plan-code-edge-cases
description: Verify "use verbatim" plan code against the plan's own stated edge-case requirements before trusting it; catch latent bugs like lstrip() dropping blank lines.
source: auto-generated
version: "1.0.0"
triggers:
  - "use it verbatim"
  - "plan-driven implementation"
  - "implement the reviewed plan"
  - "exact replacement code is in the plan"
  - "stays empty"
generated_by: coder
generated_from_task: "Implement heuristic edit_file flattening fix from a reviewed plan; plan's verbatim Phase 2 loop dropped truly-empty lines via lstrip()."
---

## Goal
When a task says to implement a reviewed plan by copying its code **verbatim**, still verify that the code actually satisfies every behavior the plan *claims* — because "use it verbatim" does not mean "the code is correct." A reviewed plan's code can contain a latent edge-case bug that the plan's own test list covers but its code doesn't.

## Procedure
### Step 1 — Extract the plan's stated behavioral requirements, not just its code
Read the plan twice: once for the exact code to paste, once for every sentence of the form "X stays Y", "preserves Z", "a truly empty line stays empty", "works for blank and non-blank lines". These are *requirements*. Treat each as an assertion your implementation must satisfy. The plan's "recommended extra test cases" section is where discrepancies most often live — it lists edge cases the author intended but whose code path may not handle them.

### Step 2 — Before pasting, dry-run the verbatim code on every stated edge case in isolation
Reproduce the core loop/expression in a scratch `code_interpreter` snippet and feed it each named edge case (empty line, whitespace-only line, last line without trailing newline, tabs, deep nesting). Compare output to the plan's claim. Do this BEFORE editing the real file — it is cheap and catches bugs that only surface on an edge the main path never hits.

### Step 3 — When a verbatim-code bug is found, apply the minimal fix and document the deviation
Fix only what's broken; keep the rest byte-identical to the plan. In your report, call out the deviation explicitly ("deviation from 'use it verbatim'") with the reason (which stated requirement the verbatim code violated) so the reviewer/user can adjudicate. Do not silently "improve" beyond the required fix.

### Step 4 — Prove the new test is a real regression guard
After adding the edge-case test, confirm it FAILS on the pre-fix code (e.g., `git stash` / revert and run) and PASSES on the fix. A test that passes on both is a tautology, not a guard.

## Tips
- **The `lstrip()` newline gotcha (root cause of this task):** for a truly-empty line `'\n'`, `str.lstrip()` returns `''` because the newline *is* leading whitespace. So `indent + line.lstrip()` becomes `indent + '' = indent`, and if indent is empty the whole line vanishes. A space-only blank line `' \n'` does NOT trigger it (`lstrip()` returns `'\n'`), which is why a naive isolated repro using spaces can mask the bug. Correct pattern to preserve a blank line:
  ```python
  body = line.lstrip()
  if not body:                       # blank/whitespace-only line
      body = line[len(own_ws.rstrip(' \t')):] or '\n'   # recover trailing newline
  ```
  where `own_ws` is the line's leading whitespace. Non-blank lines are untouched (their `lstrip()` is truthy).
- **Isolated repros can lie:** if your scratch simulation uses a slightly different input than the real pipeline (e.g., space-only vs truly-empty blank), it will disagree with production. When an isolated sim and the real run disagree, instrument the *real* code path (temporary stderr prints of intermediate values) rather than trusting the sim.
- **`rstrip(' \t')` vs `rstrip()`:** to recover a newline from leading whitespace, strip only indent chars (`' \t'`), never all whitespace — `own_ws.rstrip()` would also eat the newline and leave nothing.
- A reviewed plan is a strong prior but not proof; "CONFIRMED by reviewer" usually means line refs and design were checked, not that every edge-case code path was executed.
