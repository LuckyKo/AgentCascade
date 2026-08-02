# Implementation Plan: Endpoint Slot Scheduling and Async Agent Call Behavior

**Date**: 2026-08-02
**Author**: plan_architect_1
**Status**: DRAFT - awaiting review before implementation

---

## Target Architecture Summary (Confirmed Spec)

### Slot Pools

1. **Shared sequential pool (conc=0)**: ONE semaphore(1) for ALL conc=0 endpoints, regardless of api_base. Global hardware serialization.

2. **Per-endpoint parallel pools (conc=N > 0)**: Each api_base gets its own semaphore(N). Independent from each other and from the sequential pool.

### call_agent Flow (A calls B)

| Scenario | A's Slot | B's Slot | Execution Mode |
|----------|----------|----------|----------------|
| B gets conc>0 slot, different from A | Kept | Acquired by B in engine.run() | ASYNC via ThreadPoolExecutor |
| B gets conc=0 slot | Released | Acquired by B | SYNC |
| B needs same slot as A (same api_base or both conc=0) | Released | Acquired by B | SYNC |

### Key Properties

- A never releases a parallel (conc > 0) slot when launching an async child on a different endpoint.
- Async for conc > 0 follows existing async shell_cmd pattern: launch ID, immediate return, result injection, wake-from-sleep.
- Conc=0 remains fully serialized with sync execution.

---

## 1. Gap Analysis

### Component: EndpointScheduler (api_router.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\api_router.py`, lines 201-580

#### What's Already Correct
- **Shared sequential slot**: Lines 251-254 correctly implement `_shared_sequential_slot_` for all conc=0 endpoints. ✅
- **Per-endpoint semaphores**: Lines 263-295 create per-api_base semaphore pools for conc>0. ✅
- **Semaphore resize logic**: Lines 273-293 handle dynamic concurrency changes. ✅
- **Acquire/release pattern**: Lines 229-440 correctly block on capacity and return release callback. ✅

#### What's Wrong or Missing
- **Nothing wrong at this level**. The EndpointScheduler is already correct per the spec.

### Component: call_agent Slot Collision Detection (tool_dispatcher.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\tool_dispatcher.py`, lines 255-292

#### What's Already Correct
- **Slot holder detection**: Lines 268-273 check if caller holds a slot via `_slot_release`. ✅
- **Sequential endpoint guard**: Lines 275-285 force SYNC when child has conc=0. ✅
- **Sync path implementation**: `_run_child_sync()` (lines 401-492) correctly releases caller's slot, runs child, re-acquires. ✅
- **Async path registration**: `_run_child_async()` (lines 494-528) calls `pool.register_async_call()`. ✅

#### What's Wrong or Missing

**CRITICAL BUG - Lines 265-292: Overly aggressive "fake sync mode"**

Current logic at line 287:
```python
if caller_holds_slot:
    return self._run_child_sync(...)
else:
    return self._run_child_async(...)
```

**Problem**: This forces SYNC whenever the caller holds ANY slot, even when:
- Caller is on a parallel endpoint (conc > 0) with free capacity
- Child would use a DIFFERENT endpoint's pool
- No actual collision exists

This prevents the desired async behavior where A keeps its parallel slot while launching B asynchronously.

**Missing**: Logic to determine if B's slot type actually collides with A's slot:
1. Query scheduler for B's effective concurrency and slot key
2. Compare against A's current slot type
3. Only use SYNC when there's an actual collision

### Component: register_async_call (agent_pool.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\agent_pool.py`, lines 2379-2441

#### What's Already Correct
- **Callable creation**: Lines 2401-2439 wrap child execution in `run_child_agent()`. ✅
- **No pre-acquisition of slot**: Comment at lines 2404-2407 correctly notes that engine.run() acquires its own slot. ✅
- **Registration with AsyncToolRegistry**: Line 2441 calls `_async_registry.register()`. ✅

#### What's Wrong or Missing
- **Nothing wrong**. This is already correct - the child acquires its own slot inside `engine.run()`.

### Component: ExecutionEngine Slot Acquisition (execution_engine.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\execution_engine.py`, lines 807-825, 1068-1077

#### What's Already Correct
- **Initial slot acquisition**: Lines 1068-1077 acquire slot at engine.run() start (unless `_skip_slot_acquire` is set). ✅
- **Slot release on exit**: Finally block releases slot. ✅
- **Re-acquisition after sleep wakeup**: Lines 4097-4106, 4191-4204 re-acquire after waking from SLEEPING. ✅

