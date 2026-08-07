# Investigation Report: Deadlock A→B→C & Dismiss Agent Not Terminating Async Child

**Date:** 2026-08-07
**Investigator:** researcher (investigate_deadlock_dismiss)
**Scope:** `N:\work\WD\AgentCascade` — slot collision detection + dismiss_agent async termination

---

## Executive Summary

Both reported issues are **confirmed** as real defects in the current code.

1. **A(sync)→B(async)→C(sync) deadlock:** The slot-collision logic in `tool_dispatcher.py` only compares **the direct caller vs the child** (one level deep). It does **NOT** walk the parent/ancestor chain. When B (async child) calls C, and C needs the slot held by A (B's ancestor), no collision is detected, C is launched ASYNC, and C blocks on the endpoint semaphore held by A. A is SLEEPING waiting on B. → Deadlock (relieved only by a 30s slot-acquire timeout, not by a meaningful reject).

2. **dismiss_agent doesn't terminate a running async child:** `ThreadPoolExecutor` futures cannot force-kill an already-running thread. `future.cancel()` only works pre-start. Dismissing B removes it from the pool but the running LLM/agent thread continues. Furthermore, dismissing an async child **never wakes/notifies its parent**, so the parent is left **trapped in SLEEPING** waiting on a result that will never arrive.

---

## Issue 1: Slot collision detection is single-level only (no ancestor chain)

### Where the logic is supposed to be — and is
`tool_dispatcher.py:256-318` — `handle_call_agent()` slot-collision section.

The routing decision (lines 315-318):
```python
if caller_holds_slot:
    return self._run_child_sync(...)      # sync
else:
    return self._run_child_async(...)    # async
```

`caller_holds_slot` determination (lines 277-313):
- **Case 1** (`child_slot_info['needs_slot']` False, conc=-1) → always `False` (async).
- **Case 2** (`child_slot_info['is_sequential']`, conc=0) → always `True` (sync).
- **Case 3** caller holds no slot → `False`.
- **Case 4/5** caller holds a slot → compute caller's slot **key** and child's slot **key** and compare (lines 296-313):

```python
caller_slot_key = '_shared_sequential_slot_' if caller_is_sequential else caller_api_base
child_slot_key = child_slot_info['slot_key']
if caller_slot_key == child_slot_key:   # Case 5 same pool -> collision -> SYNC
    caller_holds_slot = True
else:                                   # Case 4 different pool -> no collision -> ASYNC
    caller_holds_slot = False
```

### Confirmed defect
The comparison is **strictly between `caller_slot_holder` (the DIRECT caller B) and the requested child C**. There is **no traversal of `parent_instance` / ancestor chain**. The `_check_nesting_depth` (line 648) counts depth but does not inspect ancestor slots.

Config paths verify the slot-key model:
- `api_router.py:253-254`: `slot_key = '_shared_sequential_slot_' if is_sequential else api_base`
- `api_router.py:853-882` `get_agent_slot_info()`: builds child slot info.
- `execution_engine.py:865` & `agent_pool.py:2453` `_acquire_slot`: child acquires its own slot inside `engine.run()` via `EndpointScheduler.acquire()`.

### The failing scenario (traced)
1. **A** (root) holds slot **K1** (`_slot_release` set, RUNNING).
2. **A calls B** (async): B's slot key differs from A's direct key (or B is a different agent class/endpoint), so **Case 4** → `caller_holds_slot=False` → `_run_child_async` (line 318). **A keeps its slot K1.** B runs in the `AsyncToolRegistry` ThreadPoolExecutor (`agent_pool.py:2513 run_child_agent` → `engine.run`).
3. **B calls C** (sync intent) where C needs slot **K1** (same endpoint as A, or the shared sequential slot).
4. `handle_call_agent` in B's context compares **B** (slot K2) vs **C** (slot K1) → keys differ → `caller_holds_slot=False` → C launched ASYNC.
5. C's `engine.run()` → `EndpointScheduler.acquire(K1)` (`api_router.py:303`) blocks on the semaphore countet by **A** (still holding slot1), which is SLEEPING waiting on B.
→ **Deadlock.** A waits B; B waits C; C waits slot held by A.
6. The only relief is `ENDPOINT_SLOT_ACQUIRE_TIMEOUT=30` (`settings.py:108`) → `TimeoutError` propagated, then `run_child_agent` except-path calls `self.dismiss_instance(child)` (`agent_pool.py:2552`).

### "Should reject" requirement
There is **no** ancestor-slot check. The code only has a **direct caller** collision detector. The check that "my ancestor holds this slot" does **not exist**.

**Fix direction:** In the `handle_call_agent` block (tool_dispatcher.py:296-313), walk `instance.parent_instance` chain; if any ancestor is RUNNING/SLEEPING and holds `_slot_release` for the slot pool the child needs, **reject** (or choose forced chain-aware sync) **before** launching async.

---

## Issue 2 — dismiss_agent does not terminate a running async child; parent left SLEEPING

### Where dismiss lives
- Entry: `handle_dismiss_agent` — `tool_dispatcher.py:320-423`
- Pool removal: `dismiss_instance` — `agent_pool.py:1056-1093`
- Termination: `terminate_instance` — `agent_pool.py:958-1041`
- Removal: `remove_instance` — `agent_pool.py:863-929`
- Backend worker: `register_async_call` → `run_child_agent` — `agent_pool.py:2491-2557`
- Registry: `AsyncToolRegistry` — `async_tools.py:49-211`

### State transitions confirmed
`dismiss_instance` → if `is_active` (state in ACTIVE_STATES) → `terminate_instance(set_global_stopped=False)`:
- adds to `terminated_instances`
- transitions inst state → TERMINATED (`agent_pool.py:999`)
- `clear_pending(instance_name)` cancels **pending** future (`async_tools.py:173-201`)
- `kill_all` shells, clears queue, clears streaming.
- Then `remove_instance` (line 1093) pops from `instances`, cleans child relationships (911-918), fires `_fire_on_dismissed`.

### Confirmed defect 2a: cannot actually kill a *running* thread
`async_tools.py:176-199` docstring & `clear_pending`:
> `cancel()` only works for tasks **not yet started** — already-running threads will **complete normally** but results are discarded.

The async child (`run_child_agent`, agent_pool.py:2513) is submitted via `register_async_call` (line 2557) to the ThreadPoolExecutor. If it has already started (acquired slot, running LLM loop), it keeps running to completion — it is **not** interrupted by `dismiss_agent`. Marks TERMINATED but the thread continues.

### Confirmed defect 2 — parent (A) left SLEEPING forever
- When A calls B async, A transitions to **SLEEPING** (`_transition_to_sleeping_if_pending`, execution_engine.py:4005-4032; `_transition_to_sleeping` at 4182-4200).
- A wakes only via `pool.has_pending` → message-queue draining in the sleep loop (`execution_engine.py:4228+` `_handle_sleeping_state_until_wakeup`, drain at 4257).
- `dismiss_instance` removes B and clears B's queue but **does NOT enqueue any wake-up/result message to A**, nor transition A out of SLEEPING. **A waits forever**, unless a user stop / global stop rescues it.
- Ownership: `parent_instance` on the AgentInstance + `pool.children` / `_update_child_relationship` (agent_pool.py:613) track parent→child, but only used to **cascade** dismissal downward (dismiss children), never to **notify the parent** when a child is dismissed.

### Confirmed defect 3 — no ACTIVE_STATE guard in single-instance path
- `handle_dispatcher` `all_idle` path **skips** RUNNING (380-381). 
- Single-instance path (400-423) only prevents self / supervisor dismissal (405-408) but will dismiss a **RUNNING** agent straight to `dismiss_instance` → terminate. For a *sync* child this is arguably intended (it's in the same thread as the caller so it can't be "running" separately), but for an **async** child that's the bug.

---

## Evidence / file-line index

| Concern | File | Lines |
|---|---|---|
| call_agent dispatch + slot detection | tool_dispatcher.py | 165-318 |
| Case 5 same-slot compare (single-level) | tool_dispatcher.py | 296-313 |
| SYNC/ASYNC routing | tool_dispatcher.py | 315-318 |
| async child runner | agent_pool.py | 2491-2557 |
| child's own slot acquire in engine | execution_engine.py | 854-874 |
| slot acquire blocks with timeout | api_router.py | 229, 303-315 |
| 30s timeout setting | settings.py | 108-109 |
| slot_key scheme | api_router.py | 253-254, 853-882 |
| dismiss_agent | tool_dispatcher.py | 320-423 |
| pool dismiss → terminate → remove | agent_pool.py | 1056-1093, 958-1041, 863-929 |
| future.cancel only pre-start | async_tools.py | 173-201 |
| SLEEPING transition (releases slot, waits) | execution_engine.py | 4005-4120, 4187-4200 |
| sleep-wake/whatever | execution_engine.py | 4228-4297 |
| parent-child relationship | agent_pool.py | 613-641, 863-918 |
| historical context (ｄesign) | .agent_lessons/async_call_agent_deadlock_fix.md | 2026-06-12 |

---

## Confidence
- **Confirmed.** Both root causes directly corroborated by code paths traced end-to-end, cross-checked across tool_dispatcher / agent_pool / execution_engine / async_tools / api_router, plus matching historical memory (`async_call_agent_deadlock_fix.md`, original single-level design).

## Open questions
1. Whether the rejection should be a hard failure string or a forced **chain-aware sync**. (Design choice; need hook in the 3-level selection.)
2. Whether a hard-kill of a *truly* running LLM worker thread is feasible at all (Python threads can't be killed); may require a cooperative abort flag checked inside `engine.run()` + `run_child_agent`, or accepting threadhelprd leak-but-discard while firing a wakeup to the parent.

## Recommended next actions
1. **Issue 1:** Add ancestor-chain traversal in `handle_call_agent` block (walk `parent_instance`; if any ancestor holds `_slot_release` and child needs same slot pool → reject with clear error or pick sync).
2. **Issue 2:** In `dismiss_instance`/`terminate_instance`, after terminating a child, **notify the parent** to wake it out of SLEEPING (enqueue wake message or transition parent RUNNING→processing).
3. **Issue 2b:** Add ACTIVE_STATES guard in the single-instance dismiss path if dismissal of actively-running async child is not desired.
4. Consider a cooperative abort/cancellation flag for pending async threads so dismissal is prompt.

## Handoff
Detailed line-indexed findings above. Delegate to Reviewer for independent verification before implementation.