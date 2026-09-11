---
name: frontend-fix-plan-critique
description: Systematically critique proposed frontend performance fix plans, evaluate correctness tradeoffs, and recommend the safest minimal implementation.
source: auto-generated
version: "1.0.0"
triggers:
  - "fix plan review"
  - "frontend performance bug"
  - "render optimization"
  - "cache invalidation"
  - "state synchronization"
generated_by: review-agent
generated_from_task: "Review a CORRECTED fix plan for a frontend turn-end full-rebuild bug in AgentCascade web_ui/app.js. Do NOT write/modify code — critique the plan and recommend the safest approach."
---

## Goal

Enable rigorous review of proposed frontend performance fixes, with emphasis on identifying correctness risks, stale UI scenarios, and simpler alternatives that preserve existing invariants.

## Procedure

### Step 1 — Verify the root cause against actual source

- Read the diagnosis report and compare to production code.
- Confirm the exact branch that fires (e.g., `if (currentCount < lastCount)`).
- Validate that the proposed trigger matches observed behavior.

**Example:** Check that `invalidateAllPanelCaches()` stamps `lastRenderedCount='999999999'` and that L4286 evaluates true on turn-end `done` frames.

### Step 2 — Evaluate each proposed option for correctness

For each candidate fix, analyze:
- **Stale UI risk:** What genuine changes would be missed? (e.g., middle message edits, function call updates, active flag changes).
- **Invariant violations:** Does it break existing resync mechanisms (hidden panels, retry, session reset)?
- **Edge cases:** Can count remain same while content changes? Does `contentKey` capture all change indicators?

**Red flags:**
- Options that rely on comparing only last message state.
- Options that reset counters without clearing content keys.
- Options that modify sentinel logic to "tolerate" it instead of removing the root cause.

### Step 3 — Search for simpler, more robust alternatives

- Ask: Is the frame itself necessary to trigger the expensive operation?
- Consider: Could we skip invalidation for this specific frame type instead of modifying the render logic?
- Verify: Does the preceding `stream_update` already render the final state incrementally?

**Example:** If the `done` frame is purely semantic and arrives after the commit frame rendered incrementally, skipping `invalidateAllPanelCaches()` on `done` frames is safe and simpler.

### Step 4 — Define acceptance criteria and test cases

- **Functional tests:** Verify no full rebuild when content unchanged; verify incremental append when new messages; verify full rebuild on genuine state changes.
- **Regression tests:** Ensure settings toggle, retry, session reset still trigger invalidation.
- **Edge tests:** Test hidden panel catch-up, rapid turn-end transitions, message order changes.

## Tips

- **Never approve work you haven't personally inspected:** Read every file involved.
- **Rate severity:** Mark issues as 🔴 Critical (stale UI), 🟠 Major (incorrectness), 🟡 Minor (inefficiency), 🔵 Nit (style).
- **Require concrete fixes:** Every issue must have a suggested implementation change.
- **Preserve existing guards:** Don't remove mechanisms that handle real desync scenarios.

## Quality Checklist

- [ ] Root cause is confirmed by reading actual source code, not just relying on the plan's claims.
- [ ] Each proposed option is critiqued with specific stale UI scenarios.
- [ ] A simpler alternative is identified if the original options are flawed.
- [ ] Implementation steps include exact file:line references.
- [ ] Test cases cover both positive (no rebuild) and negative (rebuild needed) scenarios.
