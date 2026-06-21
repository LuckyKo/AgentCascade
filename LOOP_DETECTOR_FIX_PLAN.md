# Loop Detector Rollback Bug - Implementation Plan

**Date:** 2026-06-21  
**Status:** Ready for Implementation (v3 - Final, All Reviewer Findings Incorporated)  
**Related Bug Report:** `LOOP_DETECTOR_ROLLBACK_BUG_REPORT.md`  
**Related Analysis:** `.agent_lessons/lessons_loop_detector_bug.md`

---

## Reviewer Feedback Incorporated (v3 Updates)

This version incorporates findings from the reviewer's second code review:

| Finding | Severity | Status |
|---------|----------|--------|
| Bug #0: LoopDetectedError class mismatch in agent_instance.py | 🔴 Critical | ✅ Added as Bug #0 |
| Bug #4: Parallel path swallows LoopDetectedError | 🟠 Major | ✅ Added as Bug #4 |
| Silent failure when get_instance() returns None | 🔴 Minor | ✅ Improved error handling with early break |
| Backward compat claim overstated | 🔵 Minor | ✅ Clarified in rationale |
| Missing parallel path test | 🔵 Minor | ✅ Added Test 6 |

---

## Executive Summary

This document provides a detailed implementation plan for fixing the loop detector rollback bug where child agent loops cause the root agent to be rolled back instead of the actual looping child agent.

### Problem Statement
When a child agent enters a loop:
1. `LoopDetectedError` is raised WITHOUT the `agent_name` parameter
2. Exception propagates up through multiple layers to the root recovery handler
3. Recovery handler uses its own `instance_name` parameter (root agent) for rollback
4. **Result:** Root agent's conversation is rolled back instead of child agent's

### Solution Overview
Five surgical fixes across five files:

**CRITICAL PREREQUISITE:**
0. **agent_instance.py:424-430** - Update `LoopDetectedError` class to support `agent_name` parameter (required for all other fixes to work)

