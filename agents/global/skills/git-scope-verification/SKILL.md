---
name: git-scope-verification
description: Verifying that code snippets are pre-existing and not introduced by a specific feature diff using git history, blame, and diff analysis.
source: auto-generated
version: "1.0.0"
triggers:
  - scope verification
  - pre-existing code
  - out-of-scope
  - adjudication
generated_by: coder
generated_from_task: "Verification of orchestrator adjudication that POLISH findings are pre-existing code not in bridge feature diff."
---

## Goal
Determine whether a cited code snippet was added or modified by a specific git diff range, confirming it is out-of-scope for that feature.

## Procedure

### Step 1 — Check if the string appears in the diff
Run `git diff <base>..<head>` and search for the problematic strings (e.g., `LLM_CONFIG_KEYS`, `print(`). If they do not appear at all, the snippet was likely not touched. If they do appear, inspect whether they are in added (`+`) or modified lines.

### Step 2 — Verify pre-existence via git blame
For each specific line number, run:
```bash
git blame -L <start>,<end> <path>
```
Note the commit hashes and dates. If the commits are **ancestors** of the base commit (`git merge-base --is-ancestor <commit> <base>` returns true), the code existed before the feature.

### Step 3 — Confirm the file existed at base commit
Run:
```bash
git ls-tree -r <base> --name-only | grep <filename>
```
If the file is missing, it was added by the feature and thus in-scope.

### Step 4 — Check for region-level touches
Even if a string is absent from the diff, nearby lines might have been shifted or the file could have been heavily refactored. Use `git diff -L <range>:<file>` (if supported) or compare line numbers between base and head to ensure no structural changes moved the snippet into a new context.

### Step 5 — Cross-check with extracted base/head snapshots
For absolute certainty, extract the file at both commits:
```bash
git show <base>:<path> > base_snapshot.txt
git show <head>:<path> > head_snapshot.txt
```
Then compare the specific lines to confirm they are identical.

## Tips
- Always prefer `git blame` over guessing from current code; line numbers can shift.
- A string absent from the diff is strong evidence of no change, but verify that the file itself wasn't newly added.
- For imports, check if the symbol is used anywhere in the file (not just the import line) to see if the feature could have made it unused by removing the last usage.
- Use `git merge-base --is-ancestor` to definitively place a commit relative to the base.
