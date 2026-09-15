---
name: review-fix-plan-critical-analysis
description: Systematic pre-implementation critique of proposed code changes — cross-reference every claim against actual source, identify edge cases and regression risks, validate acceptance criteria before any code is written.
source: auto-generated
version: "1.0.0"
triggers:
  - "review fix plan"
  - "critique proposed changes"
  - "verify against source"
  - "code review before implementation"
  - "plan-level critique"
---

## Goal

Rigorous pre-implementation validation: verify every claim in a proposed fix against actual source, and surface logical flaws, edge cases, and regression risks. Critique the plan only — do NOT write or modify code.

## Procedure

1. **Gather source evidence.** Read all relevant source files named in the plan (`read_file` for line ranges, `grep` to locate patterns). Never review blind — every claim must be grounded in actual code.
2. **Validate root-cause claims.** For each asserted cause: locate the exact code producing the behavior; is the mechanism unconditional or conditional; do measurements/timestamps match the logic; is it structural or content-driven?
3. **Scrutinize proposed fixes.** Backend: does logic handle throttling, deduplication, and final-state guarantees correctly (e.g., last tick suppressed but a final frame still needed)? Frontend: do DOM patterns match data structures; does incremental append preserve all invariants (dedup, resync, index-mismatch handling)? Interaction: can two fixes interfere or create new edge cases?
4. **Identify missing edge cases.** Last loop tick throttled out? Frame dropped and resync needed? Could the fix break lazy rendering, tab switching, or error recovery? Race conditions / timing issues?
5. **Rate severity + suggest fixes.** 🔴 Critical — data loss/corruption/security hole. 🟠 Major — significant regression risk or incorrect behavior. 🟡 Minor — uncovered edge case, testability issue. 🔵 Nit — minor improvement/docs. Concrete code suggestion for every issue raised.
6. **Deliver a structured verdict.** One of **APPROVE / APPROVE-WITH-CHANGES / REJECT**; prioritized list of required changes before implementation; any claims found WRONG in light of actual source; specific file:line references for all findings.

## Tips

Always read full context before reviewing (never trust a summary); use `code_interpreter` to simulate suspect code when possible; check existing tests for intended behavior and gaps; reject over-engineered solutions that hide bugs instead of fixing root cause; never approve work you haven't personally inspected against source; look for off-by-one, race conditions, state invariants; verify acceptance criteria truly cover all edge cases.
