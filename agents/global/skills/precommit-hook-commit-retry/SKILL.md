---
name: precommit-hook-commit-retry
description: Handle pre-commit hook failures (line-ending, quote-fixer, whitespace) that modify files during commit — re-stage and retry without losing changes; re-run tests if the quote-fixer touched test files or a golden fixture.
source: auto-generated
version: 1.1.0
triggers:
  - "pre-commit hook failed"
  - "mixed line ending"
  - "double-quote-string-fixer"
  - "commit failed hook"
  - "re-stage after hook"
  - "quote-fixer modified test file"
  - "CRLF will be replaced by LF"
  - "git commit aborted hooks"
generated_by: orchestrator
generated_from_task: "13-bug remediation — 6 batch commits each hit the double-quote-string-fixer + mixed-line-ending hooks modifying new test files; re-ran affected tests (and the byte-equivalence gate when a golden JSON was touched) before re-committing"
---

## Goal
Commit cleanly when pre-commit hooks modify files (line endings, quote style, trailing whitespace) and the first commit attempt fails — without shipping a hook-mutated test that now asserts something different.

## Procedure

### Step 1 — Recognize the hook-modified-file pattern
When `git commit` exits with code 1 and stderr shows a hook like:
```
- hook id: mixed-line-ending
agent_cascade/operation_manager/shell.py: fixed mixed line endings
```
or
```
- hook id: double-quote-string-fixer
Fixing strings in tests/test_something.py
```
the hook **modified the working copy** but did NOT commit. The staged version is stale; the working copy has the fix.

### Step 2 — If the quote-fixer touched TEST files (or a golden fixture), re-run those tests before re-committing
The `double-quote-string-fixer` changes only quote style (`"..."` → `'...'`), so semantics are normally unchanged. Re-run the affected test file(s) as cheap insurance before re-staging:
```bash
python -m pytest <modified-test-file> -q
```
(Do NOT strip `pytest.ini` addopts with `-o addopts=""` — that drops `--timeout=60`, so a hanging test hangs forever.) Be careful with byte-exact golden fixtures or tests that compare raw file text, where even a quote-style change is a content change. If a test now fails, inspect the diff before committing. The `mixed-line-ending` / `trailing-whitespace` hooks do NOT change semantics and need no re-run.

### Step 3 — Re-stage the exact files and retry
```bash
git add <file1> <file2> ...
git commit -m "<same message>"
```
The hook runs again, sees the already-fixed content, passes, and the commit lands.

### Step 4 — Verify the commit landed
```bash
git log --oneline -3
```
Confirm the new commit hash appears. If `scripts/install_version_hook.py` was run, a POST-COMMIT hook (running `scripts/bump_version.py --post-commit`) prints `version bumped: X.Y.Z -> X.Y.(Z+1)` and creates a follow-up chore commit — expected, not an error.

## Tips
- **Never use `git add .` or `git add -A`** to re-stage — it picks up unrelated unstaged changes from other work in progress. Always name the exact files.
- **The retry is safe**: pre-commit hooks are idempotent. If the hook already fixed the line endings, the second run sees clean content and passes immediately.
- **Multiple hooks can fire sequentially** (e.g. trailing-whitespace → mixed-line-ending → double-quote-fixer). Each one that modifies files requires a re-stage + retry. You may need 2-3 attempts for a file with multiple issues.
- **CRLF warning** (`warning: in the working copy of 'X', CRLF will be replaced by LF`) is informational, not a failure — it does not block the commit.
- If the hook modifies a file you did NOT intend to change (e.g. it reformats an unrelated string), check `git diff` before re-staging to make sure you're not committing unintended changes.
- **Scope-isolated staging** (naming exact files) is what makes the retry safe — if you had staged everything, the hook's modifications to unrelated files would get committed too.
- **Golden/fixture files**: on a dirty tree the quote-fixer/line-ending hooks can also touch a golden JSON (byte-exact expected strings). A changed file line-ending is harmless to JSON parsing, but re-run the byte-equivalence gate afterward to confirm the asserted content is intact before committing.
