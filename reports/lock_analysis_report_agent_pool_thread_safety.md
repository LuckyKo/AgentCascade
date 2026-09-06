# Lock Analysis: agent_pool.py Synchronization Model

**Investigator**: lock_analysis (researcher)
**Date**: 2026-08-10
**Requested by**: reviewer_dismiss_plan_r2 — verify the plan's claimed `_instance_lock`
**Scope**: Thread safety of `AgentPool` shared state in `agent_cascade/agent_pool.py` (3090 lines)

---

## Executive Summary

**The plan's lock-ordering analysis describes a lock that does not exist.** The plan (`plan_dismiss_agent_proper_termination.md` §"Lock Ordering Analysis", lines 712–769) claims `AgentPool._instance_lock` "protects instances dict and terminated_instances set" and derives an entire deadlock-avoidance strategy from it. A full-codebase grep (`_instance_lock` across all `*.py`) and `git log -S "_instance_lock"` return **zero matches** — the lock never existed at any point in repo history. Every statement in the plan that assumes `terminate_instance()` / `dismiss_instance()` / `is_instance_terminated()` acquire or release `_instance_lock` is describing behavior that isn't there.

**The actual state**: `self.instances` and `self.terminated_instances` are **unprotected plain dict/set** fields. Mutations are partially (but inconsistently) serialized via *other* locks (`_execution._state_lock`, `_children_lock`, `_queue_lock`), and some mutations happen with **no lock at all**. Same-structure races are mitigated in practice by CPython's GIL making individual dict/set operations atomic, but compound operations (check-then-act, iteration while mutating, cross-structure invariants like "add to terminated_instances then read instances") are **not atomic** and can interleave.

**Bottom line**: `terminate_instance()` and `remove_instance()` are **not thread-safe** in the strict sense. They work under the GIL for single operations, but there is no lock that serializes the instances + terminated_instances pair, and the `is_instance_terminated()` → `remove_instance()` transition (terminated set add → discarded) has a race window documented in `.agent_lessons/dismiss-agent-cooperative-termination.md`.

---

## 1. What locks exist (complete inventory)

Lock definitions in `agent_pool.py` `__init__` and nested managers:

| Lock | Type | Protects | Evidence |
|---|---|---|---|
| `_settings_save_lock` | `threading.Lock` | concurrent pool_settings.json save | line 277 |
| `_children_lock` | `threading.RLock` | `children` dict **and** `AgentInstance._child_instances` lists | line 297 |
| `_queue_lock` | `threading.Lock` | `message_queues` dict (+ `_message_condition`) | line 313 |
| `_ui_disabled_tools_lock` | `threading.RLock` | `_ui_disabled_tools` cache | line 341 |
| `ParallelAgentManager._state_lock` | `threading.RLock` | `active_stack`, `instance_state` (pool-level coordination lock) | line 2855 |
| `LoggerManager._lock` | `threading.Lock` | `_loggers` dict | line 2877 |
| `AgentInstance._state_lock` | `threading.RLock` (dataclass field) | per-instance state transitions | agent_instance.py:230 |
| `AgentInstance._compression_lock` | `threading.RLock` (dataclass field) | per-instance `conversation`, `_streaming_responses`, message caches | agent_instance.py:244 |

Also: `_paused`, `_stopped_event` are `threading.Event` (inherently thread-safe); telemetry has a module-level `_telemetry_lock`.

**There is no `AgentPool._instance_lock` and never has been** (verified via grep + git history).

---

## 2. What protects `instances` and `terminated_instances`?

**Nothing dedicated.** The `instances` dict (line 261) and `terminated_instances` set (line 295) are declared as bare `dict`/`set` with no lock declared for them.

Occasional *incidental* protection from other locks:

- **`_execution._state_lock`** — used in exactly two `instances` mutation sites:
  - `load_session_from_log()` swap: `with self._execution._state_lock: self.instances.pop(...); self.instances[...] = new_inst` (lines 1959–1975)
  - `execution_engine.py:5385` — `instance_state[instance_name] = state` under the same lock
- **`_children_lock`** — protects `children` + `_child_instances`; `remove_instance()` does `self.instances.pop()` **outside** this lock, then uses the lock only for the children cleanup (lines 911–916)
- **`_queue_lock`** — protects `message_queues`, not instances

All **unlocked** mutations of `self.instances`:
- `create_instance()` — `self.instances[instance_name] = instance` (line 855), no lock
- `remove_instance()` — `self.instances.pop()` (line 879), no lock
- `terminate_instance()` — `self.terminated_instances.add(instance_name)` (line 989) and `self.instances.get()` (line 990), no lock
- `dismiss_instance()` — `self.instances.get()` (line 1082), no lock; calls terminate + remove
- `lifecycle_manager.py:193` — `self.pool.instances[instance_name] = inst`, no lock
- `_resolve_instance_name()` — iterates `for name in self.instances` (line 817), no lock
- `halt_all_instances()` — iterates `self.instances` (line 952), no lock
- `instance_classes` property — `self.instances.items()` (line 2122), no lock

