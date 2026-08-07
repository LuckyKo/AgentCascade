# Bug Investigation Report — UI Tabs, Dismiss-all-idle, Async Slot Timeout, Skill Loading

**Date**: 2026-08-06
**Investigator**: bug_investigator_2
**Codebase**: `N:\work\WD\AgentCascade`
**Scope**: todo.md lines 74, 75, 76(+78-79), 81

---

## Executive Summary

| Bug | Root Cause Location | Type | Severity |
|-----|--------------------|------|----------|
| **BUG 1** (line 74) spawned sub-agent without UI tab | Async child tab-push depends on stream_update (dropped when send_queue full / throttled) AND frontend `closedTabs` filter hides re-created tabs | Backend + Frontend | High |
| **BUG 2** (line 75) dismiss-all-idle may clear running/async agents | `active_stack` is per-execution and async children not in it during the parent's idle sweep; `all_idle` guard skips by `active_set` + SLEEPING + halted — but **CREATED/queued-async** and NOT-yet-appended children can be dismissed | Backend | High |
| **BUG 3** (line 76/78-79) slot-timeout leaves child hanging | Instance is created & pushed to UI **before** the endpoint slot is acquired in `engine.run()`; on TimeoutError the child thread only logs `[Agent ... Failed]` — cleanup fix (commit `50befb0`) added `dismiss_instance` but the child **process/thread** and logfile persistence still linger | Backend | Medium-High |
| **BUG 4** (line 81) Self-Augmentation skill not consistently inserted | Two divergent injection paths + registry cleared on NONE and never re-discovered on re-enable (cache signature not invalidated) + sub-agent path appends skill only when skills enabled, but `load_full_instructions` in that path doesn't `_ensure_discovered()` | Backend | High |

---

## BUG 1 — Sub-agent spawned without a visible UI tab

**File(s)**: `web_ui/app.js` (renderSubAgents, cleanupStaleSubAgents), `agent_cascade/api_integration.py` (`_serialize_instances_incremental`, `_put_stream_update`), `agent_cascade/stream_publisher.py`, `agent_cascade/execution_engine.py` (`_create_and_run_agent`).

### Symptom
`generalist_compressor_worker_20260804_121626.jsonl` ran in background with no visible tab.

### Root-cause chain
1. **Tab creation is driven ONLY by `stream_update`/state broadcasts, never by a dedicated "agent spawned" event.** Frontend `renderSubAgents()` (app.js:3248) builds tabs out of `state.subAgents`, which is populated from `data.agent_instances` on each `stream_update`/`state` message (app.js:1593-1598). There is no explicit `agentCreated`/`agent_spawned` WS message.
2. `StreamPublisher.push_initial_state()` (stream_publisher.py:82) pushes a `stream_update` after instance creation, but it is **best-effort**: it bails silently if `_pushing_disabled`, or if `_ws_queue`/`_ws_loop` missing, and it enqueues via `_put_stream_update` which **drops on `QueueFull`** (api_integration.py:121-137, queue maxsize=128 at api_server.py:352).
3. `build_stream_update_from_pool` → `_serialize_instances_incremental` (api_integration.py:665) only re-serializes an instance when `name == instance_name OR version changed OR force_full`. For a brand-new child, `current_version` is never computed → always re-serialized (fine), **unless the enclosing `stream_update` is dropped at the queue or the throttler**.
4. **Frontend hiding — `closedTabs`.** app.js:3266 filters `state.subAgents` through `state.closedTabs` (`localStorage['agent-cascade-closed-tabs']`). `closedTabs` persists across refresh. If the user previously closed a tab for that name (or a prior run with the same `generalist_*` auto-name), the new instance of the same name is **silently hidden** — `cleanupStaleSubAgents` only removes entries for agents that no longer exist server-side, and never clears `closedTabs` for re-spawned instances. **This is the most probable cause for the observed recurring `generalist_compressor_worker`-style hide.**
5. Async children that fail slot acquisition (see Bug 3) are dismissed → then re-created on retry with the same instance name → the `push_initial_state`/`push_final_state` may fire but the earlier `closedTabs` entry still suppresses the tab.
6. Compressor (agent_invoker.py:208) uses `_create_system_agent` which DOES call `push_initial_state` (execution_engine.py:4750), so compressor tabs rely on the same fragile stream_update delivery.

