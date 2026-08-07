# Implementation Plan: Deadlock Detection + Dismiss Termination Fix

## Issue 1: A(synch)→B(async)→C(synch) Deadlock (todo.md line 89, part 1)

### Problem

Slot collision detection only checks the **direct caller**, not ancestors. When:
- A runs synchronously on slot pool X
- A calls B asynchronously (B uses different slot pool Y, so no collision → async is fine)
- B tries to call C synchronously on slot pool X
- Code checks if B holds a slot — B doesn't (it's async)
- Allows C to run, but C needs slot pool X which A still holds → deadlock/blocking

### Root Cause

`tool_dispatcher.py:_handle_call_agent()` lines 285-313 only checks `caller_slot_holder._slot_release`. When caller is an async child that doesn't hold a slot, it doesn't traverse up to check if an ancestor holds the conflicting slot.

### Solution

When determining if child needs SYNC execution due to slot collision, walk up the parent chain and check if **any active ancestor** holds a conflicting slot pool.

#### Change: `tool_dispatcher.py:_handle_call_agent()` (lines 285-313)

Add a helper method `_find_ancestor_with_slot(caller_name, child_slot_key)` that:
1. Walks up parent chain via `parent_instance` attribute
2. For each ancestor, checks if it holds a slot (`_slot_release is not None`)
3. If yes, computes ancestor's slot key and compares with child's
4. Returns the first ancestor whose slot pool conflicts with child's

Then use this in the collision detection:

```python
# After computing child_slot_key (line 306), add:
if not caller_holds_slot and child_slot_info and child_slot_info['needs_slot']:
    # Check if any ancestor holds a conflicting slot (A→B→C deadlock prevention)
    conflict_ancestor = self._find_ancestor_with_slot(caller_name, child_slot_info['slot_key'])
    if conflict_ancestor:
        caller_holds_slot = True  # Force sync — ancestor holds this slot pool
```

The helper method:

```python
def _find_ancestor_with_slot(self, start_instance_name: str, target_slot_key: str) -> Optional[str]:
    """Walk up parent chain to find an active ancestor holding a conflicting slot pool.
    
    Returns ancestor instance name if found, None otherwise.
    Max depth 10 to prevent infinite loops.
    """
    current = self.pool.get_instance(start_instance_name)
    for _ in range(10):
        if current is None:
            break
        parent_name = getattr(current, 'parent_instance', None)
        if not parent_name:
            break
        
        parent = self.pool.get_instance(parent_name)
        if parent is None:
            break
        
        # Check if this ancestor holds a slot
        if hasattr(parent, '_state_lock'):
            try:
                with parent._state_lock:
                    if parent._slot_release is not None:
                        # Compute ancestor's slot key (same logic as inline computation above)
                        router = self.pool.api_router
                        if router:
                            anc_concurrency = router.get_effective_concurrency(parent.agent_class)
                            anc_llm_cfg = router.get_llm_config(parent.agent_class)
                            anc_api_base = anc_llm_cfg.get('api_base') or anc_llm_cfg.get('model_server', 'unknown')
                            anc_is_sequential = (anc_concurrency == 0)
                            anc_slot_key = '_shared_sequential_slot_' if anc_is_sequential else anc_api_base
                            
                            if anc_slot_key == target_slot_key:
                                logger.debug(
                                    f"[DEADLOCK_PREVENTION] Ancestor '{parent_name}' holds slot pool "
                                    f"'{anc_slot_key}' conflicting with child's need — forcing sync"
                                )
                                return parent_name
            except Exception as e:
                logger.debug(f"Ancestor slot check failed for {parent_name} (non-critical): {e}")
        
        current = parent
    
    return None
```

## Issue 2: Dismiss Agent Doesn't Terminate Running Async Child (todo.md line 89, part 2)

### Problem

When dismissing an agent that's currently running:
1. `dismiss_instance()` calls `terminate_instance()` which sets state to TERMINATED and cancels async tools
2. But it doesn't actually interrupt a running LLM call or execution thread
3. Parent can be left in SLEEPING state if its async child was dismissed while parent was waiting

### Root Cause Analysis

`terminate_instance()` (agent_pool.py:958-1040):
- Sets `terminated_instances.add(instance_name)` 
- Transitions state to TERMINATED
- Cancels pending async tools via `_async_registry.clear_pending()`
- Kills async shell processes

But the execution engine thread checks termination status periodically — it doesn't get forcefully interrupted. The LLM call may still be in progress.

Additionally, when an async child is dismissed:
- Parent was set to SLEEPING while waiting for async result
- Child's dismissal removes it from pool but doesn't wake the parent
- Parent stays in SLEEPING forever

### Solution Part A: Forceful Thread Interruption on Termination

Add a mechanism to interrupt running execution threads. The execution engine already checks `terminated_instances` between turns, but we need to also interrupt blocking operations (LLM calls, waits).

The pool already has `_stopped_event` — use it per-instance where possible. Check if there's a way to signal the specific thread.

