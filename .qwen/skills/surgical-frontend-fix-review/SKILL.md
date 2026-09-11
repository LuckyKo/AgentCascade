---
name: surgical-frontend-fix-review
description: Systematic review process for validating surgical frontend code changes and their regression tests with minimal scope analysis
source: auto-generated
version: "1.0.0"
triggers:
  - "review frontend fix"
  - "validate surgical change"
  - "check test coverage"
  - "minimal scope review"
generated_by: coder
generated_from_task: "Review FIX B frontend turn-end full-rebuild fix in AgentCascade web_ui/app.js plus its regression test in tests/frontend_stream_render.test.mjs"
---

## Goal

Enable thorough validation of surgical code changes that modify a single logic path with minimal scope, ensuring correctness, safety, and proper test coverage.

## Procedure

### Step 1 — Understand the Change Context

Read the exact diff first to identify:
- What was modified (lines, files, functions)
- The nature of the change (conditional wrap, parameter adjustment, etc.)
- Surrounding code structure and control flow

**Tools:** `git diff` → `read_file` for context

### Step 2 — Verify the Core Logic

For a surgical fix, confirm:
- **Discriminator is correct** (e.g., `data.type === 'state'` vs other values)
- **Boundary conditions are safe** (does skipping this path break recovery?)
- **No unintended side effects** on adjacent code paths

**Check:** Read 20-30 lines before/after the change to ensure indentation and logic alignment.

### Step 3 — Audit Other Callers

Use `grep` to find all invocations of any affected function:
```bash
grep -n "functionName()" file.js
```
Ensure:
- Only the intended call was modified
- Other callers remain unchanged
- No new dependencies were introduced

### Step 4 — Review Regression Test

For tests that validate the fix:

**Spy/Instrumentation Tests:**
- Verify the spy setup doesn't alter behavior (just counts calls)
- Confirm the test would fail on pre-fix code (meaningful assertion)
- Check test determinism (no timers, randomness, or flaky elements)

**Example Validation:**
```javascript
// Should work for: reassigning top-level function in vm context
vm.runInContext(
  `__count = 0; fn = originalFn; fn = () => { __count++; return originalFn(); };`,
  ctx
);
```

### Step 5 — Cross-Reference Documentation

Check diagnosis/plan docs for:
- Root cause alignment (does the fix address the stated problem?)
- Accepted design decisions (is this the recommended approach?)
- Known risks and guardrails

**Files to check:** `.agent_lessons/` memories, diagnosis reports, fix plans.

### Step 6 — Compile Review Report

Structure findings with severity ratings:

| Severity | Meaning |
|----------|---------|
| 🔴 Critical | Bug or security issue |
| 🟠 Major | Logic flaw, breaks invariants |
| 🟡 Minor | Code quality, readability |
| 🔵 Nit | Style/typo (optional) |

**Must include:**
- Numbered findings with specific line/file references
- Concrete fix suggestions for every issue
- Final verdict: PASS / NEEDS WORK / FAIL

## Tips

- **Always read the actual code** — don't trust commit messages blindly
- **Verify test meaningfulness** — ensure it fails on pre-fix code
- **Check indentation rigorously** — surgical changes often have edge-case spacing
- **Look for comment quality** — good fixes document why, not just what
- **Don't miss hidden callers** — use grep across entire file/directory

## Common Pitfalls

1. **Assuming "done" frames are safe** — verify the frame contract in backend code
2. **Ignoring vm lexical scope** — top-level function declarations behave differently in `vm.runInContext`
3. **Overlooking sentinel values** — `999999999` style gates need careful validation
4. **Missing halted-agent paths** — special recovery logic may be affected

## Example Review Output

```
## Change 1 — file.js (lines X-Y)
Verdict: ✅ CORRECT
- Conditional uses right discriminator
- Indentation matches block
- Other callers confirmed unchanged via grep

## Change 2 — test file (lines A-B)
Verdict: ✅ CORRECT & MEANINGFUL
- Spy setup sound for vm context
- Would fail on pre-fix code

## Final Verdict: PASS
```