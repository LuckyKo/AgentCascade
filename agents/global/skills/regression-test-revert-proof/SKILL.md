---
name: regression-test-revert-proof
description: Prove a new/updated regression test genuinely guards the bug by reverting ONLY the production fix and confirming the test flips to failing, then restoring. Catches tautological tests that would pass pre-fix too.
source: auto-generated
version: 1.1.0
triggers:
  - "regression test"
  - "prove test fails pre-fix"
  - "tautological test"
  - "revert proof"
  - "test guards the bug"
  - "non-vacuous assertion"
  - "mutation proof"
  - "pin the assertion"
generated_by: coder
generated_from_task: "T1.5 stale _last_released_endpoint fix — prove new session-restore tests fail pre-fix / pass post-fix"
---

## Goal
A passing regression test proves nothing if it would have passed on pre-fix code too (a tautology). This procedure mechanically demonstrates the test actually guards the bug by flipping the production change off and confirming the assertion fails.

## Procedure

### Step 1 — Write the fix, then write the test against post-fix behavior
Make the production change first (so you know the exact lines to revert), then add the regression test asserting the corrected behavior. Run it once to confirm it PASSES post-fix. A test that doesn't pass post-fix is broken before you even ask whether it guards anything.

### Step 2 — Revert ONLY the production change, keep the test in place
Use a targeted `edit_file` (or `git stash push <prod-file>`) to remove JUST the fix — leave the new test file untouched and do NOT touch unrelated working-tree edits (e.g. pre-existing logging changes on a dirty tree).
```python
# edit_file: replace the fixed block with the original, e.g.
old:  self.instances[name] = new_inst
      new_inst._pool_ref = self        # <- THE fix
new:  self.instances[name] = new_inst  # reverted; test stays
```

### Step 3 — Run the test; it MUST fail for the right reason
```
python -m pytest tests/test_new_regression.py -q   # expect FAIL (serial `-n 0` only if the result is flaky)
```
Read the assertion failure. It must be the *semantically expected* one (e.g. `assert holder._pool_ref is not None` → `None is ...`). Which line goes red matters more than the exception type — a crash at the asserted line is valid pre-fix evidence. If it fails for an unrelated reason (fixture error, missing attr), your test is under-specified — fix the test, not the conclusion.

### Step 4 — Restore the fix and re-run; it MUST pass
```
# edit_file: put the fixed block back
python -m pytest tests/test_new_regression.py -q   # expect PASS (serial `-n 0` only if the result is flaky)
```
FAIL pre-fix + PASS post-fix = a real regression guard. Either half missing = not proven.

### Step 5 — Confirm the working tree is back to exactly the intended state
`git diff <prod-file>` must show ONLY your fix (no leftover revert artifacts). On a dirty tree, verify you didn't accidentally touch the pre-existing unrelated changes.

### Step 6 — Pin each assertion of a multi-assertion test with its OWN mutation
When one test guards several behaviours (state restored, flag cleared, no reload, exception propagated), a single revert only proves the *first* one. For every distinct assertion, apply a separate targeted mutation to the production code and confirm that specific assertion — and only it — flips to failing, then restore:
```
mutation A: comment out the restore line      -> expect KeyError on the registry lookup
mutation B: comment out the re-raise         -> expect the "propagated reason" assert to fail
```
If a mutation flips nothing, that assertion is decorative: either tighten it or delete it. Record in the test (or the memory) which mutation proves which assertion, so a later reader does not weaken one believing another still covers it.

## Tips
- **Do this on the actual dirty tree, not a clean checkout.** The whole point is the guard works in the real build state; pre-existing edits in other files must stay intact throughout.
- **Revert the minimal fix, not "a lot of code."** If reverting one line doesn't flip the test, the test isn't actually exercising that line — tighten the assertion or find the true load-bearing change.
- **`git stash push <file>` is the cleanest revert for multi-line fixes**, but a manual `edit_file` round-trip is fine and more surgical when the fix is a few lines. Always confirm with `git diff` afterward.
- **A test that passes on pre-fix code is a coverage illusion.** It reads as "we have a regression guard" during review while guarding nothing. This check is what turns it into a real one.
- **Stub-call recording does NOT pin control flow that dies before the stub.** `monkeypatch`-ing a function to append to a list and asserting `calls == []` looks like it proves "we never reached there", but if the call site's *arguments* fail to evaluate (e.g. a local is unbound after a swallowed exception), Python raises while building the argument tuple and the stub is never entered — the list stays empty and the test passes for the wrong reason. Pin such control flow on an observable *side effect of the real path* instead: a recorded reason string, a state flag, a log line, or a persisted failure record.
- **Prefer pinning an observable consequence over an absence.** `assert calls == []` is satisfied by many wrong worlds (never called, called-but-crashed, short-circuited). `assert failure.reason contains "<the original exception text>"` is satisfied by exactly one.
- **Make "must not happen" stubs recorders, not raisers.** A stub that raises `AssertionError` to assert unreachability is silently swallowed whenever the production code catches `Exception` on that path — the test then passes while the guard did nothing. Append to a list and assert the list is empty.
- **Guard your own guards against vacuity.** If an assertion checks that something is *absent* (a flag not set, a key missing), first prove the thing was ever *present* — record it mid-operation and assert that. Otherwise the absence assertion is trivially true in any fixture where the precondition never held.
- **Separate this from re-running the agent's claimed suite.** Re-running proves the *current* state is green; revert-proofing proves the *test* is meaningful. Do both — they catch different failures: the change-maker performs this; the orchestrator may re-run it as a spot-check.
