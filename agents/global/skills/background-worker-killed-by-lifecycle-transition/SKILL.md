---
name: background-worker-killed-by-lifecycle-transition
description: Diagnose why a feature silently stops working (no error) because its background worker/daemon thread was killed by a lifecycle transition (stop/resume, reset, shutdown) and never restarted — distinct from "the gate isn't reached". Covers the log-timeline proof and the real-harness e2e + revert-proof acceptance bar.
source: auto-generated
version: "1.0.0"
triggers:
  - "feature stopped working after resume"
  - "background worker died"
  - "daemon thread not processing"
  - "submitted but never processed no error"
  - "works until restart then breaks"
  - "hint/worker silent after restart"
generated_by: orchestrator
generated_from_task: "todo162 — memory-hint system broke at a resume: worker started once, killed by pool stop->resume cycle (stop() called, start() never re-called and was a no-op), dead until full process restart. Tests passed because they mocked the SkillManager and never exercised the worker lifecycle."
---

## Goal
Find why a feature **silently stops working with no error** because its background worker/daemon thread was started once and then **killed by a lifecycle transition** (stop→resume, reset, shutdown, re-init) and never brought back — even though unit tests pass. This is a different failure class from "the gate/condition isn't reached" (see `feature-not-firing-in-production`); here the *executor itself* no longer exists.

## When this is the suspect
- Symptom: requests/jobs are **submitted but never processed**, **no exception, no error log**, and it stays broken until a full process restart.
- The feature worked earlier in the same process, then stopped at a specific moment (a resume, reset, config change, instance dismiss).
- A "worker started" / "thread started" log line appears ONCE early and never again, while "submitted"/"queued" lines keep appearing.

## Procedure

### Step 1 — Prove the worker died from the log timeline (do this FIRST)
Grep the console/app log for the worker's **lifecycle markers** (e.g. `worker started`, `worker stopped`) and its **per-job processing markers** (e.g. `gate → fire/skip`, `queued hint`). Build a timeline:
- Find the LAST time per-job processing happened.
- Find whether a lifecycle marker (`started`/`stopped`) appears AFTER that point.
- If you see "submitted" lines continuing but NO processing lines AND no new "worker started" line → **the worker thread is dead or wedged.** This single fact disambiguates "dead executor" from "gate not reached" (which would still show the gate being evaluated).

### Step 2 — Find the kill site and the missing restart
Read the worker's `start()`/`stop()` and every caller. The classic defect trio:
1. `start()` is **idempotent via a stale flag** (`if self._started: return`) → once started, a later `start()` is a **no-op even if the thread died**. There is no liveness check.
2. `stop()` sends a sentinel; the worker loop breaks out **permanently**; `_started` is NOT reset.
3. A lifecycle setter (e.g. a `stopped` property) calls `stop()` on the True branch but the False/resume branch **never re-calls `start()`**.
Grep for every place that sets the stop flag both ways and confirm the resume path has NO matching restart. Look for an **asymmetry**: often a sibling background service (idle checker, timer) IS restarted on resume with a comment like "it may have been stopped" — but this worker was forgotten. That asymmetry is the smoking gun.

### Step 3 — Classify dead vs wedged
- **Dead:** thread exited (sentinel observed). Fix = make `start()` liveness-aware + restart on resume.
- **Wedged:** thread alive but blocked forever on a lock held by another thread (e.g. a compression/execution lock held across the transition). No exception → no log → silent, persistent. Fix = don't hold that lock across the transition, or make the worker's blocking call non-blocking/bounded.
Disambiguate: if a "worker started" line exists and no error, check whether the worker could be blocked on a lock the transitioning thread holds (lock-ordering / held-across-resume).

### Step 4 — The fix (root cause, minimal)
Make `start()` **respawnable**: guard with liveness (`if self._started and self._worker is not None and self._worker.is_alive(): return` else respawn), all under the start lock. Reset `_started=False` in `stop()` after the sentinel so a later `start()` isn't blocked by the stale flag. In the resume branch of the lifecycle setter, **re-call `start()`** mirroring how the sibling service is restarted — guarded, best-effort try/except, gated on the feature's enabled flag. A brief two-worker overlap during stop→start is benign if jobs are filtered/deduped (e.g. a generation id) — confirm that invariant rather than forbidding overlap.

### Step 5 — Acceptance bar: real-harness e2e + revert-proof (non-negotiable)
Mock-based unit tests **cannot** catch this — they structurally can't reproduce a dead/wedged thread. The fix MUST add:
- An **e2e test driving the REAL harness** (real pool/manager, not MagicMock) through the EXACT lifecycle sequence that killed it (e.g. `pool.stopped = True` then `= False`), asserting the feature STILL works afterwards.
- A **unit test** for the restart contract: start→stop→start leaves a live worker and processes a fresh job.
- **Revert-proof both**: temporarily revert ONLY the production fix, confirm these tests FAIL with the dead-worker signature, restore, confirm PASS. A test that passes pre-fix is worthless here.
- **Guard against false-green:** if the feature has per-item cooldowns/dedup, make the post-transition job match something NOT already suppressed, or the suppression logic masks liveness and the test falsely passes (see `worker-liveness-regression-test-cooldown-gotcha`). Use bounded polling (wait-for-delivery with timeout), not fixed sleeps.

## Tips
- "Submitted but never processed + no error + works until restart" is almost always a **supervisor-less daemon thread** that died or wedged — not a logic bug in the feature's own code. Stop debugging the feature logic; go straight to the worker lifecycle.
- The log timeline (Step 1) is cheap and decisive — do it before reading any code. One grep for "worker started" often ends the investigation.
- Never trust "the tests pass" as evidence the bug is fixed when the tests mock the executor. Name the mock that hides the real path.
- Do NOT attribute to a specific commit unless asked; the fix + lifecycle test is what makes it stay fixed, and bisecting history is usually a detour.
- Keep the fix additive (revive-on-resume) so it's trivially revertible if it misbehaves.
