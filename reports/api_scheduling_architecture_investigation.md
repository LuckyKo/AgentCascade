# API Scheduling Architecture — Investigation Report

**Investigator:** api_sched_investigator
**Date:** 2026-08-10
**Task:** Understand how endpoint allocation and agent scheduling works in AgentCascade, to prepare for fixing a bug where two sync agents run in parallel on the same API endpoint slot, causing model trashing.

---

## Executive Summary

The scheduling system is a **semaphore-based slot system operating at the agent-lifecycle level**, not a queue-based scheduler. The core component is `EndpointScheduler` (inside `api_router.py` — there is **no** `endpoint_scheduler.py` module). Slots are acquired by `engine.run()` when an agent starts and held until the agent exits or sleeps. Sync/async routing in `call_agent` is decided by slot-pool collision rules in `tool_dispatcher.py`.

The mechanism mostly works on paper, but the reported bug (two sync agents interleaving turns on the same slot) is made possible by **three structural gaps**:

1. **No queuing/fairness** — slot contention is a raw `threading.Semaphore.acquire(timeout=30s)`. Waiters are not tracked as a queue, there is no ordering guarantee, and on timeout the waiting agent **fails with an error** (proceeds without the slot after caller re-acquire failure, or aborts) rather than waiting in line.
2. **Async child chains can bypass slot ownership** — an async child (B) that spawns a sync grandchild (C) on the *same pool the original caller (A) still needs* can hand A's pool slot to C while A awaits B. A and C then run turns interleaved → model trashing. This is exactly the scenario in `todo.md` line 93.
3. **Slot key vs. actual endpoint skew** — the slot key is computed from the *first* entry of the endpoint chain, but `call_with_fallback` can rotate chains and fall over to other endpoints (Tier 1→2→3→4, per-instance cursor). A slot acquired for api_base X does not guarantee the actual API calls stay on X.

---

## 1. Component Map

| Component | File / Location | Role |
|---|---|---|
| `EndpointScheduler` | `api_router.py:227-636` | Per-slot semaphores + active counters; the ONLY slot gate |
| `APIRouter` | `api_router.py:641+` | Endpoint config, priority chains, concurrency resolution, Layer-2 per-call semaphores |
| `AgentPool._acquire_slot()` | `agent_pool.py:2554-2590` | Resolves concurrency + api_base for an agent class, delegates to scheduler |
| `AgentPool.register_async_call()` | `agent_pool.py:2592+` | Spawns async children on `AsyncToolRegistry` (ThreadPoolExecutor, 4 workers) |
| `ExecutionEngine.run()` | `execution_engine.py:1134-1158` | Acquires slot at start; `_release_slot` in finally; re-acquire after SLEEPING wakeup |
| `ExecutionEngine._release_slot()` | `execution_engine.py:4662-4694` | Capture-nullify-release under `_state_lock` |
| `ExecutionEngine._transition_to_sleeping()` | `execution_engine.py:4695+` | Releases slot when agent sleeps so children can proceed |
| `ToolDispatcher.handle_call_agent()` | `tool_dispatcher.py:194-351` | Chooses SYNC vs ASYNC via slot collision rules |
| `ToolDispatcher._run_child_sync()` | `tool_dispatcher.py:519-615` | Releases caller slot, runs child inline, re-acquires |
| `ToolDispatcher._reacquire_caller_slot()` | `tool_dispatcher.py:661-712` | Re-acquires caller slot (2 attempts × 0.1s) |
| `child_runner.run_child_core()` | `child_runner.py:66+` | Shared core for sync and async child execution |
| `AsyncToolRegistry` | `async_tools.py:50+` | ThreadPoolExecutor(4) for async children |

---

## 2. Endpoint → Slot Assignment (sync vs async)

Concurrency is defined per **endpoint** (`APIEndpoint.concurrency_limit`, `api_router.py:129`):

