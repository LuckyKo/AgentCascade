# Fix Plan: Agent Dismissal Zombie Threads

## Problem Summary
`dismiss_agent` removes agents from tracking but underlying threads remain active as zombies. The uncommitted attempt to fix this (thread tracking + join) fails because:
1. `join(timeout=30)` only waits — it doesn't stop a cooperative thread blocked in non-interruptible ops
2. Async child threads (ThreadPoolExecutor workers) are never registered → no join attempted
3. Registration race: thread registers after dismissal completes
4. Signal-discard bug: `if not thread or not thread.is_alive(): discard()` removes the termination signal when thread is None

## Fix Strategy
We cannot force-kill Python threads. The solution is to make cooperative termination work reliably by:
1. Keeping the termination signal alive until the thread confirms it stopped
2. Shortening join timeout so dismissal doesn't block for 30s
3. Registering async child executor workers so they can be tracked
4. Fixing the signal-discard condition

## Changes Required

### 1. agent_pool.py: Fix signal-discard condition in dismiss_instance (CRITICAL)
**Location**: Line ~1186-1187

Current (WRONG):
```python
if not thread or not thread.is_alive():
    self.terminated_instances.discard(instance_name)
```

Fix (CORRECT):
```python
# Only discard the termination signal if we actually confirmed the thread stopped.
# If no thread was registered (async executor worker, race condition) or it's still alive,
# keep the signal so cooperative stop-checks continue to work.
if thread and not thread.is_alive():
    self.terminated_instances.discard(instance_name)
```

**Rationale**: When `thread is None`, we don't know if a zombie exists — keeping the signal is safer than discarding it.

### 2. agent_pool.py: Shorten join timeout from 30s to 2s
**Location**: Line ~1172

Current:
```python
thread.join(timeout=30.0)
```

Fix:
```python
thread.join(timeout=2.0)
```

**Rationale**: 
- The join is only a courtesy wait — it doesn't stop the thread
- Blocking dismiss_agent tool for 30s per child (cascade dismissal) is unacceptable UX
- 2s is enough for cooperative threads that are actually responsive
- If still alive after 2s, the termination signal stays active and will be caught at next stop-check

### 3. agent_pool.py: Register async executor workers with _instance_threads
**Location**: async_tools.py lines ~109-111

Current in `AsyncToolRegistry.register()`:
```python
future = self._executor.submit(self._execute, entry)
entry.future = future
```

Fix: Wrap the child agent execution to register its thread. We need to modify how async children are tracked. The cleanest approach is to track via Future cancellation + ensure `terminated_instances` signal persists.

**Implementation approach**: 
- In `agent_pool.py:register_async_call()`, after registering with async_registry, also add the child_instance_name to `_instance_threads` mapping using a sentinel that points to the Future
- Modify `dismiss_instance()` to handle this case: if entry is a Future (not Thread), attempt cancellation and don't join
- Keep termination signal for async children since executor threads can't be cleanly terminated

Actually, simpler approach: Don't try to register executor threads. Instead:
- In `AsyncToolRegistry._execute()`, add a stop-check BEFORE running the tool_call that checks `pool.is_instance_terminated(child_instance_name)` if it's an async child agent
- Ensure termination signal is never discarded for async children (covered by fix #1)

**Action**: Add pre-execution termination check in async_tools.py:130-137 area — already exists for the parent instance, extend to also check child_instance_name.

### 4. agent_pool.py: Handle race condition — don't discard signal if thread not found
Already covered by fix #1. When `thread is None` (race or async), we now keep the signal.

### 5. run_agent_unified.py: Register thread BEFORE creating instance (prevent race)
**Location**: Lines ~104-119

Current order: create_instance → register_thread
Fix: register_thread → create_instance

Actually, this is tricky because registration needs the instance to exist for validation. Better approach: move registration to happen atomically with instance creation inside `create_main_agent_instance()`, or register unconditionally (the thread knows its own name).

**Simplest fix**: Register the thread immediately after we know the instance_name, without checking if instance exists yet. The cleanup in finally block handles removal on exit.

### 6. Add integration test for real-thread dismissal
Create a minimal test that:
- Spawns a real agent execution thread doing an interruptible long operation
- Calls dismiss_instance
- Asserts the thread terminates within a reasonable bound (e.g., 5s)

## Priority Order
1. **Fix #1** (signal-discard condition) — 1 line, highest impact, zero risk
2. **Fix #2** (shorten timeout) — 1 line, prevents 30s blocking
3. **Fix #5** (race prevention) — small change, closes registration race window
4. **Fix #3** (async child check) — extends existing pattern, covers most common dismissal path
5. **Fix #6** (integration test) — ensures we catch regressions

## Files Modified
- `agent_cascade/agent_pool.py` — fixes #1, #2
- `agent_cascade/run_agent_unified.py` — fix #5
- `agent_cascade/async_tools.py` — fix #3
- `tests/test_dismiss_real_thread.py` — fix #6 (new file)

## What This Does NOT Fix (Known Limitations)
- A thread blocked mid-LLM HTTP call with no stop-check will still zombie until that call completes or times out naturally. This is inherent to cooperative termination in Python. The mitigation is keeping the termination signal alive so subsequent operations abort.
- Sync child agents running inline in parent's thread cannot be independently terminated — documented limitation.
