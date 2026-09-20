---
name: regression-test-revert-proof
description: Prove a new regression test is genuine by making it fail on pre-fix code via a minimal temporary revert, then restoring the fix — not by reasoning that it "should" fail.
source: auto-generated
version: "1.0.0"
triggers:
  - "regression test"
  - "prove fails pre-fix"
  - "temporary revert"
  - "non-tautological test"
  - "test must fail before fix"
generated_by: coder
generated_from_task: "todo.md:149 last-turn tool-disable double KV reprocess — prove the new regression test genuinely fails on pre-fix code"
---

## Goal
Turn a claimed regression test into a *proven* one: demonstrate it fails against the unfixed behavior with an actual run, so you know it guards the real bug and not a tautology.

## Procedure

### Step 1 — Write the test to assert the FIXED (correct) behavior
The assertion must encode the desired post-fix state (e.g. "triggering turn kept tools enabled"). It will pass once the fix is in, but you cannot yet trust it catches the bug.

### Step 2 — Revert ONLY the fix, minimally and reversibly
Introduce the smallest change that restores pre-fix behavior at the decision point. Prefer a one-line neutralization over deleting code:
```python
_auto_skill_will_extend = False  # TEMP-REVERT: prove pre-fix failure
```
This is safer than reverting via `git` (keeps the rest of your uncommitted work intact) and leaves an obvious marker to restore. Do NOT revert test files or unrelated logic.

### Step 3 — Run and confirm the NEW test fails with the bug's exact signature
Run just the affected test/class. The failure message should show the wrong value, not a crash:
```
assert ['tool_a', 'tool_b'] is None   # tools were disabled -> full reprocess
```
A failure that looks like the actual symptom (wrong value / missing message) is strong evidence the test targets the right code path.

### Step 4 — Restore the fix and confirm green
Revert your temporary revert, re-run the same tests, confirm they now pass. Keep the diff minimal so review is clean.

## Tips
- **Reasoning alone is not proof.** "This should fail pre-fix" is a claim; a failing run is evidence. The task bar for a regression test is the Step 3 red line.
- **Multiple new tests may share one bug** — expect several to fail on revert (e.g. tool-enable, warning-suppress, and prefix-stability all encode the same fix). That's fine and confirms coverage; but each should still fail for its own reason.
- **Watch assertion placement against real layout.** A test can be "logically correct" yet assert on the wrong slot and pass even pre-fix (e.g. checking the message *after* the trigger reply when the warning actually lands *before* it). When a supposedly-regression test passes on reverted code, dump the actual data structure (conversation/ordering) and fix the assertion — don't assume.
- **Use a tracer for per-call state.** For loop/engine bugs, wrap the per-iteration entry point (e.g. `_call_llm_with_injection`) to record the relevant instance state each call; assert on the specific iteration (trigger boundary), not just final state. See [[engine-loop-test-offbyone-diagnosis]].
- **Keep it reversible and scoped.** The temporary revert must be a single obvious edit you can undo in one step, so the final diff contains only the real fix + tests.
- **Serial runs for flaky suites.** Run with xdist disabled (e.g. `-o addopts=""` in AgentCascade) so a red/green comparison isn't muddied by parallelism flakiness — see [[pytest-docker-subprocess-hang]].