- **`-1` = unlimited**: `scheduler.acquire()` returns `None` immediately — no slot, no gate.
- **`0` = sequential**: all such endpoints share **one global slot key** `'_shared_sequential_slot_'` (Semaphore(1)). Deliberate design: prevents KV-cache trashing from interleaving across different API bases (`api_router.py:278-281`).
- **`N > 0` = parallel**: per-`api_base` `Semaphore(N)`; up to N agents may hold it.

Slot key: `'_shared_sequential_slot_'` if conc==0, else `api_base` (`api_router.py:281`, `tool_dispatcher.py:98-121`).

Effective concurrency resolution: `APIRouter.get_effective_concurrency(agent_type, caller_agent_type)` (`api_router.py:850-892`) — Tier 1 own endpoints → Tier 2 caller inheritance → Tier 3 default by api_base → conservative `0` if a default api_base exists but isn't registered → `-1` if truly no config.

---

## 3. Scheduler / Routing flow (multi-agent turn requests)

```
User message / async result → pool message queue
  → engine.run(instance)  [per-instance thread]
      → acquire slot (blocking, 30s timeout)          # LAYER 1: lifecycle gate
      → loop turns:
          → LLM call via call_with_fallback            # LAYER 2: per-call semaphore + endpoint chain
          → tool call → handle_call_agent
              → SYNC: release caller slot → run child inline → re-acquire caller slot
              → ASYNC: register_async_call → ThreadPoolExecutor(4) → child engine.run() acquires own slot
      → sleep if async tools pending (slot RELEASED during sleep)
      → wake → re-acquire slot
      → exit → release slot (finally)
```

**Sync child = inline hijack of the caller's thread.** `_run_child_sync` → `run_child_core` → `_create_and_run_agent` → iterates `engine.run(inst)` directly on the same thread (`execution_engine.py:5175`). There is no separate thread for sync children. This is what guarantees serialization *when the rules fire correctly*: the caller physically cannot issue another LLM call while a sync child runs.

**Async child = ThreadPoolExecutor.** `register_async_call` → `AsyncToolRegistry._executor.submit()` (4 workers) → `run_child_core` → `engine.run()` on the worker thread, where it acquires its own slot.

**Root/orchestrator:** `ws_handlers.py` → `start_gen` → new daemon `threading.Thread` → `run_agent_thread_unified` → `engine.run()` — same slot acquisition path (`api_server.py:1118-1120`, `run_agent_unified.py`).

---

## 4. Serialization of sync agents — what exists

**There is no explicit queue.** Serialization is achieved solely by:

