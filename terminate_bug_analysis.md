# Terminate Bug Analysis - Root Cause

## Issue Summary
**TODO #41**: `Terminate` doesn't really terminate the agent properly, it keeps streaming, sometimes left as an unreachable background thread.

## Key Findings

### 1. Missing Async Task Cancellation on Instance Termination
**Location**: `agent_cascade/agent_pool.py` - `terminate_instance()` method (lines 602-665)

When an agent instance is terminated, the following cleanup occurs:
- Adds instance to `terminated_instances` set (line 622)
- Transitions state to `TERMINATED` (line 640)
- Drains async results buffer (lines 643-647)
- Clears message queues (lines 650-655)
- Clears `_streaming_responses` (lines 659-664)

**Critical Missing Step**: The method **does NOT clean up pending async background tasks** in `AsyncToolRegistry._pending`. These tasks continue executing in the shared ThreadPoolExecutor even after their owning agent is terminated.

#### AsyncToolRegistry Structure
- File: `agent_cascade/async_tools.py` (lines 47-179)
- `_pending`: Dict mapping instance_name to list of `BackgroundToolEntry` objects
- `_executor`: ThreadPoolExecutor with 4 workers

**No cancellation mechanism exists**: The registry only provides `shutdown(wait=False)` which prevents new submissions but does not cancel running tasks. Individual instance termination never calls shutdown, so pending tasks remain active indefinitely.

### 2. Streaming Generator Close Only Affects LLM Calls
**Location**: `agent_cascade/execution_engine.py` - `_execute_llm_call_with_retry()` method

During streaming (lines 1884-1898 and 1909-1923), when stop is detected:
```python
try:
    gen.close()  # Closes the LLM generator, releases HTTP connection
except RuntimeError:
    pass
yield None
break
```

This closes the generator for LLM streaming but **does not affect** async background tool execution threads. The async tasks are independent and will complete regardless of termination state.

### 3. Main Loop Continuation After Termination Signal
**Location**: `agent_cascade/execution_engine.py` - `run()` method (lines 807-810)

When `_pre_llm_checks()` detects stop conditions, it returns True causing:
```python
if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
    logger.debug(f"[PRE_LLM_CHECK] Condition met, continuing loop")
    yield response
    continue  # Continues while turns_available > 0
```

The loop yields and continues rather than breaking out immediately. This can cause extra iterations after termination is signaled, potentially yielding stale updates.

### 4. Sub-agent Termination Does Not Stop Global Pool
**Location**: `agent_cascade/ws_handlers.py` - `handle_terminate()` (lines 499-541) and `agent_pool.py` - `dismiss_instance()` (lines 666-695)

For sub-agents, termination uses `dismiss_instance()` which:
- Calls `terminate_instance(set_global_stopped=False)` (line 692)
- Does **NOT** set `pool.stopped = True`
- Removes instance from pool but does not cancel async tasks

The main execution thread (e.g., `run_agent_thread_unified`) only checks `pool.stopped` to exit its loop (lines 140-142 in `run_agent_unified.py`). Individual sub-agent termination doesn't trigger this global flag, so the main thread continues.

## Root Cause Summary

The "unreachable background threads" are **async tool execution tasks** that:
1. Are registered with `AsyncToolRegistry` when an agent calls a background tool
2. Continue running in the shared ThreadPoolExecutor after their owning agent is terminated
3. May post results to the async buffer even after termination
4. Have no cancellation mechanism - they run to completion

The "keeps streaming" behavior occurs because:
1. The main execution loop may yield extra updates after termination due to continue instead of break
2. Async results from terminated agents can still be processed and yielded in subsequent iterations

## Recommended Fixes

### Immediate Fix (Minimize Code Change)
Add cleanup of `_async_registry._pending` in `terminate_instance`:
```python
# In agent_pool.py terminate_instance(), after draining async_results:
if hasattr(self, '_async_registry') and instance_name in self._async_registry._pending:
    # Mark entries as cancelled (requires modifying BackgroundToolEntry to support cancellation flag)
    with self._async_registry._lock:
        for entry in self._async_registry._pending.get(instance_name, []):
            entry.cancelled = True  # Need to add this field
        # Optionally remove from dict
        del self._async_registry._pending[instance_name]
```

However, this only prevents new results; running tasks will still complete. A better approach:

### Proper Fix (Architectural)
1. Store `Future` objects in `BackgroundToolEntry` when submitting to executor
2. Add `cancel_pending_tasks(instance_name)` method to `AsyncToolRegistry` that calls `future.cancel()` on all pending futures for an instance
3. Call this method from `terminate_instance()`

### Additional Fix (Main Loop Exit)
Consider breaking out of the main loop immediately when termination is detected, rather than yielding and continuing:
```python
# In execution_engine.py run(), around line 807-810
if self._pre_llm_checks(...):
    if self.pool.is_instance_terminated(inst_name):
        break  # Exit immediately for terminated instances
    yield response
    continue
```

## File References
- `agent_cascade/agent_pool.py` (lines 602-695) - Termination/dismissal logic
- `agent_cascade/async_tools.py` (lines 47-179) - Async task registry
- `agent_cascade/execution_engine.py` (lines 1884-1923, 807-810) - Streaming and loop control
- `agent_cascade/run_agent_unified.py` (lines 140-142) - Main thread exit check
- `agent_cascade/ws_handlers.py` (lines 499-541) - WebSocket termination handling