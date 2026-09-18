---
name: precommit-formatter-conflict-loop
description: Diagnose and fix pre-commit formatter hooks (isort/yapf/black) that rewrite the same file on every commit attempt and never converge, blocking all commits
source: auto-generated
version: "1.0.0"
triggers:
  - "pre-commit loop"
  - "isort yapf conflict"
  - "commit keeps failing formatter"
  - "hook rewrites file every commit"
generated_by: orchestrator
generated_from_task: "Auto-skill in-loop trigger commit blocked for ~15 attempts by isort/yapf infinite rewrite loop on agent_instance.py"
---

## Goal

Unblock commits when two pre-commit formatters fight over the same file and can never agree.

## Procedure

### Step 1 — Recognize the signature
The hook output shows the SAME file being "Fixed" by a formatter on every attempt, and the commit aborts each time with no progress:
```
isort................................................Failed
- files were modified by this hook
Fixing .../some_file.py
yapf.................................................Failed
- files were modified by this hook
```
If retrying after re-staging produces the identical output, it's a conflict loop — not a one-off fix. Stop retrying immediately; more retries will never converge.

### Step 2 — Confirm the conflict is real
Run `git diff <file>` right after a failed commit. If the working tree now differs from the staged content in exactly the regions the formatters touch (import blocks, line wrapping), and re-running the same formatter produces a different layout than the other one, the two tools disagree on formatting.

### Step 3 — Fix at the source (config), not the symptom
Do NOT:
- Use `--no-verify` to bypass (masks the problem, loses all hygiene hooks)
- Stash/pop dance around it (fragile, conflicts with hook's own stash cycle)
- Manually reformat and retry (the hook will re-rewrite on next commit)

DO: Edit `.pre-commit-config.yaml` to remove one of the conflicting formatters. Keep the one whose style matches the existing codebase (check a few files' import layout). Add a comment explaining WHY it was removed so nobody re-adds it blindly:
```yaml
# NOTE: isort + yapf were removed — they produce conflicting import layouts
# and the hooks rewrote the same file on every commit without converging.
```

### Step 4 — Commit the config change FIRST, then retry the real commit
The config commit itself may hit other hygiene hooks (line endings, trailing whitespace) — those are one-shot fixes; re-stage and retry once. After the config lands, the original blocked commit proceeds normally.

## Tips

- **pre-commit stashes unstaged files before running hooks.** If the working tree has unrelated modified files, the hook's stash/restore cycle can conflict with its own auto-fixes ("Stashed changes conflicted with hook auto-fixes... Rolling back fixes"). A clean tree (only staged changes) makes commits far more predictable.
- **The version-bump commit hook may sweep in staged files.** If a post-commit hook creates a version bump, check `git show --stat HEAD` — the bump commit should be 1 file (`__init__.py`). If it contains your real changes, the main commit already landed and you're fine; if not, investigate.
- **Two formatters with "compatible" args are rarely compatible.** isort `--line-length 120` and yapf `column_limit: 120` still produce different import wrapping because they use different algorithms. If a project needs both, pick ONE as the authority and configure the other to no-op on the same file ranges — or just don't use both.
- **Check for `.isort.cfg` / `[tool.isort]` in pyproject.toml / `setup.cfg`.** If isort config exists but yapf ignores it (or vice versa), that's the root cause of the divergence. Aligning configs can fix the loop without removing either hook — but removal is faster and more reliable.
- **After removing a formatter, existing files keep their mixed formatting.** That's fine — don't reformat the whole repo in the same commit. The remaining hygiene hooks (trailing whitespace, EOF, line endings) still protect new changes.