1. **Sync-path inline execution** (caller blocks; only one agent's engine loop runs at a time on that thread).
2. **The EndpointScheduler semaphore** — a second agent attempting the same slot blocks in `acquire()` for up to `ENDPOINT_SLOT_ACQUIRE_TIMEOUT` (default **30s**, `settings.py:147-148`), then raises `TimeoutError`.
3. **Slot release on SLEEPING** — when an agent goes to sleep waiting for async results, its slot is released (`_transition_to_sleeping`, `execution_engine.py:4711-4725`) so other agents on that pool can run. **This is the intended serialization handoff — but it's also where the async-chain bug lives (see §6).**

The semaphore IS the queue — an implicit FIFO by thread scheduling, with:
- **No fairness guarantee** (raw `threading.Semaphore`).
- **No priority.**
- **No re-queuing on timeout** — the waiter aborts with `TimeoutError` instead of waiting indefinitely.
- **Acquisition is interruptible** — checks termination every 1s while blocked (`api_router.py:324-356`).

---

## 5. Where locking/acquiring happens

| Step | Location | What it does |
|---|---|---|
| Initial acquire | `execution_engine.py:1151-1158` → `_acquire_slot_with_logging` → `pool._acquire_slot()` (`agent_pool.py:2554`) → `router.scheduler.acquire()` (`api_router.py:255`) | Blocking semaphore acquire; returns release callback |
| Slot holder tracking | `api_router.py:358-378` | `active_count += 1`, holder tuple `(instance, class, timestamp, acquisition_id)` |
| Release (normal exit) | `execution_engine.py:1556-1558` finally block | `_release_slot(instance, ...)` |
| Release (sleep) | `execution_engine.py:4711-4725` | Under `_state_lock`; capture-nullify-release |
| Release (stop_session) | `agent_pool.py:1562-1585` | Iterates all instances, releases every `_slot_release` |
| Release (sync child) | `tool_dispatcher.py:549-557` | Caller releases before child; child's own `engine.run()` acquires |
| Re-acquire (after sync child) | `tool_dispatcher.py:601-615`, `_reacquire_caller_slot` (`661-712`) | 2 attempts × 0.1s; on failure caller continues WITHOUT slot (logs "Subsequent calls will use ASYNC path") |
| Re-acquire (wake from sleep) | `execution_engine.py:4806-4808`, `4871-4873` | `_acquire_slot_with_logging("after_message_wakeup" / "after_stable_drain")` |
| Bypass | `_skip_slot_acquire` flag | Security/Compressor nested agents skip acquisition (they run inside an existing turn; `security_handler.py:323`, `compression/agent_invoker.py:285`) |
| Layer-2 per-call semaphore | `api_router.py:1336-1343` (`call_with_fallback`) | `Semaphore(max(1, conc))` per api_base; gates individual LLM calls only |

---

## 6. Gaps that allow two sync agents on the same slot → model trashing

### Gap A — Async child chains break slot ownership (PRIMARY, matches todo.md bug)
Scenario (from `todo.md:93`):
- A (sync, holds pool slot) → calls B **ASYNC** (different pool — allowed per rule 4) → A continues → A calls D **SYNC** (same pool as A) → **A's slot is released** to D while A waits for D.
- Meanwhile B's async thread calls C **SYNC** (needs A's pool). Since B itself holds no slot in A's pool (B is async on a different pool, its engine is suspended), C acquires the slot.
- Now **A awaits D, and C holds the same pool slot A needs**. A and C can interleave LLM turns → model trashing.
- The system has *no memory* of the fact that A still owes work on that pool while waiting for B/D — slot ownership is purely "currently held" with no reservation/fairness.

### Gap B — No queue, only a 30s timeout
- A second sync agent trying the same slot either (a) times out after 30s → `TimeoutError` → child fails, returns error to caller — the caller keeps its slot and the *retry/re-invocation* races again; or (b) the caller's re-acquire after a sync child can fail after 2×0.1s, after which the caller **runs without a slot** — two "sync" agents then run concurrently on the same pool.
- The fix requested in todo.md ("proper queue system per endpoint slot, much longer timeouts, proper multi-thread style scheduler") is **not implemented**.
- **Zombie instances on timeout:** `register_async_call` creates the child instance BEFORE slot acquisition (instance created in `_create_and_run_agent` → `find_or_create_instance`, then `engine.run()` acquires the slot). On TimeoutError the child is dismissed via the cleanup in `run_child_agent`'s except block (`agent_pool.py:2651-2656` — this fix is already applied per `docs/async_slot_timeout_fix_plan.md`), but the parent has already received an error string — the work is lost, not queued.

### Gap C — Slot key ≠ actual endpoint used
- Slot key derives from `router.get_llm_config()` first-chain-entry api_base (`agent_pool.py:2576-2577`), but `call_with_fallback` iterates the full chain (`get_endpoint_chain`, `api_router.py:1022+`), rotates per-instance cursor on failure (`advance_instance_endpoint`), and falls back Tier 1→2→3→4. An agent holding the slot for api_base X can then make calls on api_base Y (or the general default), whose own semaphore/slot pool never got a hold — so Y can be oversubscribed.
- Related skew: `_acquire_slot` passes `caller_agent_type` to `get_effective_concurrency` but **not** to `get_llm_config` (`agent_pool.py:2573` vs `2576`) — inherited-endpoint children can resolve a different api_base than the concurrency they were gated on.

### Gap D — Root agents don't hold slots between turns
- The slot is scoped to a single `engine.run()` invocation. Multiple root sessions (multi-session, or parallel AC instances with `--instance-id`) each acquire/release per user message → interleaved turns on the same endpoint are possible at the session level.

