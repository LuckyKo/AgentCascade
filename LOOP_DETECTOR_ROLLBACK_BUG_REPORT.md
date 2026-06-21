# Loop Detector Bug Report: Root Agent Rolled Back Instead of Child Agent

## Date: 2026-06-21
## Status: Confirmed, Fix Plan Ready

---

## Executive Summary

When a child agent (sub-agent) enters a loop, the `LoopDetectedError` is raised but **does not carry the child agent's name**. The exception propagates up through the execution engine to the root recovery handler in `api_integration.py`, which then uses its own `instance_name` parameter (the root agent) to perform `surgical_rollback()`. This results in the **root agent being rolled back instead of the actual looping child agent**.

---

## Root Cause Analysis

### The Exception Propagation Chain

When a child agent loops, here is the exact propagation path:

```
Child Agent Loops
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ execution_engine.py :1187                                   │
│ ─── LoopDetectedError raised WITHOUT agent_name ─────────── │
│ raise LoopDetectedError(reason=reason, pop_count=pop_count) │
│ (BUG: missing agent_name=inst_name)                         │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼ (exception propagates up from self.run(inst))
┌─────────────────────────────────────────────────────────────┐
│ execution_engine.py :2759  (_create_and_run_agent)          │
│ ─── engine.run() yields, exception bubbles up ───────────── │
│ for resp in self.run(inst):                                 │
│ No try/except here — exception just propagates              │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼ (exception reaches outer engine.run() catch)
┌─────────────────────────────────────────────────────────────┐
│ execution_engine.py :794                                    │
│ ─── Bare re-raise, no context added ─────────────────────── │
│ except LoopDetectedError:                                   │
│     raise                                                  │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼ (exception reaches recovery wrapper)
┌─────────────────────────────────────────────────────────────┐
│ api_integration.py :351  (run_agent_in_pool_with_recovery)  │
│ ─── Uses instance_name parameter — which is the ROOT agent  │
│ except LoopDetectedError as e:                              │
│     pool.surgical_rollback(instance_name, e.pop_count)  ← BUG!
│     instance.append_message(hint_msg)          ← Wrong agent!
└─────────────────────────────────────────────────────────────┘
```

### The Three Specific Bugs

#### Bug #1: Missing `agent_name` in execution_engine.py

**File:** `agent_cascade/execution_engine.py`, line 1187
```python
# Current (BUG):
raise LoopDetectedError(reason=reason, pop_count=pop_count)

# Should be:
raise LoopDetectedError(reason=reason, agent_name=inst_name, pop_count=pop_count)
```

The `inst_name` variable is available in scope at line 710 (`inst_name = instance.instance_name`).

#### Bug #2: Missing `agent_name` in run_agent_unified.py streaming path

**File:** `agent_cascade/run_agent_unified.py`, line 412
```python
# Current (BUG):
raise LoopDetectedError(reason=reason, pop_count=pop_count)

# Should be:
raise LoopDetectedError(reason=reason, agent_name=instance_name, pop_count=pop_count)
```

The `instance_name` parameter is available in the function signature at line 381.

#### Bug #3: Recovery handler ignores `e.agent_name` in api_integration.py

**File:** `agent_cascade/api_integration.py`, lines 351-379
```python
# Current (BUG):
except LoopDetectedError as e:
    ...
    pool.surgical_rollback(instance_name, e.pop_count, reason=e.reason)
    instance.append_message(hint_msg)

# Should be:
except LoopDetectedError as e:
    ...
    looped_agent = e.agent_name if e.agent_name else instance_name
    pool.surgical_rollback(looped_agent, e.pop_count, reason=e.reason)
    looped_instance = pool.get_instance(looped_agent)
    looped_instance.append_message(hint_msg)
```

### Why This Matters

The `LoopDetectedError` class already supports the `agent_name` field (defined at line 27 of `loop_detection.py`), and it IS used correctly in `manager_ops.py:145` for child agent loop detection. But the two main detection paths (execution_engine and run_agent_unified) don't pass it, and the recovery handler ignores it even when present.

