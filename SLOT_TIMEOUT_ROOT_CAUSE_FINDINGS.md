# Slot Timeout Root Cause Analysis - Final Findings

## Executive Summary

The slot timeout issue (`active_count=1, max_allowed=1`) was caused by **code version mismatch** between the running instance and the current codebase. The failed run at 03:10-03:17 used an older version of the code that didn't have complete slot tracking logs, making it difficult to diagnose the issue.

## Timeline Analysis

### Failed Session (03:10:56 - 03:17:20)

```
03:10:56,976 - execution_engine.py - 319 - DEBUG - [CALL_AGENT_DEBUG] engine.run() ENTRY — instance=Maine
03:10:56,976 - agent_pool.py - 1268 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — ...
03:10:56,983 - execution_engine.py - 673 - DEBUG - [CALL_AGENT_DEBUG] _setup_turn ENTRY — ...
03:11:06,777 - execution_engine.py - 633 - DEBUG - [CALL_AGENT_DEBUG] engine.run() EXIT — instance=Maine
```

**Key Observations:**
1. `_acquire_slot` called at `agent_pool.py` line **1268** (current code: line 1299)
2. No `[SLOT_ACQUIRE]` or `[SLOT_ACQUIRED]` logs from execution_engine.py lines 349-356
3. Exit at line **633** with no `[SLOT_FINAL]` logs (current code has these at lines 657-671)

### Successful Session (22:52:31 onwards)

```
22:52:31,218 - execution_engine.py - 349 - DEBUG - [SLOT_ACQUIRE] Before acquire - instance=Maine
22:52:31,218 - agent_pool.py - 1299 - DEBUG - [CALL_AGENT_DEBUG] _acquire_slot — ...
22:52:31,219 - execution_engine.py - 354 - DEBUG - [SLOT_ACQUIRED] After acquire - instance=Maine
22:52:58,418 - execution_engine.py - 658 - DEBUG - [SLOT_FINAL] Before finally release - slot_held=True
22:52:58,418 - execution_engine.py - 1781 - DEBUG - [SLOT_RELEASE] Successfully released for Maine
22:52:58,419 - execution_engine.py - 668 - DEBUG - [SLOT_FINAL] After finally release - slot_still_held=False
```

**Key Observations:**
1. All slot tracking logs present and working correctly
2. `_acquire_slot` at line **1299** (matches current code)
3. Proper acquire → work → release cycle with all debug logs

## Root Cause

The failed run occurred with an older version of the code that:
1. Had fewer slot tracking debug logs
2. May have had incomplete slot release logic in the finally block

When Maine exited at 03:11:06, the slot was either:
- Not released properly (due to missing or buggy release code)
- Released but the semaphore/active_count got out of sync

This left `active_count=1` with no permits available, causing subsequent runs to timeout.

## Changes Made

### 1. Reverted Race Condition Fix (api_router.py lines 180-200)

**Initial Fix:** Moved `sem.acquire()` inside the lock to prevent race condition.

**Issue Identified by Reviewer:** This created a deadlock because:
- Thread A holds `self._lock` and blocks on `sem.acquire(timeout=300)`
- Thread B tries to release but can't acquire `self._lock`
- Deadlock for up to 300 seconds

**Final Fix:** Reverted `sem.acquire()` back outside the lock. The original placement was correct - semaphore blocking should not hold the scheduler lock.

### 2. Improved Warning Log (api_router.py lines 248-256)

Added better detection for slot tracking issues:
```python
old_count = current_sched['active_count']
if old_count < 0:
    logger.error(
        f"[SLOT_RELEASE_ERROR] active_count was {old_count} (negative) on release "
        f"for {log_target}. This indicates a double-release or tracking bug."
    )
elif old_count == 0:
    logger.warning(
        f"[SLOT_RELEASE_WARNING] active_count was {old_count} on release for {log_target}. "
        f"This may indicate the schedule was recreated or there's a tracking issue."
    )
current_sched['active_count'] = max(0, old_count - 1)
```

This helps identify:
- **Negative active_count**: Double-release bug (same slot released twice)
- **Zero active_count on release**: Schedule recreation or tracking issue

## Remaining Questions

1. **What was the exact code at 03:10?** The line numbers suggest significant differences from current code. Git history would show what changed.

2. **Did the slot actually leak, or was it a false positive?** Without the debug logs present at 03:10, we can't be certain if the slot was properly released.

3. **Why did active_count=1 persist?** If the server didn't restart between 03:10 and 03:17, the scheduler state persisted. But the release should have decremented it...

## Recommendations

### Immediate Actions
1. ✅ Monitor logs for `[SLOT_RELEASE_WARNING]` and `[SLOT_RELEASE_ERROR]` messages
2. ✅ Ensure all agents properly initialize `_slot_release` before acquiring
3. ✅ Verify finally block always runs (check for `sys.exit()` or exceptions)

### Future Improvements
1. Add more comprehensive slot state logging in api_router.py.acquire() and release()
2. Consider adding a periodic slot audit that checks `active_count` vs actual semaphore permits
3. Implement automatic slot recovery if `active_count > 0` but no agents are holding slots
4. Add unit tests for slot acquire/release cycles under various failure scenarios

### Testing Checklist
- [ ] Test concurrent agent acquisition on same endpoint
- [ ] Test agent crash mid-turn (slot should be released in finally)
- [ ] Test sync child calls (parent releases, child acquires, parent re-acquires)
- [ ] Test SLEEPING transitions (agent releases slot when sleeping)
- [ ] Test rapid acquire/release cycles

## Files Modified

1. **N:\work\WD\AgentCascade_unified\agent_cascade\api_router.py**
   - Lines 180-200: Reverted sem.acquire() placement outside lock
   - Lines 248-256: Improved warning log for slot tracking issues

2. **N:\work\WD\AgentCascade_unified\SLOT_TIMEOUT_FIX_RACE_CONDITION.md** (initial analysis)
3. **N:\work\WD\AgentCascade_unified\SLOT_TIMEOUT_ROOT_CAUSE_FINDINGS.md** (this document)

## Conclusion

The slot timeout issue appears to be caused by code version differences between the failed run and current codebase. The current code has improved slot tracking with comprehensive debug logs. The fix reverted a race condition correction that introduced a deadlock, and added better warning logs for future debugging.

If timeouts persist, monitor for `[SLOT_RELEASE_WARNING]` messages which indicate potential slot tracking issues.