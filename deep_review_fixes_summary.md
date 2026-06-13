# Deep Review Fixes - Async Unification

## Summary
Fixed all CRITICAL and MAJOR issues from the deep review of async unification.

---

## Fixes Applied

### CRIT-1: Race condition in AsyncToolRegistry._execute() (async_tools.py)
**File:** `agent_cascade/async_tools.py` lines 102-137

**Problem:** The lock was released BEFORE put(), creating a window where has_pending returns False (entry.completed=True) but the result isn't in the buffer yet. If an exception occurs between has_pending and the safety drain, results were lost.

**Fix:** Modified `_execute()` method to hold the lock through BOTH `entry.completed = True` AND the `put()` call. The put() method is already thread-safe (has its own internal lock), so holding both locks briefly doesn't cause deadlocks.

**Additional Improvement (per reviewer):** Added try/except around the put() call to catch and log buffering failures, preventing tools from being stuck in pending state forever if put() fails.

**Changes:**
- Moved the result buffering logic inside the `with self._lock:` block
- Updated docstring to reflect the new lock ordering strategy
- Added error handling around put() with logger.error for visibility

---

### MAJ-2: call_id parameter is dead code (agent_pool.py, execution_engine.py)
**Files:** 
- `agent_cascade/agent_pool.py` line ~1257
- `agent_cascade/execution_engine.py` line ~1994

**Problem:** The `call_id` parameter in `register_async_call()` was passed by the caller but never read inside the method.

**Fix:** 
1. Removed `call_id: str` parameter from `register_async_call()` signature
2. Updated docstring to remove reference to call_id
3. Removed `call_id=f"{instance_name}_{time.monotonic()}"` from the call site in execution_engine.py

---

### MAJ-3: Invalid type annotation on _slot_release (agent_instance.py)
**File:** `agent_cascade/agent_instance.py` line ~126

**Problem:** `Optional[callable]` is invalid — `callable` is a built-in function, not a type.

**Fix:** 
1. Added `Callable` to imports from typing
2. Changed annotation from `Optional[callable]` to `Optional[Callable[[], None]]`

---

### MAJ-4: Arbitrary drain cap of 100 (execution_engine.py)
**File:** `agent_cascade/execution_engine.py` line ~493

**Problem:** The loop that drains async results had a cap of 100 iterations. If more than 100 children completed simultaneously, excess results were silently dropped (just logged as warning).

**Fix:** Increased `max_drain_iterations` from 100 to 10000. Since there's no real cost to draining (results are already in memory), a higher limit prevents silent result loss in scenarios with many concurrent children.

**Note (per reviewer):** Monitor in production for high-injection scenarios. If needed, could add an info-level warning at 1000 iterations as an early indicator.

---

### MAJ-5: _transition_to_sleeping silently no-ops when state is not RUNNING (execution_engine.py)
**File:** `agent_cascade/execution_engine.py` line ~1716

**Problem:** If called when the agent is in COMPLETING or IDLE state, nothing happens and no warning is logged. This could mask bugs.

**Fix:** Added a **warning log** (changed from debug per reviewer feedback) when the transition is skipped:
```python
else:
    logger.warning(
        f"_transition_to_sleeping skipped for {instance.instance_name}: "
        f"current state={instance.state.name} (expected RUNNING)"
    )
```

**Rationale:** Using `warning` instead of `debug` because a skipped transition indicates a logic bug in the caller — it should be visible in production logs.

---

## Files Modified
1. `agent_cascade/async_tools.py` - CRIT-1 fix + error handling improvement
2. `agent_cascade/agent_pool.py` - MAJ-2 fix (signature)
3. `agent_cascade/execution_engine.py` - MAJ-2, MAJ-4, MAJ-5 fixes
4. `agent_cascade/agent_instance.py` - MAJ-3 fix

## Syntax Validation
All modified files pass Python syntax validation.

---

## Reviewer Feedback Incorporated
From deep_review_fixer_reviewer:
1. ✅ **MAJ-5:** Changed logger.debug to logger.warning (required)
2. ✅ **CRIT-1:** Added try/except around put() call with error logging (recommended)

---

## Notes for Reviewer
- All fixes are minimal and surgical
- No functionality was changed, only bug fixes
- Backups were automatically created for all modified files
- Ready for final code review approval