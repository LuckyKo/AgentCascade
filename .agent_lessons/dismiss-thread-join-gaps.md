---
tags: [dismiss-agent, threading, zombie-threads, agent-pool, thread-join]
aliases: [thread-join-dismiss-fails, instance-threads-registry-gaps]
related: [[dismiss-agent-cooperative-termination]], [[agent-pool-no-instance-lock]], [[deadlock_detection_dismiss_fix]]
confidence: verified
---

# dismiss thread-join fix (uncommitted) does not stop zombie threads — 3 verified gaps

**Fact (2026-08-10, live-reproduced)**: The uncommitted thread-tracking/join approach in `agent_pool.py` (`_instance_threads` dict, `_pool_lock`, join in `dismiss_instance`) **does not actually terminate running agent threads** in 3 concrete scenarios, verified by live reproduction:

## Gap 1 — Registered thread blocked in a long op: join TIMES OUT, thread survives
- `dismiss_instance()` (agent_pool.py:1165-1180, uncommitted) pops the thread and `join(timeout=30.0)`.
- Live repro: thread blocked in `time.sleep(120)` → log line: `"Thread for 's1_agent' did not stop within 30s timeout. Proceeding with cleanup but agent may continue briefly as zombie."` → cleanup proceeds, thread keeps running. **Join only *waits*; it cannot make a cooperative thread stop.**
- Unless the thread's blocking op has a stop-check, 30s join is a pure delay; dismissal blocks the caller for 30s and still leaves the zombie.

## Gap 2 — Async child executor workers are NEVER registered → dismiss returns immediately, zombie runs on
- Only `run_agent_thread_unified()` registers in `_instance_threads` (run_agent_unified.py:112-119, uncommitted).
- Async child agents run in `ThreadPoolExecutor` workers (`async_tools.py:77-80, 109` → `run_child_agent` agent_pool.py:2631 → `run_child_core`). **No registration anywhere in this path.**
- Live repro S2: `dismiss_instance` took **0.00s** (no thread found → `"No active thread to join"`), worker still alive 1s+ later. `terminated_instances` was empty after dismissal (signal discarded) — the worker sails on.

## Gap 3 — Registration race: thread registers AFTER dismiss → leaked dict entry + undetectable zombie
- `run_agent_thread_unified` registers at :112-119, but only *after* `create_main_agent_instance` (:104-110). If dismiss runs in that window, `dismiss_instance` sees no thread, discards the terminated signal, removes the instance; the thread then registers **after** (late) and is never joined/cleaned.
- Live repro S3: thread registered after dismiss → `_instance_threads` held a stale entry (memory leak), thread alive and invisible to stop-checks (`is_instance_terminated(name)` now False: pool entry gone + set discarded; only the local `inst.is_terminated=True` remains, but engine stop-checks call the pool function).

## Why the signal-discard condition is wrong
- `dismiss_instance` (agent_pool.py:1183-1187, uncommitted): `if not thread or not thread.is_alive(): terminated_instances.discard(name)`.
- When `thread is None` (Gap 2/3), the signal is **always discarded** even though a zombie exists. The comment intent ("keep signal if still alive") only works when a thread ref exists AND has been joined. Correct condition: `if thread and not thread.is_alive()`.
- NOTE (reviewer-verified 2026-08-10): the uncommitted code **moved** the `terminated_instances.discard()` OUT of `remove_instance` (agent_pool.py:888-892 comment: "Don't discard from terminated_instances here — keep the signal alive until the thread confirms it stopped via join in dismiss_instance()"). The committed HEAD DID discard at remove_instance:880. So the discard now lives only in `dismiss_instance` — where the `thread is None` flaw bites.
- No `_state_lock` → `_pool_lock` inversion exists in agent_pool.py/execution_engine.py (systematic search, reviewer-verified). The `_pool_lock` → `_state_lock` order in remove_instance:926-933 and terminate_instance:1020-1028 is the safe ordering. Partial-coverage issue remains (lifecycle_manager.py:193 unlocked write).

## Also relevant
- All 34 `tests/test_dismiss_termination.py` tests pass with the uncommitted changes — **they never create real threads**, so they cannot detect zombie behavior.
- The committed head already contains the *cooperative* machinery (`AgentTerminatedError`, `_check_termination` in api_router.py:38-46, `_interruptible_sleep` api_router.py:49-59, slot-acquire termination checks api_router.py:350-352, sync-child propagation tool_dispatcher.py:590-593, engine abort points execution_engine.py:896/1514/3397/3990-3995). Those are sound but cannot interrupt a thread stuck in a *non-interruptible* blocking call (plain network read, long tool, time.sleep).

## Recommended fix direction (not yet implemented)
1. Register ALL agent threads (incl. async executor workers) in `_instance_threads`, or drop the join approach entirely.
2. Never discard the termination signal unconditionally when `thread is None`; keep `terminated_instances` entry (or rely solely on `inst.is_terminated`).
3. Dismissal must not block 30s on join — use short timeout + keep signals, or make dismissal non-blocking and let the cooperative checks do the work.
4. Add a real-thread integration test (spawn a thread doing a long interruptible op, dismiss, assert it stops).

**Full evidence report**: `investigation_report_dismiss_agent_thread_termination.md` (committed baseline) + this investigation's deliverable report (zombie_investigator).