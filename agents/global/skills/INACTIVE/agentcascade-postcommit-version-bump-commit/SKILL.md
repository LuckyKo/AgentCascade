---
name: agentcascade-postcommit-version-bump-commit
description: AgentCascade git gotcha — a post-commit hook auto-creates a version-bump commit on top of yours, so the hash printed right after `git commit` is NOT HEAD. How to report the correct hash and handle the mixed-line-ending pre-commit failure that often accompanies it.
source: auto-generated
version: "1.0.0"
triggers:
  - "post-commit version bump"
  - "commit hash not HEAD"
  - "chore(version) bump commit"
  - "mixed line ending hook failed"
  - "CRLF commit fails pre-commit"
  - "report commit hash after git commit"
generated_by: coder
generated_from_task: "Commit regression fixes in AgentCascade; post-commit hook auto-bumped version creating a second commit on top, and mixed-line-ending hook failed the first attempt."
---

## Goal
Commit cleanly in `N:\work\WD\AgentCascade` and report the CORRECT commit hash — accounting for (a) a `post-commit` hook that auto-creates a `chore(version): bump to X.Y.Z` commit on top of yours, and (b) the `mixed-line-ending` pre-commit hook that fails CRLF working copies.

## The two gotchas

### Gotcha 1 — post-commit hook creates an EXTRA commit (the hash you print is stale)
AgentCascade has a **post-commit** hook that bumps `agent_cascade/__init__.py` (`0.1.121 -> 0.1.122`) and commits it as `chore(version): bump to ...`. This runs AFTER your commit lands, so:
- The short hash in the `[master <short>]` line of `git commit` output is **your** commit — but it is no longer HEAD.
- `git rev-parse HEAD` right after returns the **version-bump** commit, not yours.

Do NOT report `rev-parse HEAD` as "the commit" without checking. Your fix commit is the **parent** of the auto-bump commit.

### Gotcha 2 — mixed-line-ending pre-commit hook fails CRLF files
With `core.autocrlf=true`, a working copy can carry CRLF while HEAD is LF. The `mixed line ending` pre-commit hook normalizes CRLF→LF in place and **FAILS the commit if it had to fix anything** (first attempt dies, exit 1). Recovery: re-`git add` the same files, then recommit — the second attempt passes because the working copy is now clean LF.

## Procedure

### Step 1 — Stage ONLY the intended files
```bash
cd N:\work\WD\AgentCascade
git status --short            # note unrelated modified/untracked files; do NOT stage them
git add <file1> <file2> ...   # explicit paths, no wildcards
git --no-pager diff --cached --stat   # confirm exactly the intended files are staged
```

### Step 2 — Commit with a message file (preserve special chars / multi-line body)
Write the message to a temp file and use `git commit -F <file>` to avoid shell-quoting corruption of `→`, backticks, etc.
```bash
git commit -F /path/to/msg.txt
```

### Step 3 — If `mixed line ending` fails (exit 1)
The hook already normalized the files in place. Just re-stage and retry:
```bash
git add <file1> <file2> ...   # refresh index entries (also clears a transient 'MM' status)
git commit -F /path/to/msg.txt   # now passes; all hooks green
```

### Step 4 — Report the CORRECT hash (account for the auto-bump)
```bash
git --no-pager log --oneline -3     # you will see: <auto-bump> on top, YOUR commit below it
git rev-parse HEAD                  # = the auto-bump commit (NOT yours)
git rev-parse <your-short-hash>     # resolve YOUR fix commit to full hash
git --no-pager show --stat --oneline <your-short-hash>   # confirm it has exactly your files
```
Report **your** commit's full hash, and explicitly note that HEAD is the auto-created version-bump commit sitting on top (so a tag/checkout at HEAD includes both).

## Tips
- The `[WARNING] Unstaged files detected / Stashing unstaged files ... Restored changes` lines around every commit are normal — pre-commit stashes unrelated unstaged work, runs hooks, restores it. Not an error.
- `core.autocrlf=true` + the hook means: if you ever see `warning: in the working copy of 'X', CRLF will be replaced by LF`, expect a first-commit failure and budget for the re-add+recommit cycle.
- Do NOT use `--no-verify` to skip the line-ending hook — it exists to keep the repo LF-consistent; just let it normalize and recommit.
- Windows cmd: no `for i in` loops, no `| head`/`| tail` pipes (shell_cmd auto-rejects them). Use `for /L %i in (1,1,N) do ...` or run commands separately.
- Related project memory: `.agent_lessons/pre-commit-flake8-whole-file-pipeline.md` documents the full pre-commit pipeline + the mixed-line-ending fail-once behavior.
