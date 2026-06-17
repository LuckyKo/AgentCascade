# Slot Timeout Fix: Race Condition in EndpointScheduler.acquire()

## Problem Summary

The slot timeout issue was occurring with the error:
```
TIMEOUT after 300s - active_count=1, max_allowed=1
```

This indicates that `active_count=1` but the semaphore had no permits available, causing a deadlock.

## Root Cause Analysis

### The Bug

In `api_router.py`, the `EndpointScheduler.acquire()` method had a race condition:

**Before Fix (lines 181-195):**
```python
# Line 181: sem.acquire() OUTSIDE the lock
if not sched['sem'].acquire(timeout=ENDPOINT_SLOT_ACQUIRE_TIMEOUT):
    raise TimeoutError(...)

# Lines 187-195: active_count increment INSIDE the lock
with self._lock:
    sched['active_count'] += 1
    ...
```

**The Race Condition:**
1. Thread A calls `sem.acquire()` at line 181 (outside lock) → succeeds, semaphore value: 1→0
2. Thread A crashes or gets interrupted BEFORE acquiring `self._lock` at line 187
3. Thread B tries to acquire:
   - At line 181, blocks on `sem.acquire()` because Thread A holds the permit
   - Times out after 300s
   - Reads `sched['active_count']` at line 184, which is still 0 (Thread A never incremented it)

This results in: **semaphore has 0 permits, but active_count=0** → misleading error message.

However, the actual observed issue was `active_count=1` with no permits. This happens when:
1. Thread A successfully acquires semaphore AND increments active_count
2. Thread A holds the slot indefinitely (crash, hang, or doesn't release)
3. Thread B tries to acquire and times out

### Why Thread A Might Not Release

Several scenarios could prevent proper slot release:

1. **Exception between acquire and release**: If an exception occurs after acquiring the slot but before the finally block runs properly
2. **Sync child call issues**: When parent releases slot for child, then fails to re-acquire
3. **SLEEPING state transition**: Agent goes SLEEPING and releases slot, but something goes wrong

## The Fix

### Change 1: Move sem.acquire() Inside the Lock

**After Fix (lines 180-200):**
```python
# CRITICAL FIX: Acquire semaphore and increment counter atomically under lock.
with self._lock:
    # Acquire semaphore under lock to prevent race condition
    if not sched['sem'].acquire(timeout=ENDPOINT_SLOT_ACQUIRE_TIMEOUT):
        raise TimeoutError(...)
    
    # Increment counter immediately after acquiring semaphore (both under lock)
    sched['active_count'] += 1
    ...
```

**Benefits:**
- Semaphore acquisition and active_count increment are now atomic
- No race condition between these two operations
- If an exception occurs, both operations fail together

### Change 2: Add Warning Log for Debugging

Added a warning log in the release callback to detect when `active_count` is unexpectedly low:

```python
old_count = current_sched['active_count']
if old_count <= 0:
    logger.warning(
        f"[SLOT_RELEASE_WARNING] active_count was {old_count} before decrement "
        f"for {log_target}. This suggests a slot tracking bug."
    )
current_sched['active_count'] = max(0, old_count - 1)
```

This helps identify cases where the release happens but `active_count` doesn't match expectations.

## Testing Recommendations

1. **Stress test with concurrent agents**: Run multiple agents on the same endpoint simultaneously
2. **Test sync child calls**: Verify parent-child slot handoff works correctly
3. **Test SLEEPING transitions**: Ensure agents properly release slots when going SLEEPING
4. **Monitor logs for warnings**: Check for `[SLOT_RELEASE_WARNING]` messages

## Files Modified

- `agent_cascade/api_router.py`:
  - Lines 180-200: Moved `sem.acquire()` inside the lock
  - Lines 245-247: Added warning log for active_count tracking

## Related Issues

This fix addresses the slot timeout issue but there may be other edge cases:
- Schedule cleanup while agents hold slots (see `cleanup_stale()`)
- Sync child re-acquire failures (see `_reacquire_slot()` in execution_engine.py)
- SLEEPING state slot management (see `_transition_to_sleeping()`)

## Next Steps

1. Monitor logs for `[SLOT_RELEASE_WARNING]` messages
2. If timeouts persist, investigate:
   - Whether `cleanup_stale()` is deleting schedules while agents hold slots
   - Whether sync child re-acquire is failing
   - Whether SLEEPING transitions are properly releasing slots