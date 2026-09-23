---
name: baseline-differential-failure-verification
description: Prove which test failures a change actually introduced by stashing the task's files and re-running at baseline, separating real regressions from pre-existing AND environmental (shared-state pollution) failures.
source: auto-generated
version: "1.0.0"
triggers:
  - "pre-existing failures"
  - "verify test failures are not mine"
  - "baseline comparison"
  - "regression triage"
  - "test suite fails after change"
  - "stash and re-run baseline"
generated_by: orchestrator
generated_from_task: "Skill candidate registration flow fix — coder claimed all 15 failures pre-existing; had to prove which were real regressions vs environmental pollution of a shared metrics file."
---

## Goal
When a change lands in a suite with N failures, determine EXACTLY which failures the change introduced vs which are pre-existing or environmental — so you never commit on an unverified "it's all pre-existing" claim and never waste cycles fixing someone else's bug.

## Why this matters
Coders/reviewers frequently report "N failures, all pre-existing." That framing is often HALF right: some may be genuine regressions from the change (e.g. removing a default-rating seed breaks `test_new_skill_gets_initial_5_0`), while others are environmental (a shared file clobbered by an earlier suite in the same process). Accepting the claim at face value ships broken code or chases phantom bugs.

## Procedure

### Step 1 — Capture the "with change" failure set
Run the full relevant command and record every failing test name + count:
```bash
python -m pytest tests/test_a.py tests/test_b.py -n 0 -q 2>&1 | tail -40
```
Use `-n 0` (serial) first — xdist can mask or fabricate order-dependent failures.

### Step 2 — Establish the baseline by stashing ONLY your task files
Do NOT stash the whole tree (it may carry unrelated work you must preserve). Stage nothing; use `git stash push -- <your-task-files>`:
```bash
git stash push -m "baseline-check" agent_cascade/skills/manager.py tests/test_skill_generation.py
python -m pytest tests/test_a.py tests/test_b.py -n 0 -q   # baseline failure set
git stash pop
```
Confirm the pop restored your files (`git status`). Now you have two comparable sets.

### Step 3 — Diff the two failure sets
Classify each failing test:
- **In BOTH sets** → pre-existing (not yours). Leave it; note it.
- **Only in "with change"** → REGRESSION introduced by your change. Must fix before commit.
- **Only in baseline** → your change fixed it (good).

### Step 4 — Check for ENVIRONMENTAL pollution (the trap that hides real regressions)
Some "pre-existing" failures are actually order-dependent: an earlier suite mutates shared state (a gitignored production file, a global registry, a temp dir) that later suites read. Detect it:
- Identify the shared artifact (e.g. `agents/global/skills-metrics.json`) and inspect its on-disk state AFTER the run vs BEFORE (count entries, check specific keys/statuses).
- Re-run ONLY the later suite in ISOLATION (fresh process, clean artifact). If it passes alone but failed after the earlier suite → cross-suite pollution, environmental, not your change's fault.
- Reset the polluted artifact to a known-clean state before final verification so your commit is judged fairly.

### Step 5 — Re-run the hermetic subset for the commit gate
Run only YOUR tests + the directly-affected ones (not the whole polluted suite) and confirm they pass:
```bash
python -m pytest tests/test_a.py::TestNewFeature <directly-affected-tests> -n 0 -q
```

## Tips
- A subset of "pre-existing" may be newly-broken-by-your-change. ALWAYS split the set; never trust a single aggregate number.
- If a test fails only when run after another suite, it's order-dependent — usually shared mutable state. Grep for where that state is written and whether the writing tests redirect it to a tmp path (hermetic) or write to the real file (polluting).
- Log environmental pollution bugs to project memory / bug tracker per policy; don't silently fix them inline as part of an unrelated task.
- Related: [[reported-regression-self-consistency-check]] (validate the report's arithmetic), [[subagent-deliverable-verification]] (re-run tests yourself, treat agent reports as claims). This skill is the baseline-stash differential that separates regressions from pre-existing AND environmental causes.