### Evidence: manager_ops.py Does It Correctly

In `agent_cascade/tools/custom/manager_ops.py:145`:
```python
raise LoopDetectedError(loop_reason, agent_name=instance_name, pop_count=pop_count, turn_pop_count=len(response), resp_snapshot=list(response))
```

And the handling at lines 160-208 properly uses `e.agent_name` to rollback the correct child. This is the pattern that should be followed everywhere.

---

## Affected Code Paths

### Path A: Normal execution via ExecutionEngine (most common)
1. Root agent calls `call_agent` → child agent created
2. Child agent enters loop during `self.run(inst)` at `_create_and_run_agent` line 2759
3. Loop detected in `_pre_llm_checks` at line 1183-1187
4. Exception raised WITHOUT agent_name (Bug #1)
5. Propagates through engine.run() catch at line 794
6. Caught by `run_agent_in_pool_with_recovery` at line 351
7. Root agent rolled back instead of child (Bug #3)

### Path B: Streaming detection in run_agent_unified.py
1. `_detect_loop_in_instance()` called every 10 ticks at line 217
2. Loop detected → exception raised WITHOUT agent_name (Bug #2)
3. Caught by `run_agent_in_pool_with_recovery` at line 351
4. Wrong agent rolled back (Bug #3)

### Path C: manager_ops.py child loop detection (already works correctly)
1. Child agent loops during internal execution
2. Loop detected → exception raised WITH agent_name ✓
3. Caught and handled locally in manager_ops.py lines 160-208 ✓
4. Correct child agent rolled back ✓

---

## Additional Concerns

### Pop Count Calculation May Be Wrong Cross-Agent

When the loop is detected for a child agent but the root agent's conversation is rolled back, the `pop_count` computed from the child's message window is applied to the root agent's conversation. This can cause:
- Too many messages removed from root (if root has longer history)
- Too few messages removed (if root has shorter recent pattern)

### Cooldown Flag Is Per-Agent But Rollback Affects Wrong Agent

The `_suppress_loop_detection_next_turn` flag is set on the instance that was detected. But if the wrong agent gets rolled back, the cooldown doesn't help because the wrong agent's conversation was modified.

---

## Fix Plan

### Step 1: Add `agent_name` to LoopDetectedError in execution_engine.py
- **File:** `agent_cascade/execution_engine.py`, line 1187
- **Change:** `raise LoopDetectedError(reason=reason, agent_name=inst_name, pop_count=pop_count)`

### Step 2: Add `agent_name` to LoopDetectedError in run_agent_unified.py  
- **File:** `agent_cascade/run_agent_unified.py`, line 412
- **Change:** `raise LoopDetectedError(reason=reason, agent_name=instance_name, pop_count=pop_count)`

### Step 3: Use `e.agent_name` in recovery handler
- **File:** `agent_cascade/api_integration.py`, lines 351-379
- **Change:** Resolve the correct agent from `e.agent_name`, fallback to `instance_name` for backward compatibility

### Step 4: Handle cross-agent rollback edge case
- If a child agent loops and the exception propagates out of `_create_and_run_agent`, the recovery handler needs to know whether it should even retry (retrying the root agent won't fix a child agent's loop)
- Consider: if `e.agent_name != instance_name`, should we re-raise instead of retry?

---

## Testing Scenarios

1. **Child agent loops, root does not** → Verify child is rolled back, root is untouched
2. **Root agent loops** → Verify root is still rolled back correctly (no regression)
3. **Both root and child loop simultaneously** → Verify each gets handled independently
4. **Loop detected via streaming path (run_agent_unified)** → Verify correct agent rollback

---

## Files to Modify (Summary)

| File | Line(s) | Change |
|------|---------|--------|
| `agent_cascade/execution_engine.py` | 1187 | Add `agent_name=inst_name` |
| `agent_cascade/run_agent_unified.py` | 412 | Add `agent_name=instance_name` |
| `agent_cascade/api_integration.py` | 351-379 | Use `e.agent_name` for rollback target |