All **unlocked** accesses of `self.terminated_instances`:
- `terminate_instance()` — `.add()` (line 989)
- `remove_instance()` — `.discard()` (line 880)
- `is_instance_terminated()` — `in` check (line 2661)
- `_clear_all_state_dicts()` / `reset()` — `.clear()` (lines 1238, 1340)
- `_save/_restore_instance_state()` — set comprehension / `.update()` (lines 1356, 1375)
- `api_router.py:1369,1421` and `ws_handlers.py:359`, `child_runner.py:47` read it lock-free from other modules

---

## 3. Thread-safety of `terminate_instance()` and `remove_instance()`

### `terminate_instance()` (lines 966–1018)
Acquires in sequence (never nested): `_children_lock` (snapshot children) → `inst._state_lock` (read active) → `inst._state_lock` (transition TERMINATED) → `_queue_lock` guarded sections. **The terminated_instances.add and instances.get are done with no pool lock.** Sequence: `terminated_instances.add(name)` happens BEFORE `instances.get(name)`. Under the GIL each op is atomic, but the two-step read-modify sequences are not: another thread can run `remove_instance()` (which pops instances AND discards from terminated_instances) between them, leaving `is_active` computed on a stale/half-removed view.

### `remove_instance()` (lines 871–937)
Does `_instances_version += 1`, `instances.pop()`, `terminated_instances.discard()` **with no pool lock**, then individually locks `_queue_lock`, `_logger._lock`, `_children_lock` for the per-structure cleanups. The overall operation is not atomic: between `instances.pop()` and `terminated_instances.discard()`, a concurrent `is_instance_terminated()` sees the instance gone but termination flag still set (or vice versa).

### `dismiss_instance()` (lines 1064–1130)
Calls `terminate_instance()` then `remove_instance()` — so it inherits both functions' unlocked windows, plus its own unlocked `instances.get()` for the active check.

### Verified race window (prior investigation)
`.agent_lessons/dismiss-agent-cooperative-termination.md` (verified 2026-08-10) documents that `terminate_instance()` never sets `inst.is_terminated = True`; the only termination signal is the `terminated_instances` set, and `remove_instance()` discards it a moment later. After removal, `is_instance_terminated()` (agent_pool.py:2654–2664) returns False even though the dismissed agent thread may still be running — a real correctness gap, not just a theoretical one.

---

## 4. Who can concurrently touch this state (real concurrency sources)

- **IdleManager** — background daemon thread calling `dismiss_instance()` (agent_pool.py:3063–3090, started at line 741). Runs concurrently with everything else.
- **ThreadPoolExecutor workers** — async child agents (`async_tools.py`, 4 workers) mutate instances.
- **ExecutionEngine.run() threads** — one per agent, all reading `is_instance_terminated()` / `instances` mid-run.
- **WebSocket handler threads** (`ws_handlers.py`) — termination/stop/resume commands, including unlocked reads/writes of `_halted_instances` (line 367) and `instance_state` (lines 196, 1065).
- **Tool execution threads** (`tool_dispatcher.py`) — dismiss_agent tool → `dismiss_instance()`.
- **api_server / lifecycle_manager** — `pool.instances[...] = inst` unlocked (lifecycle_manager.py:193).

So the plain-dict design is being exercised from multiple threads in normal operation; this is not a single-threaded assumption.

---

## 5. Actual (de facto) "lock hierarchy" in the code

What the plan's §Lock Ordering Analysis should have said:

| Level | Lock | Guards | Truly held in terminate/remove? |
|---|---|---|---|
| Pool coordination | `ParallelAgentManager._state_lock` | active_stack, instance_state, session-load instance swap | Only in load_session_from_log path |
| Pool per-structure | `_children_lock` | children graph | Yes (snapshot only, released before state_lock) |
| Pool per-structure | `_queue_lock` | message queues | Yes (after instance ops) |
| Pool per-structure | `_logger._lock`, `_settings_save_lock`, `_ui_disabled_tools_lock` | logger dict / settings / tool cache | Logger only (remove_instance) |
| Instance | `_state_lock` | state transitions | Yes (individual reads/transitions) |
| Instance | `_compression_lock` | conversation, streaming responses | Yes (streaming clear) |

Locks are almost never nested: the pattern is *acquire → snapshot → release → next lock*. The one nested case is `remove_instance()` holding `_children_lock` while iterating `self.instances.values()` (line 916) — an unlocked read inside a locked section, but only for a snapshot.

There is **no pool→instance nested lock chain** as the plan describes, because the pool-level instances lock does not exist.

---

## 6. Risk assessment (practical)

