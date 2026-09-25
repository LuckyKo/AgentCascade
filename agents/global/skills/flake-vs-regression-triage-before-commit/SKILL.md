---
name: flake-vs-regression-triage-before-commit
description: Before committing, distinguish a pre-existing test flake from a regression your change introduced when an independent re-run of the suite fails but the sub-agent reported green.
source: auto-generated
version: "1.0.0"
triggers:
  - "test flake"
  - "intermittent test failure"
  - "worker said passed but my run failed"
  - "passes in isolation"
  - "regression or flake"
  - "pre-existing test failure before commit"
generated_by: orchestrator
generated_from_task: "Telegram bridge unknown-slash-command fix — worker reported 89 passed, independent re-run showed 1 failed; had to prove the failure was a pre-existing supervisor timing flake, not a regression, before committing."
---

## Goal
Decide whether an intermittent test failure is a **pre-existing flake** (safe to commit) or a **regression your change introduced** (must fix first), using isolation runs and attribution — so you never ship a real break and never block on noise.

## Procedure

### Step 1 — Reproduce the discrepancy yourself
Never trust a sub-agent's "N passed" at face value. Re-run the exact suite(s) yourself:
```
python -m pytest <file1> <file2> -q 2>&1 | findstr /R "passed failed"
```
If it's green, you still have a latent flake to characterize (Step 4). If it fails, continue.

### Step 2 — Confirm YOUR changed tests are deterministic in isolation
Run only the tests you added/modified, twice, sequentially (NOT with `&` — concurrent runs add CPU contention that skews the very timing you're measuring):
```
python -m pytest "<file>::<your_test_1>" "<file>::<your_test_2>" -q 2>&1 | findstr /R "passed failed" && echo "--- again ---" && python -m pytest "<file>::<your_test_1>" "<file>::<your_test_2>" -q 2>&1 | findstr /R "passed failed"
```
If your tests pass deterministically in isolation, your change is very likely clean.

### Step 3 — Capture the NAME of the intermittently-failing test
A bare `-q` run won't tell you which test flaked. Loop with `-rf` (report failures) until it fails:
```
for /L %i in (1,1,6) do (python -m pytest <files> -q -rf 2>&1 | findstr /R "FAILED passed failed")
```
Note: a `for /L` loop of full suites can exceed the shell_cmd timeout — if so, run single `-q -rf` invocations one at a time until you catch the FAILED line. Record the exact test id(s).

### Step 4 — Prove the failing tests are pre-existing (pass in isolation)
Run the captured failing test(s) alone, twice:
```
python -m pytest "<file>::<flaky_test>" -q 2>&1 | findstr /R "passed failed" && echo "--- again ---" && python -m pytest "<file>::<flaky_test>" -q 2>&1 | findstr /R "passed failed"
```
**Passes in isolation + fails only under full-suite load = timing/load flake**, not a deterministic regression.

### Step 5 — Attribute by scope
Confirm the flaky tests are in code your change did NOT touch (e.g. you changed command dispatch; the flakes are in supervisor child-process start/stop timing). If the failing tests exercise unrelated subsystems and pass in isolation, attribute them as **pre-existing** and note it in your commit message + report to the user. Do NOT fix out-of-scope flakes inline — offer to do it separately.

## Tips
- The signature of a load-timing flake: **deterministic green in isolation, intermittent red under full-suite/xdist load.** That bisection is the whole test.
- On Windows, run repeated pytest invocations with `&&` (sequential), never `&` (concurrent) — concurrency changes timing and produces unreliable evidence.
- A `for /L` loop of heavy suites often times out shell_cmd; prefer a few single `-q -rf` runs to catch the FAILED name.
- Always document the flake in the commit message ("only intermittent failures are pre-existing X flakes that pass in isolation") so the next person doesn't re-investigate.
- If you CAN'T get the failing tests to pass in isolation, STOP — that's a real regression; don't commit.
