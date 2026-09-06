# Investigation: Does dismiss_agent Actually Terminate a Running Agent Thread?

**Date:** 2026-08-10
**Investigator:** investigator_dismiss_thread
**Task source:** todo.md line 94 — "check if dismiss_agent actually terminates a running agent thread"
**Related concern:** todo.md line 93 — parallel sync agents causing model trashing

---

## Executive Summary

**No — `dismiss_agent` does NOT forcibly terminate a running agent thread.** It performs a cooperative, async-cleanup-oriented dismissal. The thread (whether the main agent's `run_agent_thread_unified` daemon thread, or a `ThreadPoolExecutor` worker running an async child) is *not* killed, interrupted, or joined. It continues executing until it reaches a cooperative stop-check point.

What dismissal *does* reliably do:
1. Marks the instance as terminated (`terminated_instances.add()` + state → `TERMINATED`, `agent_pool.py:989-1007`).
2. Cancels **pending** (not-yet-started) async tool futures (`async_tools.py:181-213` — best-effort only).
3. Kills background shell subprocesses owned by the agent (`agent_pool.py:1020-1030`).
4. Clears message queues and streaming responses (`agent_pool.py:1032-1047`).
5. Removes the instance from the pool and fires dismissal callbacks (`agent_pool.py:1128-1130`).

**But the executing thread keeps running.** It will only stop if/when it reaches one of the cooperative stop-checks in `execution_engine.py` (`_is_terminal_stop()` / `_is_stopped()`), which are checked *between* LLM calls, *during* LLM streaming (every 20 ticks / ~100ms), and *before* each tool execution. During a long blocking operation (a synchronous LLM API call that hasn't started streaming, a synchronous tool like `shell_cmd` reading a pipe, a `wait_for_message()` block, or a 30s endpoint-slot `semaphore.acquire()`) **there is no check at all** — the thread is genuinely stuck until that blocking call returns.

Critically: **the cooperative check points can be silently defeated** because `remove_instance()` (called at the end of `dismiss_instance()`) does `terminated_instances.discard(instance_name)` (agent_pool.py:880). After dismissal, `is_instance_terminated()` falls back to `inst.is_terminated` — but `terminate_instance()` **never sets** `inst.is_terminated = True` (the only assignment in the codebase resets it to `False` in lifecycle_manager.py:496). So after the instance is removed from the pool, later checks see neither a pool entry nor a terminated flag, and the thread can sail through subsequent checks as if nothing happened.

---

## Key Findings

### F1. The dismissal call chain (evidence)

| Step | Location | What it does |
|---|---|---|
| Tool dispatch | `tool_dispatcher.py:160-164` | Routes `dismiss_agent` → `handle_dismiss_agent()` |
| Ownership checks | `tool_dispatcher.py:438-452` | Blocks self-dismiss, supervisor-dismiss, non-child dismiss (root can dismiss anyone) |
| Bulk path (all_idle) | `tool_dispatcher.py:394-432` | Skips root, SLEEPING, halted, and *actively running* (in `active_stack`) agents |
| Single path | `tool_dispatcher.py:455-461` | **No ACTIVE_STATES guard** — will dismiss a RUNNING agent |
| Pool dismissal | `agent_pool.py:1064` `dismiss_instance()` | Recursively dismisses children first, then if active → `terminate_instance()` |
| Setup termination | `agent_pool.py:966` `terminate_instance()` | `terminated_instances.add()`, state→TERMINATED, cancel pending futures, kill shells, clear queues |
| Pool removal | `agent_pool.py:871` `remove_instance()` | Pops from `instances`, **discards from `terminated_instances` (line 880)**, closes logger, fires callbacks |

### F2. `terminate_instance()` never sets `inst.is_terminated = True`

- `agent_pool.py:966-1007`: adds to `terminated_instances`, transitions state to `TERMINATED`, but does **not** set the dataclass field `is_terminated` (default `False`, agent_instance.py:238).
- The only assignment in the codebase sets it `False`: `lifecycle_manager.py:496`.
- Consequence: the termination signal lives **only** in the `terminated_instances` set — which `remove_instance()` then discards (agent_pool.py:880). `is_instance_terminated()` falls back to `inst.is_terminated` (agent_pool.py:2661-2664), which is still `False`; and after pool removal `inst` is `None` → returns `False`.

So the intended "durable per-instance termination flag" is effectively never set. This is a latent bug: termination relies entirely on a set entry that is removed moments later.

### F3. No forced-thread termination mechanism exists

- Grep for `PyThreadState_SetAsyncExc`, `_thread._async_exc`, `interrupt_main`, `.join()` on agent threads, `thread.ident` tracking: **no matches** in the agent execution path. All `.join()` calls relate to idle-checker threads, async-shell drain threads, and MCP cleanup — not agent threads.
- Agent threads are:
  - Main agent: `threading.Thread(target=run_agent_thread_unified, daemon=True)` spawned in `api_server.py:1119-1121` (and the same pattern in `run_agent_unified.py:68-73`).
  - Async children: `ThreadPoolExecutor(max_workers=4)` in `async_tools.py:76-80`, submitted at `async_tools.py:109` (`_execute` → `entry.tool_call()` → `run_child_agent` at agent_pool.py:2550).
  - Sync children: run **inline in the caller's own thread** (`tool_dispatcher.py:518-610` → `run_child_core`), so they cannot be independently "killed" at all.

### F4. Cooperative stop checks exist but are sparse

`execution_engine.py` checks `_is_terminal_stop()` (pool stopped / generation mismatch / `is_instance_terminated`) at these sites:
- At run entry and after slot acquire: `execution_engine.py:1150, 1158`
- During LLM streaming every ~20 ticks: `_check_stream_termination` (`execution_engine.py:1936`), plus two explicit mid-stream checks `execution_engine.py:3062, 3091`
- After LLM stream completes: `execution_engine.py:1411`
- Post-turn processing: `execution_engine.py:4601`
- Before each tool execution: `execution_engine.py:3958` (inside `_process_response`)
- SLEEPING-state wake loop: `execution_engine.py:4779, 4835, 4858`

**Gaps (no check during):**
- An LLM call that is blocked *before* first token (waiting on endpoint slot `semaphore.acquire(timeout=30s)`, `api_router.py:303`, or the HTTP request itself `api_router.py:1425` → blocking for the whole stream). The pre-API-call termination checks at `api_router.py:1368-1371` and `api_router.py:1420-1423` only prevent *starting* new calls; they do not interrupt one in flight.
- A synchronous long-running tool (e.g., `shell_cmd` waiting on a pipe, `code_interpreter` running a long container job) — no mid-tool stop check.
- `wait_for_message()` blocking (agent_pool.py:2434-2467) — used by async-parent sleep loops; a dismissed child's thread can be parked here (though the SLEEPING-parent wakeup in `dismiss_instance()` agent_pool.py:1095-1122 mitigates the parent side).

### F5. async child: `future.cancel()` cannot interrupt started work

`async_tools.py:181-213` `clear_pending()`:
- `entry.future.cancel()` — Python `Future.cancel()` only succeeds if the task **has not started**; a running worker thread is not interrupted.
- The docstring is explicit: *"already-running threads will complete normally but results are discarded when the pending list is removed."*
- Because `_execute` (async_tools.py:114-157) enqueues the result into the pool message queue even for terminated parents, and `terminate_instance` clears the queue (agent_pool.py:1032-1038), the result is discarded — but the LLM work still ran to completion.

### F6. Dismissing a RUNNING agent is allowed (single path)

- `handle_dismiss_agent` single-instance path (tool_dispatcher.py:455-461) has **no active-state guard** — unlike the all_idle path (tool_dispatcher.py:414). It will call `dismiss_instance()` on an agent whose state is RUNNING.
- `dismiss_instance()` then calls `terminate_instance()` (agent_pool.py:1090-1093), which marks it terminated but cannot stop the thread (F3/F4/F5).

### F7. Double-dismiss / re-entry is defended but results are "reported as dismissed"

- `remove_instance()` pops the instance (agent_pool.py:879), so a second dismissal gets `[status=not_found]` (tool_dispatcher.py:455-456). No crash, but the UI is told "dismissed" while the thread may still be mid-LLM-call.

---

## Runtime Behavior Scenarios

1. **Async child dismissed mid-LLM-call**: The child thread (ThreadPoolExecutor worker) keeps streaming/completing the LLM response; `terminate_instance` clears queues and streaming responses; `remove_instance` discards the terminated marker; the worker finishes, `_execute` tries to enqueue to a now-removed queue (agent_pool.py:2361 recreates it! — `enqueue_message` does `setdefault`), then the wrapped `run_child_agent` (agent_pool.py:2564-2585) completes. The "dismissed" child may effectively resurrect as a leftover queue entry, or its thread simply finishes silently. **Model tokens are still consumed.**

2. **Sync child dismissed**: `call_agent` sync path runs the child inline in the parent's thread (tool_dispatcher.py:558-569). If parent A → child B (sync) and B is dismissed while running, the parent thread is blocked inside `run_child_core`; the dismissal sets B terminated but A's thread continues waiting for B's `run_child_core` to return, which itself is blocked in the LLM call. A cannot proceed until the LLM call returns. This is the scenario from todo.md:93 — dismissal does not release the caller.

3. **Main/orchestrator dismissed**: Root instances are protected — single path blocks supervisor/self (tool_dispatcher.py:439-442), all_idle skips root (tool_dispatcher.py:405-406), and the UI handler `ws_handlers.py:515-533` refuses to terminate root (falls back to IDLE + stop event). So the main agent thread is effectively never dismissed; only sub-agents are.

4. **Idle-only dismissals work fine**: `handle_dismiss_agent(all_idle=True)` skips RUNNING agents (tool_dispatcher.py:414), so idle sub-agents are genuinely removed without a running thread.

---

## Confidence Level

**High Confidence** on the core finding: dismissal is cooperative, not forced. Evidence is direct code inspection of the full call chain (tool_dispatcher → agent_pool → async_tools → execution_engine → api_router), corroborated by two prior memory files (`.agent_lessons/deadlock_detection_dismiss_fix.md`, `deadlock_a_b_c_dismiss_async.md`, both 2026-08-07) documenting the same limitation.

**Medium Confidence** on the "is_terminated never set" bug: confirmed by comprehensive grep (only assignment is `= False`), but I did not run a live test to observe runtime behavior (e.g., whether `_transition(TERMINATED)` path sets the field elsewhere via a property — it does not; `is_terminated` is a plain dataclass field).

---

## Open Questions / Remaining Unknowns

1. Does the engine's run loop check `_is_terminal_stop` at the **top of each turn iteration** reliably? Evidence shows checks at llm-call entry and streaming, but the top-of-loop structure is complex (multiple `yield`/`continue` paths around execution_engine.py:1300-1450). A live trace would confirm exact responsiveness windows.
2. Is the `terminated_instances.discard()` in `remove_instance()` ever intended to be the *only* terminal signal, with `_transition(TERMINATED)` state persisting via the instance object? The instance object survives in local scope of the thread even after pool removal — but `is_instance_terminated()` checks the set *first* then `inst.is_terminated` (both gone/False). This appears to be a real gap.
3. Whether the model-trashing scenario (todo.md:93) is aggravated by dismissal not releasing the parent's blocked sync call — strongly suggested by scenario 2 but requires a live reproduction to confirm the exact interleaving.

---

## Suggested Next Actions

1. **Fix the termination signal durability** (smallest, highest-value): in `terminate_instance()`, set `inst.is_terminated = True` before `remove_instance()` discards the set entry. This makes `is_instance_terminated()` return `True` for the thread's entire remaining lifetime.
2. **Add a pre-LLM-call and post-blocking-op stop check** in `api_router.call_with_fallback()` to raise promptly; consider wrapping `execute_with_sem` so termination during slot-wait also aborts (the 30s acquire window is currently uninterruptible).
3. **For true forced cancellation**: introduce a per-instance "cancel" flag checked at the fine-grained sites above, or (for genuinely stuck synchronous tools) use process-level isolation (child in separate process that can be killed). Python threads cannot be force-killed safely; that is a hard constraint.
4. **Close the sync-caller gap**: when a sync child is terminated by dismissal, the parent blocked in `run_child_core` should be released promptly (e.g., a stop-aware wait/exception path in `child_runner.run_child_core` rather than waiting for the LLM call to finish).
5. Consider documenting/UX: show "[dismissed — thread still finishing]" status instead of claiming full dismissal.

---

## Supporting Evidence (file:line refs)

- `agent_cascade/tool_dispatcher.py:160-164` — dismiss_agent routing
- `agent_cascade/tool_dispatcher.py:394-432` — all_idle path skips active
- `agent_cascade/tool_dispatcher.py:438-461` — single path, no active guard
- `agent_cascade/agent_pool.py:966-1007` — terminate_instance: mark + cancel + kill shells
- `agent_cascade/agent_pool.py:871-880` — remove_instance: pop + `terminated_instances.discard`
- `agent_cascade/agent_pool.py:1064-1130` — dismiss_instance: cascade + terminate + remove
- `agent_cascade/agent_pool.py:2434-2467` — wait_for_message blocking
- `agent_cascade/agent_instance.py:238` — `is_terminated: bool = False` default
- `agent_cascade/agent_instance.py:546-562` — state transitions (TERMINATED terminal)
- `agent_cascade/async_tools.py:76-80, 109-112, 181-213` — ThreadPoolExecutor + clear_pending (best-effort cancel)
- `agent_cascade/agent_pool.py:2550-2594` — run_child_agent async wrapper
- `agent_cascade/execution_engine.py:1835-1847, 1891-1912` — terminal-stop / stopped helpers
- `agent_cascade/execution_engine.py:1936, 3062, 3091, 1411, 4601, 3958` — cooperative check sites
- `agent_cascade/api_router.py:303, 1363-1423` — slot semaphore + pre-call termination checks (no mid-call interrupt)
- `agent_cascade/api_server.py:1119-1121` — main agent daemon thread spawn
- `agent_cascade/run_agent_unified.py:38-73, 134-137` — main thread loop + is_stopped helper
- `agent_cascade/ws_handlers.py:504-546` — UI terminate handler (root protected)
- `agent_cascade/lifecycle_manager.py:495-496` — only `is_terminated = False` assignment

## Related Memory Files

- `.agent_lessons/deadlock_detection_dismiss_fix.md` (2026-08-07) — prior fix: termination checks in api_router, SLEEPING-parent wakeup; explicitly notes "Does NOT interrupt mid-stream LLM calls"
- `.agent_lessons/deadlock_a_b_c_dismiss_async.md` (2026-08-07) — prior root-cause: dismiss doesn't terminate running async child; suggested cooperative-cancellation flag
- `.agent_lessons/cascade-4bugs-ui-tabs-dismiss-slots-skills.md` (2026-08-06) — all_idle dismissing async agents before active_stack append

## Reviewer Verification

**Independent review performed** (2026-08-10, reviewer agent): All nine numbered claims verified against source by direct code inspection. Final verdict: **CONFIRMED** — no corrections needed. Additional confirmation of the latent `is_terminated` bug and the accuracy of the stop-check gap analysis.