# Loop Detection Bug Fix - api_server.py

## Summary
Fixed critical bug in loop detection rollback logic where deletion was performed on a slice copy instead of the original list.

## The Bug
**Location:** `api_server.py`, lines 1145-1212 (before fix)

**Root Cause:**
```python
new_responses = responses[prev_responses_len:] if len(responses) > prev_responses_len else []
# ... later:
del new_responses[-refined_pop:]  # ← DELETES FROM SLICE COPY ONLY!
```

A Python slice `responses[start:]` creates a NEW independent list. Deleting from `new_responses` does NOT affect the original `responses` list. So the rollback silently failed — looped messages remained in `responses`.

## The Fix (Option A - Exception-Based)
**Approach:** Match the cleaner exception-based approach used in `agent_orchestrator.py`.

**Changes Made:**
1. Added module-level import: `from agent_orchestrator import LoopDetectedError` (line 71)
2. Removed inline rollback logic (~70 lines removed)
3. Now raises `LoopDetectedError` exception when loop is detected (lines 1157-1163)
4. Leverages existing outer try/except handler (line 1169+) which properly handles:
   - Surgical rollback on original `current_history` list
   - Logging the rollback to persistent logs  
   - Injecting hints into conversation history
   - Notifying UI about retry/stop

**New Code (lines 1151-1163):**
```python
if loop_info:
    loop_reason, pop_count = loop_info
    # Raise LoopDetectedError to be caught by outer try/except for cleaner state management.
    # This matches the approach used in agent_orchestrator.py for consistency.
    # FIX: The original code did `del new_responses[-refined_pop:]` which deleted from a slice copy,
    # not the original responses list, causing rollback to fail silently.
    raise LoopDetectedError(
        reason=loop_reason,
        agent_name='Orchestrator',
        pop_count=pop_count,
        turn_pop_count=len(responses),
        resp_snapshot=list(responses)
    )
```

## Benefits
1. **Fixes the bug:** Rollback now operates on `current_history` directly via the exception handler
2. **Code consistency:** Matches `agent_orchestrator.py`'s approach for sub-agent loop detection
3. **Cleaner separation:** Concern separation between detection (inner loop) and handling (outer try/except)
4. **Reduced code:** ~70 lines removed, replaced with 12-line exception raise
5. **Better import organization:** Module-level import avoids repeated inline imports

## Files Modified
- `N:\work\WD\AgentCascade\api_server.py` 
  - Line 71: Added module-level import of LoopDetectedError
  - Lines 1143-1165: Replaced inline rollback with exception raise
  - Line 1169+: Existing exception handler now handles both sub-agent and orchestrator loops

## Review Status
✅ **PASSED** by reviewer_loop_fix agent
- All edge cases handled correctly (auto_rollback_enabled, retry_count, agent_pool=None)
- Syntax validated
- Consistent with agent_orchestrator.py reference implementation

## Testing Notes
The existing outer try/except handler already handles `LoopDetectedError` for sub-agents. This fix extends it to also handle orchestrator-level loops detected in the main streaming loop. The handler correctly:
1. Computes `main_pop = max(0, pop_count - turn_pop_count)` to adjust for uncommitted turn output
2. Deletes from `current_history[-refined_pop:]` (the authoritative list)
3. Records rollback in persistent logs
4. Injects loop hints into conversation history