# Investigation Report: dismiss_agent Thread Termination — Zombie Threads

**Investigator**: zombie_investigator (researcher)
**Date**: 2026-08-10
**Scope**: AgentCascade agent dismissal → thread termination failure; uncommitted thread-join fix
**Requested by**: Maine (orchestrator)

---

## Executive Summary

`dismiss_agent` **does not reliably terminate the underlying agent thread**. The committed architecture is *cooperative* — dismissal sets flags/state and relies on the agent loop hitting stop-check points. The uncommitted fix adds thread tracking (`_instance_threads`) + `join(timeout=30)`, but it **fails in every practical scenario**:

1. **Registered thread blocked in a non-interruptible op** → join times out after 30s, thread survives as zombie (live-reproduced).
2. **Async child executor workers are never registered** → no join at all; dismissal returns instantly, worker runs on (live-reproduced).
3. **Registration race** → thread registers *after* dismissal; leaked dict entry + invisible zombie (live-reproduced).

The 34 existing `test_dismiss_termination.py` tests all pass because **they never create real threads**.

Root cause: **Python threads cannot be force-killed**, and the codebase's cooperative-termination signal (stop-checks) is not present inside every blocking operation the threads can be stuck in. The uncommitted join approach assumed tracking + join would be enough, but tracking coverage is incomplete and join is only a *wait*, not a *stop*.

---

## 1. How agent dismissal currently works (committed HEAD)

`dismiss_agent` tool → `tool_dispatcher.py:160-461` → `pool.dismiss_instance()` → `agent_pool.py:1064-1190`:

- **`terminate_instance(name)`** (agent_pool.py:966-1018, committed): adds name to `terminated_instances` set, transitions state to TERMINATED, calls `inst.terminate()` (agent_instance.py:611-646 — sets `is_terminated = True`, clears streaming responses), cancels pending async futures (`clear_pending`), kills background shells.
- **`dismiss_instance(name)`** (agent_pool.py:1064-1190): cascade-dismisses children, terminates if active, wakes SLEEPING parent, then removes from pool.
- **`remove_instance(name)`** (agent_pool.py:880-937, committed HEAD): `instances.pop()` + **`terminated_instances.discard(name)`** — the *critical* detail in the committed baseline: it erases the only pool-level termination signal. *(Note: the uncommitted changes move the discard out of `remove_instance` — see RC4 correction, §4.)*

The engine's run loop checks `pool.is_instance_terminated()` at many cooperative points (execution_engine.py:1150/1158, streaming ticks ~1936, mid/post-stream, pre-tool :3958, SLEEPING wake :4779+), raising `AgentTerminatedError` (agent_pool.py:2654-2664 / api_router.py:38-46, 350-352).

**Key committed mechanism**: `inst.is_terminated = True` (agent_instance.py:627, commit 9ae00da) is durable — it survives pool removal because the engine holds the *instance object*. But `pool.is_instance_terminated()` only checks the set/instances dict → **once removed from pool, pool-level stop-checks return False**; only direct `inst.is_terminated` checks (e.g. execution_engine.py:3990-3995) still fire.

## 2. How agents are spawned / threads created

| Path | Thread producer | Registered in `_instance_threads`? |
|---|---|---|
| Main agent | `run_agent_thread_unified` → `threading.Thread(target=...)` (run_agent_unified.py:112-119) daemon=True | ✅ YES (uncommitted) |
| Async child | `ThreadPoolExecutor` workers (async_tools.py:76-80, 109) → `run_child_agent`/`run_child_core` | ❌ NO |
| Sync child | Inline in parent's thread (tool_dispatcher.py:518-610) | ❌ NO (by design — can't join without killing parent) |
| IdleManager | Background daemon thread (agent_pool.py:3063-3090) | N/A |

## 3. What the uncommitted changes attempt

`git diff` modified `agent_pool.py`, `execution_engine.py`, `lifecycle_manager.py`, `run_agent_unified.py`, `shell_cmd.py`:

- **agent_pool.py:292-294**: `_instance_threads: Dict[str, Thread]`, `_instance_threads_lock`, `_pool_lock (RLock)` added.
- **agent_pool.py:1165-1190** (changes in `dismiss_instance`): pop thread → `join(timeout=30.0)` → warn "did not stop within 30s" → conditional `terminated_instances.discard()` only if thread stopped → `remove_instance()`.
- **run_agent_unified.py:112-119**: thread self-registers into `_instance_threads`.
- **run_agent_unified.py:302-311**: thread cleans up its registry entry on exit (finally).
- `_pool_lock` wraps create/terminate/dismiss registrations and `is_instance_terminated`.

## 4. Root causes (why thread termination isn't happening)

### RC1 — Join cannot stop a cooperative thread; 30s timeout = delay + zombie (VERIFIED live)
```log
Thread for 's1_agent' did not stop within 30s timeout. Proceeding with cleanup but agent may continue briefly as zombie.
```
A blocked thread (LLM HTTP call, sync tool, `time.sleep`, `semaphore.acquire`) has no stop-check inside the blocking call. `thread.join(timeout=30)` only *waits*; on timeout the code proceeds with cleanup **and the thread continues**. Dismissal blocks the caller for the full 30s and still produces a zombie.

### RC2 — Async-child threads never registered → no join attempted (VERIFIED live)
S2 repro: `dismiss_instance` took **0.00s** ("No active thread to join"), worker still alive 1s+ after dismissal, `terminated_instances` empty. Async children (ThreadPoolExecutor workers) completely bypass `_instance_threads`. This is the **most common dismissal path** (any `call_agent` async child).

### RC3 — Registration race: late registration leaks thread + dict entry (VERIFIED live)
S3 repro: dismiss before `run_agent_thread_unified` reaches registration (the window between `create_main_agent_instance` at :104-110 and registration at :112-119) → no thread found → signal discarded → thread registers *after* → stale dict entry (memory leak), zombie undetectable via pool checks.

### RC4 — Signal-discard condition is wrong for `thread is None`
`if not thread or not thread.is_alive(): terminated_instances.discard(...)` (agent_pool.py:1186-1187). When `thread is None` (RC2/RC3), the signal is **always discarded** regardless of zombie existence — precisely the case that needs it most. The correct condition would be `if thread and not thread.is_alive()` — keep the signal if no thread was registered or if the thread is still alive.

### RC5 — Stop-check gaps in blocking operations (committed baseline)
Prior verified investigation (`investigation_report_dismiss_agent_thread_termination.md`) documented: **no stop-checks during** pre-first-token HTTP calls, 30s slot `semaphore.acquire()` (api_router.py:303), long sync tools (shell pipe wait, code_interpreter), `wait_for_message()` (agent_pool.py:2434-2548). The cooperative machinery cannot interrupt these.

### RC6 — Partial `_pool_lock` coverage (speculative lock-order concern, not realized)
The lock analysis report (`lock_analysis_report_agent_pool_thread_safety.md`) confirmed no dedicated instances lock existed pre-change. The new `_pool_lock` is **only partially applied**: lifecycle_manager.py:193 raw `pool.instances[...] = inst` remains unlocked, and external readers of `terminated_instances` (api_router.py, ws_handlers.py, child_runner.py) read lock-free. A systematic search of `agent_pool.py` and `execution_engine.py` found **no code path that nests `_state_lock` → `_pool_lock`** (the `_pool_lock` → `_state_lock` order in `remove_instance` at :926-933 and `terminate_instance` at :1020-1028 is the safe ordering). The earlier report's lock-inversion concern is **not currently realized** — flagged as latent risk only.

**Note on `remove_instance` (correction after independent review)**: the uncommitted code **moved** `terminated_instances.discard()` out of `remove_instance` (agent_pool.py:888-892 now explicitly comments "Don't discard from terminated_instances here — keep the signal alive until the thread confirms it stopped via join in dismiss_instance()"). This is an intentional corrective change vs. the committed HEAD (which did discard at remove_instance:880). The discard now lives exclusively in `dismiss_instance` (:1183-1187) — where the `thread is None` flaw (RC4) applies.