**PRIMARY FIXES:**
1. **execution_engine.py:1187** - Add `agent_name=inst_name` when raising exception
2. **run_agent_unified.py:412** - Add `agent_name=instance_name` when raising exception  
3. **api_integration.py:351-381** - Use `e.agent_name` for rollback target with fallback
4. **agent_pool.py:1569, 1589** - Re-raise `LoopDetectedError` in parallel agent path (don't swallow it)

**Why Bug #0 is Critical:** The codebase has TWO `LoopDetectedError` classes:
- `loop_detection.py:25-33` — Has correct signature with `agent_name`, `turn_pop_count`, `resp_snapshot`
- `agent_instance.py:424-430` — Only has `(reason, pop_count)`, used by ALL execution code

All files import from `agent_instance.py`. Without updating that class first, adding `agent_name=` to raise statements will cause `TypeError: __init__() got an unexpected keyword argument 'agent_name'`.

---

## Detailed Implementation Plan

### Bug #0: LoopDetectedError Class Mismatch (CRITICAL PREREQUISITE)

**File:** `agent_cascade/agent_instance.py`  
**Lines:** 424-430  
**Context:** `LoopDetectedError` class definition used by all execution code

#### Problem
The codebase has TWO `LoopDetectedError` class definitions:

1. **`loop_detection.py:25-33`** — Correct signature with all parameters:
   ```python
   class LoopDetectedError(Exception):
       def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
           self.reason = reason
           self.agent_name = agent_name
           self.pop_count = pop_count
           self.turn_pop_count = turn_pop_count
           self.resp_snapshot = resp_snapshot or []
   ```

2. **`agent_instance.py:424-430`** — Simplified signature (ONLY 2 params):
   ```python
   class LoopDetectedError(Exception):
       def __init__(self, reason: str, pop_count: int):
           super().__init__(reason)
           self.reason = reason
           self.pop_count = pop_count
   ```

**All execution files import from `agent_instance.py`:**
- `execution_engine.py:46` — `from .agent_instance import AgentInstance, LoopDetectedError, AgentState`
- `run_agent_unified.py:28` — `from .agent_instance import LoopDetectedError`
- `api_integration.py:23` — `from .agent_instance import AgentInstance, AgentState, LoopDetectedError`

**Without this fix**, Bugs #1 and #2 will cause:
```python
TypeError: __init__() got an unexpected keyword argument 'agent_name'
```

#### Current Code (Lines 424-430)
```python
class LoopDetectedError(Exception):
    """Raised when detect_loop() finds a repetitive pattern in agent conversation."""

    def __init__(self, reason: str, pop_count: int):
        super().__init__(reason)
        self.reason = reason
        self.pop_count = pop_count  # How many messages to roll back
```

#### Required Change
```python
class LoopDetectedError(Exception):
    """Raised when detect_loop() finds a repetitive pattern in agent conversation."""

    def __init__(self, reason, agent_name=None, pop_count=None, turn_pop_count=0, resp_snapshot=None):
        self.reason = reason
        self.agent_name = agent_name
        self.pop_count = pop_count
        self.turn_pop_count = turn_pop_count
        self.resp_snapshot = resp_snapshot or []
        super().__init__(f"Loop detected for {agent_name or 'agent'}: {reason}")
```

**Rationale:** This brings `agent_instance.py`'s `LoopDetectedError` in line with `loop_detection.py`'s version. All execution code imports from `agent_instance.py`, so this single change enables all other fixes to work. The signature matches the existing `loop_detection.py` version for consistency.

**Impact:** Low risk — only changes exception class definition, no logic changes. All existing `raise LoopDetectedError(reason=..., pop_count=...)` calls remain valid (agent_name is optional with default None).

---

### Bug #1: Missing agent_name in execution_engine.py

**File:** `agent_cascade/execution_engine.py`  
**Line:** 1187  
**Context:** Loop detection in `_pre_llm_checks()` method

#### Current Code (Lines 1183-1192)
```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = self._detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        logger.warning(f"Loop detected for {inst_name}: {reason}")
        raise LoopDetectedError(reason=reason, pop_count=pop_count)  # ← BUG: missing agent_name
else:
    # Clear the cooldown flag now that we've skipped loop detection this turn.
    # Next turn will run normal loop detection (no more suppression).
    instance._suppress_loop_detection_next_turn = False
```

#### Required Change
```python
if not getattr(instance, '_suppress_loop_detection_next_turn', False):
    loop_info = self._detect_loop(messages)
    if loop_info:
        reason, pop_count = loop_info
        logger.warning(f"Loop detected for {inst_name}: {reason}")
        raise LoopDetectedError(reason=reason, agent_name=inst_name, pop_count=pop_count)  # ← FIXED
else:
    # Clear the cooldown flag now that we've skipped loop detection this turn.
    # Next turn will run normal loop detection (no more suppression).
    instance._suppress_loop_detection_next_turn = False
```

**Rationale:** The `inst_name` variable is already in scope (defined at line 1175 as `inst_name = instance.instance_name`). Adding it to the exception ensures the exception carries context about which agent looped.

---

### Bug #2: Missing agent_name in run_agent_unified.py

**File:** `agent_cascade/run_agent_unified.py`  
**Line:** 412  
**Context:** Streaming loop detection in `_detect_loop_in_instance()` function

#### Current Code (Lines 407-417)
```python
try:
    loop_info = _detect_loop_func(all_msgs)
    if loop_info:
        reason, pop_count = loop_info
        # Raise LoopDetectedError so the recovery wrapper handles rollback
        raise LoopDetectedError(reason=reason, pop_count=pop_count)  # ← BUG: missing agent_name
except LoopDetectedError:
    # Re-raise — let run_agent_in_pool_with_recovery handle surgical rollback + retry
    raise
except Exception as e:
    logger.debug(f"Loop detection failed for {instance_name}: {e}")
```

#### Required Change
```python
try:
    loop_info = _detect_loop_func(all_msgs)
    if loop_info:
        reason, pop_count = loop_info
        # Raise LoopDetectedError so the recovery wrapper handles rollback
        raise LoopDetectedError(reason=reason, agent_name=instance_name, pop_count=pop_count)  # ← FIXED
except LoopDetectedError:
    # Re-raise — let run_agent_in_pool_with_recovery handle surgical rollback + retry
    raise
except Exception as e:
    logger.debug(f"Loop detection failed for {instance_name}: {e}")
```

**Rationale:** The `instance_name` parameter is available in the function signature (line 381). Adding it ensures consistency with the execution_engine fix.

---

### Bug #3: Recovery handler ignores e.agent_name in api_integration.py

**File:** `agent_cascade/api_integration.py`  
**Lines:** 351-381  
**Context:** Loop recovery in `run_agent_in_pool_with_recovery()` function

#### Current Code (Lines 351-381)
```python
except LoopDetectedError as e:
    if not auto_rollback_enabled or retry_count >= max_auto_retries:
        logger.warning(
            f"Loop detected for {instance_name}: {e.reason}. "
            f"Exceeded retries ({retry_count}/{max_auto_retries}). Stopping."
        )
        # Yield error state so UI can display it
        error_msg = Message(
            role=ASSISTANT,
            content=f"[SYSTEM: Loop detected — {e.reason}]",
        )
        yield [error_msg]
        return

    logger.warning(
        f"Loop detected for {instance_name}: {e.reason}. "
        f"Surgical rollback (Retry {retry_count + 1}/{max_auto_retries})."
    )

    # Surgical rollback + hint injection under per-instance lock for atomicity
    pool.surgical_rollback(instance_name, e.pop_count, reason=e.reason)  # ← BUG: uses instance_name

    # Inject loop avoidance hint (atomic with rollback)
    hint_msg = Message(
        role=USER,
        content=f"[SYSTEM]: A repetitive loop was detected ({e.reason}). "
                f"Please try a different approach.",
    )
    instance.append_message(hint_msg)  # ← BUG: adds to wrong instance (root instead of child)

    retry_count += 1
```

#### Required Change
```python
except LoopDetectedError as e:
    # FIX: Use agent_name from exception if available, fallback to instance_name for backward compat
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

    # FIX: Surgical rollback on the CORRECT agent (the one that actually looped)
    pool.surgical_rollback(looped_agent, e.pop_count, reason=e.reason)

    # FIX: Get the correct instance for hint injection
    looped_instance = pool.get_instance(looped_agent)
    if looped_instance:
        # Inject loop avoidance hint (atomic with rollback)
        hint_msg = Message(
            role=USER,
            content=f"[SYSTEM]: A repetitive loop was detected ({e.reason}). "
                    f"Please try a different approach.",
        )
        looped_instance.append_message(hint_msg)
    else:
        # REVIEWER FINDING #2: If instance not found, hint won't be injected and retry may be wasted
        # Use error level logging since this indicates a logic issue
        logger.error(
            f"Could not find instance '{looped_agent}' for hint injection after rollback. "
            f"Retry may not break the loop without hint message."
        )
        # Consider breaking out of retry loop early if we can't inject the hint
        # This prevents wasting remaining retries on an agent that never gets the hint
        break

    retry_count += 1
```

**Rationale:** 
- Line 352: Resolve the correct agent name from exception, with fallback for backward compatibility
- Lines 354-363: Update logging and error messages to show correct agent name (user-facing improvement)
- Lines 365-370: Log correct agent name in retry message
- Line 373: Rollback the correct agent (looped_agent instead of instance_name)
- Lines 376-391: Get the correct instance and inject hint there, with improved error handling

**Note on Backward Compatibility:** The fallback `e.agent_name if e.agent_name else instance_name` is a belt-and-suspenders measure. After all fixes are applied, ALL code paths that raise `LoopDetectedError` will include `agent_name`:
- execution_engine.py:1187 (fixed by Bug #1)
- run_agent_unified.py:412 (fixed by Bug #2)
- manager_ops.py:145 (already correct)

The fallback ensures resilience if new code paths are added in the future without `agent_name`.

---

### Bug #4: Parallel Agent Path Swallows LoopDetectedError

**File:** `agent_cascade/agent_pool.py`  
**Line:** 1589  
**Context:** Async parallel agent execution in `register_async_call()` method

#### Current Code (Lines 1569-1590)
```python
try:
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.compression.helpers import extract_instance_output

    engine = ExecutionEngine(self)
    # initialize() now called automatically in __init__ (Phase 4.5 cleanup)
    inst, child_conv = engine._create_and_run_agent(agent_class, child_instance_name, args, caller, nest_depth)

    if inst is None or child_conv is None:
        return f"[Parallel Agent '{child_instance_name}' Failed]: Internal error — agent creation returned None."

    if not child_conv:
        return f"[Parallel Agent '{child_instance_name}' Failed]: Execution terminated with no output."

    # Check if agent was terminated by user
    was_terminated = child_instance_name in self.terminated_instances
    result = extract_instance_output(child_conv, child_instance_name, was_terminated=was_terminated)
    status = "Terminated" if was_terminated else "Finished"
    return f"[Parallel Agent '{child_instance_name}' {status}]:\n{result}"

except Exception as e:
    return f"[Parallel Agent '{child_instance_name}' Failed]:\n{str(e)}"  # ← BUG: swallows LoopDetectedError
```

#### Required Change
```python
try:
    from agent_cascade.execution_engine import ExecutionEngine
    from agent_cascade.compression.helpers import extract_instance_output
    from .agent_instance import LoopDetectedError  # ← ADD IMPORT (use .agent_instance, not loop_detection!)

    engine = ExecutionEngine(self)
    # initialize() now called automatically in __init__ (Phase 4.5 cleanup)
    inst, child_conv = engine._create_and_run_agent(agent_class, child_instance_name, args, caller, nest_depth)

    if inst is None or child_conv is None:
        return f"[Parallel Agent '{child_instance_name}' Failed]: Internal error — agent creation returned None."

    if not child_conv:
        return f"[Parallel Agent '{child_instance_name}' Failed]: Execution terminated with no output."

    # Check if agent was terminated by user
    was_terminated = child_instance_name in self.terminated_instances
    result = extract_instance_output(child_conv, child_instance_name, was_terminated=was_terminated)
    status = "Terminated" if was_terminated else "Finished"
    return f"[Parallel Agent '{child_instance_name}' {status}]:\n{result}"

except LoopDetectedError:  # ← FIX: Re-raise LoopDetectedError before it gets swallowed
    # Let LoopDetectedError propagate to the caller's recovery handler
    raise
except Exception as e:
    return f"[Parallel Agent '{child_instance_name}' Failed]:\n{str(e)}"
```

**Rationale:** 
- The generic `except Exception` at line 1589 catches ALL exceptions including `LoopDetectedError`
- When a parallel agent loops, the exception is converted to a string like `"[Parallel Agent 'worker1' Failed]:\nLoop detected for agent: ..."`
- This string becomes a normal tool response — no rollback happens
- By adding a specific `except LoopDetectedError: raise` BEFORE the generic handler, we ensure loop exceptions propagate to the recovery wrapper

**Impact:** This affects agents called via `register_async_call()` which is used for parallel/asynchronous agent execution. Without this fix, parallel agent loops are silently swallowed and never trigger recovery.

---

## Edge Case Handling

### Edge Case #1: Child Agent Loop Propagates to Root Recovery Handler

**Scenario:**
```
Root agent (Maine) runs via run_agent_in_pool_with_recovery()
    ↓
Root calls child agent via call_agent → creates "worker1" instance
    ↓
Child executes in _create_and_run_agent() at line 2759
    ↓
Child loops → LoopDetectedError raised with agent_name="worker1"
    ↓
Exception propagates OUT of child's engine.run() 
    ↓
Exception propagates OUT of root's engine.run() at line 794
    ↓
Caught by run_agent_in_pool_with_recovery (root's wrapper)
```

**Question:** Should we retry the root agent (which won't fix child's loop) or re-raise?

**Analysis:**
The current flow has a subtle issue: when a child loops and the exception propagates all the way to the ROOT's recovery handler, retrying the root agent will just re-execute the entire root flow, including calling the child again. This IS the correct behavior because:

1. The child agent gets rolled back (via Bug #3 fix)
2. The hint message is injected into the child's conversation
3. When root retries, it calls the child again, but child now has modified state (rolled back + hint)
4. Child should exit the loop on retry

**However**, there's a concern: What if the exception keeps propagating beyond the root's recovery handler? Let's trace the full path:

```
api_integration.py::run_agent_in_pool_with_recovery() [catches LoopDetectedError]
    ↓
If retry succeeds → returns normally
If retry exhausted → yields error message and returns
```

**Conclusion:** The current flow is CORRECT. The recovery handler catches the exception, rolls back the correct agent (after our fix), and retries. If retries are exhausted, it yields an error and stops. No need to re-raise.

### Edge Case #2: Cross-Agent Rollback with Mismatched pop_count

**Scenario:** Child agent "worker1" loops with `pop_count=5`, but root agent "Maine" has different conversation length.

**Current Behavior (Before Fix):**
```python
pool.surgical_rollback(instance_name="Maine", e.pop_count=5, reason=e.reason)
```
This applies child's pop_count to root's conversation — potentially wrong!

**After Fix:**
```python
looped_agent = e.agent_name  # "worker1"
pool.surgical_rollback(looped_agent="worker1", e.pop_count=5, reason=e.reason)
```
Now the pop_count from child's loop detection is applied to child's conversation — CORRECT!

**Why This Works:**
- `pop_count` is calculated based on the agent's own message window (where the loop was detected)
- After Bug #1 and #2 fixes, `e.agent_name` identifies which agent's conversation the pop_count applies to
- Bug #3 fix ensures we rollback that specific agent

### Edge Case #3: Nested Child Agents (Grandchild Loops)

**Scenario:** Root → Child → Grandchild, where grandchild loops.

**Flow:**
```
Root (Maine) 
  ↓ calls Child (coder_worker1)
    ↓ calls Grandchild (researcher_worker2)
      ↓ Grandchild loops
        ↓ LoopDetectedError(agent_name="researcher_worker2")
          ↓ Propagates through Child's engine.run()
            ↓ Propagates through Root's engine.run()
              ↓ Caught by Root's recovery handler
                → Rolls back "researcher_worker2" (correct!)
```

**Analysis:** The fix handles this correctly because:
1. Bug #1 ensures `agent_name` is set at the source (grandchild)
2. Exception propagates without modification (line 794 bare `raise`)
3. Bug #3 uses `e.agent_name` which is still "researcher_worker2"

**Result:** Grandchild gets rolled back correctly, even though root's recovery handler caught it.

---

## Backward Compatibility

### Scenario: Root Agent Loops (Normal Case)

**Before Fix:**
```python
# execution_engine.py line 1187
raise LoopDetectedError(reason=reason, pop_count=pop_count)  # agent_name=None

# api_integration.py line 351
except LoopDetectedError as e:
    looped_agent = e.agent_name if e.agent_name else instance_name  # Falls back to instance_name
    # instance_name IS the root agent name → Correct!
```

**After Fix:**
```python
# execution_engine.py line 1187
raise LoopDetectedError(reason=reason, agent_name=inst_name, pop_count=pop_count)  
# inst_name IS the root agent name

# api_integration.py line 351
except LoopDetectedError as e:
    looped_agent = e.agent_name if e.agent_name else instance_name  # Uses e.agent_name
    # e.agent_name == instance_name → Same result!
```

**Conclusion:** Backward compatible. When root loops, `e.agent_name` equals `instance_name`, so behavior is identical.

### Scenario: Old Code Paths Without agent_name

If any code path still raises `LoopDetectedError` without `agent_name`:
```python
looped_agent = e.agent_name if e.agent_name else instance_name  # Falls back safely
```

**Conclusion:** The fallback ensures backward compatibility with any existing code that doesn't pass `agent_name`.

---

## Testing Scenarios

### Test 0: LoopDetectedError Class Accepts agent_name (Bug #0 Verification)
**Setup:** Import and instantiate LoopDetectedError with all parameters
```python
from agent_cascade.agent_instance import LoopDetectedError
try:
    raise LoopDetectedError(
        reason="test loop", 
        agent_name="test_agent", 
        pop_count=5,
        turn_pop_count=2,
        resp_snapshot=[]
    )
except LoopDetectedError as e:
    assert e.agent_name == "test_agent"
    assert e.reason == "test loop"
    assert e.pop_count == 5
```
**Expected:** No TypeError, all attributes accessible

### Test 1: Root Agent Loops (Regression Test)
**Setup:** Create a root agent that calls the same tool repeatedly
**Expected:**
- `e.agent_name` = root agent name
- Root agent's conversation rolled back
- Hint injected into root agent
- Log shows "Loop detected for {root_name}"

### Test 2: Child Agent Loops (Primary Bug Fix)
**Setup:** 
```python
root_agent.call_agent("coder", "worker1", task="Write code")
# worker1 enters loop
```
**Expected:**
- `e.agent_name` = "worker1"
- "worker1" conversation rolled back (NOT root)
- Hint injected into "worker1" (NOT root)
- Log shows "Loop detected for worker1"

### Test 3: Grandchild Agent Loops (Nested Case)
**Setup:**
```python
root → child ("coder_worker1") → grandchild ("researcher_worker2")
# researcher_worker2 enters loop
```
**Expected:**
- `e.agent_name` = "researcher_worker2"
- "researcher_worker2" rolled back
- Root and child untouched

### Test 4: Streaming Path Loop Detection
**Setup:** Trigger `_detect_loop_in_instance()` in run_agent_unified.py
**Expected:**
- Same as Test 2, but via streaming detection path

### Test 5: Retry Exhaustion
**Setup:** Agent loops more than `max_auto_retries` times
**Expected:**
- Error message yielded with correct agent name
- No infinite retry loop

### Test 6: Parallel Agent Loops (Bug #4)
**Setup:** 
```python
# Use register_async_call for parallel execution
pool.register_async_call(instance_name, function_id, "coder", "parallel_worker1", args)
# parallel_worker1 enters a loop during its execution
```
**Expected:**
- `LoopDetectedError` propagates from parallel path (not swallowed by generic except)
- Exception reaches recovery handler in api_integration.py
- "parallel_worker1" rolled back correctly (via Bug #3 fix)
- Log shows "Loop detected for parallel_worker1"

---

## Implementation Order

**MUST DO FIRST (CRITICAL PREREQUISITE):**
0. **Fix Bug #0** (agent_instance.py:424-430)
   - CRITICAL PREREQUISITE for all other fixes to work
   - Updates LoopDetectedError class signature to support agent_name parameter
   - Low risk — backward compatible expansion with defaults

**THEN IN ORDER:**
1. **Fix Bug #1** (execution_engine.py:1187)
   - Most critical — primary execution path
   - One-line change
   - Low risk

2. **Fix Bug #2** (run_agent_unified.py:412)
   - Secondary detection path
   - One-line change
   - Low risk

3. **Fix Bug #3** (api_integration.py:351-381)
   - Most complex change
   - Handles the actual rollback logic
   - Medium risk — requires careful testing

4. **Fix Bug #4** (agent_pool.py:1569, 1589)
   - Adds import and exception handler for parallel path
   - Two-line change (import + except clause before generic handler)
   - Low-Medium risk — need to verify parallel execution still works

---

## Files Modified Summary

| File | Line(s) | Change Type | Risk |
|------|---------|-------------|------|
| `agent_cascade/agent_instance.py` | 424-430 | Class signature update | **Low (CRITICAL PREREQUISITE)** |
| `agent_cascade/execution_engine.py` | 1187 | Add parameter | Low |
| `agent_cascade/run_agent_unified.py` | 412 | Add parameter | Low |
| `agent_cascade/api_integration.py` | 351-381 | Logic change | Medium |
| `agent_cascade/agent_pool.py` | 1569, 1589 | Add import + except clause | Low-Medium |

---

## Verification Checklist

After implementation:

- [ ] Syntax check all modified files
- [ ] Verify LoopDetectedError class updated in agent_instance.py (Bug #0)
- [ ] Verify `inst_name` is in scope at execution_engine.py:1187
- [ ] Verify `instance_name` is in scope at run_agent_unified.py:412
- [ ] Verify `LoopDetectedError` import added to agent_pool.py (Bug #4)
- [ ] Test root agent loop scenario (regression)
- [ ] Test child agent loop scenario (primary fix)
- [ ] Test nested agent scenario
- [ ] Test parallel agent loop scenario (Bug #4)
- [ ] Check logs show correct agent names
- [ ] Verify UI shows correct agent tabs updated
- [ ] Confirm no duplicate hint messages
- [ ] Verify parallel agents still complete successfully (no regression)

---

## Rollback Plan

If issues arise:

1. Revert api_integration.py changes first (most complex)
2. Revert agent_pool.py changes (parallel path - Bug #4)
3. Revert execution_engine.py and run_agent_unified.py changes
4. Revert agent_instance.py changes last (Bug #0)
5. System returns to "root always rolled back" behavior (known bug, but stable)

**Note:** Bug #4 can be reverted independently if parallel execution issues arise while keeping Bugs #0-3 fixes active.

---

## Notes for Reviewer

Key points to verify during code review:

1. **Bug #0 class update:** Verify `LoopDetectedError` in `agent_instance.py` now matches signature in `loop_detection.py`
2. **agent_name propagation:** Ensure `agent_name` flows from exception source to handler without modification
3. **Null handling:** Verify `e.agent_name if e.agent_name else instance_name` handles all cases
4. **Instance lookup:** Confirm `pool.get_instance(looped_agent)` returns correct instance
5. **Log consistency:** All log messages should use `looped_agent` not `instance_name`
6. **Backward compat:** Fallback ensures old code paths still work (belt-and-suspenders)
7. **Parallel path:** Verify Bug #4 correctly re-raises `LoopDetectedError` before generic except handler
8. **Hint injection error handling:** Verify early break when instance not found prevents wasted retries

---

## Related Files

- Bug Report: `LOOP_DETECTOR_ROLLBACK_BUG_REPORT.md`
- Analysis: `.agent_lessons/lessons_loop_detector_bug.md`
- Exception Class (CORRECT): `agent_cascade/loop_detection.py` (lines 25-33) — has all parameters
- Exception Class (NEEDS FIX): `agent_cascade/agent_instance.py` (lines 424-430) — only has reason, pop_count
- Working Example: `agent_cascade/tools/custom/manager_ops.py` (lines 145, 160-208)

---

**End of Implementation Plan**