### Proposed fixes
- **Primary (frontend):** When a state/`stream_update` arrives with an `agent_instances` entry whose name is in `closedTabs`, auto-remove it from `closedTabs` (and localStorage) so re-spawned agents reappear. (app.js `cleanupStaleSubAgents` / merge loop, ~line 1593-1598).
- **Secondary (backend):** Add a dedicated, lossless `agent_spawned` WS event pushed on instance creation (with a high-priority `put()` not `put_nowait`-drop) and have the frontend create the tab immediately. This removes dependence on throttled/dropped `stream_update`.
- **Defensive:** In `push_initial_state`, do not gate on `_pushing_disabled` for the *first* state of a newly created instance — the "3 consecutive errors" disable can permanently hide new tabs.

---

## BUG 2 — "Dismiss all idle" may clear running/async agents or their tabs

**File(s)**: `agent_cascade/tool_dispatcher.py` (`handle_dismiss_agent`, lines 319-397), `agent_cascade/agent_pool.py` (`dismiss_instance`, `remove_instance`, `_is_idle`), `web_ui/app.js` (cleanupStaleSubAgents).

### Root-cause chain
1. `handle_dismiss_agent` (tool_dispatcher.py:359-397) treats `all_idle=true` by iterating `self.pool.instances` and dismissing everything that is **not** in `active_set` (= `self.pool.active_stack` snapshot, line 360), not `SLEEPING`, not halted, not a root.
2. The active stack only contains agent names while they are *appended* (execution_engine.py:4534 `active_stack.append`, popped in finally at 4662). But an **async child is created and runs in a separate ThreadPoolExecutor thread** (agent_pool.py `register_async_call` → async_tools.py `register`/`_execute`, max_workers=4). During the window between submission and the child's own `active_stack.append`, the child is in state `CREATED`/`IDLE` (default per agent_instance.py:228) but **not in active_stack** — so a concurrent `dismiss_all_idle` treats it as dismissible.
3. Even worse, an async child in `SLEEPING` state is explicitly skipped (good, line 373-374 → skip), but a child that hasn't started making a LLM call yet (waiting to be deserialized in the thread pool queue) is IDLE-default and gets dismissed → its queued `run()` wakes to a removed thread → lost result.
4. If children are dismissed, `remove_instance` (agent_pool.py:808) fires `_fire_on_dismissed` → WS broadcast → frontend `cleanupStaleSubAgents` removes the tab → tab disappears even for a still-running async agent (its thread keeps running, keeps producing messages to the **caller**, but the child tab is gone).

