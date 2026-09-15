---
name: systematic-debugging
description: Evidence-based debugging for complex bugs — reproduction before fix, data-flow tracing over mechanism debugging, single-hypothesis isolation, and anti-confirmation-bias checkpoints. Use for non-trivial bugs in multi-layer systems, concurrency, timeouts, deadlocks, or when prior fixes failed.
source: auto-generated
version: "1.0.0"
triggers:
  - "debug a bug"
  - "root cause analysis"
  - "investigate why this is failing"
  - "fix didn't work"
  - "persistent timeout"
  - "deadlock investigation"
  - "race condition debug"
  - "intermittent failure"
  - "trace the data flow"
  - "reproduce the bug"
---

## Core principles (non-negotiable)

1. **No fix before root cause is confirmed.** A fix that hides the symptom without explaining WHY will return.
2. **Trace data, not mechanism.** When X fails, verify X's INPUTS are correct before debugging X's internals — the bug is often upstream.
3. **Reproduce before fixing.** No repro → can't verify the fix.
4. **One hypothesis at a time.** Change one variable, verify, then move on. Stacking fixes obscures causation.
5. **A failed fix invalidates the hypothesis, not the approach.** Step back to evidence; don't pile on a second guess.
6. **Three failed fixes = architectural question** — likely design, not implementation.

## Phases

**0 — Triage (5 min).** Read the FULL error message (every word is a clue: "Timed out after Nones" means `timeout` was None). Identify the exact failing component + immediate caller. Get the log window (±5 min). If it was "fixed" before, read the prior fix and why it failed. **Identity check:** in multi-layer systems, list all identity/name concepts (instance, class, session, template) and which flows through each boundary.

**1 — Reproduce (30 min max).** Minimal test/script that triggers the exact failure. If only reproducible in prod logs, identify what differs (timing, state, concurrency, config). Add targeted logging at boundaries if needed. *Checkpoint: can I see it fail? No → still reproducing, don't hypothesize.* Anti-pattern: "let me just try fixing X and see."

**2 — Data-flow trace (the critical phase).** Most bugs live here — the mechanism is fine, the data into it is wrong. For EACH parameter of the failing function, trace backward to its provider. At every boundary crossing (call, thread handoff, queue, registration) verify the value. Look for name/identity mismatches (static vs dynamic, template-time vs runtime). Diff against a WORKING case — the divergence point IS the bug.
*5 Whys example:*
```
Security timed out? → couldn't acquire slot.
Slot held by screen_capture_fix, never released? → yield logic looked for 'Maine', not 'screen_capture_fix'.
Why 'Maine'? → ap['agent_name'] was 'Maine' (None → fallback).
Why wrong? → approval registered with static template name, not runtime instance name.
```
*Checkpoint: can I point to the exact line where the wrong value enters? No → keep tracing.*

**3 — Hypothesis + single-variable test.** Write ONE concrete hypothesis ("bug at file.py:LINE because VALUE_X should be Y but is Z"). Smallest test that distinguishes it from alternatives. Change one variable, run, observe. Confirmed → Phase 4; not confirmed → back to Phase 2 (do NOT add a second change). Anti-pattern: fix yield + force-release fallback + logging all at once — if it works you can't tell which fixed it.

**4 — Fix & verify.** Regression test FIRST (the repro, now asserting correct behavior) → minimal fix → regression passes → full suite no regressions → ideally replay the original prod scenario.

**5 — Post-fix review.** Does the fix address root cause or just this instance? Grep for other sites with the same pattern. Add a memory/lesson (what, why missed, actual root cause). If prior investigation missed it, write a post-mortem on why.

## Anti-patterns

| Pattern | Why dangerous |
|---|---|
| Mechanism debugging ("why didn't the yield fire?") when the real question is "was the right name passed?" | Fixes mechanism, leaves input broken |
| Hypothesis anchoring (accepting a researcher's framing unvalidated) | Long correct analysis creates false confidence |
| Fix stacking (force-release + logging + pool check at once) | Can't isolate which change fixed it |
| Confirmation grep (searching for evidence that confirms, not disconfirms) | You'll find support for any hypothesis in a large codebase |
| Context loss (compression blurs similar identifiers: `agent_name` vs `agent_instance_name`) | One-letter difference, days lost |
| Symptom suppression (clearer error, retries, more capacity) | Underlying bug persists, manifests differently |

## Multi-layer systems

Draw the boundary map (who owns which data, where identity changes). Log at every boundary (entry/exit, full param dump at DEBUG). Check async races (read-after-write or under lock). Verify the "obvious" assumption — often two code paths use similar names for different purposes. Use a working case as ground truth and diff step by step.

## When to escalate / fresh eyes

After 2 failed hypotheses → second agent reviews your Phase-2 trace independently. After context compression loses detail → re-read original error/logs fresh, not the summary. >1 hour without repro → stop, write down what you know, change angle (data-flow vs mechanism, top-down vs bottom-up). *Fresh-eyes test:* if an external reviewer finds it in minutes, your investigation was anchored on the wrong question — ask "what would I check first knowing nothing?"

## Quality gate (before declaring fixed)

- Root cause explainable in ONE sentence without "it's complex."
- Regression test fails without the fix, passes with it.
- Checked for other instances of the same pattern.
- A fresh reviewer can follow the explanation and confirm the logic.
- Fix is minimal — >20 lines means question whether you're fixing the right thing.
