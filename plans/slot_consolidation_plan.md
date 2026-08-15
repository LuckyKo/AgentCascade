# Implementation Plan: Slot/Concurrency Consolidation (Single FIFO Queue Per Endpoint)

**Date**: 2026-08-15
**Status**: IMPLEMENTED ✅ (committed as `3d1d6f5` on 2026-08-15)
**Author**: slot_consolidation_planner (delegated by Maine)
**Investigation refs**:
- `N:\work\WD\AgentCascade\reports\layer2_concurrency_control_report.md`
- `N:\work\WD\AgentCascade\.agent_lessons\api-scheduling-architecture.md`
- `N:\work\WD\AgentCascade\.agent_lessons\fallback-fifo-slotpool-routing.md`
- `N:\work\WD\AgentCascade\plans\api_scheduler_queue_refactor_plan.md` (the prior Layer-1 refactor that created SlotPool)

---

## 0. Problem Statement

The system currently has **two overlapping concurrency-control layers** that conflict and cause self-deadlocks + model trashing:

| Layer | Component | Scope | Mechanism | Location |
|-------|-----------|-------|-----------|----------|
| **Layer 1** | `EndpointScheduler` → `SlotPool` (FIFO, ticket-based) | Agent **lifecycle** (whole turn, incl. SLEEPING) | FIFO queue + capacity permits | `api_router.py:229`, `slot_queue.py` |
| **Layer 2** | `APIRouter._semaphores` (`threading.Semaphore`) | Per **individual LLM call** | Semaphore sized `max(1, conc)` | `api_router.py:674`, used in `call_with_fallback()` ~1339-1490 |

### The conflict (root cause of the bugs)
An agent holds a **Layer-1** slot for its whole turn. While running, each LLM call also tries to acquire a **Layer-2** semaphore/slot on the *same* endpoint. Because the two layers are independent primitives:

1. **Self-deadlock**: A Security/Compressor child inherits the caller's `conc=0` endpoint (shared sequential slot). The parent holds the Layer-1 slot; the child's LLM call then tries to acquire the *same* slot at Layer 2 → blocks forever. Today this is papered over by `_skip_slot_acquire=True` + an **ancestor-walk** heuristic in `call_with_fallback()` (lines ~1367-1388) that skips per-call acquisition when an ancestor holds the slot.
2. **Model trashing**: The two layers don't agree on ordering. Layer 2 has no FIFO fairness; a second agent's call can interleave with a first agent's turn even though Layer 1 says "one at a time."

### User's vision (verbatim)
> "trash the whole semaphores, use fifo's (we can probably use existing ones too for same slot queueing), no more locks, no more ancestors, who gets slot keeps it, who doesnt waits in queue"
> "system clears parent's slot if it needs to invoke a sec or compressor agent"

