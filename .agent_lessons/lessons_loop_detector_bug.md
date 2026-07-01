# Loop Detector Bug Analysis Report

## Executive Summary

**Bug**: When a child agent (sub-agent) enters a loop, the `LoopDetectedError` is raised correctly with the child's name, but it propagates up through the exception chain and gets caught by the **root agent's** recovery handler in `run_agent_in_pool_with_recovery()`, causing the wrong agent to be rolled back.

**Root Cause**: The LoopDetectedError doesn't carry enough context about which agent instance it was detected for when raised during child agent execution, and the exception propagation path loses track of the original agent name.

---

## Detailed Bug Analysis

### 1. LoopDetectedError Class Definition

**File**: `agent_cascade/loop_detection.py` (lines 25-33)

```python
class LoopDetectedError(Exception):
    """Raised when a repetitive loop is detected in agent turns."""
    def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
        self.reason = reason
        self.agent_name = agent_name  # ← This field exists but...
        self.pop_count = pop_count
        self.turn_pop_count = turn_pop_count
        self.resp_snapshot = resp_snapshot or []
        super().__init__(f"Loop detected for {agent_name or 'agent'}: {reason}")
```

**Issue**: The `agent_name` parameter is **optional** (defaults to `None`). When not provided, the error message shows "Loop detected for agent: ..." instead of the actual agent name.

---

### 2. Where LoopDetectedError is Raised - Missing agent_name

#### A. In execution_engine.py (Child Agent Loop Detection)

**File**: `agent_cascade/execution_engine.py` (line 1187)

```python
if loop_info:
    reason, pop_count = loop_info
    logger.warning(f"Loop detected for {inst_name}: {reason}")
    raise LoopDetectedError(reason=reason, pop_count=pop_count)  # ← agent_name NOT passed!
```

**Bug**: `inst_name` is logged but **NOT passed to LoopDetectedError**. The exception is raised without the `agent_name` parameter, so `e.agent_name` is `None`.

#### B. In run_agent_unified.py (Root Agent Streaming Path)

**File**: `agent_cascade/run_agent_unified.py` (line 412)

```python
if loop_info:
    reason, pop_count = loop_info
    # Raise LoopDetectedError so the recovery wrapper handles rollback
    raise LoopDetectedError(reason=reason, pop_count=pop_count)  # ← agent_name NOT passed!
```

**Bug**: Same issue - `instance_name` is available in scope but **NOT passed to LoopDetectedError**.

---

### 3. Exception Propagation Path for Child Agents

When a child agent loops during `call_agent` execution:

1. **Loop detected** in `execution_engine.py::_pre_llm_checks()` at line 1187
   - `LoopDetectedError` raised with `agent_name=None`
   
2. **Propagates out of** `engine.run()` at line 794-796:
   ```python
   except LoopDetectedError:
       # Propagate to consumer-level recovery wrapper (DESIGN_REWRITE §7.2)
       raise
   ```

3. **Propagates out of** `_create_and_run_agent()` at line 2759 (no except handler for LoopDetectedError)

4. **Caught in** `tool_dispatcher.py::_run_child_sync()` at line 313-322:
   - No specific except handler for LoopDetectedError
   - Exception propagates further

5. **Caught in** `execution_engine.py::_execute_detected_tools()` at line 1908-1916:
   ```python
   except Exception as e:
       logger.error(f"Tool {tool_name} failed for {inst_name}: {e}")
       tool_result = f"Error: {e}"
       _tool_success = False
       _tool_error = str(e)
       # Re-raise loop detection errors so the turn loop stops as intended
       if isinstance(e, LoopDetectedError):
           raise  # ← Re-raised, still with agent_name=None
   ```

6. **Propagates out of** `_process_response()` → back to main turn loop in `run()`

7. **Caught in** `execution_engine.py::run()` at line 794-796:
   ```python
   except LoopDetectedError:
       # Propagate to consumer-level recovery wrapper (DESIGN_REWRITE §7.2)
       raise
   ```

8. **Finally caught in** `api_integration.py::run_agent_in_pool_with_recovery()` at line 351:
   ```python
   except LoopDetectedError as e:
       if not auto_rollback_enabled or retry_count >= max_auto_retries:
           logger.warning(
               f"Loop detected for {instance_name}: {e.reason}. "
               f"Exceeded retries ({retry_count}/{max_auto_retries}). Stopping."
           )
           # ...
       
       logger.warning(
           f"Loop detected for {instance_name}: {e.reason}. "
           f"Surgical rollback (Retry {retry_count + 1}/{max_auto_retries})."
       )
       
       # Surgical rollback + hint injection under per-instance lock for atomicity
       pool.surgical_rollback(instance_name, e.pop_count, reason=e.reason)  # ← Uses instance_name from wrapper!
   ```

**The Bug**: At step 8, `instance_name` is the parameter to `run_agent_in_pool_with_recovery()`, which is the **root agent's name** (e.g., "Maine"), NOT the child agent's name. The exception `e.agent_name` is `None`, so there's no way to know which agent actually looped!

---

### 4. How run_agent_in_pool_with_recovery Works

**File**: `agent_cascade/api_integration.py` (lines 317-395)

