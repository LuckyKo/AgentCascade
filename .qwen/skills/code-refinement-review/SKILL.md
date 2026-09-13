---
name: code-refinement-review
description: Systematic quality assurance review of committed code changes focusing on robustness, bloat, test quality, and cross-cutting correctness
source: auto-generated
version: "1.0.0"
triggers:
  - "refinement review"
  - "quality gate"
  - "refinement pass"
  - "robustness check"
  - "test quality"
generated_by: refine_combined
generated_from_task: "Final refinement/quality review of two committed fixes (backend frame dedup + frontend invalidation gating) for a sub-agent streaming burst in AgentCascade before closing the task. Focus on robustness nits, bloat, and test quality — correctness already independently PASSed."
---

## Goal

Perform a thorough, evidence-driven review of code changes to identify robustness issues, unnecessary bloat, test problems, and cross-cutting gaps before finalizing work.

## Procedure

### Step 1 — Examine the Commits
Use `git show <commit>` to view each change in full. Note which files were modified and the scope of changes.

```bash
git show <commit-hash> --stat
git show <commit-hash>
```

### Step 2 — Read Actual Code
Don't rely on diff summaries. Open the modified files at the relevant lines using `read_file`. Verify:
- The exact logic implementation
- Comment quality and necessity
- Variable naming and clarity

### Step 3 — Analyze Edge Cases
Look for fragile assumptions:
- Float equality comparisons (`==`) that should use tolerance or alternative logic
- Time-based checks with monotonic clocks
- State transitions and race conditions
- Defensive coding gaps (null/undefined checks)

### Step 4 — Review Tests
Check new tests for:
- **Clarity**: Do names clearly describe the scenario?
- **Flakiness**: Are assertions timing-sensitive without proper guards?
- **Redundancy**: Do multiple tests cover the same edge case?
- **Value**: Do they add coverage beyond existing tests?

### Step 5 — Cross-Cutting Analysis
If multiple related fixes exist, verify:
- They work together correctly
- No residual gaps remain
- Overlapping functionality is intentional

### Step 6 — Report Findings
Structure the review as a numbered list with severity ratings:
- 🔴 Critical (must fix immediately)
- 🟠 Major (should fix before release)
- 🟡 Minor (nice-to-have improvements)
- 🔵 Nit (cosmetic/pedantic)

Each finding should include:
- File and line number
- Exact current code
- Concrete recommended edit
- Reasoning and impact

## Tips

- **Never review blind**: Always read the actual files, not just diffs.
- **Evidence-driven**: Cite exact lines and logic in your critique.
- **Prioritize**: Lead with the most critical issues first.
- **Be constructive**: Provide actionable fixes for every problem.
- **Use tools**: Leverage `grep`, `shell_cmd`, and `code_interpreter` to verify claims.

## Decision Format

Conclude with a clear verdict:
- **PASS**: No critical issues, changes are ready
- **NEEDS WORK**: Minor/nit issues that should be addressed
- **FAIL**: Critical issues that must be fixed before proceeding

## Example Output Template

```
🔴 MUST-FIX: [Description]
File: path/to/file.py Line 123
Current: `code here`
Recommended: `better code`
Reason: Impact on correctness/robustness

🟠 NICE-TO-HAVE: [Description]
...

✅ Cross-cutting analysis: ...

Final Verdict: NEEDS WORK (apply MUST-FIX before closing)
```