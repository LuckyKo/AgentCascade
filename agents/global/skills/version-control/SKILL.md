---
name: version-control
description: Git operations — commit message conventions, branch management, diff analysis, merge conflict resolution, and rebase strategies.
source: manual
version: "1.0.0"
triggers:
  - "git"
  - "commit message"
  - "branch"
  - "merge conflict"
  - "rebase"
  - "diff"
  - "version control"
---

## Commit messages (Conventional Commits)

Format `<type>: <short description>`: `feat`, `fix`, `refactor` (no behavior change), `docs`, `test`, `chore`, `perf`.
Multi-line: subject + blank line + bullet list of what/why + optional `Closes #N`. One logical change per commit.

## Branching

- Name `<type>/<short-description>` (`feat/user-auth`, `fix/login-timeout`, `hotfix/crash`).
- Lifecycle: create from `main` → small commits → rebase onto latest `main` before push → PR → squash/rebase-merge → delete branch.
```bash
git checkout -b feat/add-search main
git fetch origin && git rebase origin/main   # keep history linear
git push -u origin feat/add-search
git branch --merged main | grep -v '^\*\|main\|develop' | xargs git branch -d
```

## Diff / review before commit

`git status`; `git diff --cached` (staged); `git diff` (unstaged); `git diff main..HEAD`; `git diff --name-only HEAD~3`; `git diff --word-diff`.
Selective staging: `git add -p` (hunks), `git add <files>`, `git restore --staged <file>` (unstage).

## Merge conflicts

1. Scope: `git diff --name-only --diff-filter=U`; find markers with `grep -n "<<<<<<<\|=======\|>>>>>>>" file`.
2. Both sides: `git diff --base|--ours|--theirs file` (ancestor vs yours / theirs).
3. Resolve: edit out the three marker lines; keep both if compatible, pick one if same logic; `git mergetool --tool=vimdiff` for complex cases.
4. Finish: `git add file`; `git diff --check` (no markers left); `git commit` (merge) or `git rebase --continue`.

## Rebase

- Interactive cleanup: `git rebase -i HEAD~N`; ops `pick/squash/reword/edit/drop`.
- **Golden rule:** never rebase commits already pushed to a shared branch.

**Rebase vs Merge:**

| Approach | When to use | History shape |
|---|---|---|
| `git merge` | Shared branches, long-lived features | Branching tree (full history) |
| `git rebase` | Local feature branches before PR | Linear (cleaner log) |

## Quick reference

```bash
git checkout -- file.py            # discard unstaged changes
git reset HEAD~1                   # uncommit last, keep staged
git reset --hard HEAD~1            # remove last commit entirely ⚠️
git add f && git commit --amend --no-edit   # amend last commit
git log --oneline --graph --all    # branch graph
git blame file.py                  # per-line authorship
git stash push -m "WIP" && git stash pop
git tag -a v1.2.0 -m "..." && git push origin v1.2.0
```

## Key configuration values

| Parameter | Recommended | Why |
|---|---|---|
| Commit frequency | Every logical unit of work | Easier to bisect, review, revert |
| Rebase window | `HEAD~5`–`HEAD~10` before PR | Clean history without over-rewriting |
| Branch lifespan | < 2 weeks ideally | Cuts merge-conflict probability sharply |

## What NOT to do

- Don't commit unrelated changes together; don't `push -f` to shared branches; always check `git status` before committing; don't skip pre-commit hooks without reason; rebase onto main before merging (no stray merge commits in feature history).