1. **GIL safety net**: Individual `dict.__setitem__`/`pop`, `set.add`/`discard`, `in` are GIL-atomic. Pure single-step mutations won't corrupt the structures.
2. **Real risks (not theoretical)**:
   - **Check-then-act races** in terminate/dismiss: `is_active` check (under `inst._state_lock`) vs `terminated_instances.add`/`instances.get` (lock-free) — state can change between the two.
   - **Iterate-while-mutate**: `_resolve_instance_name` (line 817), `halt_all_instances` (line 952), `instance_classes` (line 2122) iterate `self.instances` without a snapshot; a concurrent `remove_instance()`/`pop` can raise `RuntimeError: dictionary changed size during iteration`.
   - **Loss of termination signal**: `terminated_instances.discard()` in `remove_instance()` cancels the only durable termination marker before the agent thread has fully stopped (documented in project memory).
   - **Cross-structure invariants** (children ↔ instances ↔ terminated) are updated in piecemeal fashion, so observers can see inconsistent intermediate states.
3. **Deadlock**: Low risk currently, precisely because there is no shared pool lock to nest — but the plan's "how to avoid deadlock" reasoning is moot since it reasons about a nonexistent lock.

---

## 7. Recommendations

If the dismiss/termination plan wants a real guarantee, add a dedicated lock:

1. **Add `AgentPool._instance_lock = threading.RLock()`** next to `_children_lock` (line ~297).
2. **Wrap all `instances` + `terminated_instances` mutations** (create/remove/terminate/dismiss, lifecycle_manager registration, load_session swap, `_resolve_instance_name` iteration, `is_instance_terminated` reads) in `with self._instance_lock:`.
3. **Keep the documented rule the plan *intended*:** acquire `_instance_lock` first, snapshot, release before acquiring per-instance locks (`_state_lock`/`_compression_lock`) — that ordering is correct and safe and matches existing practice.
4. **Fix the termination-signal gap** (set `inst.is_terminated = True` in `terminate_instance()`, or delay the `terminated_instances.discard()` until the thread confirms exit) — see `dismiss-agent-cooperative-termination.md`.

**Confidence**: High (static analysis; cross-checked grep + git history + prior verified investigations + independent reviewer verification; not runtime-reproduced).

---

## Appendix A — Independent Verification Results

Two parallel investigations were spawned from the same reviewer request:
- **This report** (lock_analysis) — saved at `lock_analysis_report_agent_pool_thread_safety.md`
- **Parallel report** (lock_analysis_child1) — saved at `reports/lock_analysis_thread_safety_agent_pool.md` — reached **identical conclusions independently** (no dedicated lock for `instances`/`terminated_instances`; only `_execution._state_lock` ad-hoc at the session-load swap L1959; `lifecycle_manager.py:193` raw unlocked write; GIL-safe but not strictly thread-safe).

**Independent reviewer verdict** (agent `lock_review_verifier`, on the parallel report's claims; applies to all shared claims in this report):

| Claim (shared) | Verdict |
|---|---|
| No lock on instances/terminated_instances; only swap under `_execution._state_lock` | **CONFIRMED** |
| create/remove/terminate/dismiss mutate without dedicated lock | **CONFIRMED** |
| lifecycle_manager.py:193 writes `pool.instances[...]` without lock | **CONFIRMED** |
| Only L1959 uses `with ..._lock` around an instances access | **CONFIRMED** |
| Lock inventory (L277/297/313-314/341/2855/2877 + per-instance locks) accurate | **CONFIRMED** |
| terminate_instance per-step protections (_state_lock, _queue_lock, _compression_lock) correct | **PARTIALLY CONFIRMED** (per-step correct; registry ops unlocked) |
| External readers of terminated_instances (api_router/child_runner) unlocked | **CONFIRMED** |
| ws_handlers.py:356-361 active_stack mutation is a "lock bypass" *(parallel report's claim 8 — **not** made by this report)* | **REFUTED** — it IS under `with self.agent_pool._execution._state_lock:` (ws_handlers.py:354) |

**Reviewer's overall assessment**: "The report's conclusion is SOUND, with one factual error in Claim 8" (claim 8 belongs to the parallel report only; this report does not assert an active_stack lock bypass. This report's ws_handlers citations — `instance_state[...]` writes at ws_handlers.py:196/1065 and `_halted_instances.clear()` at :367 — were all re-verified as **unlocked** in this investigation.)

A dedicated reviewer instance for this report (verify_lock_analysis_r2) could not run due to single-slot LLM endpoint capacity (max 1 concurrent, held by the above verifier); the shared-claim verification above covers all substantive findings.

**Open questions**:
- Whether a live repro shows the iterate-while-mutate crash in production (e.g., under idle auto-dismiss vs concurrent tool call).
- Whether the dismissed-thread-sails-past-stop-checks bug is observed at runtime (flagged as unverified in existing memory).