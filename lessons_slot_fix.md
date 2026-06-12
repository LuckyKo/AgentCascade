# Concurrency Slot Blocking Fix - AgentPool.submit_task()

## Problem

In `agent_pool.py`, the `submit_task()` method was acquiring the endpoint slot **before** submitting to the ThreadPoolExecutor. This caused a blocking issue:

1. Parent agent calls `call_agent()` → `submit_task()` is called
2. `_acquire_slot()` is called in parent's thread (line ~1562)
3. If child shares an endpoint with another running agent, `semaphore.acquire()` **blocks** the parent's thread
4. Parent can't finish its turn, can't go to SLEEPING state
5. System effectively deadlocks because parent is waiting for child, but child hasn't started yet

## Root Cause

```python
# OLD CODE (BEFORE FIX)
def submit_task(...):
    # This blocks the PARENT's thread!
    endpoint_release = self._acquire_slot(agent_class, instance_name)
    
    def task_wrapper():
        # Child only starts executing after parent releases this function
        ...
    
    future = self.executor.submit(task_wrapper)
```

The `_acquire_slot()` call uses a semaphore that blocks when capacity is reached. Since it was called in the parent's thread before `executor.submit()`, the entire parent execution stopped waiting for a slot.

## Solution

Move slot acquisition **inside** `task_wrapper()` so it happens in the worker thread:

```python
# NEW CODE (AFTER FIX)
def submit_task(...):
    def task_wrapper():
        # Slot acquisition happens HERE, in the WORKER thread
        endpoint_release = self._acquire_slot(agent_class, instance_name)
        ...
    
    future = self.executor.submit(task_wrapper)  # Parent returns immediately
    
# Flow after fix:
# 1. Parent calls call_agent() → submit_task() submits to thread pool (non-blocking)
# 2. task_wrapper starts in worker thread → waits for slot via _acquire_slot()
# 3. Meanwhile parent continues its turn, goes to SLEEPING at end of turn
# 4. When child finishes, it sends result via async result buffer → parent wakes up
```

## Key Changes Made

### File: `agent_cascade/agent_pool.py`

1. **Removed** `_acquire_slot()` call from before `executor.submit()` (lines ~1560-1566)
2. **Added** `_acquire_slot()` call at the start of `task_wrapper()`, before creating ExecutionEngine
3. **Initialized** `endpoint_release = None` at the start of task_wrapper for proper finally block handling
4. **Restructured** exception handling with nested try-except:
   - Outer try: handles slot acquisition failures
   - Inner try: handles execution failures (after slot acquired)
5. **Added helper method** `_notify_async_error()` to consolidate duplicated error handling logic (DRY principle)
6. **Updated** docstring to reflect new behavior

### Exception Handling Structure

```python
def task_wrapper():
    endpoint_release = None  # Initialize for finally block
    
    try:
        # Outer try: slot acquisition
        endpoint_release = self._acquire_slot(agent_class, instance_name)
        
        try:
            # Inner try: actual execution (all post-slot-acquisition code here)
            engine = ExecutionEngine(self.pool)
            inst, conv = engine._create_and_run_agent(...)
            
            # Post-execution operations also in inner try for proper exception handling
            result = extract_instance_output(conv, instance_name)
            self.pool.add_async_result(caller, completion_msg)
            self.pool.complete_async_call(caller, call_id)
            ...
        except Exception as e:
            # Handle execution errors (slot already acquired)
            self._notify_async_error(instance_name, caller, error_msg, call_id)
    
    except Exception as e:
        # Handle slot acquisition errors
        self._notify_async_error(instance_name, caller, error_msg, call_id)
    
    finally:
        if endpoint_release is not None:
            endpoint_release()
```

### Helper Method (DRY Principle)

```python
def _notify_async_error(self, instance_name: str, caller: str, error_msg: str, call_id: Optional[str] = None):
    """Consolidates duplicated error handling logic across multiple paths."""
    self.pool.add_async_result(caller, error_msg)
    
    if call_id:
        self.pool.complete_async_call(caller, call_id)
        logger.debug(f"completed async call {call_id} for caller {caller}")
    else:
        logger.warning(f"no call_id provided for {instance_name}")
    
    self.pool.send_message(instance_name, caller, error_msg)
```

## Benefits

1. **Parent thread doesn't block**: Submit to thread pool returns immediately
2. **Child waits in its own thread**: Slot acquisition happens in worker thread context
3. **Proper async flow**: Parent can go to SLEEPING, child completes independently
4. **No deadlock**: System continues progressing while children wait for slots
5. **Cleaner code**: DRY error handling via helper method reduces maintenance burden

## Testing Notes

- Test with multiple agents sharing the same endpoint (e.g., all using "coder" class)
- Verify parent agent goes to SLEEPING state after calling call_agent()
- Check that concurrent execution respects concurrency limits per endpoint
- Monitor logs for `[CALL_AGENT_DEBUG] task_wrapper — acquired endpoint slot` messages
- Verify error handling paths properly complete async calls so parent doesn't hang

## Related Files

- `agent_cascade/agent_pool.py` - Main fix location (submit_task method, ~line 1541-1700)
- `agent_cascade/api_router.py` - Scheduler implementation with semaphore-based concurrency control
- `lessons_async_call_agent.md` - General async call_agent patterns and documentation