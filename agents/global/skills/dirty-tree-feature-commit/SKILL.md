---
name: dirty-tree-feature-commit
description: Commit a single feature cleanly from a working tree that contains other unrelated uncommitted changes — staged-file isolation, pre-commit hook fixes, and the no-revert discipline.
source: auto-generated
version: "1.0.0"
triggers:
  - "git commit"
  - "dirty working tree"
  - "unrelated changes"
  - "pre-commit hooks"
  - "partial commit"
generated_by: orchestrator
generated_from_task: "Commit propose_skill similarity gate while preserving unrelated uncommitted settings changes in AgentCascade"
---

## Goal
Ship one reviewed feature as a clean, minimal commit even when the working tree carries other people's (or earlier sessions') uncommitted edits.

## Procedure

### Step 1 — Map the dirty tree BEFORE touching anything
Run `git status --short` and `git diff <feature-files>` on exactly the files your feature touched. Classify every changed file: feature-owned vs pre-existing unrelated. Expect surprises — implementers may have drifted whitespace or left stray edits in "their" files.

### Step 2 — Never revert a shared file to isolate a hunk
`git checkout -- <file>` discards ALL uncommitted changes in that file and is usually auto-rejected for good reason. If a feature-owned file also contains unrelated pre-existing edits (e.g., a threshold someone else changed), leave the unrelated edit in place or note it; do not destroy it to make the diff prettier.

### Step 3 — Strip incidental drift from your own hunks
If your implementer incidentally re-aligned an unrelated line inside a feature file, restore that single line to its HEAD form with a surgical `edit_file` (verify exact whitespace first via `read_file`; heuristic match mode helps). Re-run `git diff <file>` until only intended changes remain.

### Step 4 — Stage by explicit path list only
`git add file1 file2 ...` — never `-A`, never globs. This keeps pre-existing unrelated edits and untracked files out of the commit while still letting hooks run on staged content.

### Step 5 — Expect the first commit to fail on auto-fixing hooks
pre-commit hooks like `double-quote-string-fixer` or `mixed-line-ending` MODIFY working-tree files and fail the commit, then restore unstaged changes from their stash. The fix is mechanical: inspect `git status` (look for `MM` entries), review the unstaged hook modifications with `git diff <file>`, re-`git add` the same explicit paths, and re-run `git commit`. Do not fight the hooks by disabling them.

### Step 6 — Verify after commit
Re-run the feature's tests once more (hook rewrites can touch committed lines) and confirm `git status` still shows exactly the pre-existing unrelated changes you left behind.

## Tips
- A commented-out debug line or a config value change in a non-feature file is NOT yours to commit — leave it, mention it in your final report.
- Pre-commit stashing of unstaged files is normal noise (`[INFO] Stashing unstaged files...`), not data loss; the restore line confirms it round-tripped.
- Version-bump hooks may fire on commit (e.g., `version bumped: 0.1.78 -> 0.1.79`) — check the hook output so your report reflects what actually landed.
- Keep the rejection-message/commit-message discipline: the commit message should describe only what is in the commit, not what was in the working tree.