### Design principles (agreed)
1. **Single FIFO queue per endpoint.** One `SlotPool` per endpoint: `conc=0 → capacity 1`, `conc=N → capacity N`. No semaphores, no dual-layer.
2. **Who gets the slot keeps it.** Once an agent acquires a slot for its LLM call, it holds it until done; no re-acquisition within the same turn.
3. **No ancestor checks.** If you need the slot and don't have it, you wait in the FIFO queue. Simple.
4. **Parent yields slot for Security/Compressor.** When a parent must invoke a Security or Compressor agent (which inherits the caller's endpoint), the system releases the parent's slot first, lets the child acquire & run, then re-acquires for the parent.

---

## 1. What Already Exists (KEEP)

The good news: **Layer 1 is already exactly what we want.** `slot_queue.py` is fully semaphore-free and is a proper FIFO ticket queue that supports N slots. We do NOT rewrite it — we make it the *only* layer.

### `SlotPool` (`N:\work\WD\AgentCascade\agent_cascade\slot_queue.py`)
- `SlotPool(key, capacity)` — `capacity=1` for conc=0, `N` for conc=N, `inf` for conc=-1 (unlimited → `acquire()` returns a no-op release lambda immediately, line 150-151).
- **FIFO ticket queue**: `_waiters: OrderedDict[ticket_id → QueueTicket]`, head = `next(iter(_waiters))`. Only the head waiter is granted when capacity frees (strict FIFO, condition-variable based, 1s tick for interruptibility).
- **Permit model**: `_running: Dict[instance_name → SlotHolder]`; permits are explicit entries, NOT semaphores.
- `acquire(instance_name, agent_class, timeout=None) -> release_cb` — fast path (capacity free) or slow path (enqueue + wait). Raises `SlotQueueTimeout` / `SlotCancelled`.
- `release(holder)` — idempotent via `acquisition_id`; stale releases ignored.
- `cancel(ticket_id | agent_name)`, `terminate_for_agent(agent_name)` — clean abort on dismiss/termination.
- `get_status()` — diagnostics (waiters + holders).

**Verdict**: SlotPool already handles per-call acquire/release cleanly, supports N slots, and has a solid timeout/cancellation story. It is the single source of truth going forward.

### `EndpointScheduler` (`api_router.py:229`)
- `_pools: Dict[slot_key → SlotPool]`, created lazily via `_get_or_create_pool(api_base, concurrency_limit)`.
- `acquire(api_base, concurrency_limit, instance_name, agent_class, pool=None, timeout=None) -> Optional[release_cb]` (line 282). Returns `None` for unlimited endpoints. Wraps the SlotPool release cb with logging (`release()` closure at line 348).
- Diagnostics: `count_active`, `get_status`, `cleanup_stale`, `get_slot_holders`, `detect_stuck_slots`, `get_slot_info`, `cancel`, `cancel_all`, `terminate_for_agent`.

**Verdict**: KEEP. It is the thin wrapper that maps `(api_base, concurrency_limit) → SlotPool`. It does NOT use semaphores internally (only `_pools`).

### Slot-key mapping (KEEP — critical anti-cache-trashing design)
- `conc=-1` → unlimited, no slot (`acquire` returns `None`).
- `conc=0` → **ALL** such endpoints share ONE global pool keyed `'_shared_sequential_slot_'` with capacity 1. (Deliberate: prevents KV-cache trashing from interleaving across API bases.)
- `conc=N>0` → per-`api_base` pool with capacity N.

### Existing "yield and reacquire" helpers (KEEP + REUSE)
These are the exact primitives we reuse for the Security/Compressor yield:
- `_release_slot(slot_holder, holder_name, context)` — `execution_engine.py:4676`. Thread-safe (uses `_state_lock`), nullifies `_slot_release` and `_slot_key`, invokes the release cb.
- `_acquire_slot_with_logging(instance, context)` — `execution_engine.py:878`. Acquires via `pool._acquire_slot()`, sets `_slot_release` + `_slot_key`.
- `_reacquire_caller_slot(slot_holder, name, context_label)` — `tool_dispatcher.py:688`. Queue-aware reacquire with 30s timeout; on failure clears state and degrades to async-only. **This is the proven "yield → run child → reacquire" pattern.**
- `_run_child_sync(...)` — `tool_dispatcher.py:530`. The canonical flow: release caller slot → `run_child_core()` → reacquire in `finally`.

### Instance fields (KEEP)
`agent_instance.py:284`:
```python
_slot_release: Optional[Callable[[], None]] = None   # release cb for the held slot
_slot_key: Optional[str] = None                       # key of currently-held SlotPool slot
_skip_slot_acquire: bool = False                      # ← TO BE DELETED (see §3)
_pool_ref: Optional['AgentPool'] = None               # for queue cleanup on terminate()
```

---

## 2. Target Architecture

**One FIFO queue per endpoint. One layer. No semaphores. No ancestor walks.**

```
┌─────────────────────────────────────────────────────────────────────┐
│                      Single Concurrency Layer                        │
│                                                                     │
│   EndpointScheduler (api_router.py)                                 │
│     _pools: Dict[slot_key → SlotPool]                               │
│       slot_key = '_shared_sequential_slot_'  (conc=0, cap=1)        │
│                | api_base                          (conc=N, cap=N)  │
│                                                                     │
│   SlotPool (slot_queue.py) — FIFO ticket queue + permits            │
│     acquire(instance_name, agent_class, timeout) → release_cb       │
│     release(holder)                                                 │
│     cancel / terminate_for_agent                                    │
└─────────────────────────────────────────────────────────────────────┘

Rule: "who gets the slot keeps it; who doesn't waits in the FIFO queue."
```

### New flow — normal agent LLM call
Today (Layer 1 + Layer 2):
```
engine.run() → acquire LIFECYCLE slot (SlotPool)      [held whole turn]
   └─ each LLM call → call_with_fallback()
        └─ acquire Layer-2 semaphore / per-call SlotPool  ← CONFLICTS with Layer 1
```

Target:
```
engine.run() → acquire slot (SlotPool)                [held whole turn — UNCHANGED]
   └─ each LLM call → call_with_fallback()
        └─ NO per-call acquisition. The agent already holds the slot for its endpoint.
           (If it somehow doesn't hold one, it waits in the SAME FIFO queue.)
```

**Key simplification**: because an agent holds its endpoint's slot for the entire turn (Layer 1), and Layer 2 was gating *the same* endpoint, Layer 2 is redundant for conc=0 and merely a cap for conc=N. We remove Layer 2 entirely. The lifecycle slot IS the concurrency control. An agent that "doesn't have the slot" simply waits in the FIFO queue — exactly the user's rule.

> **Note on "per-call" vs "per-turn":** The user said "who gets slot keeps it, who doesnt waits in queue." In practice the slot is held for the turn (as today), which is a superset of per-call holding and is strictly safer (prevents interleaving mid-turn). We keep the turn-level hold — it already satisfies "keeps it" and matches the existing `_slot_release` lifecycle.
>
> **Explicit clarification (reviewer R3): there is NO change to *when* an agent holds its slot.** It still acquires at `engine.run()` entry and releases at turn end / SLEEPING, exactly as today. The only change is the *removal of the redundant per-call second gate*. "Who gets the slot keeps it" is satisfied by the existing turn-level hold; we are not introducing a new per-LLM-call acquire/release cycle.

### New flow — Security / Compressor (parent yields)
Today: parent holds slot → child created with `_skip_slot_acquire=True` → child runs without a slot (works but is a special case that breaks the invariant "every LLM call happens while holding the endpoint's slot").

Target (reuse `_run_child_sync`'s proven pattern):
```
parent needs to invoke Security/Compressor on its own conc=0 endpoint:
  1. parent._release_slot()            # yield the shared sequential slot
  2. run child (engine.run(child))     # child acquires the SAME slot via normal path
       └─ child's LLM calls hold the slot (no _skip, no ancestor check)
  3. finally: parent._reacquire_caller_slot()   # parent gets its slot back (FIFO wait if contended)
```

This eliminates `_skip_slot_acquire` and the ancestor-walk special case. The child now behaves like any other agent: it acquires the endpoint's slot and holds it while calling the LLM.

---

## 3. What Gets DELETED

| Item | File / Line | Action |
|------|-------------|--------|
| `APIRouter._semaphores` dict | `api_router.py:674` | **DELETE** (no per-call semaphores) |
| `APIRouter._sem_lock` | `api_router.py:675` | **DELETE** (only protected `_semaphores`) |
| Layer-2 semaphore setup in `call_with_fallback()` | `api_router.py:1347-1354` (`sem = None; if concurrency_limit >= 0: ... threading.Semaphore(sem_size)`) | **DELETE** |
| Semaphore acquire/release inside `execute_with_sem()` | `api_router.py:~1456, 1463, 1490` (`with self._sem_lock`, `sem.acquire()`, `sem.release()`) | **DELETE** — collapse to a plain call (or keep only the SlotPool release path) |
| Ancestor-walk deadlock prevention in `call_with_fallback()` | `api_router.py:~1367-1388` (`_ancestor = inst; for _depth in range(10): ...`) | **DELETE** — no more ancestor checks |
| `already_holds` per-call SlotPool path in `call_with_fallback()` | `api_router.py:~1356-1421` (the whole "Two-layer concurrency" block that does a *second* SlotPool acquire) | **DELETE** — the lifecycle slot already covers it; do not re-acquire within a turn |
| `PER_CALL_TIMEOUT = 30.0` constant | `api_router.py:102` | **DELETE** (no per-call acquisition left) |
| `reset_semaphores()` method | `api_router.py:1814-1833` | **DELETE** (nothing to reset). Update caller in `agent_pool.py:1623`. |
| `_waiting_agents` set tracking (Layer-2 waiting indicator) | `api_router.py:1452-1464, 1489-1491` (`self._waiting_agents.add/discard` under `_sem_lock`) | **DELETE** — this set backs the semaphore-wait UI indicator; replaced by SlotPool waiter state (§3 note below) |
| `is_waiting(agent_name)` semaphore-based impl | `api_router.py:2013-2017` (reads `_waiting_agents`) | **REWRITE** to query SlotPool waiter state (`pool.get_status()['waiting_count']` / whether the instance has a pending ticket), or return False if no longer meaningful. Used by `api_integration.py:829,1505-1512` for the ActivityBar "Waiting for API slot..." indicator — keep the public signature. |
| Stale "semaphore"/"Layer 2" comments & docstrings | `api_router.py:244, 296, 423, 1297`; `execution_engine.py:3100, 3129` (`gen.close() ... releases HTTP connection + semaphore`) | **UPDATE** — comments only; the `gen.close()` calls themselves stay (they still close the generator / release the HTTP connection), just drop the "semaphore" wording. |
| `_skip_slot_acquire` field | `agent_instance.py:284` | **DELETE** |
| `_skip_slot_acquire = True` (Security) | `security_handler.py:373` | **DELETE** + replace with parent-yield flow (§5.1) |
| `_skip_slot_acquire = True` (Compressor) | `compression/agent_invoker.py:217` | **DELETE** + replace with parent-yield flow (§5.2) |
| SLOT_BYPASS branch in `engine.run()` | `execution_engine.py:1154-1184` (`skip_slot_acquire = getattr(...); if not skip_slot_acquire:`) | **SIMPLIFY** — remove the flag check; always acquire the slot (the yield happens *before* `engine.run(child)` is called, so the child acquires normally). |
| `_find_ancestor_with_slot()` | `tool_dispatcher.py:483-528` | **DELETE** (no ancestor checks) |
| Deadlock-prevention block using it | `tool_dispatcher.py:347-357` | **REWRITE** — sync/async decision no longer needs ancestor-walk. Replace with the simpler rule in §5.3. |
| Ancestor-walk test helper | `tests/test_fallback_fifo_ordering.py:291-349` (`_walk_ancestors`) | **DELETE / REWRITE** tests to match new behavior |

> **Keep the SlotPool per-call path's *generator wrapper* logic if it is still needed for clean release-on-exhaustion** — but only as a safety net, not as a second gate. In practice, since the slot is held for the whole turn and released in `engine.run()`'s `finally`, no per-call generator wrapping is required. Verify during implementation that no code path relies on per-call release (see Risk R4).

---

## 4. What Gets KEPT (unchanged)

- **`slot_queue.py`** — entire module (SlotPool, QueueTicket, SlotHolder, exceptions, helpers). No changes expected; possibly add a tiny `has_pending_ticket(instance_name)` helper for `is_waiting()`.
- **`EndpointScheduler`** class core (`_pools`, `_get_or_create_pool`, `acquire`, `release` wrapper, diagnostics). Only the *callers* change.
- **Slot-key mapping** (conc=-1/0/N → unlimited / shared sequential / per-base). Unchanged.
- **Lifecycle slot acquire/release** in `engine.run()` (`_acquire_slot_with_logging` at 878, `_release_slot` at 4676, sleep transition release at ~4730-4746). Unchanged — this is now the *only* concurrency layer.
- **`_run_child_sync` / `_reacquire_caller_slot`** in `tool_dispatcher.py` — kept and *generalized* (see §5) as the yield/reacquire primitive for Security/Compressor.
- **FIFO ticket system**, cancellation on termination, timeout handling. All unchanged.

---

## 5. New Flows (detailed)

### 5.1 Security agent — parent yields slot
**File**: `N:\work\WD\AgentCascade\agent_cascade\security_handler.py`

Current (lines ~370-398): sets `sec_instance._skip_slot_acquire = True`, then runs the Security engine in a background daemon thread.

Target:
1. Before running the Security agent, if the caller (`caller_agent`) holds a slot on the shared sequential endpoint, **yield it**:
   ```python
   caller_inst = self.agent_pool.get_instance(caller_agent)
   if caller_inst and getattr(caller_inst, "_slot_release", None) is not None:
       self.engine._release_slot(caller_inst, caller_agent, "before_security_check")
   ```
2. Run the Security agent **without** `_skip_slot_acquire` (field no longer exists). Its `engine.run()` acquires the shared sequential slot normally (it inherits the caller's endpoint via `caller=caller_agent`, already set at lines 314-319).
3. In a `finally`, **reacquire** for the caller using the proven helper:
   ```python
   finally:
       # reuse tool_dispatcher._reacquire_caller_slot pattern (30s timeout, FIFO wait)
       self.engine.reacquire_for(caller_inst, caller_agent, "after_security_check")
   ```

**Threading caveat (IMPORTANT)**: Security currently runs on a **background daemon thread**, while the caller's turn is *blocked* waiting for the verdict. Because the yield→run→reacquire must be atomic with respect to the caller holding its slot, and the caller is blocked on the child result, this works only if the caller does NOT continue making LLM calls while the Security check is in flight (it doesn't — it's waiting for the tool result). The reacquire happens on the *caller's* thread after the background thread signals completion. **Decision to make during implementation**: run Security inline on the caller thread (like Compressor, like `_run_child_sync`) so yield/reacquire are trivially in-order, OR keep the daemon thread and do yield before spawn + reacquire after join. Recommend **inline** for simplicity and to match the existing sync-child pattern; this also removes the RLock/timeout dance (lines 380-398) that existed only to guard the bypass path. Flag as a design decision for review.

### 5.2 Compressor agent — parent yields slot
**File**: `N:\work\WD\AgentCascade\agent_cascade\compression\agent_invoker.py`

Current (lines ~210-224): sets `comp_instance._skip_slot_acquire = True`, then runs `engine.run(comp_instance)` **inline on the caller's thread**. The caller holds the shared sequential slot.

Target:
1. Yield the caller's slot before running:
   ```python
   caller_inst = agent_pool.get_instance(caller_name)
   if caller_inst and getattr(caller_inst, "_slot_release", None) is not None:
       engine._release_slot(caller_inst, caller_name, "before_compression")
   ```
2. Run `engine.run(comp_instance)` normally (no `_skip`). Compressor acquires the shared sequential slot in its own `engine.run()` entry.
3. In a `finally`, reacquire for the caller:
   ```python
   finally:
       engine.reacquire_for(caller_inst, caller_name, "after_compression")
   ```

Because this is already inline (same thread), yield→run→reacquire is trivially in-order — the cleanest case. **No threading risk.**

### 5.3 Sync/async child decision (tool_dispatcher) — drop ancestor walk
**File**: `N:\work\WD\AgentCascade\agent_cascade\tool_dispatcher.py`

Current rules (~305-357) use `_find_ancestor_with_slot()` to force sync when an ancestor holds the child's slot pool. With "who gets the slot keeps it, who doesn't waits in the FIFO queue," the decision simplifies:

- **Child needs no slot (conc=-1)** → ASYNC (unchanged).
- **Caller does not hold a slot** → ASYNC (child acquires its own on the async thread) (unchanged).
- **Caller holds a slot AND child needs the SAME slot pool** → SYNC. Caller yields, child acquires in `engine.run()`, caller reacquires (`_run_child_sync` — unchanged). This is exactly the existing sync path; we just remove the *ancestor-walk* pre-check because it's no longer needed: if the caller holds the slot, yielding it lets the child acquire; if some *other* ancestor (not the direct caller) held it, that ancestor would have already yielded before reaching this point (each level yields before invoking its child).
- **Caller holds a parallel slot (conc>0), child uses a DIFFERENT pool** → ASYNC (unchanged).

**Deletion**: `_find_ancestor_with_slot()` and the block at 347-357. The sync/async decision now depends only on "does the direct caller hold the child's target slot pool?" — a single, local check. This is simpler and removes the 10-level parent walk.

> **Correctness argument (why removing ancestor-walk is safe under yield):** In the old A→B(async)→C(sync needing A's pool) scenario, B doesn't hold a slot but A does; C would deadlock. Under the new model, A yields its slot *before* invoking B (if B is sync) — but B is async, so A does NOT yield for an async child. However: with "who doesn't have it waits in the FIFO queue," C simply **waits in the FIFO queue** for A's slot instead of deadlocking. The only remaining requirement is that A eventually releases (it will, when A's turn ends or A yields for its next sync child). This converts a hard deadlock into a bounded FIFO wait. **This is the single most important behavioral change and must be covered by a dedicated regression test** (see §7, T6).

### 5.4 `call_with_fallback()` — remove Layer 2
**File**: `N:\work\WD\AgentCascade\agent_cascade\api_router.py`

- Delete the semaphore setup (1347-1354), the ancestor-walk (1367-1388), and the per-call SlotPool re-acquisition block (1356-1421).
- `execute_with_sem()` becomes a plain executor: call `call_fn(...)`, handle generator vs non-generator, no semaphore acquire/release. If any defensive SlotPool release is still needed for edge cases, keep only that — but the default path performs **no** per-call acquisition because the agent already holds its endpoint's slot for the turn.
- Update all comments referencing "Layer 1 / Layer 2."

---

## 6. Files to Modify (checklist)

| File | Change type | Summary |
|------|-------------|---------|
| `N:\work\WD\AgentCascade\agent_cascade\api_router.py` | **MODIFY (major)** | Delete `_semaphores`, `_sem_lock`, Layer-2 semaphore block, ancestor-walk, per-call SlotPool re-acquire, `PER_CALL_TIMEOUT`, `reset_semaphores()`. Rewrite `is_waiting()` to query SlotPool. Simplify `execute_with_sem()`. |
| `N:\work\WD\AgentCascade\agent_cascade\slot_queue.py` | **KEEP (+ optional)** | No core change. Optional: add `has_pending_ticket(instance_name)` for `is_waiting()`. |
| `N:\work\WD\AgentCascade\agent_cascade\agent_instance.py` | **MODIFY (minor)** | Delete `_skip_slot_acquire` field (line 284). |
| `N:\work\WD\AgentCascade\agent_cascade\execution_engine.py` | **MODIFY (moderate)** | Remove SLOT_BYPASS flag check in `engine.run()` (1154-1179) → always acquire. Add/confirm a public `reacquire_for(instance, name, context)` helper (wraps `_reacquire_caller_slot` logic) usable by security_handler + agent_invoker. Keep `_release_slot`, `_acquire_slot_with_logging`. |
| `N:\work\WD\AgentCascade\agent_cascade\security_handler.py` | **MODIFY (moderate)** | Remove `_skip_slot_acquire=True` (373). Add yield→run→reacquire around the Security engine run. Decide inline vs daemon-thread (§5.1). Possibly remove RLock/timeout dance (380-398) if it only guarded the bypass path — verify first. |
| `N:\work\WD\AgentCascade\agent_cascade\compression\agent_invoker.py` | **MODIFY (moderate)** | Remove `_skip_slot_acquire=True` (216). Add yield→run→reacquire around `engine.run(comp_instance)`. |
| `N:\work\WD\AgentCascade\agent_cascade\tool_dispatcher.py` | **MODIFY (moderate)** | Delete `_find_ancestor_with_slot()` (483-528) + deadlock-prevention block (347-357). Simplify sync/async decision (§5.3). Keep/generalize `_run_child_sync` + `_reacquire_caller_slot`. |
| `N:\work\WD\AgentCascade\agent_cascade\agent_pool.py` | **MODIFY (minor)** | Update `stop_session()` line 1623: remove/replace the `reset_semaphores()` call. Verify slot cleanup still uses SlotPool `terminate_for_agent`. |
| `N:\work\WD\AgentCascade\agent_cascade\api_integration.py` | **MODIFY (minor)** | `is_waiting` consumers (829, 1505-1512) — keep working with rewritten `is_waiting()`. No logic change if signature preserved. |
| `N:\work\WD\AgentCascade\agent_cascade\api_server.py` | **VERIFY** | `is_waiting: False` defaults (564, 925) — no change expected; confirm nothing else references `_semaphores`. |

---

## 7. Test Plan

### Existing tests to UPDATE
| Test file | Why it changes |
|-----------|----------------|
| `tests/test_fallback_fifo_ordering.py` | Contains `_walk_ancestors` helper (291-349) mirroring the deleted ancestor-walk. **Test 6 ("per-call acquisition skipped when an ancestor holds the target slot") must be REWRITTEN, not just updated** — the "skip" behavior no longer exists; replace with an assertion that the child *waits in the FIFO queue* and is granted in order. |
| `tests/test_api_endpoints.py` | APIRouter behavior tests — grep for `_semaphores` / semaphore assertions; update any that inspect the deleted per-call semaphore state. |
| `tests/test_generator_finalization.py` | Asserts **semaphore** release on generator exception (lines 4-5, 57-306). Must be rewritten to assert SlotPool release semantics (or deleted if the per-call path is gone). Note: this file already has a pre-existing `_pool` fixture issue (see `.agent_lessons/fallback-fifo-slotpool-routing.md`). |
| `tests/test_endpoint_scheduler_stress.py` | "Semaphore resize during active use" (181-286) — semaphore no longer exists. Rewrite to test SlotPool capacity/resize, or delete that class. |
| `tests/test_security_handler_deadlock_fixes.py` | Verifies RLock usage + bypass behavior (100, 291). Update if the bypass path / RLock is removed (§5.1 decision). |
| `tests/test_scheduler_integration.py` | Likely references per-call acquire/release (line 941 `def release()`). Review and update to SlotPool-only flow. |
| `tests/test_call_agent_sync_async_selection.py` | Sync/async selection rules change (§5.3, ancestor-walk removed). Update expected sync/async outcomes. |
| `tests/test_concurrency_dispatch.py`, `tests/test_rate_limiting_concurrency.py` | Concurrency behavior for conc=N changes from semaphore-cap to SlotPool-permit. Verify still pass; update assertions if they inspect `_semaphores`. |
| `tests/test_nested_agent_calls.py`, `tests/test_compression*.py`, `tests/test_security_endpoint_inheritance.py` | May assert `_skip_slot_acquire` or bypass behavior. Grep and update. |

### New tests to ADD
- **T1 — Single-layer FIFO ordering**: N agents target one conc=0 endpoint; assert they run strictly in FIFO order via SlotPool (no interleaving). Reuse the 8-thread contention test pattern from `api_scheduler_queue_refactor_plan.md`.
- **T2 — "Who gets the slot keeps it"**: an agent holds its slot across multiple LLM calls in one turn; assert no re-acquisition occurs and no second agent interleaves mid-turn.
- **T3 — Security yield/reacquire**: parent (holds shared sequential slot) invokes Security → assert parent's slot is released before child runs, child acquires & completes, parent reacquires afterward (and KV state restored). No deadlock.
- **T4 — Compressor yield/reacquire**: same as T3 for the inline compression path. Assert in-order yield→run→reacquire and that the caller resumes with its slot.
- **T5 — Reacquire contention**: parent yields, another agent grabs the slot, parent's reacquire must FIFO-wait (not deadlock) and eventually succeed within 30s.
- **T6 — A→B(async)→C scenario (regression for removed ancestor-walk)**: reproduce the old deadlock scenario; assert C now *waits in the FIFO queue* and completes once A releases, instead of deadlocking. This is the key behavioral change.
- **T7 — Timeout/cancel under single layer**: a waiter times out (`SlotQueueTimeout`) or is cancelled (`SlotCancelled`) on dismiss; assert clean abort and no leaked permits.
- **T8 — conc=N capacity**: N agents on a conc=N endpoint run up to N concurrently, (N+1)th waits in FIFO. Assert permit count never exceeds N.

### Verification gates
- `pytest tests/test_slot_queue.py` (unchanged core) must stay green.
- Full suite: `pytest tests/ -k "slot or scheduler or fallback or concurrency or security or compress or nested"` before declaring done.
- Manual smoke: multi-agent cascade with a conc=0 endpoint + a Security check mid-turn; confirm no deadlock and correct KV-cache behavior (no trashing).

---

## 8. Migration Strategy

**Recommendation: incremental, staged behind the existing SlotPool (which is already live), NOT big-bang.**

Rationale: Layer 1 (SlotPool) is already the source of truth for lifecycle slots and is fully tested (`test_slot_queue.py`, `test_scheduler_integration.py`). We are *removing* a redundant layer and *re-routing* two special cases (Security/Compressor) onto the existing yield/reacquire path. Each stage leaves the system in a working, testable state.

### Stage 0 — Baseline & safety net
- Run full test suite; record current pass/fail baseline (esp. the known pre-existing `test_generator_finalization.py` `_pool` issue).
- Tag/branch. No code changes.

### Stage 1 — Add the yield/reacquire helper + Security/Compressor re-route (additive)
- Add public `ExecutionEngine.reacquire_for(...)` (wraps existing `_reacquire_caller_slot` logic).
- Rework `security_handler.py` and `compression/agent_invoker.py` to yield→run→reacquire, but **keep** `_skip_slot_acquire` as a no-op fallback flag during transition if needed.
- Update/add T3, T4, T5. Gate: these tests green + existing security/compression suites green.

### Stage 2 — Remove Layer 2 from `call_with_fallback()`
- Delete semaphore block, ancestor-walk, per-call SlotPool re-acquire in `api_router.py`; simplify `execute_with_sem()`.
- Rewrite `is_waiting()` to SlotPool; update `reset_semaphores()` callers.
- Update T1, T2 + affected tests (test_generator_finalization, test_endpoint_scheduler_stress). Gate: green.

### Stage 3 — Remove ancestor-walk from tool_dispatcher + `_skip_slot_acquire`
- Delete `_find_ancestor_with_slot()` + block 347-357; simplify sync/async decision (§5.3).
- Remove `_skip_slot_acquire` field and all references (engine.run SLOT_BYPASS branch, agent_instance, security_handler, agent_invoker).
- Add T6 (the key regression). Update test_call_agent_sync_async_selection, test_fallback_fifo_ordering. Gate: green + manual smoke.

### Stage 4 — Cleanup & hardening
- Remove now-dead code (`PER_CALL_TIMEOUT`, `_semaphores`, `_sem_lock`, `reset_semaphores`).
- Final review pass for leftover references (grep `_semaphores`, `_skip_slot_acquire`, `ancestor`, `Layer 2`).
- Full regression run + manual smoke.

**Rollback**: each stage is independently revertable (git). If Stage 3's T6 reveals a real contention problem, we can keep the yield mechanism and re-introduce a *bounded* wait rather than an ancestor-walk.

---

## 9. Risk Assessment & Mitigations

| # | Risk | Severity | Mitigation |
|---|------|----------|------------|
| R1 | **Removing ancestor-walk can still produce a circular wait** — C now waits in FIFO for A's slot. If A is itself blocked (directly or transitively) on work that needs C to finish, we get a *new* cycle: A holds slot X waiting for C; C waits for slot X. This is NOT the same as the old self-deadlock — it is a genuine inter-agent circular wait. | **HIGH** | (1) T6 regression reproduces the exact A→B(async)→C scenario. (2) The 300s `QUEUE_WAIT_TIMEOUT` acts as a **circuit breaker, not prevention** — it converts an infinite hang into a bounded `SlotQueueTimeout`. (3) **Must verify** that `engine.run()` cleans up correctly on `SlotQueueTimeout` (releases any partial state, propagates the error to the caller rather than silently continuing slotless). (4) Confirm A's release path is guaranteed (turn end / yield before next sync child) so the cycle resolves in practice. If T6 shows a real unresolvable cycle, reintroduce a *minimal* "yield before an async child that transitively needs my slot" rule — but only on evidence, not speculatively. |
| R2 | **Security on daemon thread + yield/reacquire race** — if Security stays on a background daemon thread, the reacquire must be performed by the **caller's own thread after join**, never by the daemon (a daemon cannot safely acquire the caller's slot on its behalf). A mis-ordered reacquire could let another agent in and trash KV cache. | **MEDIUM-HIGH** | **Strongly recommend INLINE execution for Security** (matches Compressor + `_run_child_sync`): yield → run → reacquire all happen in-order on one thread, no cross-thread synchronization needed, and it lets us drop the RLock/timeout dance at `security_handler.py:380-398`. If inline is truly not possible (Security must not block the caller), then: caller thread yields *before* spawn, caller thread reacquires *after join*, with the 30s timeout. Do NOT let the daemon thread do the reacquire. T3 covers both variants. |
| R3 | **`is_waiting()` semantics change** breaks ActivityBar UI indicator. | LOW | Preserve public signature; back it with SlotPool `get_status()` waiter state. Update consumers minimally. Manual UI check. |
| R4 | **A hidden code path relied on per-call (Layer 2) release** — e.g., an agent that makes concurrent LLM calls within one turn and depended on the semaphore to cap them. Removing it could allow unbounded concurrent calls on conc=N endpoints. | MEDIUM | Grep for all `call_with_fallback` callers; confirm agents make LLM calls sequentially within a turn (they do — single-threaded per turn). For conc=N, the SlotPool permit is held for the whole turn anyway, so concurrency is already bounded by agent count, not call count. T8 validates capacity. |
| R5 | **Slot-key vs actual-endpoint skew** (known pre-existing vuln #1 in `api-scheduling-architecture.md`): slot acquired for endpoint X, but `call_with_fallback` falls over to endpoint Y. Consolidation does NOT fix this and could make it more visible. | MEDIUM | Out of scope for this refactor, but **must not regress**. The lifecycle slot is keyed on the first chain entry; fallback rotation to a different conc endpoint remains a known gap. Document as follow-up; do not expand scope. |
| R6 | **Reacquire timeout leaves caller slotless** (existing behavior in `_reacquire_caller_slot`): after 30s failure the caller degrades to async-only and may run concurrently on the same pool. | MEDIUM | Keep the existing degrade-to-async fallback + warning log; T5 asserts it eventually succeeds under normal contention. Monitor `[SLOT_SYNC_REACQUIRE_FAILED]` logs post-deploy. |
| R7 | **KV-cache trashing regression** if the shared sequential slot is ever held by two agents at once (permit leak). | MEDIUM | SlotPool permits are idempotent-release guarded by `acquisition_id`; T1/T8 assert no over-capacity. Add a canary assertion in tests that `_running` count never exceeds capacity. |
| R8 | **Scope creep** — the "no more locks" phrasing could be read as removing the security RLocks / state locks too. | LOW | Clarify: "no more locks" = no more *concurrency-control* semaphores/locks for slot gating. The `_state_lock` (protects `_slot_release` mutation) and security execution RLock are **correctness** locks, not concurrency gates — KEEP them. State this explicitly in the plan to avoid over-deletion. |

---

## 10. Open Design Decisions (need Maine / user sign-off before implementation)

1. **Security execution model**: inline on caller thread (recommended) vs keep background daemon thread with pre-spawn yield + post-join reacquire. (§5.1, R2)
2. **`is_waiting()` semantics**: return "instance has a pending SlotPool ticket" (accurate) vs always False (simplest). (§3, R3)
3. **Scope of "no more locks"**: confirm we keep `_state_lock` and security RLocks (correctness), removing only slot-gating semaphores. (R8)
4. **Slot hold granularity**: confirm turn-level hold (current behavior, safer) satisfies "who gets the slot keeps it," vs a stricter per-LLM-call acquire/release. This plan assumes turn-level (no change to when slots are held). (§2 note)

---

## 11. Definition of Done
- All Layer-2 semaphore code deleted; no `_semaphores` / `threading.Semaphore` references remain in the concurrency path.
- No ancestor-walk code remains (`_find_ancestor_with_slot`, the call_with_fallback walk).
- `_skip_slot_acquire` fully removed; Security & Compressor use yield→run→reacquire.
- Single FIFO queue per endpoint is the only concurrency control.
- T1–T8 new tests green; all updated existing tests green; full regression suite green.
- Manual smoke: multi-agent cascade with conc=0 endpoint + mid-turn Security check → no deadlock, no KV trashing.
- Independent review PASS (reviewer must not be the implementer).
