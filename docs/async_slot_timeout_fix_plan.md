# Async Slot Timeout Fix Plan

## Problem Statement

When an async agent call times out waiting for an endpoint slot (30s timeout on semaphore acquire), the child agent instance has already been created and registered in the pool. The `TimeoutError` propagates up and is caught by a bare except that returns an error string, but **no cleanup of the zombie instance occurs**. The orphaned instance persists in pool state indefinitely.

Example error:
```
Timed out after 30s waiting for endpoint slot on https://opencode.ai/zen/v1. 
Current active count: 1, max allowed: 1. Currently held by: phase1_reviewer_worker (generalist)
```

## Root Cause Summary

The execution flow creates the instance **before** acquiring a slot:

1. `agent_pool.py:register_async_call` → submits thread via async_tools registry
2. Thread calls `execution_engine._create_and_run_agent`
3. Line 4529: `find_or_create_instance()` — **instance created and registered in pool**
4. Lines 4536-4590: System message, conversation, WebUI state all initialized
5. Line 4624: `for resp in self.run(inst)` starts execution loop
6. Inside `engine.run()`, line 1121: `_acquire_slot_with_logging()` → calls scheduler.acquire()
7. `api_router.py:303`: Semaphore acquire times out → raises `TimeoutError`
8. Error propagates through `engine.run()` → `run_child_core` → caught at `agent_pool.py:2483`
9. Bare except returns formatted error string — **instance never dismissed**

The instance remains in:
- Pool's instance registry (`pool.instances`)
- `pool.instance_conversations`
- Lifecycle manager state
- WebUI agent list (visible as a dead tab)

## Proposed Fixes

### Fix 1: Instance Cleanup on Slot Timeout (REQUIRED)

**Goal:** When `TimeoutError` occurs during slot acquisition, dismiss the zombie instance from all pool state.

**Primary location:** `agent_pool.py`, `run_child_agent` closure except block (lines 2483-2485)

**Rationale:** This is the single choke point for all async agent execution failures. Adding cleanup here handles TimeoutError and any other exception that occurs after instance creation but before completion.

**Changes:**

File: `agent_cascade/agent_pool.py`

Current code (lines 2483-2485):
```python
except Exception as e:
    # Catch generic exceptions to preserve the structured agent-specific prefix
    return f"[Agent '{child_instance_name}' Failed]:\n{str(e)}"
```

Replace with:
```python
except Exception as e:
    # Cleanup zombie instance on failure (e.g., slot timeout after instance creation)
    try:
        self.dismiss_instance(child_instance_name)
    except Exception as cleanup_err:
        logger.warning(f"Failed to dismiss zombie instance {child_instance_name} during error cleanup: {cleanup_err}")
    return f"[Agent '{child_instance_name}' Failed]:\n{str(e)}"
```

**Risk Assessment:**
- **Low risk.** `dismiss_instance` is already used elsewhere (user stop, manual dismiss). It's idempotent-safe — calling it on an already-dismissed instance should be harmless. If not, the try/except prevents cascading failures.
- **Edge case:** If the instance completed successfully but something else raised afterward, we'd incorrectly dismiss a good instance. However, this is unlikely — `run_child_core` only returns normally on success; any exception means failure.
- **Mitigation:** Add logging to track when cleanup happens for debugging.

**Alternative location (less preferred):** `execution_engine.py:_create_and_run_agent` finally block. Downsides: runs on both success and failure, requires discriminator logic, duplicates cleanup responsibility across two layers.

### Fix 2: Fallback Routing in Scheduler.acquire (DEFERRED - NEEDS SEPARATE DESIGN)

**STATUS: DEFERRED** — Review identified fundamental design issues. Slot acquisition and request routing are decoupled in the current architecture, making simple fallback impractical without larger changes. Will require a separate design effort.

**Original Goal:** When the primary endpoint's semaphore is full, try acquiring from another available endpoint before timing out.

**Review Findings (from reviewer_async_slot_fix2):**
1. **Fundamental flaw:** Acquiring a slot from alternative endpoint B doesn't ensure the LLM request uses endpoint B — `get_llm_config(agent_class)` returns the primary endpoint regardless.
2. **Missing method:** `get_endpoint_list_for_agent` doesn't exist; would need to use `get_endpoint_chain` or create a new method.
3. **Model compatibility:** No mechanism to filter endpoints by model support in fallback loop.

**Recommendation:** Defer Fix 2. Address fallback routing at a higher level where both slot acquisition and request routing are coordinated, or redesign the endpoint selection flow so that the acquired slot determines the request destination. This is out of scope for the current zombie cleanup fix.

### Fix 3: Reorder Instance Creation (ARCHITECTURAL, OPTIONAL)

**Goal:** Acquire the slot BEFORE creating the instance, so on timeout there's no zombie to clean up.

**Location:** `execution_engine.py:_create_and_run_agent` and/or `agent_pool.py:register_async_call`

**Changes:** Move slot acquisition earlier in the flow (e.g., in `register_async_call` before submitting to thread pool).

**Risk Assessment:**
- **High risk.** This is a structural change that affects the entire async agent lifecycle.
- The existing comment at agent_pool.py:2450 explicitly warns about deadlock if slot is acquired both before AND inside engine.run(). Would need to either skip the second acquire or restructure significantly.
- Not worth the complexity when Fix 1 solves the problem cleanly.

**Recommendation:** Skip this for now. Fix 1 is simpler and safer.

## Implementation Order

1. **Fix 1 only** (Instance cleanup) — required, low risk, immediate relief from zombie instances
2. **Fix 2** — deferred for separate design effort due to architectural complexity
3. **Fix 3** — not recommended at this time

## Testing Checklist

For Fix 1:
- [ ] Simulate slot timeout (set concurrency_limit=0 or ENDPOINT_SLOT_ACQUIRE_TIMEOUT very low)
- [ ] Verify zombie instance is dismissed from pool state after timeout error
- [ ] Verify parent agent receives the error message correctly
- [ ] Verify no double-cleanup if instance was already in some cleanup state
- [ ] Verify normal (non-timeout) async agent execution still works

## Related Issues

- todo.md line 76: Main issue tracking this work
- Compression fix (todo.md line 72): Similar pattern of defense-in-depth with multiple cleanup layers