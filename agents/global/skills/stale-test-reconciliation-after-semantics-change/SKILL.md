---
name: stale-test-reconciliation-after-semantics-change
description: Reconcile failing tests that were written for OLD semantics after a production formula/behavior change — re-derive expected values from the CURRENT code, probe ground truth before asserting, and stop the re-run loop.
source: auto-generated
version: "1.0.0"
triggers:
  - "stale test"
  - "test written for old semantics"
  - "assertion mismatch after refactor"
  - "evict_threshold"
  - "got X expected Y"
  - "rebalance"
  - "count-cap"
generated_by: coder
generated_from_task: "Phase C skill-scoring: fix 2 stale legacy rebalance tests that assumed old count-cap semantics after the min_cap/OR-composition change."
---

## Goal
When a production formula/behavior changes and pre-existing tests fail, stop guessing at expected values from memory — re-derive them from the CURRENT code, verify with a ground-truth probe, then edit. This breaks the "edit → run → still wrong → guess again" loop.

## Procedure
### Step 1 — Confirm the implementation is correct, not the test
Before touching tests, get an authoritative statement (from supervisor or by reading the production function) of what the NEW behavior is. In this task: `cap_target = active_before - evict_threshold`, so K=1.0 + all-qualified → cap evicts nothing by design. If the impl is right, the failing assertions are stale — fix tests, not code.

### Step 2 — Re-derive expected values from the ACTUAL production formula
Read the real function (don't rely on a docstring or memory). Identify every input that feeds the asserted output:
- `n_qualified` counts `total_loads>=1 OR ratings.count>=1` — an "unqualified" fixture entry with `ratings={'count':2}` is STILL qualified. To be genuinely unqualified it needs no loads AND no ratings.
- `evict_threshold` is the ACTIVE count to evict DOWN TO, not a removal count: items removed = `active_before - threshold`.
- A D-SAFE safety cap (`max_evictions_per_pass`, default 25) can silently truncate large evictions — raise it in tests that expect >25.

### Step 3 — Probe ground truth before asserting (break the loop)
When your mental model keeps failing, STOP simulating by hand. Write a tiny throwaway script (or add a temporary print) that runs the real function on the fixture and prints the actual summary + intermediate values (class counts, n_qualified, raw, threshold, A/age). Read the numbers, then write assertions that match reality. Delete the probe before finishing.

### Step 4 — Make the test genuinely meaningful (don't just weaken it)
A stale test that now asserts a no-op is weak coverage. Prefer re-shaping the fixture so the path under test actually fires: e.g. make only some skills qualified so `raw < active_before` and the cap evicts the worst-ranked down to threshold. Keep the original intent ("over-cap → evict worst-ranked").

### Step 5 — Handle state-dependent classification deterministically
If eviction is gated on a PROTECTED/fair-window check that depends on an activity clock, use the smallest knob to force the desired state (here `fair_window_turns=1` makes every aged skill non-PROTECTED regardless of counter state). Avoid depending on wall-clock fallbacks or frozen counters unless you've verified them.

## Tips
- "got 0 expected N" usually means a filter/cap excluded everything — find which one (PROTECTED? safety cap? threshold math?) by reading the code, not by re-running.
- A self-contradictory comment in a failing test (e.g. "30 - 30 = 10") is a strong signal the whole fixture was written for old semantics — rewrite it, don't patch one assert.
- After fixing, run the FULL suite serially (`-n0`) to catch sibling tests that shared the same stale assumption.
- Save the non-obvious formula facts (threshold-as-target, OR-condition qualification, safety-cap default) as a project memory so the next phase doesn't re-derive them.
