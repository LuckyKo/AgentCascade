---
name: dirty-tree-feature-commit
description: Git commit scoping discipline for a dirty working tree — inventory all uncommitted changes, attribute each to your session vs the user, stage only your files by explicit path, and never revert pre-existing user edits you didn't make.
source: auto-generated
version: "1.0.0"
triggers:
  - "git commit"
  - "working tree"
  - "uncommitted changes"
  - "commit scoping"
  - "git checkout -- file"
  - "revert stray change"
  - "git add"
generated_by: orchestrator
generated_from_task: "Git commit of the heuristic edit_file indentation fix in a dirty working tree that also contained the user's own unrelated uncommitted changes; I wrongly reverted them as 'stray' during commit scoping and had to restore them after the user corrected me."
---

## Goal
Land a scoped git commit for your reviewed change without destroying or disturbing the user's own uncommitted work in the same working tree.

## Procedure
### Step 1 — Inventory the dirty tree BEFORE staging
Run `git status --short` + `git diff --stat` (and `git diff <file>` for anything outside your task's file list). Attribute every change: **mine** (produced by this session or its sub-agents — verify via their reports/logs) vs **pre-existing user work**.

### Step 2 — Never revert changes you didn't make
A change in a file outside your task scope is NOT automatically "stray noise" — it may be the user's intentional, uncommitted work (e.g., a commented-out noisy debug log). `git checkout -- <file>` on it destroys their intent. If it blocks nothing, simply DON'T stage it; leave it in the working tree. Committing while unrelated changes sit unstaged is normal and fine.

### Step 3 — Stage explicitly by path
`git add <your-file-1> <your-file-2>` — never `git add -A`. Verify with `git diff --cached --stat` that only your files are staged before committing.

### Step 4 — When in doubt, ask
If a pre-existing change is in the SAME file you're modifying (so it would ride along in your commit), ask the user whether to include or exclude it instead of guessing.

## Tips
- "Stray" is a valid label only for changes YOUR sub-agents produced; user-attributed changes are protected.
- After committing, re-run `git status --short` and confirm the user's pre-existing edits are still present and untouched.
- Pre-commit hooks that auto-modify staged files (line-ending fixers etc.) are a different case — see `precommit-hook-commit-retry`; hook-driven changes to YOUR files can be re-staged safely, user changes in other files cannot.
