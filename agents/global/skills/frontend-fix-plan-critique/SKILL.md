---
name: frontend-fix-plan-critique
description: Systematically critique a proposed frontend performance fix plan — verify the root cause against actual source, evaluate each option for correctness/stale-UI risk, and recommend the safest minimal implementation. Critique only; do not write code.
source: auto-generated
version: "1.0.0"
triggers:
  - "fix plan review"
  - "frontend performance bug"
  - "render optimization"
  - "cache invalidation"
  - "state synchronization"
---

## Goal

Rigorous review of a proposed frontend performance fix, emphasizing correctness risks, stale-UI scenarios, and simpler alternatives that preserve existing invariants. Critique the plan — do NOT write/modify code.

## Procedure

1. **Verify the root cause against actual source.** Read the diagnosis report and compare to production code. Confirm the exact branch that fires (e.g., `if (currentCount < lastCount)`). Validate the proposed trigger matches observed behavior.
2. **Evaluate each proposed option for correctness.** For each candidate fix analyze:
   - *Stale-UI risk:* what genuine changes would be missed? (middle-message edits, function-call updates, active-flag changes)
   - *Invariant violations:* does it break existing resync mechanisms (hidden panels, retry, session reset)?
   - *Edge cases:* can the count stay the same while content changes? Does the change-key capture all change indicators?

   **Red flags:** options that compare only last-message state; options that reset counters without clearing content keys; options that modify sentinel logic to "tolerate" it instead of removing the root cause.
3. **Search for simpler, more robust alternatives.** Is the triggering frame itself necessary for the expensive operation? Could you skip invalidation for this specific frame type instead of modifying render logic? Does the preceding update already render the final state incrementally? (If a terminal/semantic frame arrives after the incremental commit rendered, skipping invalidation on it is often safe and simpler.)
4. **Define acceptance criteria + test cases.** Functional: no full rebuild when content unchanged; incremental append on new messages; full rebuild on genuine state changes. Regression: settings toggle / retry / session reset still trigger invalidation. Edge: hidden-panel catch-up, rapid turn-end transitions, message-order changes.

## Tips

Never approve work you haven't personally inspected — read every file involved. Rate severity: 🔴 Critical (stale UI), 🟠 Major (incorrectness), 🟡 Minor (inefficiency), 🔵 Nit (style). Require a concrete implementation change for every issue. Preserve existing guards that handle real desync scenarios.

## Quality checklist

- [ ] Root cause confirmed by reading actual source, not just the plan's claims.
- [ ] Each proposed option critiqued with specific stale-UI scenarios.
- [ ] A simpler alternative identified if the original options are flawed.
- [ ] Implementation steps include exact file:line references.
- [ ] Test cases cover both positive (no rebuild) and negative (rebuild needed) scenarios.
