---
tags: [slot-queue, single-flight, starvation, concurrency, incident-2026-09-30]
aliases: [d5-inflight-gate-starvation, slotpool-single-flight-starvation]
related:
  - "[[app-logger-no-basicconfig-slot-logger-blindness]]"
  - "[[slot-release-audit-2026-09-30]]"
  - "[[stop-session-slot-leak-race-flake]]"
confidence: verified
author: stopfix-coder2_20260930_151706.jsonl
---

# D-5 single-flight gate: starvation regression and the cond-block merge fix

**Verified 2026-09-30 against v0.2.138; fix committed as `cd1134b5` (v0.2.139).**

## The defect (original D-5 gate, `38205de8`)

The single-flight `_inflight` gate in `SlotPool.acquire` had three compounding problems:

1. **Lock released between mark and capacity check.** The thread registered its
   in-flight Event under `self._cond`, then RELEASED the lock before checking
   capacity. A duplicate could observe a stale view of `_running`.
2. **Unconditional finally-clear (the wedge).** `finally` deleted whatever Event
   it found in `_inflight[instance]` — no owner check. A looped-back duplicate
   (one that timed out or was cancelled and re-entered the gate) could delete the
   PRIMARY's entry while the primary still owned the blocking window, corrupting
   the gate for subsequent acquires of the same instance.
3. **Duplicate waiters were uninterruptible.** A duplicate blocked on the
   primary's `Event` had no cancellation path — it could only exit on its own
   timeout, so waiters for a busy instance exceeded the 2s starvation threshold
   even when slots were free. The stress matrix classified this as STARVATION
   (13/30 scenarios, seed 665932189 in `test_sticky_churn_liveness`).

## The fix (`cd1134b5`)

- **One lock acquisition**: probe → mark (first-time only) → fast-path capacity
  check all under a single `with self._cond:`. No raise-capable code between the
  `_inflight` read and write (plan binding rule).
- **`owned` flag**: only the thread that SET the entry clears it in `finally`
  (also under `self._cond`, consistent with the release/notify pattern) and sets
  its Event. A duplicate looping back can no longer evict the primary's entry or
  wake waiters early.
- **Interruptible duplicate waiter**: each 1s tick calls `_ticket_cancelled()`
  (snapshot under lock, flag check outside — same idiom as the main wait loop)
  and raises `SlotCancelled` once all of the instance's tickets are removed.

## Verification (unchanged stress thresholds)

- D-5 regression tests incl. `test_inflight_never_wedges` +
  `TestLeak4DoubleAcquireDetection`: 9 passed.
- `test_sticky_churn_liveness` (starvation repro): passed.
- Full stress matrix: 8 passed; stress proof phase exit 0.
- Full suite: 3575 passed, 96 skipped, 1 failed — the failure was a pre-existing
  flake (`test_code_interpreter_extra_mounts`, passes in isolation) plus a stray
  `agent_cascade/slot_queue_broken.py` leftover that tripped
  `test_no_undefined_names`; both unrelated to this change.

## Gotchas worth remembering

- **Do NOT raise stress thresholds to "fix" starvation** — that masks the gate
  corruption instead of fixing it.
- A stray broken `.py` file inside `agent_cascade/` makes
  `tests/test_no_undefined_names.py` fail for reasons unrelated to your change;
  check `git status` for untracked files before bisecting suite failures.
- The pytest tail `OSError: [Errno 9] Bad file descriptor` INTERNALERROR on
  console flush is a harness artifact of long redirected runs, not a test failure.

## Related

- Procedure skill: `single-flight-gate-starvation-fix` (global skills).
- Logger targeting for these tests (`agent_cascade_logger` + instance suffix,
  never the module logger): [[app-logger-no-basicconfig-slot-logger-blindness]].
- Shared caplog helper now lives in `tests/slot_test_helpers.py::app_logger_names`.
