# IMPLEMENTATION REPORT — Endpoint-Slot Deadlock Fix Plan (BUG-1/2, 4/8, 6, 7a, 11)

**Date:** 2026-08-22
**Implementer:** deadlock-impl-1
**Spec:** [fix_plan_endpoint_slot_deadlock.md](fix_plan_endpoint_slot_deadlock.md) (+ per-bug plans in `fix_plans/`)
**Status:** Implemented + tested. NOT committed. No todo.md items marked done.

## Files changed

### 1. `agent_cascade/engine/core.py` — BUG-1 + BUG-6 + BUG-4/8

**`_wait_for_compression_to_clear` (was lines 1158–1172)** — the root-cause fix. All three suspension call sites (post-stream core.py:~712, `_post_turn_checks` ~1932, tool loop `tool_execution.py`:104) route through this one method; no call-site edits needed.

- **BUG-1:** On entry, resolves the instance, saves KV state (`state_ops.save_instance_state`), then releases the endpoint slot via the existing `_release_slot(instance, inst_name, "compression_halt")`. While suspended, other agents (incl. the Compressor required to clear the halt) can acquire the slot. On resume, re-acquires through the existing public helper `reacquire_for(instance, inst_name, context="after_compression_resume")` and only after a successful re-acquire restores KV state (`restore_instance_state`) — exactly the sync-child idiom ordering (`tool_dispatcher.py:564–573`). Re-acquire failure degrades to slotless with `[SLOT_REACQUIRE_FAILED]` (plan default, index open-question #5).
- **BUG-6:** Wait tick replaced: `self.pool.wait_if_paused(timeout=_COMPRESSION_WAIT_TIMEOUT)` → `time.sleep(_COMPRESSION_WAIT_TIMEOUT)`. The old call waited on the GLOBAL `_paused` event while compression-halt is per-instance, so it returned instantly and spun at 100% CPU for the whole suspension.
- Terminal stop during wait returns `False` with the slot still released; run()'s exit finally releases again idempotently. Termination *while blocked inside* `reacquire_for` propagates `AgentTerminatedError` into run()'s existing clean-abort handler.

**run() entry (~line 450):** resets `instance._compression_suspended_at = 0.0` next to the per-run slot init (per BUG-4/8 plan).

**run() exit finally (~lines 821–846, 897–916) — BUG-4/8:**
- Computes `suspended_this_run` / `terminated` / `outstanding` (`pool.has_pending() or pool.has_messages()`).
- `preserve = suspended_this_run and not terminated and outstanding`. When False (all normal completions + terminal stops), cleanup is byte-for-byte today's code (clear_pending + drain_queue). When True, both are skipped so queued wakeups and pending async-child registrations survive a suspension-driven exit.
- Final state choice: `SLEEPING if preserve else IDLE` (SLEEPING protects from IdleManager dismissal; per user clarification no wakeup machinery added — agents wake from queued messages in either state). Sets `sleeping_since` when preserving. Logs `EXIT - <name> <FROM>→<TO> [suspension-preserved]`.

### 2. `agent_cascade/compression/handler.py` — BUG-7a

**`execute_force_compression` (~lines 759–885):**

- **Backoff gate FIRST**, before `halt_all_instances`: reads `_force_compress_fail_streak`; if > 0 and `elapsed = time.monotonic() - _last_force_compress_fail_at` < `min(60·2^(streak−1), 600)`s, logs `[COMPRESSION_BACKOFF]`, injects the standard compression warning (`engine._inject_compression_warning` with fresh token counts) and returns False — no pool-wide halt for an attempt expected to fail. Lives inside this method so the critical-threshold cooldown override in `compression_exec.py:162–169` cannot bypass it.
- **Exception path (was line 845 `return True`):** now increments streak under `_compression_lock`, stamps timestamp, `return False`.
- **Soft-failure path (`result.success == False`, was falling off returning None):** same streak recording + explicit `return False`.
- **Success path:** resets streak under lock and explicitly `return True`.

### 3. `agent_cascade/agent_instance.py` — fields for BUG-7 + BUG-4/8

Three dataclass fields (slots dataclass, additive, defaults only):

```python
_force_compress_fail_streak: int = field(default=0)        # next to _force_compress_count (line ~271)
_last_force_compress_fail_at: float = field(default=0.0)
_compression_suspended_at: float = field(default=0.0)      # next to _slot_release/_slot_key (line ~287)
```

### 4. `agent_cascade/slot_queue.py` — BUG-11 (observability-only)

DEBUG traces on logger `agent_cascade.slot_queue`, all prefixed `[SLOTPOOL]`:

| Event | Location | Fields |
|---|---|---|
| Queued | `acquire()` immediately after ticket insertion, BEFORE the poll loop | agent (name/class), ticket id, position, waiters count, holders list, timeout |
| Granted | `_grant()` (single choke point for fast-path AND queued grant; new optional `ticket=None` param) | agent, acquisition id, `(fast-path)` tag or `ticket=<id> waited=<n>s` |
| Released | `release()` after the stale/idempotent early-return, before `notify_all()` | agent, held duration, running/capacity |
| Cancelled ×2 | both silent pre-raise sites in `acquire()` (mid-loop check + fall-through) | agent, ticket |
| Cancelled/Terminated | `cancel(ticket_id=...)`, `cancel(agent_name=...)`, `terminate_for_agent()` when they actually cancel ≥1 ticket | agent, ticket ids |

No changes to scheduler.py, no logging inside the 1s poll loop (structurally spam-free), timeout WARNING untouched. Fast-path vs contended acquisitions are distinguishable in logs.

## Tests added (30 total, all passing)

0. **`tests/test_e2e_agent_calls.py::TestSecuritySlotYieldOnSharedSlot`** (1 integration test — REWRITTEN 2026-08-22, see "Test repair" below)
   - Full integration through the REAL stack: real `APIRouter` + conc=0 endpoint → real `_shared_sequential_slot_` SlotPool; both agents driven through genuine `engine.run()` with LLM stubbed at the engine boundary (`_call_llm_with_injection`) and gated for determinism.
   - Scenario: caller holds the shared permit mid-stream → Security queues FIFO on the same pool → caller is halted exactly like forced compression does (`pool.halt_all_instances(except_instances=["Security_repro"])`) → **asserts Security acquires within the shortened 3s QUEUE_WAIT_TIMEOUT (well under it in practice, ~0.2s)** → `resume_all_instances()` → caller re-acquires at FIFO tail → both complete with no `[SYSTEM ERROR … Timed out … endpoint slot]` messages and committed final turns.
   - QUEUE_WAIT_TIMEOUT patched to 3s (both modules that read it) so a BUG-1 regression reproduces as a fast failure instead of a hang; whole test ~2.7s.
1. **`tests/test_bug1_bug6_bug4_8_slot_deadlock.py`** (13 tests)
   - BUG-1: release-on-suspend + reacquire-on-resume with save-before-release/restore-after-reacquire ordering; no-slot fast no-op; reacquire-timeout degrade (no exception, slotless state, NO restore); terminal-stop mid-wait returns False without leak or reacquire; save→release event ordering.
   - BUG-6: exactly N `time.sleep(_COMPRESSION_WAIT_TIMEOUT)` ticks and zero `pool.wait_if_paused` calls; prompt exit when flag never set.
   - BUG-4/8: suspension-driven exit preserves registry+queue, lands SLEEPING with `[suspension-preserved]` log; normal-exit regression guard (clear+drain+IDLE); suspended-but-drained → IDLE; terminal stop cleans up; marker reset at run entry; IdleManager skips preserved-SLEEPING agent but dismisses timed-out IDLE.
2. **`tests/test_bug7_compression_backoff.py`** (6 tests)
   - Exception → streak=1 + False; gate short-circuits before halt_all_instances on immediate retry (halt called exactly once across two attempts); gate expiry proceeds to halt+compress; soft-failure records streak + False; success zeroes streak; warning still injected while gated.
3. **`tests/test_bug11_slot_queue_debug_tracing.py`** (10 tests)
   - Enqueue log once-per-enqueue with all required fields; queued grant carries ticket+waited (≥ real queue time); fast-path grant tagged without ticket field; release logs held duration ≥ sleep delta; stale release stays silent; cancel-before-raise, cancel-by-ticket, terminate_for_agent all logged; no-spam guard (≥3 poll ticks, still exactly 1 Queued record); `_log_acquire_timeout` WARNING regression guard.

### Test repair: `TestSecuritySlotDeadlockRepro` → `TestSecuritySlotYieldOnSharedSlot`

The old `test_security_check_deadlocks_on_shared_slot` asserted the OLD BUGGY behavior (Security timing out on the shared slot) and was already broken on pristine HEAD: it expected the initial-acquire `TimeoutError` to propagate out of `engine.run()`, but run()'s generic `except Exception` handler converts it into a yielded `[SYSTEM ERROR]` message, so its captured `result["exc"]` stayed None on old AND new code — unpassable as written (its own failure text invited this rewrite). Rewritten per code review to assert the FIXED behavior instead of deleting it:

- Caller now runs through real `engine.run()` holding the permit (required — BUG-1 releases at *in-run* checkpoints; the old test's out-of-band `pool._acquire_slot()` permit could never be released by the fix).
- Halts via the same API forced compression uses, so no compression machinery needs mocking.
- Negative-control verified: with HEAD core.py (fix reverted), the new test FAILS ("Security never acquired the shared slot…") in ~100s; with the fix it passes in ~2.7s. It genuinely guards BUG-1.

## Test results

**Full suite:** `python -m pytest tests/` (pytest.ini addopts: `-n auto --timeout=60`, excludes live_api/skip_if_no_local/extra_* markers). Output captured in `test_results_deadlock_fix.txt`.

```
1 failed, 1740 passed, 1 skipped, 85 warnings in 127.92s (0:02:07)
FAILED tests/test_e2e_agent_calls.py::TestSecuritySlotDeadlockRepro::test_security_check_deadlocks_on_shared_slot
```

That snapshot predates the test repair below. After rewriting the repro test (see "Test repair"):

```
tests/test_e2e_agent_calls.py::TestSecuritySlotYieldOnSharedSlot + all 3 new files:
30 passed in 9.93s
```

All 30 relevant tests green: the rewritten integration test (1), `tests/test_bug1_bug6_bug4_8_slot_deadlock.py` (13), `tests/test_bug7_compression_backoff.py` (6), `tests/test_bug11_slot_queue_debug_tracing.py` (10). Syntax checks pass on all touched files. No known failing tests remain from this change set.

## Plan deviations

1. **Success path now returns True (handler.py).** The plan's pseudo-code showed only "success resets streak" without an explicit return; as-written the method would still fall off returning None on success — contradicting its own docstring ("True if compression successful") and the plan's honest-return-value goal. Added `return True` after the streak reset. Callers treat falsy as "not compressed", so this strictly improves correctness of `_proactive_compression_check`'s skip-warning branch. Flagged for reviewer confirmation.
2. **No other deviations.** Timeout model untouched (single shared QUEUE_WAIT_TIMEOUT=300s, no per-agent plumbing anywhere). FIFO semantics untouched (no reservations/priority/bypass; retry = fresh tail acquire). Only pre-existing helpers reused (`_release_slot`, `reacquire_for`, `save_instance_state`, `restore_instance_state`). Zero scheduler.py changes for BUG-11.

## Non-obvious facts discovered (for reviewer attention)

- **Early-exit drain duplication is pre-existing:** run()'s `if not messages:` block already drains the queue once (core.py:510) before the exit finally may drain again. Unchanged by this fix; BUG-4/8 tests assert exact call counts accounting for it.
- **Terminal-stop exits bypass `_setup_turn`:** the pre-try terminal guard returns before setup, so those exits have no early-exit drain — the finally's drain is the only one.
- **`_setup_turn` calls `restore_instance_state` (core.py:920–921)** — relevant context for KV restore placement in BUG-1 (restore-after-reacquire mirrors it).
- **`_grant` had exactly two call sites** (fast path L160, queued grant L200), both in-module — verified before adding the optional param.
- **`_is_idle` order matters:** SLEEPING check precedes the execution-stack check in idle_manager.py:106+, so the preserved-SLEEPING test can use lightweight mocks.
- **Test-side races fixed twice:** waiter threads must be confirmed queued via `pool.get_status()` polling (`wait_queued` helper), not by signaling before `acquire()` is entered — initial test drafts raced here.

## Reviewer focus areas

1. **BUG-1 finally-block semantics:** on terminal stop we deliberately skip re-acquire and return False with slot released — confirm no caller assumes the slot is held after a False return (all three call sites break/exit into run()'s finally, which releases idempotently).
2. **The `return True` deviation above.**
3. **preserve-condition breadth:** `outstanding = has_pending() or has_messages()` — an agent suspended mid-run that later gets a queued message and exits will be preserved+SLEEPING. This matches the plan's intent ("recent wakeups") but widens preservation slightly vs. suspension-caused work alone.
4. **Backoff gate vs overfeeding safety net:** gated attempts don't increment `_force_compress_count` beyond the pre-increment done by cooldown paths; the hard guard (`ContextWindowExceeded` after `compression_max_attempts`) still fires independently — index risk #3 accepted.
5. **Rewritten e2e test:** `TestSecuritySlotYieldOnSharedSlot::test_suspended_holder_yields_shared_slot_to_waiter` stubs LLM at `_call_llm_with_injection` (engine boundary) and orchestrates two real `run()` threads with events. Scrutinize for hidden races: Security-queued detection polls `shared._waiters`; caller-held assertion requires `"caller" in shared._running`; halt is applied only after both are verified. Negative control confirmed (fails on HEAD core.py).
