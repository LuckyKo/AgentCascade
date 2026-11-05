---
name: scoped-tests-default-full-suite-on-instruction
description: Default verification is the scoped test files affected by the change — the CODER runs them and reports exact counts; the ORCHESTRATOR runs the full regression suite after a major feature or fix (or on supervisor instruction); the REVIEWER verifies the code and the provided test evidence, no re-runs. Do not make full-suite runs a completion default.
triggers:
  - "run the tests"
  - "verify the fix"
  - "regression suite"
  - "full suite"
  - "keep the suite green"
version: "1.0.2"
---

# Scoped tests by default; full suite on instruction

## The rule

- **Default:** run ONLY the test files affected by the change (the ones named in the plan/task, plus any file that pins a string/contract the change touches). Report the scoped counts.
- **Parallel by default, serial on need.** Run scoped files with the project's normal xdist invocation; serial (`-n 0`) runs are also very time-consuming — use them ONLY when isolating a suspected flake or investigating a critical failure, not as a routine verification mode.
- **Full suite (`python -m pytest` default invocation):** the ORCHESTRATOR runs it
  - after a major feature or fix, OR
  - the supervisor explicitly asks, OR
  - it is a release/milestone gate, OR
  - investigating a flake / triaging a red suite (see `full-suite-flake-adjudication-by-run-counting`, `regression-suite-failure-triage`), OR
  - the change is cross-subsystem (touches 2+ subsystems or a shared API) — and say so in the report.
- When you do NOT run the full suite, note it in one line in the report: "full suite not run (scoped: X files, N passed)" — so the supervisor can request it if the milestone demands it.

## Responsibility

- **CODER** owns scoped testing: runs the scoped test files its change touches (the project's normal xdist invocation) and reports exact pass/fail/skip counts with the exact invocation.
- **ORCHESTRATOR** runs the full regression suite after a major feature or fix (or on supervisor instruction).
- **REVIEWER** verifies the code and the change-maker's test evidence (exact counts + invocation) — does not re-run tests, and never launches full runs.

## Why

- A full-suite run costs ~5-8 minutes of wall time on this repo and carries known environmental flakes (inner-loop FP ×2 under xdist, probe_trigger timing, Docker/zmq env skips). Every willy-nilly run adds minutes and flake noise to the signal.
- Scoped files prove the change; the full suite proves nothing regressed ELSEWHERE — that is a milestone property, not a per-round property. Review rounds in the same task do not each need their own full-suite proof.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| "keep the suite green" as a standing instruction in every task | turns a 30-second scoped run into a 6-minute full run per round |
| re-running the full suite after every review-fix round of the same task | one full-suite proof at the final round is enough; earlier rounds use scoped files |
| treating a flaky full-suite failure as a real failure | adjudicate first (`full-suite-flake-adjudication-by-run-counting`); the known flakes are documented |