#### What's Wrong or Missing
- **Nothing wrong at this level**. Engine correctly acquires/releases its own slot.

### Component: AsyncToolRegistry (async_tools.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\async_tools.py`, lines 50-213

#### What's Already Correct
- **ThreadPoolExecutor**: Lines 74-78 create executor with 4 workers. ✅
- **Registration and submission**: Lines 80-105 register entry and submit to executor. ✅
- **Result buffering**: Lines 122-150 execute, mark completed, put result into AsyncResultBuffer. ✅
- **Completion tracking**: `has_pending()` (lines 152-172) correctly tracks pending tools. ✅

#### What's Wrong or Missing
- **Nothing wrong**. Already matches the target pattern.

### Component: AsyncResultBuffer and Wake-from-Sleep (execution_engine.py)

**File**: `N:\work\WD\AgentCascade\agent_cascade\async_tools.py`, lines 215-298
**File**: `N:\work\WD\AgentCascade\agent_cascade\execution_engine.py`, lines 4038-4209

#### What's Already Correct
- **Buffer put/drain**: AsyncResultBuffer provides thread-safe put() and drain(). ✅
- **Wake-from-sleep on async results**: Lines 4064-4109 detect async results, transition to RUNNING, inject messages. ✅
- **Re-acquire slot after wakeup**: Lines 4097-4106 re-acquire concurrency slot. ✅
- **Condition variable for waiting**: Lines 280-298 provide `wait_for_next()` for blocking wait. ✅

#### What's Wrong or Missing
- **Nothing wrong**. The wake-from-sleep mechanism is already correct.

---

## 2. Step-by-Step Implementation Plan

### Change 1: Add Scheduler Query Method to EndpointScheduler

**File**: `N:\work\WD\AgentCascade\agent_cascade\api_router.py`
**Location**: After line 580 (end of EndpointScheduler class)
**Purpose**: Allow callers to query what slot type a given agent_class would get, without actually acquiring.

Add method:
```python
def get_slot_info(self, api_base: str, concurrency_limit: int) -> dict:
    """Get slot information for an endpoint without acquiring.
    
    Returns dict with:
      - slot_key: The internal key used ('_shared_sequential_slot_' or api_base)
      - is_sequential: True if conc=0 (shared sequential pool)
      - concurrency_limit: The effective limit (-1, 0, or N>0)
    """
    is_sequential = (concurrency_limit == 0)
    slot_key = '_shared_sequential_slot_' if is_sequential else api_base
    
    return {
        'slot_key': slot_key,
        'is_sequential': is_sequential,
        'concurrency_limit': concurrency_limit,
    }
```

### Change 2: Add Helper to APIRouter for Querying Child's Slot Info

**File**: `N:\work\WD\AgentCascade\agent_cascade\api_router.py`
**Location**: After line 790 (after get_effective_concurrency)
**Purpose**: High-level method that resolves agent_class → endpoint → slot info.

Add method:
```python
def get_agent_slot_info(self, agent_class: str) -> dict:
    """Get the slot type that an agent_class would use.
    
    Args:
        agent_class: The class name of the agent
        
    Returns:
        Dict with keys: slot_key, is_sequential, concurrency_limit, api_base
    """
    concurrency = self.get_effective_concurrency(agent_class)
    if concurrency == -1:
        return {
            'slot_key': None,
            'is_sequential': False,
            'concurrency_limit': -1,
            'api_base': None,
            'needs_slot': False,
        }
    
    llm_cfg = self.get_llm_config(agent_class)
    api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
    
    slot_info = self.scheduler.get_slot_info(api_base, concurrency)
    slot_info['api_base'] = api_base
    slot_info['needs_slot'] = True
    
    return slot_info
```

### Change 3: Compute Caller Slot Key Inline in handle_call_agent

**File**: `N:\work\WD\AgentCascade\agent_cascade\tool_dispatcher.py`
**Location**: Within the slot collision detection block (Change 4)
**Purpose**: Determine caller's slot key directly using router methods—no need to add methods to AgentInstance.

The caller's slot key is computed inline in handle_call_agent using:
- `router.get_effective_concurrency(caller_slot_holder.agent_class)` → concurrency_limit
- `router.get_llm_config(caller_slot_holder.agent_class)` → api_base
- Same logic as EndpointScheduler.acquire(): sequential if conc==0, else api_base