### Proposed fixes
- Add a `CREATED`/`PENDING` eligibility check: the threaded submit state (via `AsyncToolRegistry._pending`) must be consulted before dismissing. Simplest: treat any instance with `pool.has_pending(child_instance_name)` or any entry in `_async_registry._pending` / `active_stack`/`SLEEPING` as non-idle.
- In `handle_dismiss_agent`, build `active_set = {name for name,_ in pool.active_stack}` from the **thread-safe property** (already done) **and** complement with the set of instances currently queued/executing in the async registry & async shell tracker, plus any instance whose state is `CREATED`/`RUNNING/`COMPLETING` (not just SLEEPING).
- Don't talk about `active` (sync) only: use `AgentState` values consistently (RUNNING, SLEEPING, COMPLETING plus pending-async) via the same guard `_is_idle()` uses (agent_pool:2916).
- Frontend: `cleanupStaleSubAgents` only delete tabs for names absent from server; since dismissed children are removed server-side this already protects *running* children — but ensure the broadcast is on the *dismissed* event, not a full state (already handled by `dismissal` signal in api_server.py:658).

Severity: high — a single `dismiss_agent(all_idle=True)` call during a busily-running multi-agent session can kill and hide sub-agents whose threads continue consuming slots.

---

## BUG 3 — Async slot-timeout leaves child process/instance hanging

**File(s)**: `agent_cascade/api_router.py` (EndpointScheduler.acquire → TimeoutError, lines 303-315), `agent_cascade/agent_pool.py` (`register_async_call` → `run_child_agent`, lines 2447-2491), `agent_cascade/execution_engine.py` (run() slot acquire at 1123-1129, `_create_and_run_agent` at 4488-4597), `agent_cascade/async_tools.py` (register/_execute).

### Root-cause chain
1. The error text (`Timed out after 30s waiting for endpoint slot on ... Currently held by: phase1_reviewer_worker`) comes from `api_router.py:303-315` → `TimeoutError`.
2. Flow for a failing child:
   - `tool_dispatcher._run_child_async` → `agent_pool.register_async_call` (agent_pool:2491) submits `run_child_agent` to the ThreadPoolExecutor (async_tools.py:101).
   - `run_child_agent` calls `run_child_core` → `_create_and_run_agent`, which **creates instance, builds conversation, `push_initial_state` (tab), then enters `self.run(inst)`** → inside `run()` it acquires the slot at line 1129 → `TimeoutError` raised.
   - TimeoutError propagates to the `except` at agent_pool.py:2483.
3. **Fix already partially applied (commit `50befb0`, 2026-08-05):** that `except` now calls `self.dismiss_instance(child_instance_name)`. This removes the instance from pool maps. **BUT it does NOT terminate the thread** — the ThreadPoolExecutor worker is still occupied, and if `dismiss_instance` is not reached (e.g., the exception is a `SystemExit`/other), a dormant half-started instance remains.
4. **No endpoint fallback on slot-busy:** The scheduler `acquire` blocks for `ENDPOINT_SLOT_ACQUIRE_TIMEOUT` on the *primary* endpoint's semaphore and raises after 30s (api_router.py:303-315). Slot acquisition and LLM-request routing are decoupled (`get_llm_config` always returns the primary `api_base`; a fallback endpoint wouldn't be honored — this was explicitly documented in `docs/async_slot_timeout_fix_plan.md` as too risky to implement). So the router never "uses the fallback to another API when the slot is busy" — the todo's expectation is not implemented.

### Proposed fixes
- **Make the child not idle until slot acquired** (either Fix 2 of `docs/async_slot_timeout_fix_plan.md` — acquire slot *before* creating instance, or at least set state to `RUNNING` for the child before submitting to the pool so a pending child isn't dismissed by Bug-2 sweep) — this closes the cross with Bug 2.
- **Proper thread/process cleanup:** in the `except` in agent_pool.py, add a `cancel`/`force` path: `self._async_registry.clear_pending(child_instance_name)` (and cancel the entry's future) so the worker thread is freed and no further wake-up enqueues.
- **Endpoint fallback when slot busy (the actual todo ask):** Implement slot-aware routing — i.e., when `TimeoutError` fires, before failing the child, invoke `api_router`'s `get_endpoint_chain`/fallback to remap the child's `api_base` to a less-congested endpoint, then retry `engine.run()` once. This is deferred from commit; recommend a dedicated fix (`KickEnd`/`rotate`) tied to `advance_instance_endpoint` logic used elsewhere (execution_engine.py:2504-2524).

---

## BUG 4 — Self-Augmentation skill insertion is inconsistent

**File(s)**: `agent_cascade/execution_engine.py` (`_inject_self_augmentation_skill` 583-629, `_inject_skills_to_system_message` 516-580, `_create_and_run_agent` 4488-4519), `agent_cascade/config_handlers.py` (`_handle_default_load_skill_mode` 302-315), `agent_cascade/skills/manager.py` (discover cache, `_ensure_discovered`, `load_full_instructions`), `agent_cascade/settings.py:257-259`.

### Root-cause chain
1. **Two divergent insertion paths:**
   - **Main/root agent:** `api_integration.py:305-312` and `agent_pool.py:1928-1932` call `_inject_self_augmentation_skill(pool, instance)` which first calls `skill_manager._ensure_discovered()` (execution_engine.py:621).
   - **Sub-agent via `_create_and_run_agent`:** at execution_engine.py:4490-4519 the injection happens inside the `if skill_manager and load_skill_value_upper != LOAD_SKILL_NONE:` block. This path calls `skill_manager.resolve_load_skill()` (which internally `_ensure_discovered()`s, manager.py:513) — OK for the matched skills, but the `load_full_instructions("self-augmentation")` at line 4514 is called **inside `resolve...` block only when skills are enabled** and doesn't independently `_ensure_discovered` first. If discovery hasn't run yet, `load_full_instructions("self-augmentation")` returns `None` (registry empty) → skill silently omitted.
   - The **inconsistency** thus comes from two different layout orders AND the fact that the sub-agent path's safety net is `load_skill_value`:
     - If `args.load_skill` is provided by the LLM/agent, the pool setting is ignored. `load_skill_value = args.get('load_skill')` (line 4489) → if the caller passes `NONE` or an explicit list *without* `self-augmentation`, the meta-skill is skipped.
     - If pool `default_load_skill_mode == 'NONE'` (toggle off), self-augmentation is entirely skipped for sub-agents even though the main agent's `_inject_self_augmentation_skill` also skips — OK consistency; but the toggle is **only effective for *new* agents/instances, not existing running ones** (system prompt already built) → **requires restart** for the toggle to apply to already-instantiated agent templates. That matches the todo complaint "does `Enable skills` toggle require restart to take effect?"
2. **Severer bug on toggle re-enable:** `_handle_default_load_skill_mode` (config_handlers.py:302-315) when set to `'NONE'` clears `skill_manager._skills_registry` and calls `_rebuild_index()`. But `discover()` (manager.py:204-214) short-circuits **unless `now - _cache_timestamp >= TTL` OR `compute_scan_signature(...) != _cache_signature`**. Clearing the registry **does not invalidate the cache** — so toggling NONE → AUTO in the UI does **NOT** re-discover (signature still matches), registry stays empty → no skills (including self-augmentation) load until `SKILL_CACHE_TTL_SECONDS` (default 30s) passes or paths change. This is a concrete, reproducible "skills don't consistently get inserted."
3. **Idempotency guard side effect:** `_inject_skills_to_system_message` (execution_engine.py:550-554) returns early if `sys_msg.role != SYSTEM or "## Active Skills" in sys_msg.content`. If the message was previously built with an older skill block or the ordering/`## AVAILABLE AGENTS` block changed (todo line 73), the guard prevents re-injection, so toggling skills ON for an ALREADY-BUILT conversation won't update it.

