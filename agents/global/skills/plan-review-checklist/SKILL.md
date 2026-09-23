---
name: plan-review-checklist
description: Systematic methodology for independently reviewing implementation plans before code is written. Covers anchor verification, control flow, test expectations, and edge cases.
source: auto-generated
version: "1.0.0"
triggers:
  - "plan review"
  - "implementation plan"
  - "code review plan"
  - "verify before implement"
generated_by: plan_review_149b
generated_from_task: "Independent plan review for AgentCascade todo149 fix"
---

## Goal

Provide a repeatable, adversarial review process to catch bugs, logic flaws, and missing changes in an implementation plan before any code is written.

## Procedure

### Step 1 — Read the full plan and understand the bug context
- Read the entire plan document thoroughly.
- Identify the exact bug being fixed and the three-part solution (or however many parts).
- Note which files are touched and what the intended behavior changes are.

### Step 2 — Verify every anchor line exactly
For each "Before" block referenced in the plan:
- Open the exact file and line numbers.
- Confirm the code matches **exactly** (whitespace/indentation included).
- Flag any drift, even a single space change.
- Critical anchors to verify:
  - Core loop guards and decrement sites
  - Condition gates that will be removed or modified
  - Phase-specific blocks (e.g., Phase 4 vs Phase 5)
  - Instance field definitions
  - Reset sites for stateful fields

### Step 3 — Mentally compile each "After" diff
- Check indentation is correct relative to surrounding control flow.
- Ensure all names are defined in scope.
- Verify new helper methods are correctly defined as `self` methods with proper return types.
- Confirm both call sites of any new helper use it consistently.

### Step 4 — Trace the new control flow
- For each new branch (e.g., Phase-4 trigger), confirm it sits inside the correct conditional so it only runs on intended paths.
- Verify that when a new condition fails, control correctly falls through to existing logic.
- Ensure no infinite loops or skipped cleanup are introduced.
- Check that one-shot flags prevent double-firing.

### Step 5 — Validate test expectations against harness behavior
For each new or flipped test:
- Trace the harness setup (`_make_pool`, `fake_llm`, `_post_turn_checks_driver`, etc.).
- Confirm the assertions are arithmetically and logically correct.
- Especially verify:
  - Turn counts and indices line up with expected iteration numbers.
  - Tool-call shapes are recognized by the actual detection code.
  - The returned output path matches the intended behavior (snapshot vs messages[-1]).
  - Expected warning positions are feasible.

### Step 6 — Check harness feasibility
- Read the actual detection/execution functions (e.g., `_check_for_tool_calls_in_output`).
- Confirm that the proposed test drive (e.g., tool-call message shape) will actually be detected.
- If the harness cannot cleanly drive the scenario, flag it as a blocker.

### Step 7 — Audit cross-file impact
- Open other test files that might be affected by the changes (e.g., warning count tests).
- Verify whether the fixtures enable the conditions that would change their expectations.
- Recommend running them post-implementation to confirm.

### Step 8 — Analyze edge cases
For each edge case mentioned in the plan:
- Confirm the described behavior is correct by tracing the code paths.
- Verify that state is properly initialized or reset (e.g., new fields in `_make_inst`).
- Check that no unintended side effects leak into non-triggered runs.

### Step 9 — Scope check
- Ensure nothing in scope shouldn't be, and that all required changes are present.
- Example: Does `_make_inst` need explicit field initialization because `__new__` bypasses defaults?

## Tips
- **Be adversarial**: Your job is to find what will break, not to rubber-stamp.
- **Never trust claims**: Read the actual code; anchors can drift.
- **Focus on surgical edits**: Whitespace and indentation matter in diffs.
- **Rate severity**: Use 🟠 Major, 🟡 Minor, 🔵 Nit for each finding.
- **Always provide a concrete fix** for every issue raised.
- **End with a clear verdict**: PASS, NEEDS WORK, or FAIL.

## Common Pitfalls
- Assuming `_consume_turn` always decrements by 1 without verifying.
- Forgetting that `AgentInstance.__new__` bypasses dataclass defaults in tests.
- Overlooking that a new branch must be inside the correct conditional.
- Not checking if a harness can actually produce the needed message shapes.
- Missing that one-shot flags block re-entry across iterations.
