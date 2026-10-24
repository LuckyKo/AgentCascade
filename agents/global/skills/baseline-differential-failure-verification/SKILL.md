---
name: baseline-differential-failure-verification
description: Prove which test failures a change actually introduced by stashing the task's files and re-running at baseline, separating real regressions from pre-existing AND environmental (shared-state pollution) failures.
source: auto-generated
version: "1.0.1"
triggers:
  - "regression verification"
  - "baseline test comparison"
  - "which failures did my change cause"
  - "pre-existing test failure"
  - "flaky under xdist load"
generated_by: coder
generated_from_task: "Full regression triage for todo 135 slow-streaming fix — 3 watchdog failures appeared only in the full-suite xdist run but passed standalone (-n0 and isolated xdist)"
---

## Goal
Attribute test failures correctly after a change: distinguish (a) regressions your change introduced, (b) pre-existing flakes, and (c) environmental failures that only appear under full-suite parallel load.

## Procedure
### Step 1 — Establish the baseline BEFORE changing anything
Run the named test files (and ideally a full-suite collection count) at HEAD and record output verbatim. This makes every later delta attributable.

### Step 2 — Prove your new tests fail pre-fix (revert-proof)
```bash
git stash push -- <your production files>   # keep ONLY the new test file
python -m pytest tests/test_new_thing.py -q  # expect the bug-proving test to FAIL
git stash pop
```
A regression test that passes on unpatched code proves nothing.

### Step 3 — Post-fix full regression: compare counts, then triage each failure
For every failure in the post-fix run, ask "is this caused by my change?" with escalating evidence:
1. **Standalone serial**: `python -m pytest <file> -q -n 0` → passes?
2. **Standalone under xdist** (same parallelism as the full run): `python -m pytest <file> -q` → passes?
3. If it passes both ways but failed in the full suite, it is an **environmental flake**: shared resources (ZMQ sockets — "No buffer space available", ports, temp dirs) or worker crashes under load ("[gwN] node down: Not properly terminated" + "worker crashed while running").

### Step 4 — Report with attribution
- Pass counts vs baseline, explained delta (= your new tests).
- Each failure: which of the three categories, with the command that proved it.
- Never silently drop a failing file from the comparison; never claim "all green" when you excluded something.

## Tips
- xdist worker crashes are NOT assertion failures — the test may have passed fine; the worker process died (OOM, socket exhaustion). The crash line names the test but the cause is environmental. Running the file alone (same `-n` config) isolating it from the load is the decisive A/B.
- Windows + xdist: ZMQ "No buffer space available" thread exceptions in jupyter_client during teardown are a known symptom of socket exhaustion under parallel load — not a code regression.
- If a failure's file is unrelated to your diff (check `git diff --stat` overlap), category (b)/(c) is almost always right — but still prove it with Step 3, don't just assert it.
- Record the exact commands in the report so a reviewer can re-run the attribution themselves.