### Gap E — Layer-2 semaphore is per-call, not per-turn
- `call_with_fallback`'s `Semaphore(max(1, conc))` (`api_router.py:1336-1343`) only prevents parallel *calls*, not parallel *turns*. For conc=0 it is arguably redundant with Layer-1 (comment `api_router.py:1328-1333` acknowledges this). It cannot prevent turn interleaving.

---

## 7. Current safeguards that DO work

- Sync children inline on the caller's thread — physical serialization within one delegation chain.
- Shared sequential slot for all conc=0 endpoints — global hardware serialization when the rules fire.
- Slot released on SLEEPING — lets other agents run while one waits on async tools.
- Termination checks during acquire — no infinite blocking on terminated instances.
- Double-release guards (`_released` closure flag, unique `acquisition_id` matching).
- Slot holders tracking + `detect_stuck_slots()` diagnostics.
- Deadlock-prevention ancestor walk (`_find_ancestor_with_slot`) — catches direct A→B→C chains where B is async, but does NOT cover the simultaneous-branch case (A→B async + A→D sync + B→C sync).

---

## 8. Diagnostics available (for confirming the bug live)

- `router.scheduler.get_status()` → per-slot `active_count`/`max_active`/`slot_holders`.
- `router.scheduler.detect_stuck_slots(60)` → slots held > 60s with holder names.
- `router.scheduler.get_slot_holders(slot_key)` → deep-copied holder list.
- `router.get_agent_slot_info(agent_class, caller_type)` → slot_key/is_sequential/conc/api_base/needs_slot.
- `router.is_waiting(agent_name)` → Layer-2 semaphore waiters.
- Log markers: `[EndpointScheduler]` acquired/released, `[SLOT_STUCK_DETECTION]`, `[SLOT_SYNC_RELEASE/REACQUIRE]`, `[SLOT_SYNC_REACQUIRE_FAILED]`.

---

## 9. Confidence levels

- **Confirmed:** component map, slot key rules, sync-inline/async-executor threading, 30s timeout, sleep-release, no queue object anywhere (only semaphores), `todo.md:93` describes the same scenario.
- **High confidence:** the async-chain (Gap A) and no-fairness (Gap B) mechanisms are the root-cause class for "two sync agents on the same slot."
- **Moderate:** Gap C (slot key vs actual endpoint skew) contributes in endpoint-fallback configurations; needs log verification of which api_base each agent's calls actually hit vs which slot it holds.

## 10. Open questions / unknowns

1. Does the observed production scenario use conc=0 (shared sequential slot) or conc>0 parallel slots? Gap A applies to both, but the fix shape differs (global queue vs per-base queue).
2. Are there logs already showing `[SLOT_SYNC_REACQUIRE_FAILED]` or `[SLOT_STUCK_DETECTION]` around the trashing timestamps (logs dir)?
3. Is `_skip_slot_acquire` ever set on non-Security/Compressor agents in the wild (would bypass serialization entirely)?
4. What endpoints/concurrency are actually configured in the running instance (`config/` or `api_router` state)?

---

## 11. Suggested next actions (for the fix design)

1. **Introduce a real per-slot waiting queue** in `EndpointScheduler` (ordered waiters, FIFO, ticket-based) with configurable long timeouts and optional cancel-on-termination — replaces raw semaphore blocking. Keep semaphore as the *release* mechanism but track order.
2. **Reservation semantics for async chains:** when an agent sleeps/asynchronously waits, record its pending interest in its pool slot(s) so a grandchild (C in the example) cannot acquire a slot that an ancestor (A) is owed. Simplest robust form: when any ancestor in the parent chain is active or awaiting re-acquire on a pool, route the child SYNC through the ancestor's thread instead of a fresh async thread.
3. **Bind slot acquisition to the actual chain endpoint** (acquire on the concrete api_base used per call, or acquire all chain endpoints' slots, or route fallback within the same pool).
4. **Make re-acquire reliable:** after a sync child, block (with termination checks) until the caller's slot is re-acquired rather than giving up after 2×0.1s.
5. Add regression tests: A→B(async)→C(sync) on same pool; A→D(sync) concurrently; assert no interleaving.