## 5. Antipatterns spotted

1. **Tracking-gate pattern**: termination correctness depends on registration completeness — broken for executor workers.
2. **Discard-before-stop**: removing the only pool-level termination signal before the thread confirms exit (pre-existing, aggravated by RC4).
3. **Fixed 30s blocking join in a tool path**: `dismiss_agent` tool execution blocks the dismissing agent's thread for up to 30s per dismissed child.
4. **Tests that mock away the very thing they test**: 34 tests, zero real threads → green suite, broken behavior.

## 6. Evidence summary

| Finding | Evidence | Confidence |
|---|---|---|
| Join timeout → zombie persists | Live repro S1 log line (agent_pool.py:1173-1177) | **Confirmed** (runtime) |
| Async executor workers unregistered | async_tools.py:76-80,109; repro S2 (0.00s dismiss, worker alive) | **Confirmed** (runtime) |
| Late registration race | run_agent_unified.py:104-119 registration window; repro S3 | **Confirmed** (runtime) |
| Signal discarded for thread=None | agent_pool.py:1186-1187 (uncommitted) | **Confirmed** (static) |
| Cooperative machinery works when reachable | api_router.py:38-59,350-352; engine abort sites | **High** |
| Stop-check gaps in blocking ops | prior verified investigation | **High** |
| 34 tests pass but don't exercise real agent threads | `pytest tests/test_dismiss_termination.py` → 34 passed; only `_interruptible_sleep` tests use a real thread (lines 313/327), none spawn an agent execution thread | **Confirmed** |
| `remove_instance` no longer discards `terminated_instances` in uncommitted code | agent_pool.py:888-892 explicit comment; discard moved to `dismiss_instance` | **Confirmed** (reviewer-verified) |
| No `_state_lock`→`_pool_lock` inversion realized | systematic search of agent_pool.py + execution_engine.py | **Confirmed** (reviewer-verified) |

## 7. Recommendations

**(A) Stop treating join as a termination mechanism.** Keep cooperative termination; the join is only useful if threads are guaranteed cooperative — they are not (RC1). Options:
1. **Register ALL agent threads** (async executor workers included) AND keep the signal until the thread actually exits (wake the parent, then clean).
2. **Shorten join to ~2s**, never block a tool call 30s; if still alive: keep `terminated_instances` entry, log, and let cooperative checks do the work.
3. **Never discard the signal when `thread is None`**; discard only when the thread has confirmed exit.

**(B) Close stop-check gaps** in the truly blocking calls: slot acquire (make interruptible), pre-first-token HTTP (timeout + check), `wait_for_message` already checks per-iteration — extend the same pattern.

**(C) Add a real-thread integration test**: spawn a thread doing an interruptible long op, dismiss, assert the thread terminates within a bound.

**(D) For sync children** (inline in parent): document that dismissal cannot unblock a parent stuck in `run_child_core`; the only safe fix is process isolation — out of scope for thread-level changes.

**(E) Complete `_pool_lock` coverage** or remove it — partial application (lifecycle_manager.py:193) gives false confidence; resolve the `_state_lock`/`_pool_lock` ordering explicitly.

## 8. Open questions

- Do async executor workers ever hit `dismiss_instance`'s join path in production? (No — verified they don't register.)
- Whether making `wait_for_message`/slot-acquire checks alone is sufficient for the observed production zombies (likely largest contributors).
- Whether process isolation for sync tools is acceptable cost.

## Handoff artifacts

- Live repro scripts deleted; findings preserved in `.agent_lessons/dismiss-thread-join-gaps.md` (new memory, `confidence: verified`).
- Prior related memories: `[[dismiss-agent-cooperative-termination]]`, `[[agent-pool-no-instance-lock]]`, `[[deadlock_detection_dismiss_fix]]`.

---

**Confidence**: High. Core findings runtime-verified; static analysis cross-checked against prior verified investigations and lock analysis report.