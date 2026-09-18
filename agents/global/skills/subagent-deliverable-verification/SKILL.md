---
name: subagent-deliverable-verification
description: Orchestrator protocol for verifying a sub-agent's completed implementation before review/commit — re-run tests yourself, read the actual diffs, probe edge paths independently, and treat agent reports as claims to be checked.
source: auto-generated
version: "1.0.0"
triggers:
  - "verify sub-agent work"
  - "review implementation output"
  - "agent reported tests pass"
  - "pre-commit verification"
generated_by: orchestrator
generated_from_task: "Candidate-flow skill upgrade feature — verifying coder agent's uncommitted implementation in AgentCascade before independent review and commit"
---

## Goal
Prevent a sub-agent's self-reported success from reaching the reviewer or the commit without independent evidence. Agents hit turn limits, misread failures, or test only happy paths; the orchestrator is the last line of defense before code ships.

## Procedure
### Step 1 — Re-run the agent's claimed tests YOURSELF
Never trust "all tests pass" in an agent report. Run the exact suites in the same environment:
```
python -m pytest tests/test_a.py tests/test_b.py -n 0 -q   # project conventions may mandate flags (e.g. -n 0 on Windows)
```
Compare pass/fail counts against the claim. Any discrepancy → the agent's report is unreliable; dig into every failure yourself before delegating a fix round.

### Step 2 — Read the actual diffs, not the summary
`git diff --stat` first (scope check: are only the expected files touched?), then full `git diff <file>` on each changed file. Cross-check against the approved plan file. Watch for:
- Files the agent didn't mention in its report
- Deviations from locked semantics that were "noted" but silently implemented
- Pre-existing user edits mixed into the working tree (soul files, metrics JSON) — these must NEVER be staged or reverted

### Step 3 — Probe edge paths independently
The agent's tests cover what the agent thought of. Pick 1-2 error paths the plan implies but tests may miss and probe them directly with a throwaway script (host shell, real file paths — sandboxed interpreters can mangle Windows paths):
- Failed I/O on write paths (pre-create the target as a directory to force failure)
- Concurrent/duplicate trigger idempotency
- Legacy data formats hitting new code paths
Delete probe scripts when done.

### Step 4 — Fix cycle, then commit with targeted staging
After reviewer PASS: `git add <explicit file list>` — never `git add -A` in a tree with user edits. Pre-commit hooks that auto-fix formatting (isort/yapf/line endings) will fail the first commit and fix the files; just retry once. Verify post-commit: `git status --short` shows only the expected unstaged user edits, re-run one test suite to confirm hook reformatting didn't break anything.

## Tips
- Agent reports that say "hit turn limit" are RED FLAGS — the final verification step likely never ran; verify everything from scratch.
- When an agent's root-cause explanation differs from yours (e.g., "registry wipe" vs actual version-key collision), trust the failing test output over both narratives.
- Keep the approved plan file path in every delegation message so fixes stay anchored to locked semantics.
- A reviewer FAIL verdict with a specific blocker is worth more than a PASS with vague praise — fix exactly what was cited, re-run tests, and only then commit.
