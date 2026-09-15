---
name: code-refinement-review
description: Systematic quality-assurance review of committed code changes focusing on robustness, bloat, test quality, and cross-cutting correctness. Use as a final refinement/quality gate before closing work whose correctness is already verified.
source: auto-generated
version: "1.0.0"
triggers:
  - "refinement review"
  - "quality gate"
  - "refinement pass"
  - "robustness check"
  - "test quality"
---

## Goal

Evidence-driven review of committed changes to surface robustness issues, unnecessary bloat, test problems, and cross-cutting gaps before finalizing. (For a general correctness/security review use `code-review`; this one is the polish/quality gate.)

## Procedure

1. **Examine the commits.** `git show <sha> --stat` then `git show <sha>` to see each change in full; note files + scope.
2. **Read actual code, not diff summaries.** Open the modified files at the relevant lines (`read_file`). Verify exact logic, comment quality/necessity, naming/clarity.
3. **Analyze edge cases.** Fragile assumptions: float equality (`==`) that should use tolerance; time-based checks (use monotonic clocks); state transitions/race conditions; defensive-coding gaps (null/undefined).
4. **Review tests.** Clarity (names describe the scenario?); flakiness (timing-sensitive assertions without guards?); redundancy (multiple tests cover the same edge case?); value (do they add coverage beyond existing tests?).
5. **Cross-cutting analysis.** If multiple related fixes exist: do they work together correctly; any residual gaps; is overlapping functionality intentional?
6. **Report findings.** Numbered list with severity — 🔴 Critical (must fix now), 🟠 Major (fix before release), 🟡 Minor (nice-to-have), 🔵 Nit (cosmetic). Each finding: file + line, exact current code, concrete recommended edit, reasoning/impact.

## Verdict

Conclude with exactly one of:
- **PASS** — no critical issues, ready.
- **NEEDS WORK** — minor/nit issues to address.
- **FAIL** — critical issues must be fixed before proceeding.

## Tips

Never review blind (read the actual files); cite exact lines/logic; lead with the most critical issues; every problem gets an actionable fix; use `grep`/`shell_cmd`/`code_interpreter` to verify claims.
