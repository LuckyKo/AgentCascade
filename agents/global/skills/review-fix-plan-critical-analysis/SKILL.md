---
name: review-fix-plan-critical-analysis
description: Systematic critique of proposed code changes by cross-referencing claims with actual source implementation, identifying edge cases, and validating acceptance criteria before any code is written.
source: auto-generated
version: "1.0.0"
triggers:
  - "review fix plan"
  - "critique proposed changes"
  - "verify against source"
  - "code review before implementation"
  - "plan-level critique"
generated_by: reviewer
generated_from_task: "Review a FIX PLAN for correctness, risk, and missing edge cases. Do NOT write or modify any code — critique the plan only."
---

## Goal

Enable rigorous pre-implementation validation of proposed fixes by systematically verifying every claim against actual source code, identifying logical flaws, edge cases, and regression risks.

## Procedure

### Step 1 — Gather Source Evidence
Read all relevant source files mentioned in the plan. Use `read_file` for specific line ranges and `grep` to locate patterns. Do not review blind — every claim must be grounded in actual code.

### Step 2 — Validate Root Cause Claims
For each asserted root cause:
- Locate the exact code that produces the behavior
- Check if the mechanism is unconditional or conditional
- Verify measurements/timestamps against actual logic
- Confirm whether the issue is structural or content-driven

### Step 3 — Scrutinize Proposed Fixes
For each proposed change:
- **Backend:** Check if logic handles throttling, deduplication, and final-state guarantees correctly. Look for cases where the last tick was suppressed but a final frame is still needed.
- **Frontend:** Verify DOM manipulation patterns match data structures. Ensure incremental append preserves all invariants (dedup, resync, index-mismatch handling).
- **Interaction:** Identify whether two fixes could interfere or create new edge cases.

### Step 4 — Identify Missing Edge Cases
Consider:
- What happens when the last loop tick was throttled out?
- What if a frame is dropped and resync is needed?
- Could the fix break lazy rendering, tab switching, or error recovery?
- Are there race conditions or timing issues?

### Step 5 — Rate Severity and Suggest Fixes
Use severity ratings:
- 🔴 **Critical**: Data loss, corruption, or security hole
- 🟠 **Major**: Significant regression risk or incorrect behavior
- 🟡 **Minor**: Edge case not covered, testability issue
- 🔵 **Nit**: Minor improvement, documentation

Provide concrete code suggestions for every issue raised.

### Step 6 — Deliver Structured Verdict
Final report must include:
- One of: **APPROVE / APPROVE-WITH-CHANGES / REJECT**
- Prioritized list of required changes before implementation
- Any claims found to be WRONG in light of actual source
- Specific file:line references for all findings

## Tips

- Always read the full context before reviewing — never trust a summary.
- Use `code_interpreter` to simulate suspect code when possible.
- Check existing tests to understand intended behavior and gaps.
- Don't allow over-engineered solutions that hide bugs instead of fixing root cause.
- Never approve work you haven't personally inspected against source.
- Look for off-by-one errors, race conditions, and state invariants.
- Verify that acceptance criteria truly cover all edge cases.