This avoids adding a method to AgentInstance and keeps the slot key computation localized where it's used.

### Change 4: Rewrite Slot Collision Detection in handle_call_agent

**File**: `N:\work\WD\AgentCascade\agent_cascade\tool_dispatcher.py`
**Location**: Lines 255-292 (the "Slot Collision Detection" section)
**Purpose**: Replace the overly aggressive "if caller_holds_slot → SYNC" with proper collision detection.

Replace lines 255-292 with:

```python
# ── Slot Collision Detection (Target Architecture) ────────────────────────
# Determine whether A calling B requires sync or async execution based on
# slot pool membership, not just "does A hold a slot".
#
# Rules:
# 1. If child needs no slot (conc=-1): always ASYNC, even if caller holds a slot
# 2. If B uses conc=0 (shared sequential pool): always SYNC
#    - Only one agent can use the shared sequential slot at a time
# 3. If A holds no slot: ASYNC is safe (A doesn't block anything)
# 4. If A holds a parallel slot (conc>0) and B uses a DIFFERENT slot pool:
#    - ASYNC: A keeps its slot, B acquires its own in engine.run()
# 5. If B would need the SAME slot pool as A (same api_base with limited conc):
#    - SYNC: A releases, B acquires that slot, runs to completion

caller_slot_holder = self.pool.get_instance(caller_name)
router = self.pool.api_router

# Get child's slot info
child_slot_info = router.get_agent_slot_info(agent_class) if router else None

# Case 1: Child needs no slot (conc=-1) → always ASYNC, caller_holds_slot forced False
if not child_slot_info or not child_slot_info.get('needs_slot'):
    caller_holds_slot = False
else:
    # Case 2: Child uses conc=0 (shared sequential pool) → always SYNC
    if child_slot_info['is_sequential']:
        caller_holds_slot = True  # Force sync for sequential child
    else:
        # Check if caller holds a slot
        caller_holds_slot = False
        if caller_slot_holder and hasattr(caller_slot_holder, '_state_lock'):
            with caller_slot_holder._state_lock:
                if caller_slot_holder._slot_release is not None:
                    caller_holds_slot = True
        
        # Case 3: Caller holds no slot → ASYNC is safe (handled by else branch below)
        
        # Case 4/5: Caller holds a slot. Check for collision.
        if caller_holds_slot and child_slot_info and child_slot_info['needs_slot']:
            # Get caller's slot key (computed inline—see Change 3)
            caller_concurrency = router.get_effective_concurrency(caller_slot_holder.agent_class)
            caller_llm_cfg = router.get_llm_config(caller_slot_holder.agent_class)
            caller_api_base = caller_llm_cfg.get('api_base') or caller_llm_cfg.get('model_server', 'unknown')
            
            # Compute caller's slot key using same logic as EndpointScheduler.acquire()
            caller_is_sequential = (caller_concurrency == 0)
            caller_slot_key = '_shared_sequential_slot_' if caller_is_sequential else caller_api_base
            
            child_slot_key = child_slot_info['slot_key']
            
            # Case 5: Same slot pool → collision → SYNC
            if caller_slot_key == child_slot_key:
                caller_holds_slot = True  # Keep sync path (collision detected)
            else:
                # Case 4: Different slot pools → no collision → ASYNC is safe
                caller_holds_slot = False

if caller_holds_slot:
    return self._run_child_sync(agent_class, instance_name, args, caller_slot_holder, caller_name, child_depth)
else:
    return self._run_child_async(caller_name, function_id, agent_class, instance_name, args, child_depth)
```

### Change 5: (Removed) Update _run_child_async Return Message

**Decision**: Drop this change entirely. The existing message text is fine; keeping changes minimal.

---

## 3. Integration with Existing Async Machinery

### How register_async_call, AsyncToolRegistry, AsyncResultBuffer Are Used

The flow for async agent calls (conc > 0, different slot pool):

1. **A decides to call B** → `handle_call_agent()` determines no collision → calls `_run_child_async()`.

2. **Registration**: `_run_child_async()` calls `pool.register_async_call()` with:
   - `instance_name=caller_name` (results go back to A)
   - `function_id` from LLM tool call
   - Child's agent_class, instance_name, args, nest_depth

