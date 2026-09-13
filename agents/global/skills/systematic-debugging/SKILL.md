---
name: systematic-debugging
description: Evidence-based debugging procedure for complex software bugs. Enforces reproduction-before-fix, data-flow tracing over mechanism debugging, single-hypothesis isolation, and anti-confirmation-bias checkpoints. Use when debugging non-trivial bugs in multi-layer systems, concurrency issues, timeouts, deadlocks, or when prior fixes have failed to resolve the issue.
source: auto-generated
version: "1.0.0"
triggers:
  - debug a bug
  - root cause analysis
  - investigate why this is failing
  - fix didn't work
  - persistent timeout
  - deadlock investigation
  - race condition debug
  - intermittent failure
  - why does this error occur
  - trace the data flow
  - reproduce the bug
  - hypothesis testing
generated_by: orchestrator
generated_from_task: "Post-mortem of 2-day security slot deadlock where wrong identity in approval registration was missed while symptomatic mechanism fixes were applied"
---

## Systematic Debugging Goal

Provide a structured, anti-bias debugging procedure that prevents the most common failure mode: **fixing the mechanism instead of the input**. Forces evidence collection before hypothesis formation, and reproduction before any fix.

## Core Principles (Non-Negotiable)

1. **No fix before root cause is confirmed.** A "fix" that makes the symptom disappear without explaining WHY it occurred is a band-aid. It will return.
2. **Trace data, not mechanism.** When X fails, first verify X's INPUTS are correct before debugging X's internal logic. The bug is often upstream.
3. **Reproduce before fixing.** If you can't reproduce it, you can't verify the fix. A 30-minute reproduction test saves days of guessing.
4. **One hypothesis at a time.** Change one variable. Verify. Then move on. Stacking fixes obscures causation.
5. **A failed fix invalidates the hypothesis, not the approach.** Step back to evidence. Don't pile on a second guess.
6. **Three failed fixes = architectural question.** If three targeted fixes don't work, the problem is likely in the design, not the implementation.

## Procedure

### Phase 0 — Triage (5 min)

Before diving deep, establish:

- [ ] Read the FULL error message. Every word. "Timed out after Nones" means `timeout` was None — that's a clue, not noise.
- [ ] Identify the EXACT failing component and its immediate caller.
- [ ] Note the timestamp and find the full log window (±5 min) around the incident.
- [ ] Check: has this been "fixed" before? If yes, read the prior fix and understand WHY it didn't work.
- [ ] **Identity check**: In multi-layer systems, list all identity/name concepts in play (instance names, class names, session names, template names). Which one flows through each boundary?

### Phase 1 — Reproduce (30 min max)

**Goal:** A deterministic or reliably-triggerable reproduction.

- [ ] Write a minimal test/script that triggers the exact failure mode.
- [ ] If the bug is in production logs but not reproducible locally, identify what's different (timing, state, concurrency, config).
- [ ] Add targeted logging at component boundaries if existing logs are insufficient.
- [ ] **Checkpoint:** Can I see the failure happen? If NO → still in reproduction phase. Do not proceed to hypothesis.

**Anti-pattern:** "I think it's X, let me just try fixing X and see if the error goes away." This is guessing, not debugging.

### Phase 2 — Data Flow Trace (the critical phase)

**Goal:** Verify every input to the failing component is correct.

This is where most bugs live. The mechanism is usually fine; the data flowing into it is wrong.

- [ ] Identify the failing function/method and its parameters.
- [ ] For EACH parameter, trace backward: who provides this value? From where?
- [ ] **At each boundary crossing** (function call, thread handoff, message queue, approval registration), verify the value is what you expect.
- [ ] Look for **name/identity mismatches**: static vs. dynamic values, template-time vs. runtime values, different kwargs carrying similar data.
- [ ] Compare with a WORKING case: find a similar flow that succeeds and diff the data at each step.

**The "5 Whys" applied to data flow:**
```
Why did Security time out? → It couldn't acquire the slot.
Why couldn't it acquire? → The slot was held by screen_capture_fix.
Why wasn't it released? → The yield logic looked for 'Maine', not 'screen_capture_fix'.
Why did it look for 'Maine'? → ap['agent_name'] contained 'Maine' (or None → fallback).
Why was ap['agent_name'] wrong? → The tool registered the approval with self.agent_name (static template name) instead of the runtime instance name.
```

