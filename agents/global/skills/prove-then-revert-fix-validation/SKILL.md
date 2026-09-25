---
name: prove-then-revert-fix-validation
description: Validate a proposed code fix end-to-end without committing it — backup, patch, run repro + regression suite, restore, verify — so the shared codebase stays clean for plan-driven implementation.
source: auto-generated
version: "1.0.0"
triggers:
  - "prove fix then revert"
  - "validate proposed fix without committing"
  - "backup restore source file"
  - "experimental patch keep codebase clean"
  - "researcher deliver plan not implementation"
generated_by: researcher
generated_from_task: "Investigate heuristic edit_file indentation flattening; prove an offset-based fix end-to-end then revert to leave the codebase clean for a coder to implement from the plan."
---

## Goal
Prove a candidate fix actually works (resolves the repro AND causes no regressions) before deciding to ship it — while leaving the shared codebase untouched so implementation can proceed from a written plan.

## Procedure

### Step 1 — Backup the source file BEFORE patching
`copy_file` the target file to a scratch name in the same repo (e.g. `_file_ops_backup.py`). Keep it until you've restored. It is your exact-restore source of truth — more reliable than re-applying the original text by hand.

### Step 2 — Apply the candidate patch
Use `edit_file`. **Gotcha:** if the region contains a Unicode em-dash (`—`) or other non-ASCII in comments, `match_mode='exact'` often fails to match even when the text looks byte-identical. Fall back to `match_mode='delete_and_insert'` with an explicit `range` (e.g. `'1192:1237'`) — it is line-addressed and immune to invisible-character mismatches.

### Step 3 — Prove correctness AND no regression
Run BOTH, not just one:
- Your minimal repro script exercising the REAL code path in a temp dir → assert the bug is gone (capture before/after outputs).
- The component's existing test suite → assert zero regressions.
On Windows, `shell_cmd` **rejects `| head` / `| tail`** pipe stages — run the base command; AgentCascade auto-truncates output (use `read_file`/`grep` for targeted extraction). Long pytest runs launch in the background; manage with `__wait` / `__status`.

### Step 4 — Restore and VERIFY restoration
`copy_file` the backup back over the source. Then verify rather than assume:
1. Read a distinctive line that changed (confirm the original text is back).
2. Re-run the baseline test file to confirm it still passes in the restored state.
Delete the scratch backup + repro scripts afterward so the tree is clean.

## Tips
- This is DIG-phase discipline: the researcher proves the fix and delivers a plan; the coder implements from the plan. Don't leave an unreviewed experimental patch in a shared tree — it surprises the orchestrator and risks clobbering a concurrent edit.
- The restore-verify step catches silent copy failures (e.g., a path resolving to the wrong Docker mount). A one-line read + baseline run is cheap insurance; never trust that "the copy succeeded" alone.
- Complementary, opposite directions: `regression-test-revert-proof` proves a test CATCHES the bug (revert fix → test fails); this skill proves a fix RESOLVES it (apply fix → repro passes). Use both for a high-confidence handoff.
- If you only need to show the repro flips, Step 3 can be just the repro script; but for a plan that will actually be implemented, run the component's suite too so the plan's "no regression" claim is evidence-backed, not assumed.