3. **AsyncToolRegistry.register()**: Creates BackgroundToolEntry wrapping `run_child_agent()` callable, submits to ThreadPoolExecutor.

4. **Background execution**: `run_child_agent()` runs in thread pool:
   - Creates ExecutionEngine for child B
   - Calls `run_child_core()` → creates instance → calls `engine.run()`
   - **Inside engine.run()**: B acquires its own slot via `_acquire_slot_with_logging()` (line 1076)
   - B runs to completion
   - Result string returned

5. **Result buffering**: AsyncToolRegistry._execute() (lines 122-150):
   - Marks entry.completed = True
   - Puts result into `pool._async_results.put(caller_name, result, function_id)`

6. **Wake-from-sleep**: If A is sleeping in `_handle_sleep_transition()` (execution_engine.py lines 4038-4209):
   - Line 4066: `drain_async_results()` finds B's result
   - Lines 4070-4079: Transition to RUNNING, clear sleeping_since
   - Lines 4083-4087: Inject async result as tool response message
   - Lines 4097-4106: Re-acquire A's slot (A never lost it since we're on parallel pool)

7. **A processes B's result**: LLM receives the injected tool response and continues.

### Consistency with Async shell_cmd

The async agent call pattern is already consistent with async shell_cmd:

| Aspect | async shell_cmd | async call_agent | Status |
|--------|-----------------|------------------|--------|
| Launch mechanism | AsyncShellTracker.launch() → subprocess | register_async_call() → ThreadPoolExecutor | ✅ Both non-blocking |
| Result delivery | pool._async_results.put() | pool._async_results.put() | ✅ Same buffer |
| Wake-from-sleep | _handle_sleep_transition() drains async results | Same code path | ✅ Shared |
| Slot behavior | Caller keeps slot during shell execution | Caller keeps parallel slot during agent execution (after fix) | ✅ After Change 4 |

### No Adjustments Needed to Async Machinery

The existing async infrastructure is already correct:
- AsyncToolRegistry uses ThreadPoolExecutor with proper result buffering
- AsyncResultBuffer provides thread-safe put/drain with condition variable
- Wake-from-sleep in execution_engine.py already handles async results generically
- register_async_call already delegates slot acquisition to engine.run()

---

## 4. Test Plan

### New Tests Needed

**File**: Extend `N:\work\WD\AgentCascade\tests\test_call_agent_sync_async_selection.py`

#### Test Group 1: Parallel-to-Different-Parallel Async Launch

```python
def test_parallel_to_different_parallel_uses_async(self):
    """A on parallel endpoint (conc=3) calls B on different parallel endpoint (conc=2).
    
    Expected: ASYNC path - A keeps its slot, B acquires its own.
    """
```

Setup:
- Endpoint E1: api_base="http://e1", concurrency_limit=3
- Endpoint E2: api_base="http://e2", concurrency_limit=2
- Agent "coder" → E1, Agent "reviewer" → E2
- Caller holds slot on E1, calls reviewer on E2

Verify:
- `_run_child_async` is called (not `_run_child_sync`)
- No slot release for caller before child launch

#### Test Group 2: Parallel-to-Same-Parallel Sync Launch

```python
def test_parallel_to_same_parallel_uses_sync(self):
    """A on parallel endpoint calls B on SAME parallel endpoint.
    
    Expected: SYNC path - same slot pool, would collide.
    """
```

Setup:
- Single endpoint E1: concurrency_limit=3
- Both agents use E1
- Caller holds slot on E1, calls another agent also on E1

Verify:
- `_run_child_sync` is called (collision detected)

#### Test Group 3: Sequential Endpoint Always Sync

```python
def test_sequential_endpoint_always_sync(self):
    """Any call to a conc=0 agent uses SYNC path.
    
    Expected: SYNC - shared sequential pool, only one agent at a time.
    """
```

Setup:
- Endpoint with concurrency_limit=0
- Any caller calls agent on this endpoint

Verify:
- `_run_child_sync` is called regardless of caller's slot state

#### Test Group 4: Unlimited Caller Launching Async Child

```python
def test_unlimited_caller_async_launch(self):
    """Caller on unlimited endpoint (conc=-1) launches child on parallel endpoint.
    
    Expected: ASYNC - caller has no slot, can't collide.
    """
```

Setup:
- Caller's endpoint: concurrency_limit=-1
- Child's endpoint: concurrency_limit=2

Verify:
- `_run_child_async` is called

