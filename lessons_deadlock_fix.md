# Deadlock Fix: Nested Sync Agent Calls

## Date: 2026-05-31
## Agent: DeadlockFixer (reviewed by Reviewer)

## Problem

Nested synchronous agent calls via `call_agent` would deadlock when both parent and child agents used the same `api_base`. The EndpointScheduler uses a `threading.Semaphore` per api_base with `effective_concurrency=0` meaning one-at-a-time. Flow:

1. Root agent (nest_depth=0) acquires semaphore slot → sem goes from 1→0
2. Root agent executes, calls nested agent sync (nest_depth=1)
3. Nested agent tries to acquire same semaphore → blocks forever (sem at 0)
4. Root agent can't finish (waiting for nested) → never releases → **deadlock**

## Root Cause

`_execute_agent_sync` in `execution_engine.py` acquired an endpoint slot via `_acquire_slot` for ALL calls, including nested ones. This was added as "Fix #5" but introduced the deadlock when both parent and child share an api_base.

## Solution

**Skip endpoint slot acquisition for nested sync calls (nest_depth > 0).** The parent already holds the slot — the child doesn't need its own. Nested agents are serialized by the caller anyway, so top-level concurrency control is sufficient.

### Key Code Change (execution_engine.py, ~line 2032)

```python
if nest_depth > 0:
    # Skip — parent already holds the slot
    logger.debug(f"SKIPPING endpoint slot acquisition for {instance_name} (nest_depth={nest_depth})")
else:
    # Root-level call: acquire a slot
    if not hasattr(self.pool, '_execution') or not hasattr(self.pool._execution, '_acquire_slot'):
        logger.error(f"fatal: pool missing _ExecutionManager or _acquire_slot for {instance_name}")
        return f"Error: Endpoint scheduling not available for '{instance_name}'."
    endpoint_release = self.pool._execution._acquire_slot(agent_class, instance_name)
```

And in the `finally` block, release only if a slot was acquired (endpoint_release is not None).

### Known Trade-off

Nested sync agents bypass per-endpoint concurrency enforcement. This is acceptable because:
- Nested calls are **serialized** by definition (parent blocks until child returns)
- Top-level concurrency control (parallel task slots via `submit_task`) limits total capacity
- The main branch never acquired slots for sync calls at all — this behavior matches the proven design

### Debug Logging Added

All debug logs use `[CALL_AGENT_DEBUG]` prefix with `logger.debug()` level:

| File | Location | What's Logged |
|------|----------|--------------|
| `api_router.py` | `EndpointScheduler.acquire()` | api_base, concurrency_limit, new_max, semaphore blocking state |
| `api_router.py` | `release()` callback | api_base, active_count after release |
| `agent_pool.py` | `_acquire_slot()` | agent_class, instance_name, resolved api_base, concurrency_limit |
| `execution_engine.py` | `_execute_agent_sync()` | Skip reason with nest_depth, acquire/release status |

### Files Modified

1. `N:\work\WD\AgentCascade_unified\agent_cascade\execution_engine.py` — The deadlock fix
2. `N:\work\WD\AgentCascade_unified\agent_cascade\api_router.py` — Debug logging in acquire/release
3. `N:\work\WD\AgentCascade_unified\agent_cascade\agent_pool.py` — Debug logging in _acquire_slot

### Verification

- All three files pass Python AST syntax check
- Reviewer (Reviewer agent) approved on second review after addressing findings:
  - Finding #1 (🔴 concurrency accounting): Documented trade-off instead of adding complexity
  - Finding #2 (🟠 silent fallback): Added explicit error for missing _acquire_slot on root calls
  - Finding #4 (🟡 contradictory comments): Consolidated into clear comment block
  - Finding #5 (🟡 fragile hasattr): Root-level calls now return explicit error if pool misconfigured
  - Finding #6 (🔵 nit): Clarified new_max comment