**Checkpoint:** Can I point to the EXACT line where the wrong value enters the system? If not, keep tracing.

### Phase 3 — Hypothesis & Single-Variable Test

**Goal:** Confirm root cause with a minimal change.

- [ ] Write down ONE concrete hypothesis: "The bug is at file.py:LINE because VALUE_X should be Y but is Z."
- [ ] Design the smallest possible test that distinguishes this hypothesis from alternatives.
- [ ] Change ONE variable. Run. Observe.
- [ ] If confirmed → proceed to Phase 4.
- [ ] If NOT confirmed → return to Phase 2. The hypothesis was wrong. Do NOT add a second change.

**Anti-pattern:** "Let me fix the yield logic AND add a force-release fallback AND improve logging." Three changes, one test run — if it works, you don't know which one fixed it. If it doesn't, you've made debugging harder.

### Phase 4 — Fix & Verify

- [ ] Write the regression test FIRST (the reproduction from Phase 1, now asserting the correct behavior).
- [ ] Apply the minimal fix.
- [ ] Run the regression test → must pass.
- [ ] Run the full test suite → no regressions.
- [ ] **If possible, verify against the original production scenario** (replay the log, run the same user action).

### Phase 5 — Post-Fix Review

- [ ] Does the fix address the ROOT CAUSE or just this instance? (e.g., "use runtime name" vs. "add force-release for when name is wrong")
- [ ] Are there other sites with the same pattern? Grep for it.
- [ ] Add a memory/lesson documenting: what the bug was, why it was missed, what the actual root cause was.
- [ ] If the bug was missed by prior investigation: write a post-mortem on WHY the investigation went wrong.

## Anti-Patterns (Recognize and Avoid)

| Pattern | What it looks like | Why it's dangerous |
|---------|-------------------|-------------------|
| **Mechanism debugging** | "Why didn't the yield fire?" when the real question is "was the right instance name passed to the yield?" | Fixes the mechanism, leaves the input broken |
| **Hypothesis anchoring** | Accepting a researcher's framing without validating their assumptions | 474 lines of correct mechanism analysis creates false confidence |
| **Fix stacking** | Adding force-release + logging + pool verification all at once | Can't isolate which change (if any) actually fixed it |
| **Confirmation grep** | Grepping for evidence that confirms your hypothesis, not disconfirms it | You'll find supporting evidence for any hypothesis in a large codebase |
| **Context loss** | Letting context compression blur the distinction between similar-looking identifiers | `agent_name` vs `agent_instance_name` — one letter difference, 2 days lost |
| **Symptom suppression** | Making the error message clearer, adding timeout retries, increasing capacity | The underlying bug persists and will manifest differently |

## Multi-Layer System Specifics

When debugging across threads, processes, or service boundaries:

1. **Draw the boundary map.** Which component owns which data? Where does identity change?
2. **Log at every boundary.** Entry/exit with full parameter dump (at DEBUG level). This is cheap insurance.
3. **Check for async races.** If component A reads a value that component B might modify concurrently, verify the read happens after the write (or under a lock).
4. **Verify the "obvious" assumption.** In complex systems, the obvious answer ("tools get the instance name") is often wrong because there are two code paths that both use similar names for different purposes.
5. **Use the working case as ground truth.** Find a flow that succeeds with the same components. Diff the data at each step. The divergence point IS the bug.

## When to Escalate / Get Fresh Eyes

- After 2 failed hypotheses → get a second agent to review your Phase 2 trace independently.
- After context compression loses critical detail → re-read the original error and logs fresh, not from summary.
- If you've been debugging >1 hour without reproduction → stop, write down what you know, take a different angle (data flow vs. mechanism, bottom-up vs. top-down).
- **The "fresh eyes test":** If an external reviewer with no prior context can identify the bug in minutes, your investigation was anchored on the wrong question. Ask: "What would I check FIRST if I knew nothing about this system?"

## Quality Gate (Before Declaring Bug Fixed)

- [ ] I can explain the root cause in ONE sentence without saying "it's complex."
- [ ] The regression test fails without the fix and passes with it.
- [ ] I've checked for other instances of the same pattern.
- [ ] A fresh reviewer (no prior context) can follow my explanation and confirm the logic.
- [ ] The fix is minimal — if it's >20 lines, question whether I'm fixing the right thing.
