---
name: version-control
description: Git operations including commit messages, branch management, diff analysis, merge conflict resolution, and rebase strategies
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

## Goal

Manage source code versions effectively using Git with clear commit messages, organized branching strategies, clean history through rebasing, and systematic merge conflict resolution.

## Procedure

### Step 1 — Commit message conventions

**Format: `<type>: <short description>` (Conventional Commits)**

| Type | Purpose | Example |
|---|---|---|
| `feat` | New feature | `feat: add user profile endpoint` |
| `fix` | Bug fix | `fix: handle null response from API` |
| `refactor` | Code restructuring (no behavior change) | `refactor: extract validation logic to separate module` |
| `docs` | Documentation only | `docs: update API authentication guide` |
| `test` | Test additions or changes | `test: add edge cases for CSV parser` |
| `chore` | Maintenance, deps, config | `chore: upgrade httpx to 0.27` |
| `perf` | Performance improvement | `perf: cache database connection pool` |

**Multi-line commit messages:**
```bash
git commit -m "feat: add retry logic for failed API calls

- Implement exponential backoff with jitter (max 5 retries)
- Add configurable timeout per endpoint
- Log retry attempts at DEBUG level

Closes #142"
```

### Step 2 — Branch management strategy

**Branch naming convention:** `<type>/<short-description>`
- `feat/user-auth` — New feature work
- `fix/login-timeout` — Bug fix
- `refactor/db-layer` — Refactoring
- `hotfix/production-crash` — Urgent production fixes

```bash
# Create and switch to a new feature branch from main
git checkout -b feat/add-search main

# After work is done, sync with upstream before PR
git fetch origin
git rebase origin/main   # Keep history linear (see Step 5)

# Push the branch
git push -u origin feat/add-search

# Clean up merged branches locally
git branch --merged main | grep -v '^\*\|main\|develop' | xargs git branch -d
```

**Branch lifecycle:**
1. Create from `main` (or current stable branch)
2. Work in small, logical commits
3. Rebase onto latest `main` before pushing
4. Open PR/MR for review
5. Squash-merge or rebase-merge into `main`
6. Delete the feature branch

### Step 3 — Diff analysis and review preparation

**Review your changes before committing:**
```bash
# See what files changed (staged + unstaged)
git status

# Diff of staged changes (what will be committed)
git diff --cached

# Diff of unstaged changes (not yet added to staging)
git diff

# Diff against a specific commit/branch
git diff main..HEAD

# Show only filenames that changed
git diff --name-only HEAD~3  # Last 3 commits

# Word-level diff for detailed review
git diff --word-diff

# Visual diff with color and context
git diff -U5 --color=always
```

**Staging selectively (partial commits):**
```bash
# Stage specific hunks interactively
git add -p

# Stage only certain files
git add src/module.py tests/test_module.py

# Unstage a file you added by mistake
git restore --staged src/accidental_file.py
```

### Step 4 — Merge conflict resolution

**Systematic approach to resolving conflicts:**

1. **Identify the conflict scope:**
   ```bash
   # See which files have conflicts
   git diff --name-only --diff-filter=U

   # View conflict markers in a file
   cat src/config.py | grep -n "<<<<<<<\|=======\|>>>>>>>"
   ```

2. **Understand both sides:**
   ```bash
   # See what changed on YOUR branch vs theirs
   git diff --base src/config.py   # Common ancestor vs your changes
   git diff --ours src/config.py   # Your version vs merged result
   git diff --theirs src/config.py # Their version vs merged result
   ```

3. **Resolve conflicts:**
   - Edit the file directly, removing `<<<<<<<`, `=======`, `>>>>>>>` markers
   - Keep both changes if they're compatible (e.g., different functions)
   - Choose one side if they conflict on the same logic
   - Use merge tools for complex cases: `git mergetool --tool=vimdiff`

4. **Verify and complete:**
   ```bash
   # After editing, stage the resolved file
   git add src/config.py

   # Check no unresolved conflicts remain
   git diff --check

   # Complete the merge/rebase
   git commit     # For merges
   git rebase --continue  # During rebases
   ```

**Conflict prevention tips:**
- Rebase frequently onto `main` to catch conflicts early (smaller diffs)
- Communicate with teammates about overlapping work areas
- Make atomic, focused changes rather than large sweeping edits

### Step 5 — Rebase strategies

**Interactive rebase for clean history:**
```bash
# Clean up last 4 commits interactively
git rebase -i HEAD~4

# Common operations:
# pick   → keep commit as-is
# squash → merge into previous commit (combine messages)
# reword → change the commit message
# edit   → stop to amend files or split commits
# drop   → remove the commit entirely
```

**Rebase vs. Merge:**
| Approach | When to use | History shape |
|---|---|---|
| `git merge` | Shared branches, long-lived features | Branching tree (shows full history) |
| `git rebase` | Local feature branches before PR | Linear history (cleaner log) |

**Golden rule:** Never rebase commits you've already pushed to a shared branch.

### Step 6 — Useful Git commands reference

```bash
# Undo changes
git checkout -- file.py           # Discard unstaged changes in a file
git reset HEAD~1                  # Uncommit last commit (keep changes staged)
git reset --hard HEAD~1          # Remove last commit entirely ⚠️

# Amend the last commit (add forgotten files or fix message)
git add forgotten_file.py
git commit --amend --no-edit

# View history
git log --oneline --graph --all  # Visual branch graph
git log -p -n5                   # Last 5 commits with patches
git blame src/file.py            # Who changed each line and when

# Stash work-in-progress
git stash push -m "WIP: half-done feature"
git stash pop                    # Restore most recent stash

# Tag releases
git tag -a v1.2.0 -m "Release 1.2.0"
git push origin v1.2.0
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| Commit frequency | Every logical unit of work | Easier to bisect, review, and revert |
| Rebase window | `HEAD~5` to `HEAD~10` before PR | Keeps history clean without too much rewriting |
| Branch lifespan | < 2 weeks ideally | Reduces merge conflict probability dramatically |

## What NOT to do

- Do not commit unrelated changes together — one logical change per commit
- Do not force-push (`git push -f`) to shared branches — it rewrites others' history
- Do not ignore `git status` before committing — review what you're about to commit
- Do not use `--no-verify` to skip pre-commit hooks without reason
- Do not leave merge commits in feature branch history — rebase onto main first