```python
def run_agent_in_pool_with_recovery(
    pool: AgentPool,
    instance_name: str,  # ← This is FIXED for the duration of the call
    max_auto_retries: int = 3,
    auto_rollback_enabled: bool = True,
) -> Iterator[List[Message]]:
    
    retry_count = 0
    instance = pool.get_instance(instance_name)
    
    while retry_count <= max_auto_retries:
        try:
            # Execute through unified engine
            yield from run_agent_in_pool(pool, instance_name)
            return  # Success — no loop detected
            
        except LoopDetectedError as e:
            # ... logging ...
            
            # BUG: This always rolls back 'instance_name' parameter,
            # which is the ROOT agent, not necessarily the agent that looped!
            pool.surgical_rollback(instance_name, e.pop_count, reason=e.reason)
            
            # Inject hint message into the WRONG agent's conversation
            hint_msg = Message(
                role=USER,
                content=f"[SYSTEM]: You appear to be stuck in a loop — {e.reason}. "
                        f"Try a different approach to break the pattern.",
            )
            instance.append_message(hint_msg)  # ← Adds to root agent, not child!
            
            retry_count += 1
```

**The Problem**: 
- `instance_name` is the **root agent name** passed as a parameter
- When a child agent loops, the exception bubbles up through the call stack
- The recovery handler uses `instance_name` (root) instead of `e.agent_name` (which is None)
- Result: Root agent's conversation is rolled back instead of child agent's

---

### 5. Specific Bug Location Summary

| File | Line | Issue |
|------|------|-------|
| `execution_engine.py` | 1187 | `LoopDetectedError` raised without `agent_name=inst_name` |
| `run_agent_unified.py` | 412 | `LoopDetectedError` raised without `agent_name=instance_name` |
| `api_integration.py` | 371 | Uses `instance_name` parameter instead of `e.agent_name` for rollback |
| `api_integration.py` | 378 | Injects hint message into wrong instance (root instead of child) |

---

## The Fix

### Part 1: Pass agent_name when raising LoopDetectedError

**File**: `agent_cascade/execution_engine.py` (line 1187)
```python
# Before:
raise LoopDetectedError(reason=reason, pop_count=pop_count)

# After:
raise LoopDetectedError(reason=reason, agent_name=inst_name, pop_count=pop_count)
```

**File**: `agent_cascade/run_agent_unified.py` (line 412)
```python
# Before:
raise LoopDetectedError(reason=reason, pop_count=pop_count)

# After:
raise LoopDetectedError(reason=reason, agent_name=instance_name, pop_count=pop_count)
```

### Part 2: Use e.agent_name in recovery handler

**File**: `agent_cascade/api_integration.py` (lines 351-381)
```python
except LoopDetectedError as e:
    # FIX: Use the agent_name from the exception if available, otherwise fallback to instance_name
    looped_agent = e.agent_name if e.agent_name else instance_name
    
    if not auto_rollback_enabled or retry_count >= max_auto_retries:
        logger.warning(
            f"Loop detected for {looped_agent}: {e.reason}. "
            f"Exceeded retries ({retry_count}/{max_auto_retries}). Stopping."
        )
        # Yield error state so UI can display it
        error_msg = Message(
            role=ASSISTANT,
            content=f"[SYSTEM: Loop detected for {looped_agent} — {e.reason}]",
        )
        yield [error_msg]
        return

    logger.warning(
        f"Loop detected for {looped_agent}: {e.reason}. "
        f"Surgical rollback (Retry {retry_count + 1}/{max_auto_retries})."
    )

    # FIX: Rollback the correct agent (the one that looped, not necessarily instance_name)
    pool.surgical_rollback(looped_agent, e.pop_count, reason=e.reason)

    # FIX: Get the correct instance for hint injection
    looped_instance = pool.get_instance(looped_agent)
    if looped_instance:
        # Inject loop avoidance hint (atomic with rollback)
        hint_msg = Message(
            role=USER,
            content=f"[SYSTEM]: You appear to be stuck in a loop — {e.reason}. "
                    f"Try a different approach to break the pattern.",
        )
        looped_instance.append_message(hint_msg)
    else:
        logger.warning(f"Could not find instance '{looped_agent}' for hint injection")

    retry_count += 1
```

---

## Testing Scenarios

### Scenario 1: Root Agent Loops
- **Before Fix**: ✓ Works (root agent rolls back correctly)
- **After Fix**: ✓ Still works (e.agent_name = root name, matches instance_name)

### Scenario 2: Child Agent Loops
- **Before Fix**: ✗ Bug - root agent rolls back instead of child
- **After Fix**: ✓ Fixed - child agent rolls back correctly

### Scenario 3: Nested Child Agent Loops (Grandchild)
- **Before Fix**: ✗ Bug - root agent rolls back instead of grandchild
- **After Fix**: ✓ Fixed - grandchild agent rolls back correctly

---

## Additional Notes

1. **Why this wasn't caught earlier**: The bug only manifests when a child agent loops. Root agent loops work correctly because `instance_name` happens to match the looping agent.

2. **Impact**: When a child agent loops, the root agent's conversation history gets surgically rolled back, potentially losing important context and causing confusion in the UI.

3. **Related Issues**: This may explain reported issues where "random" rollbacks happen on the main agent when sub-agents are working.

4. **Edge Case**: If `e.agent_name` is somehow different from any instance in the pool, the fix includes a null check before injecting hint messages.

---

## Files to Modify

1. `agent_cascade/execution_engine.py` - Line 1187
2. `agent_cascade/run_agent_unified.py` - Line 412  
3. `agent_cascade/api_integration.py` - Lines 351-381

---

## Verification Steps

After applying the fix:

1. Create a test scenario where a child agent (e.g., coder) enters a loop
2. Verify that ONLY the child agent's conversation is rolled back
3. Verify that the root agent's conversation remains intact
4. Check logs confirm "Loop detected for {child_name}" not "Loop detected for {root_name}"
5. Verify hint message is injected into child's conversation, not root's