**Actually**: Looking at the code, `terminate_instance` with `set_global_stopped=False` does NOT set `_stopped_event`. The execution engine checks both:
1. `terminated_instances` set (checked between turns)
2. `_stopped_event` (checked during LLM calls via router's call_with_fallback)

The fix: when terminating an instance, also set a per-instance stop signal that the LLM call path can check. Or use the existing `_paused` mechanism temporarily.

**Simpler approach**: Modify `terminate_instance()` to also briefly set `_stopped_event` and then clear it after a short delay, allowing the terminated instance's thread to notice but not affecting others long-term. Actually this is risky.

**Better approach**: The execution engine already checks `terminated_instances` in its main loop. The real issue is that LLM calls block without checking. We need to make the router's `call_with_fallback` also check if the instance has been terminated during a long-running call.

#### Change: `api_router.py:call_with_fallback()` 

Add a check at the start of each endpoint retry attempt:

```python
def call_with_fallback(self, agent_type, call_fn, allocated_tokens=None, 
                       agent_instance_name=None, caller_agent_type=None):
    # ... existing code ...
    
    for cfg in chain:
        # Check if instance was terminated during a previous failed attempt
        if self._pool and agent_instance_name:
            if agent_instance_name in self._pool.terminated_instances:
                logger.debug(f"[TERMINATION] Instance '{agent_instance_name}' terminated, aborting LLM call")
                raise RuntimeError(f"Instance '{agent_instance_name}' has been terminated")
        
        # ... existing retry logic ...
```

This requires the router to have a reference to the pool. It already does via `self._pool` (set during init).

### Solution Part B: Wake Parent When Async Child is Dismissed

When dismissing an async child, check if its parent is in SLEEPING state waiting for it. If so, inject a completion message so the parent wakes up.

#### Change: `agent_pool.py:dismiss_instance()` 

After terminating the instance but before removing it:

```python
def dismiss_instance(self, instance_name: str):
    # ... existing cascade termination code ...
    
    inst = self.instances.get(instance_name)
    
    # NEW: If this instance was an async child with a SLEEPING parent, 
    # inject a result so parent wakes up
    if inst and inst.parent_instance:
        parent_name = inst.parent_instance
        parent = self.get_instance(parent_name)
        if parent:
            with parent._state_lock:
                parent_is_sleeping = (parent.state == AgentState.SLEEPING)
            
            if parent_is_sleeping:
                # Inject a completion message for the async call_agent result
                from agent_cascade.tool_result import ToolResult
                result_msg = f"[Agent '{instance_name}' Dismissed]:\nAgent was dismissed before completing."
                
                # Get the function_id this child was registered under
                func_id = None
                if hasattr(self, '_async_registry'):
                    func_id = self._async_registry.get_function_id(instance_name)
                
                if func_id:
                    try:
                        self._async_registry.complete(func_id, result_msg)
                    except Exception as e:
                        logger.debug(f"Completing async result for dismissed agent {instance_name} failed: {e}")
    # ... rest of existing code ...
```

### Solution Part C: Ownership Check in Dismiss

The todo mentions "must check if the child was owned by the caller and not root agent". Currently dismiss allows any agent to dismiss any other (except self and direct supervisor). We should restrict dismissal to only agents that can dismiss their own children or explicitly authorized cases.

#### Change: `tool_dispatcher.py:handle_dismiss_agent()` 

Add ownership check before allowing dismissal:

```python
# After line 408 (supervisor check), add:
target_inst = self.pool.get_instance(target_name)
if target_inst:
    # Only allow dismissing your own children, or root agent can dismiss anyone
    is_own_child = (target_inst.parent_instance == instance.instance_name)
    is_root_dismissing = (instance.parent_instance is None)
    
    if not is_own_child and not is_root_dismissing:
        return f"[status=error] Cannot dismiss '{target_name}' — you can only dismiss your own children. " \
               f"(Target's parent is '{target_inst.parent_instance or 'root'}')"
```

## Files to Modify

1. **tool_dispatcher.py**:
   - Add `_find_ancestor_with_slot()` helper method
   - Use it in `_handle_call_agent()` collision detection (lines 285-313)
   - Add ownership check in `handle_dismiss_agent()` (after line 408)

2. **api_router.py**:
   - Add termination check in `call_with_fallback()` between retry attempts

3. **agent_pool.py**:
   - In `dismiss_instance()`, wake SLEEPING parent when async child is dismissed

## Risk Assessment

- **Medium risk**: Touches core execution flow and slot management
- Ancestor traversal adds slight overhead but bounded by max depth 10
- Termination check in router is safe — just raises RuntimeError if terminated
- Ownership restriction may break existing patterns where root orchestrator delegates dismissal authority

## Testing Checklist

1. A(synch)→B(async)→C(synch needing A's slot) → C should run sync, not deadlock
2. Dismiss running agent → thread actually terminates (check logs for termination message)
3. Dismiss async child while parent is SLEEPING → parent wakes up with dismissal result
4. Non-owner tries to dismiss unrelated agent → rejected
5. Root orchestrator can still dismiss any agent
6. Existing behavior unchanged when no ancestors hold conflicting slots