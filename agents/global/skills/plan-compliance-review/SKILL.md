---
name: plan-compliance-review
description: Verify that implementation follows an approved plan verbatim, checking edit lists, test plans, and tracking deviations.
source: auto-generated
version: "1.0.0"
triggers:
  - "plan compliance"
  - "verify implementation against plan"
  - "dedup refactor review"
  - "edit list verification"
generated_by: reviewer
generated_from_task: "Code review of server-restart dedup + Windows port-bind race fix against approved plan."
---

## Goal

Provide a structured method to review code changes against an approved implementation plan, ensuring verbatim adherence to the edit list and test plan while identifying deviations and quality issues.

## Procedure

### Step 1 — Read the Plan and Changed Files
- Load the approved plan document (e.g., `plans/...-plan.md`).
- Read every file listed in the plan’s §5 edit list and any additional changed files.
- Verify that each described change is present and matches the specification.

### Step 2 — Check for Duplicated Logic
- Search the codebase for duplicated patterns (e.g., re-exec/relaunch logic, bind-retry loops).
- Confirm there is exactly one copy of each deduplicated component (e.g., `restart_server_process` in `server_restart.py`).
- Ensure old implementations are fully removed (e.g., inline `os.execl`, Popen blocks, dead EADDRINUSE handlers).

### Step 3 — Verify Behavior Preservation
- Confirm that broadcast/notice semantics remain unchanged (caller responsibility).
- Check that token gates, error handling, and signal-handler interplay match the plan.
- Validate edge cases: `os._exit(0)` placement, Popen failure propagation, EADDRINUSE detection on both platforms.

### Step 4 — Evaluate Test Quality
- Ensure tests are non-vacuous with concrete assertions.
- Verify hermeticity (mocking, patching) and absence of flakiness risks.
- Confirm cleanup/restore correctness (sockets closed, threads joined).

### Step 5 — Report Findings with Severity
- Use severity ratings: 🔴 Critical, 🟠 Major, 🟡 Minor, 🔵 Nit.
- For each finding, cite exact file/line and provide a concrete fix.
- List all required changes (🔴/🟠) before the final verdict.

### Step 6 — Issue Verdict
- **PASS**: No 🔴 or 🟠 issues; implementation genuinely verified.
- **NEEDS WORK**: ≥1 🟠 (or risky cluster of 🟡), no 🔴.
- **FAIL**: ≥1 🔴, or fundamentally wrong / unverifiable.

## Tips

- Always read the actual source; never review blind from a description.
- Use `grep` to detect residual duplicated logic or dead code.
- When the plan says “no change” but code changed, note the deviation (could be an improvement or oversight).
- If a plan risk (e.g., Risk #3) is not fully addressed, flag it even if tests pass.
- Keep the review scoped to the plan; use other skills for whole-repo bloat passes.