### Proposed fixes
- In `config_handlers._handle_default_load_skill_mode` when clearing the registry set `sm._cache_timestamp = 0` (or `_cache_signature = None`) so the next `_ensure_discovered()` re-scans and repopulates immediately on re-enable. (This mirrors the same "registry cleared but cache not invalidated" class).
- Make self-augmentation injection independent of `load_skill`: always call `skill_manager._ensure_discovered()` before `load_full_instructions("self-augmentation")`, and append it *outside* the `if load_skill != NONE` block (only guard on the master "skills enabled" switch).
- On toggling skills ON, proactively re-inject into all existing instances' system messages (re-run `_inject_self_augmentation_skill` on `pool.instances`) so no restart is needed.

---

## Cross-cutting risks

- **Bug 2 ↔ Bug 3 interplay:** A child that has been submitted to the async executor but not yet acquired its slot (Bug-3 window) is IDLE-default and not in `active_stack` → a concurrent `dismiss_agent(all_idle=True)` will kill it (Bug 2). Both have the same root: instance lifecycle and async-thread lifecycle are not atomic.
- **Tab reliability (Bug 1) relies on `stream_update` (throttled + dropped-on-full queue) and persistent `closedTabs` (per-refresh).** A dedicated `agent_spawned`/`agentUpdated` message removes both fragile paths.

---

## Recommended Action Order

1. **Bug 4 (highest-value, most reproducible)** — fix cache-invalidation on skills toggle in `config_handlers.py`; make self-augmentation unconditional when skills on; no-restart re-injection.
2. **Bug 2** — make `dismiss_agent(all_idle)` consult async-registry pending set + non-IDLE states before dismissing; use the same guard as `_is_idle`.
3. **Bug 1** — clear `closedTabs` for re-spawned instances + add `agent_spawned` WS event.
4. **Bug 3** — actually implement the documented "fallback to another endpoint on slot-busy" (design deferred in `docs/async_slot_timeout_fix_plan.md`); plus cancel pending registry entry on timeout.

---

## Open Questions / Uncertainties
- Whether the `generalist_compressor_worker_*` auto-names come from the LLM picking an `instance_name` for a compressor-class delegation or from an auto-gen naming helper — not found in code; likely the orchestrator LLM chose the name. The tab-hide is consistent with `closedTabs` retention regardless.
- Whether `ENDPOINT_SLOT_ACQUIRE_TIMEOUT=30s` is configurable at runtime (seen default value only); a lower value would shrink the hang window.
- Full confirmation of Bug-2 multi-threaded `_async_registry._pending` visibility would benefit from a directed unit test.