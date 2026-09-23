---
name: precommit-hook-commit-retry
description: How to commit in an AgentCascade (or similar) repo where pre-commit hooks auto-modify staged files, on a Windows host — the first git commit aborts with rc=1 but has already fixed the tree, so re-stage and re-commit; plus Windows shell_cmd path/pipe gotchas.
source: auto-generated
version: "1.0.0"
triggers:
  - "pre-commit"
  - "commit failed rc 1"
  - "mixed line ending"
  - "double-quote-string-fixer"
  - "CRLF will be replaced by LF"
  - "git commit aborted hooks"
generated_by: orchestrator
generated_from_task: "Committed fence-preservation fix in AgentCascade; pre-commit hooks (mixed-line-ending, double-quote-string-fixer) auto-modified files and aborted the first git commit."
---

## Goal
Commit cleanly in a repo whose pre-commit hooks auto-reformat staged files, on a Windows host — without misreading an aborted-but-fixed first commit as a real failure.

## Procedure

### Step 1 — Expect the two-pass commit
Hooks like `mixed-line-ending`, `double-quote-string-fixer`, `trim-trailing-whitespace` STASH unstaged changes, modify your staged files, then restore. If they changed anything, the hook exits non-zero and **git aborts the commit** — but the fixes are now applied in-tree (unstaged). This is normal, not a bug.

```
[INFO] Stashing unstaged files to .../pre-commit/patchXXXX.
fix double quoted strings....Failed   # "files were modified by this hook"
mixed line ending..............Failed  # "fixed mixed line endings"
[INFO] Restored changes from .../pre-commit/patchXXXX.
```

### Step 2 — Re-stage the SAME files and commit again
Do NOT use `git add -A` / wildcards (scope creep + unrelated unstaged noise). Re-add exactly the files you staged, then re-run the identical `git commit`. The second pass passes all hooks because the tree is already clean.

```
git add agent_cascade/tools/custom/file_ops.py tests/test_x.py
git commit -m "..."   # now: all hooks Passed
```

### Step 3 — Confirm success signals
Success = "all hooks Passed" AND a `[branch <sha>]` line. AgentCascade also prints `version bumped: X.Y.Z -> X.Y.(Z+1) (patch)` automatically on commit — that's expected, not an error.

## Tips
- **rc=1 ≠ your code is broken.** If the only "failures" are auto-fixer hooks (`double-quote-string-fixer`, `mixed-line-ending`), the fix is mechanical; just re-stage + re-commit. Only treat rc=1 as a real problem if a *check* hook (e.g. a linter that can't auto-fix, or a test) fails.
- **Windows: don't use pipes in shell_cmd.** `| tail`, `| head` are auto-rejected ("not available on Windows... redundant"). Run the base command; output is truncated with spillover automatically. Use read_file/grep for targeted extraction.
- **Windows: pass `cwd` param, not `cd /n/...`.** POSIX-style `cd /n/work/...` fails with "The system cannot find the path specified." Use the tool's `cwd` parameter with the real Windows path (e.g. `N:\work\WD\AgentCascade`).
- **CRLF warnings are harmless.** `warning: in the working copy ... CRLF will be replaced by LF` is informational; the `mixed-line-ending` hook handles it on first pass.
- Verify tests still pass after the hook auto-edits (hooks can touch your new test file's quotes/line-endings) before relying on the commit.