#### Test Group 5: End-to-End Async Agent Execution with Slot Independence

```python
def test_async_agent_execution_slot_independence(self):
    """Integration test: A on parallel endpoint launches B async, both make progress.
    
    Verifies that A's slot is not released and A can continue LLM calls while B runs.
    """
```

This requires more elaborate mocking but validates the full flow including:
- Slot acquisition for both agents
- Async registration and execution
- Result delivery to caller

#### Test Group 6: Sequential Caller → Parallel Child (ASYNC)

```python
def test_sequential_to_parallel_uses_async(self):
    """A on sequential endpoint (conc=0) calls B on parallel endpoint (conc>0).
    
    Expected: ASYNC - A keeps its sequential slot, B gets its own parallel slot.
    """
```

Setup:
- Caller's endpoint: concurrency_limit=0 (sequential)
- Child's endpoint: concurrency_limit=2 (parallel)

Verify:
- `_run_child_async` is called
- Caller's sequential slot is NOT released before child launch

#### Test Group 7: Unlimited Caller → Sequential Child (SYNC)

```python
def test_unlimited_to_sequential_uses_sync(self):
    """A on unlimited endpoint (conc=-1) calls B on sequential endpoint (conc=0).
    
    Expected: SYNC - child is conc=0, shared sequential pool requires sync.
    """
```

Setup:
- Caller's endpoint: concurrency_limit=-1
- Child's endpoint: concurrency_limit=0

Verify:
- `_run_child_sync` is called regardless of caller having no slot

#### Test Group 8: Unlimited Caller → Parallel Child, Different Pool (ASYNC)

```python
def test_unlimited_to_parallel_different_pool_uses_async(self):
    """A on unlimited endpoint (conc=-1) calls B on parallel endpoint (conc>0).
    
    Expected: ASYNC - child needs no collision check; caller has no slot.
    """
```

Setup:
- Caller's endpoint: concurrency_limit=-1
- Child's endpoint: concurrency_limit=3

Verify:
- `_run_child_async` is called

#### Test Group 9: Unlimited Child Always Async

```python
def test_unlimited_child_always_async(self):
    """Any caller calls B on unlimited endpoint (conc=-1).
    
    Expected: ASYNC - child needs no slot, so collision is impossible.
    Tests the explicit conc=-1 guard in Change 4.
    """
```

Setup:
- Caller holds a parallel slot (conc=2)
- Child's endpoint: concurrency_limit=-1

Verify:
- `_run_child_async` is called despite caller holding a slot

### Existing Tests to Update

**File**: `N:\work\WD\AgentCascade\tests\test_call_agent_sync_async_selection.py`

Several existing tests may need adjustment because they were written against the old "caller_holds_slot → always sync" behavior:

1. **Tests that expect SYNC when caller holds any slot but child uses different pool** - These need to be updated to expect ASYNC instead.

2. **Sequential Endpoint Guard tests** (around lines 200-250) - These should still pass since conc=0 → SYNC is preserved.

3. **Tests relying on the "fake sync mode" comment rationale** - Update comments to reflect new collision-based logic.

### Test Execution Strategy

1. Run existing test suite first to establish baseline:
   ```
   pytest tests/test_call_agent_sync_async_selection.py -v
   pytest tests/test_endpoint_scheduler_stress.py -v
   pytest tests/test_nested_agent_calls.py -v
   ```

2. Apply changes in order (Change 1 → Change 4)

3. Run updated/new tests to verify:
   - Parallel-to-different-parallel uses async
   - Sequential endpoint still forces sync
   - Same-slot-pool collision still forces sync
   - Async result delivery and wake-from-sleep work correctly

---

## Summary of Changes

| Change | File | Lines | Type | Risk |
|--------|------|-------|------|------|
| 1 | api_router.py | ~580+ | Add method `get_slot_info()` | Low |
| 2 | api_router.py | ~790+ | Add method `get_agent_slot_info()` | Low |
| 3 | tool_dispatcher.py | inline in Change 4 | Compute caller slot key inline | Low |
| 4 | tool_dispatcher.py | 255-292 | Rewrite collision detection (add conc=-1 guard) | Medium (core behavior change) |
| 5 | — | — | Removed (cosmetic only) | None |

**Total files modified**: 2 files
**Total lines changed**: ~60-80 lines
**No refactoring needed** - changes are surgical additions and targeted rewrite of the collision detection block.