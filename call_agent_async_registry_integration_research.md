# call_agent → AsyncToolRegistry Integration Research

**Date:** 2026-06-12  
**Researcher:** AsyncRegistryIntegrationResearcher  
**Status:** Complete — All findings documented below.

---

## Executive Summary

The `call_agent` tool currently uses a **custom parallel execution path** (`submit_parallel` → `submit_task` → `task_wrapper`) that duplicates infrastructure provided by the existing `AsyncToolRegistry`. The AsyncToolRegistry is already fully wired up in `AgentPool.__init__()` and used for `has_pending()` checks, but its `register_async_call()` method creates a **dummy callable** — indicating it was set up as a placeholder while the real work went through the custom path.

**Key Finding:** The entire `submit_parallel` / `submit_task` / `task_wrapper` chain can be replaced by wrapping all necessary logic into a single callable and passing it to `AsyncToolRegistry.register()`. This eliminates ~150 lines of code in agent_pool.py, removes a separate ThreadPoolExecutor for parallel tasks, and unifies all async tool execution under one mechanism.

---

## 1. What Does `submit_task` / `task_wrapper` Do That AsyncToolRegistry Doesn't?

### Current `task_wrapper()` Operations (agent_pool.py lines 1611-1739):

| # | Operation | Description |
|---|-----------|-------------|
| 1 | **Deep copy history** | `_copy.deepcopy(history)` — thread safety for the child agent's conversation |
| 2 | **Acquire endpoint slot** | `self._acquire_slot(agent_class, instance_name)` — calls `router.scheduler.acquire(api_base, concurrency_limit)` which blocks at a semaphore until capacity is available |
| 3 | **Create ExecutionEngine** | `engine = ExecutionEngine(self.pool)` — creates a NEW engine instance per parallel task (intentional to avoid shared state) |
| 4 | **Run child agent** | `engine._create_and_run_agent(agent_class, instance_name, args, caller, nest_depth)` — this is the core agent execution logic |
| 5 | **Extract result** | `extract_instance_output(conv, instance_name)` — extracts text output after the last tool call from the conversation |
| 6 | **Format completion message** | `f"[Parallel Agent '{instance_name}' Finished]:\n{result}"` |
| 7 | **Put result in buffer** | `pool.add_async_result(caller, completion_msg, function_id=function_id)` — thread-safe put into `_async_results` |
| 8 | **Complete async call** | `pool.complete_async_call(caller, call_id)` — marks the BackgroundToolEntry as completed (currently a no-op since _execute already does this) |
| 9 | **Error handling** | Nested try/except blocks with three tiers: slot acquisition failure, execution failure, and general failure — each calls `_notify_async_error()` which puts error in buffer + completes async call |
| 10 | **Release endpoint slot** | `endpoint_release()` in finally block — releases the semaphore acquired in step 2 |
| 11 | **Mark activity** | `pool._mark_activity(instance_name)` — updates last_activity timestamp (line 1738) |

### Current AsyncToolRegistry `_execute()` Operations (async_tools.py lines 102-130):

| # | Operation | Description |
|---|-----------|-------------|
| 1 | **Call the tool_call** | `entry.result = entry.tool_call()` — executes whatever callable was registered |
| 2 | **Error capture** | Catches any exception, stores as `entry.error = str(e)` |
| 3 | **Mark completed** | Sets `entry.completed = True` under lock |
| 4 | **Put result in buffer** | `pool._async_results.put(agent_instance_name, result_msg, function_id=entry.function_id)` — puts formatted message into the async result buffer |

### Gap Analysis

The AsyncToolRegistry's `_execute()` is a thin wrapper around calling `tool_call()`. Everything the custom path does (endpoint slot acquisition, ExecutionEngine creation, result extraction, etc.) happens **inside** the callable. This means:

- ✅ **All operations can be wrapped in a callable** — task_wrapper's logic becomes the body of a lambda/closure
- ✅ **The AsyncToolRegistry already handles error capture and marking completed**
- ✅ **The AsyncToolRegistry already puts results into AsyncResultBuffer with function_id**
- ❌ **`complete_async_call()` is still called from task_wrapper** — but it's currently a no-op (pass), so removing it has no effect

---

## 2. Can We Wrap All of That Into a Callable for `AsyncToolRegistry.register()`?

### Yes — Here's the Proposed Structure:

```python
def register_async_call(self, instance_name: str, call_id: str, function_id: Optional[str] = None, 
                        agent_class: str = None, target_instance_name: str = None,
                        args: dict = None, history: List[Message] = None, 
                        caller: str = None, nest_depth: int = 0):
    """Register a call_agent background task via AsyncToolRegistry."""
    
    def run_child():
        # 1. Deep copy history
        safe_history = _copy.deepcopy(history)
        
        # 2. Acquire endpoint slot (inside worker thread — same as current behavior)
        endpoint_release = self._execution._acquire_slot(agent_class, target_instance_name)
        
        try:
            # 3. Create ExecutionEngine and run child agent
            from agent_cascade.execution_engine import ExecutionEngine
            from agent_cascade.compression.helpers import extract_instance_output
            
            engine = ExecutionEngine(self.pool)
            inst, conv = engine._create_and_run_agent(
                agent_class, target_instance_name, args, caller, nest_depth
            )
            
            if inst is None or conv is None:
                raise ValueError(f"Agent creation returned None for {target_instance_name}")
            if not conv:
                raise ValueError(f"Empty conversation for {target_instance_name}")
            
            # 4. Extract result
            result = extract_instance_output(conv, target_instance_name)
            completion_msg = f"[Parallel Agent '{target_instance_name}' Finished]:\n{result}"
            
            return completion_msg
            
        except Exception as e:
            error_msg = f"[Parallel Agent '{target_instance_name}' Failed]:\n{str(e)}"
            raise RuntimeError(error_msg)
        finally:
            # 5. Release endpoint slot
            if endpoint_release is not None:
                try:
                    endpoint_release()
                except Exception:
                    pass
            
            self.pool._mark_activity(target_instance_name)
    
    # Register with AsyncToolRegistry — it handles execution, error capture, and result buffering
    entry = self._async_registry.register(
        instance_name=instance_name,
        tool_call=run_child,
        function_id=function_id
    )
    
    # Note: call_id is no longer needed for tracking (AsyncToolRegistry uses BackgroundToolEntry.completed)
    # but we could store it in entry.call_id if needed for debugging
```

### What Changes:

| Current | After Integration |
|---------|-------------------|
| `register_async_call()` creates a dummy callable + submits real work via `submit_parallel` → `submit_task` → `task_wrapper` | `register_async_call()` creates a **real** callable with all task_wrapper logic and passes it to `AsyncToolRegistry.register()` |
| Two separate ThreadPoolExecutors (AsyncToolRegistry: 4 workers, ParallelAgentManager: pool.max_workers) | One ThreadPoolExecutor (AsyncToolRegistry's 4 workers handles everything) |
| `complete_async_call()` called from task_wrapper as no-op | Removed entirely — AsyncToolRegistry._execute already marks completed |

---

## 3. Endpoint Slot Acquisition — Thread Pool Differences

### Current Setup:

| Component | ThreadPoolExecutor Config | max_workers |
|-----------|--------------------------|-------------|
| **AsyncToolRegistry** | `ThreadPoolExecutor(max_workers=4, thread_name_prefix="async_tool")` (async_tools.py line 72-75) | 4 |
| **ParallelAgentManager** | `ThreadPoolExecutor(max_workers=pool.settings.max_workers)` (agent_pool.py line 1465) | pool.default = 10 |

### Key Insight: The Slot Acquisition is the Same Either Way

`_acquire_slot()` calls `router.scheduler.acquire(api_base, concurrency_limit)` which is a **semaphore-based** acquisition. It blocks until capacity is available. The thread pool that runs it doesn't matter functionally — what matters is:

1. **It happens in a worker thread (not the parent's main thread)** — ✅ Both pools satisfy this
2. **Multiple concurrent calls can wait for slots without blocking each other** — ✅ 4 workers vs 10 workers both allow concurrency; the limiting factor is the endpoint scheduler, not the thread pool

### Recommendation:

- The AsyncToolRegistry's 4 workers should be sufficient. All call_agent tasks go through this single pool.
- If concurrent parallel agents exceed 4, we may want to increase `max_workers` from 4 to a configurable value (e.g., `min(4, pool.settings.max_workers)` or just use `pool.settings.max_workers`).
- **Important:** The ParallelAgentManager's executor would be eliminated entirely, freeing those resources.

---

## 4. Changes Needed for BackgroundToolEntry and AsyncResultBuffer

### Current State (Already Correct):

**BackgroundToolEntry** (async_tools.py lines 20-44):
```python
@dataclass
class BackgroundToolEntry:
    tool_call: Callable[[], str]        # ✅ Already exists
    agent_instance_name: str            # ✅ Already exists
    timeout: float = 30.0               # ✅ Already exists
    start_time: float = ...             # ✅ Already exists
    result: Optional[str] = None        # ✅ Already exists
    error: Optional[str] = None         # ✅ Already exists
    completed: bool = False             # ✅ Already exists
    function_id: Optional[str] = None   # ✅ Already exists
```

**AsyncResultBuffer** (async_tools.py lines 163-206):
```python
def put(self, instance_name: str, result: str, function_id: Optional[str] = None):
    # ✅ Stores tuples: (result, function_id)
    self._results.setdefault(instance_name, []).append((result, function_id))

def drain(self, instance_name: str) -> List[tuple]:
    # ✅ Returns List[tuple]
    return self._results.pop(instance_name, [])
```

### What's Already Done (No Changes Needed):

- `function_id` field exists in BackgroundToolEntry ✅
- `AsyncResultBuffer.put()` accepts `function_id` parameter ✅
- `AsyncResultBuffer` stores tuples `(result, function_id)` ✅
- `AsyncResultBuffer.drain()` returns `List[tuple]` ✅

### Minor Fix Needed: Type Annotation Mismatch

In agent_pool.py line 1204:
```python
def drain_async_results(self, instance_name: str) -> List[str]:
```
Should be:
```python
def drain_async_results(self, instance_name: str) -> List[tuple]:
```

---

## 5. What Happens to `submit_task` / `submit_parallel`?

### Call Sites Analysis:

| File | Line | Usage |
|------|------|-------|
| execution_engine.py | 1945 | `self.pool.submit_parallel(...)` — the ONLY caller of submit_parallel |
| agent_pool.py | 1410 | `submit_parallel` delegates to `submit_task` |
| api_router.py | 664 | Comment only, not actual code |

### Verdict: **Both can be removed.**

- `submit_parallel()` is called only from `_handle_call_agent()` in execution_engine.py
- `submit_task()` is called only from `submit_parallel()`
- After integration, `_handle_call_agent()` will call `pool.register_async_call()` with a real callable instead of delegating to `submit_parallel`

### What Else to Clean Up:

1. **Remove** `ParallelAgentManager` class (agent_pool.py lines 1450-1748) — including its executor, `_acquire_slot`, `_notify_async_error`, `has_active_tasks`, `count_by_class`, `resize_executor`
2. **Remove** `submit_parallel()` method from AgentPool (lines 1396-1412)
3. **Remove** `submit_task()` method from ParallelAgentManager (lines 1585-1747)
4. **Remove** `_notify_async_error()` helper from ParallelAgentManager (lines 1516-1557) — its logic is now in the callable itself
5. **Keep** `_acquire_slot()` — move it to a shared location or keep it on ParallelAgentManager for use by the integrated callable (or move to AgentPool._execution)
6. **Remove** `complete_async_call()` from AgentPool (lines 1256-1269) — it's a no-op and AsyncToolRegistry handles completion
7. **Clean up** `_async_pending_calls` dict in AgentPool — it's already marked as deprecated and unused

---

## 6. Complete Flow After Integration

### Step-by-Step Traced Flow:

```
1. LLM generates tool_use for call_agent
   └─ tool_call_id = "call_abc123" (function_id)

2. Engine calls _execute_tool → _handle_call_agent(function_id="call_abc123")
   └─ Validates args (instance_name, agent_class)
   └─ Handles recursive self-call cloning
   └─ Checks class mismatch
   └─ Enforces nesting depth limits
   └─ Generates call_id = f"{instance_name}_{time.monotonic()}"

3. _handle_call_agent creates a REAL callable (not dummy):
   def run_child():
       safe_history = _copy.deepcopy(history)
       endpoint_release = self._execution._acquire_slot(agent_class, instance_name)
       try:
           engine = ExecutionEngine(self.pool)
           inst, conv = engine._create_and_run_agent(...)
           result = extract_instance_output(conv, instance_name)
           return f"[Parallel Agent '{instance_name}' Finished]:\n{result}"
       finally:
           if endpoint_release: endpoint_release()
           self.pool._mark_activity(instance_name)

4. Calls AsyncToolRegistry.register(caller_name, run_child, function_id="call_abc123")
   └─ Creates BackgroundToolEntry with tool_call=run_child, function_id="call_abc123"
   └─ Adds entry to _pending[caller_name] list
   └─ Submits self._execute(entry) to ThreadPoolExecutor (4 workers)

5. AsyncToolRegistry worker thread runs _execute(entry):
   └─ Calls entry.tool_call() → run_child() executes:
       a. Acquires endpoint slot (semaphore — may block if at capacity)
       b. Creates ExecutionEngine(self.pool)
       c. Runs engine._create_and_run_agent(...) 
          - Creates/reuses AgentInstance
          - Builds system + task messages
          - Propagates settings from caller
          - Adds to active_stack
          - Runs engine.run(inst) through unified loop
          - Handles WebSocket stream updates
          - Cleans up active_stack
       d. Extracts result via extract_instance_output()
       e. Returns formatted completion message
   
6. _execute captures the return value: entry.result = run_child()

7. Marks entry.completed = True (under lock)

8. Puts result into AsyncResultBuffer:
   pool._async_results.put(caller_name, 
                           f"[Background Tool Result]:\n{result}", 
                           function_id="call_abc123")
   → Stored as tuple: (f"[Background Tool Result]:\n{result}", "call_abc123")

9. Parent agent finishes its turn → goes to SLEEPING at end-of-turn (_post_turn_checks)

10. On wakeup in engine.run():
    a. Checks instance.state == SLEEPING
    b. Calls pool.drain_async_results(inst_name)
       → Returns [(f"[Background Tool Result]:\n{result}", "call_abc123"), ...]
    
11. Unpacks tuples and formats messages:
    for result_content, function_id in async_results:
        if function_id:
            prefix = f"[BACKGROUND TOOL RESULT for {function_id}]"
        else:
            prefix = "[BACKGROUND TOOL RESULT]"
        result_msg = Message(role=USER, content=f"{prefix}: {result_content}")
    
12. Injects messages into conversation → transitions to RUNNING

13. LLM sees the background tool result and continues processing
```

---

## 7. Files That Need Modification

| File | Changes |
|------|---------|
| **agent_pool.py** | - Rewrite `register_async_call()` to create a real callable with full task_wrapper logic<br>- Remove `submit_parallel()` method<br>- Remove `complete_async_call()` method (or keep as no-op for compatibility)<br>- Remove `drain_async_results` return type annotation fix: `List[str]` → `List[tuple]`<br>- Clean up `_async_pending_calls` dict (deprecated, unused)<br>- Remove ParallelAgentManager class entirely |
| **execution_engine.py** | - Modify `_handle_call_agent()` to pass additional args to `register_async_call()` (agent_class, instance_name, args, history, caller, nest_depth) instead of calling `submit_parallel()`<br- Remove the direct call to `self.pool.submit_parallel(...)` and replace with `self.pool.register_async_call(...)` |
| **async_tools.py** | No changes needed — already has all required fields and methods |

---

## 8. Risk Assessment

| Risk | Severity | Mitigation |
|------|----------|------------|
| ThreadPoolExecutor capacity (4 workers vs current 10) | Low | Monitor; increase max_workers if needed during testing |
| Lock ordering changes | Medium | AsyncToolRegistry acquires `_lock` then calls `put()` outside lock — same pattern as current code |
| Loss of ParallelAgentManager features (resize_executor, count_by_class) | Low | These are unused in the call_agent path; verify no other callers |
| Deep copy overhead for history | None | Same as current implementation — already done in task_wrapper |
| WebSocket stream update timing changes | Low | Stream updates happen inside _create_and_run_agent which is unchanged |

---

## 9. Summary of Key Findings

1. **The AsyncToolRegistry infrastructure is complete and ready** — BackgroundToolEntry has `function_id`, AsyncResultBuffer stores tuples with function_id, and the `_execute()` method already handles result buffering with function_id.

2. **`register_async_call()` creates a dummy callable** — This confirms it was designed as a placeholder while the real work went through the custom submit_parallel path.

3. **All task_wrapper logic can be wrapped in a single callable** — The callable signature `Callable[[], str]` is sufficient because all state (history, agent_class, instance_name, etc.) is captured via closure.

4. **Endpoint slot acquisition works in any thread pool** — It's semaphore-based and doesn't depend on which ThreadPoolExecutor runs it.

5. **submit_parallel/submit_task are only called from _handle_call_agent** — They can be safely removed after integration.

6. **No changes needed to async_tools.py** — The type annotations, data structures, and methods are already correct for this integration.

7. **The drain_async_results return type annotation should be fixed** from `List[str]` to `List[tuple]` to match actual behavior.

---

## Appendix A: Code Diff Summary (What Gets Removed)

### agent_pool.py — Methods to Remove (~350 lines):
- `submit_parallel()` (lines 1396-1412) — 17 lines
- `ParallelAgentManager` class (lines 1450-1748) — ~300 lines  
- `complete_async_call()` (lines 1256-1269) — 14 lines

### agent_pool.py — Methods to Rewrite:
- `register_async_call()` (lines 1238-1254) — ~17 lines → ~50 lines (real callable)
- `drain_async_results()` return type annotation fix — 1 line

### execution_engine.py — Method to Modify:
- `_handle_call_agent()` (lines 1941-1953) — change from calling `register_async_call` + `submit_parallel` to just `register_async_call` with full args