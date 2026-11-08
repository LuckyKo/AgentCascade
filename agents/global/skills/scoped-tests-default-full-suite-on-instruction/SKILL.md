---
name: scoped-tests-default-full-suite-on-instruction
description: Default verification is the scoped test files affected by the change; the full regression suite runs only when the supervisor explicitly instructs it (release/milestone gate, flake investigation, cross-subsystem change, regression triage). Do not make full-suite runs a completion default.
triggers:
  - "run the tests"
  - "verify the fix"
  - "regression suite"
  - "full suite"
  - "keep the suite green"
version: 1.0.1
---

# Scoped tests by default; full suite on instruction

## The rule

- **Default:** run ONLY the test files affected by the change (the ones named in the plan/task, plus any file that pins a string/contract the change touches). Report the scoped counts.
- **Parallel by default, serial on need.** Run scoped files with the project's normal xdist invocation; serial (`-n 0`) runs are also very time-consuming — use them ONLY when isolating a suspected flake or investigating a critical failure, not as a routine verification mode.
- **Full suite (`python -m pytest` default invocation):** run it ONLY when
  - the supervisor explicitly asks, OR
  - it is a release/milestone gate, OR
  - investigating a flake / triaging a red suite (see `full-suite-flake-adjudication-by-run-counting`, `regression-suite-failure-triage`), OR
  - the change is cross-subsystem (touches 2+ subsystems or a shared API) — and say so in the report.
- When you do NOT run the full suite, note it in one line in the report: "full suite not run (scoped: X files, N passed)" — so the supervisor can request it if the milestone demands it.

## Responsibility

- **Testing is the change-maker's job.** The coder who made the change runs the scoped tests (and the full suite when the rule above allows it) and reports measured counts with the exact invocation.
- **Reviewers verify evidence, they do not perform full runs.** A reviewer checks that the change-maker ran the right suites, and may RE-RUN the named scoped files (cheap) when a claim is in question. A reviewer does NOT launch a full-suite run "to be safe" — if a full-suite proof is genuinely required, that is a gap to flag in the verdict and delegate back to the coder.
- **Orchestrator verification follows the same rule:** re-run the named scoped suites a sub-agent claimed, never a willy-nilly full regression (see `subagent-deliverable-verification`).

## Why

- A full-suite run costs ~5-8 minutes of wall time on this repo and carries known environmental flakes (inner-loop FP ×2 under xdist, probe_trigger timing, Docker/zmq env skips). Every willy-nilly run adds minutes and flake noise to the signal.
- Scoped files prove the change; the full suite proves nothing regressed ELSEWHERE — that is a milestone property, not a per-round property. Review rounds in the same task do not each need their own full-suite proof.
- When reviewers absorb testing "for a lazy coder", time-to-completion balloons and accountability blurs: the change-maker never learns their evidence was insufficient. Flag and delegate instead.

## Anti-patterns

| Pattern | Why it fails |
|---|---|
| "keep the suite green" as a standing instruction in every task | turns a 30-second scoped run into a 6-minute full run per round |
| re-running the full suite after every review-fix round of the same task | one full-suite proof at the final round is enough; earlier rounds use scoped files |
| a reviewer launching a full-suite run to double-check the coder | long-duration, not the reviewer's job; flag the gap and delegate to the coder |
| treating a flaky full-suite failure as a real failure | adjudicate first (`full-suite-flake-adjudication-by-run-counting`); the known flakes are documented |
