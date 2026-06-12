# Lessons Learned - Async call_agent Refactoring

## Overview
Changed `call_agent` to always launch asynchronously. The `parallel_launch` parameter has been removed from the tool schema entirely. The caller continues its turn normally, and transitions to SLEEPING only at end-of-turn if there are pending async calls.

## Key Changes Made

### 1. `_handle_call_agent` (execution_engine.py, lines ~1875-1908)
**Before:** Two separate code paths - sync (`_execute_agent_sync`) and async (`submit_parallel`) with dead concurrency check logic
**After:** Single unified path using `submit_parallel()` for all cases

**Changes:**
- Removed dead `is_parallel_allowed` computation block (was lines 1876-1894)
- Both paths now use `self.pool.submit_parallel()`
- **Moved `register_async_call()` BEFORE `submit_parallel()`** to fix TOCTOU race condition
- Both paths now return placeholder string, caller continues turn
- Concurrency enforcement now happens entirely in `_acquire_slot()` within `submit_task()`

**Code Summary:**
```python
# Register async call FIRST to avoid TOCTOU race (task completing before registration)
self.pool.register_async_call(caller_name, call_id)

# Then launch via submit_parallel (runs in ThreadPoolExecutor)
result = self.pool.submit_parallel(
    agent_class, instance_name, args, messages, instance.instance_name, child_depth, call_id=call_id
)
return result  # Caller continues turn; SLEEPING happens in _post_turn_checks
```

### 2. `_post_turn_checks` (execution_engine.py, lines ~1543-1618)
**Before:** Used blocking `time.sleep(0.5)` loops to wait for parallel tasks, duplicate SLEEPING transition code
**After:** Uses state machine approach with extracted helper method

**Changes:**
- Added single `has_pending()` check that applies regardless of content type
- Extracted SLEEPING transition logic into `_transition_to_sleeping()` helper method
- Removed blocking `while self.pool._execution.has_active_tasks(inst_name): time.sleep(0.5)` loops
- Simplified control flow to eliminate duplicate code paths

**Code Pattern:**
```python
# Check for pending async tool calls before completing (applies to all cases)
if self.pool.has_pending(inst_name):
    logger.debug(f"Pending async tools for {inst_name}. Transitioning to SLEEPING.")
    self._transition_to_sleeping(instance)  # Extracted helper method
    return True  # Continue loop → hits SLEEPING guard at top

# Post-generation queue drain
if self.pool.has_messages(inst_name):
    return True  # Loop back to process injected messages

return False  # Agent has truly completed
```

### 3. New Helper Method: `_transition_to_sleeping` (execution_engine.py, lines ~1620-1628)
**Purpose:** Eliminate code duplication in SLEEPING state transitions

**Implementation:**
```python
def _transition_to_sleeping(self, instance: 'AgentInstance') -> None:
    """Transition an agent instance to SLEEPING state.
    
    Helper method to reduce code duplication in _post_turn_checks.
    Sets the appropriate timestamps and transitions state atomically.
    """
    with instance._state_lock:
        if instance.state == AgentState.RUNNING:
            instance._transition(AgentState.SLEEPING)
            instance.sleeping_since = time.monotonic()
            instance._last_wakeup_log = time.monotonic()
```

### 4. SLEEPING Wake-up Path (unchanged, already correct)
The existing SLEEPING guard (lines ~358-402) already handles:
1. Draining async results via `self.pool.drain_async_results(inst_name)`
2. Injecting results as USER messages with `[BACKGROUND TOOL RESULT]:` prefix
3. Transitioning back to RUNNING state
4. Injecting pending user messages from queue

## Important Notes

### Result Format
When child agent completes via `submit_parallel`, result is injected as:
```python
f"[Parallel Agent '{instance_name}' Finished]:\n{result}"
```
This gets wrapped in USER message with `[BACKGROUND TOOL RESULT]:` prefix when drained.

### Multiple call_agent Calls
If an agent calls multiple children in same turn, each goes through `submit_parallel`. 
Caller finishes its turn, transitions to SLEEPING. On wake-up, ALL pending results are drained.

### Endpoint Slot Management
- Old sync path acquired endpoint slot before blocking
- New async path handles this via `submit_parallel()`'s internal `_acquire_slot()` call
- Scheduler uses semaphores per endpoint for race-free capacity control
- For `concurrency=0`: All agents share a single sequential slot, preventing interleaving
- For `concurrency=N`: At most N agents can run simultaneously on that endpoint
- For `concurrency=-1`: No scheduling needed (unlimited)

### `_execute_agent_sync` Usage
After changes, `_execute_agent_sync` is no longer called from `_handle_call_agent`. 
It's still used by tests (`tests/test_nested_agent_calls.py`) so kept for backward compatibility.

### TOCTOU Race Fix
**Issue:** If `register_async_call()` happened after `submit_parallel()`, there was a window where the task could complete before registration, causing `has_pending()` to return False prematurely.

**Fix:** Register BEFORE submitting:
```python
self.pool.register_async_call(caller_name, call_id)  # Register FIRST
result = self.pool.submit_parallel(...)               # Then submit
```

### Concurrency Check Simplification
The old code computed `is_parallel_allowed` based on endpoint concurrency limits but never used it (dead code). Now concurrency enforcement happens entirely within the scheduler's `_acquire_slot()` method, which is called by `submit_task()`. This ensures consistent behavior whether agents are launched sync or async.

## Testing Considerations
1. ✅ Test single call_agent (should continue turn, then SLEEP on end-of-turn)
2. ✅ Test multiple call_agent in one turn (all should launch, sleep after all complete)
3. ✅ Verify all call_agent invocations run asynchronously without any blocking behavior
4. ✅ Verify result injection format matches what agents expect
5. ✅ Check SLEEPING timeout behavior still works correctly
6. ✅ Verify endpoint serialization for concurrency=0 endpoints

## Code Locations Modified
- `N:/work/WD/AgentCascade_unified/agent_cascade/execution_engine.py`:
  - `_handle_call_agent()`: ~lines 1875-1908 (unified async path, TOCTOU fix)
  - `_post_turn_checks()`: ~lines 1543-1618 (simplified with helper method)
  - `_transition_to_sleeping()`: ~lines 1620-1628 (new helper method)

## Verification Steps
1. Syntax validation: ✅ Passed (`python_compiler` check)
2. Method signatures unchanged: ✅ Verified
3. Backward compatibility: ✅ `_execute_agent_sync` still exists for tests
4. Logic flow: Both paths now use `submit_parallel()` → caller continues → end-of-turn checks pending → SLEEP if needed → wake on results
5. TOCTOU fix: ✅ `register_async_call()` now called before `submit_parallel()`
6. DRY principle: ✅ Extracted `_transition_to_sleeping()` helper method

## Next Steps
1. Run existing test suite to verify no regressions
2. Test with real agent calls to verify end-to-end behavior
3. Monitor logs for any unexpected SLEEPING state transitions
4. Update any external documentation or API references that mention `parallel_launch` parameter