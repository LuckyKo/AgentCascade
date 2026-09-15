---
name: surgical-frontend-fix-review
description: Systematic review of a small, single-path frontend code change and its regression test — verify the discriminator/boundary logic, audit all callers, and confirm the test is meaningful (fails on pre-fix code). Minimal-scope analysis.
source: auto-generated
version: "1.0.0"
triggers:
  - "review frontend fix"
  - "validate surgical change"
  - "check test coverage"
  - "minimal scope review"
---

## Goal

Validate a surgical code change that modifies a single logic path with minimal scope — correctness, safety, and proper test coverage.

## Procedure

1. **Understand the change context.** Read the exact diff first: what was modified (lines/files/functions), the nature of the change (conditional wrap, parameter adjustment, etc.), surrounding control flow. Tools: `git diff` → `read_file`.
2. **Verify the core logic.** For a surgical fix confirm: the discriminator is correct (e.g., `data.type === 'state'` vs other values); boundary conditions are safe (does skipping this path break recovery?); no unintended side effects on adjacent paths. Read 20–30 lines before/after to check indentation and logic alignment.
3. **Audit all callers.** `grep -n "functionName(" file.js` across the whole file/directory. Ensure only the intended call was modified, other callers are unchanged, and no new dependencies were introduced.
4. **Review the regression test.** For spy/instrumentation tests: verify the spy setup doesn't alter behavior (just counts calls); confirm the test would FAIL on pre-fix code (meaningful assertion); check determinism (no timers/randomness/flaky elements).
   - JS note: top-level function declarations behave differently inside `vm.runInContext` — reassigning a top-level function in the vm context to wrap it is a valid spy pattern; verify the wrapper still delegates to the original.
5. **Cross-reference documentation.** Check diagnosis/plan docs and project-memory files in `.agent_lessons/` (the standard per-project memory location) for: root-cause alignment (does the fix address the stated problem?), accepted design decisions, known risks/guardrails.
6. **Compile the review report.** Severity: 🔴 Critical (bug/security), 🟠 Major (logic flaw/breaks invariants), 🟡 Minor (quality/readability), 🔵 Nit (style/typo). Include numbered findings with specific file/line refs, a concrete fix for every issue, and a final verdict: PASS / NEEDS WORK / FAIL.

## Tips

Always read the actual code (don't trust commit messages); verify test meaningfulness (it must fail pre-fix); check indentation rigorously — surgical changes often have edge-case spacing; good fixes document *why*, not just what; use grep across the whole file/directory to catch hidden callers.

## Common pitfalls

1. Assuming "done"/terminal frames are safe — verify the frame contract in backend code.
2. Ignoring vm lexical scope — top-level function declarations behave differently in `vm.runInContext`.
3. Overlooking sentinel values (e.g., `999999999`-style gates) that need careful validation.
4. Missing halted/recovery paths — special recovery logic may be affected.
