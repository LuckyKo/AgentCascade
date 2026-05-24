# Comprehensive Scheduling Fix Plan

## Problem Statement

Parallel agents landing on non-concurrent (concurrency=0) API endpoints are NOT being properly serialized. Multiple agents can start simultaneously, causing model trashing via interleaved LLM calls and context switching. The Security Advisor is particularly affected because it inherits the caller's endpoint but the concurrency of that inherited endpoint is never checked.

---

## Root Causes Identified

### Issue 1: Default Endpoint Concurrency Never Read
**File**: `api_router.py`, lines 268-278

In `call_with_fallback()`, the variable `concurrency_limit` is initialized to `0`, but only read from the endpoint object when `is_default=False`. For the default endpoint (last in chain), it stays at `0` regardless of actual config. This means:
- A configured endpoint with concurrency=-1 (unlimited) will have its concurrency checked correctly
- The default fallback endpoint will ALWAYS get concurrency_limit=0 (sequential semaphore) even if the actual API is unlimited

### Issue 2: Parallel Launch Check Uses Agent-Type Priorities, Not Actual Endpoint
**File**: `agent_orchestrator.py`, lines 1153-1154

`get_concurrency_limit(agent_class)` only checks agent-specific endpoint priorities. If no custom endpoints exist for the agent class (like 'security_advisor'), it returns `-1` (unlimited). This allows parallel execution even though the underlying default endpoint may have concurrency=0.

**Flow**: Security Advisor → no custom endpoints → `get_concurrency_limit('security_advisor')` returns -1 → `is_parallel_allowed = True` → agents launch in parallel → all land on same default endpoint with concurrency=0 → model trashing.

### Issue 3: Semaphore Serializes Individual LLM Calls, Not Agent Lifecycle
**File**: `api_router.py`, lines 292-328

The semaphore protects individual `call_with_fallback()` invocations (one LLM call). An agent making multiple sequential LLM calls releases the semaphore between each. Between two LLM calls from Agent A, Agent B can acquire the semaphore and make its own LLM call. This allows interleaving across agents which causes model trashing.

**Example**:
```
Time  Agent A                    Agent B
T1    [Acquire semaphore]
T2    [LLM Call #1 - streaming]
T3    [LLM Call #1 complete, tool decision]
T4    [Release semaphore]
T5                                    [Acquire semaphore]  ← Agent B starts!
T6                                    [LLM Call #1]        ← Interleaved!
T7    [Tool execution wait...]
T8    [Acquire semaphore]             ← Agent A back in
T9    [LLM Call #2]                   ← Model context trashed!
```

---

## Plan of Action

### Phase 1: Fix `call_with_fallback` to Read Default Endpoint Concurrency (api_router.py)

**What**: When the default endpoint is reached in the fallback chain, look up its actual concurrency_limit from the configured endpoints instead of leaving it at 0.

**How**:
- After `is_default = (cfg_idx == len(chain) - 1)`, if `is_default` is True, still try to find the matching endpoint by `api_base` and read its `concurrency_limit`.
- This ensures the default endpoint's actual concurrency setting is respected.

**Lines affected**: ~268-278 in api_router.py

**Risk**: Low - just reading more data from existing config.

### Phase 2: Add Endpoint Resolution for Parallel Launch Check (agent_orchestrator.py)

**What**: The parallel launch check must resolve the ACTUAL endpoint that will be used (including default fallback), not just agent-type priorities.

**How**:
- Before checking `get_concurrency_limit(agent_class)`, we need to determine the actual API base that will be used.
- Add a new method to APIRouter: `get_effective_api_base(agent_type)` that returns the actual api_base URL of the endpoint that will be used (including default fallback).
- Then check concurrency against the REAL endpoint, not just agent-type priorities.
- If `get_concurrency_limit` returns -1 (no custom endpoints), we must STILL look up the default endpoint's concurrency.

**New APIRouter method needed**:
```python
def get_effective_concurrency(self, agent_type: str) -> int:
    """Returns the concurrency limit of the actual endpoint that will be used,
    including the default fallback. Returns -1 only if truly unlimited."""
    # First check agent-specific priorities
    with self._lock:
        for eid in self.agent_priorities.get(agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                return ep.concurrency_limit
    # Fall back to default endpoint - find it by api_base
    default_base = (self.default_llm_cfg or {}).get('api_base') or (self.default_llm_cfg or {}).get('model_server', '')
    with self._lock:
        for ep in self.endpoints.values():
            if ep.api_base == default_base:
                return ep.concurrency_limit
    # If not found in endpoints, it's truly unlimited
    return -1
```

**Lines affected**: ~1153-1164 in agent_orchestrator.py (replace `get_concurrency_limit` with `get_effective_concurrency`)

**Risk**: Low - new method that extends existing logic correctly.

### Phase 3: Implement Per-Endpoint Serialization Queue (api_router.py + agent_orchestrator.py)

**What**: Replace the per-call semaphore with a proper per-endpoint serialization queue that serializes entire agent lifecycles, not individual LLM calls.

**Design**:
Create an `EndpointScheduler` class in api_router.py:

```python
class EndpointScheduler:
    """Manages per-API-base scheduling with lifecycle-aware serialization."""
    
    def __init__(self):
        self._lock = threading.Lock()
        # api_base -> (active_count, waiting_queue)
        self._schedules: Dict[str, Tuple[int, queue.Queue]] = {}
    
    def acquire(self, api_base: str, concurrency_limit: int) -> Optional[Callable]:
        """Acquire a slot on the endpoint. Returns a cleanup callback for limited endpoints."""
        if concurrency_limit == -1:  # unlimited
            return None
        
        with self._lock:
            if api_base not in self._schedules:
                self._schedules[api_base] = (0, queue.Queue())
            active_count, wait_queue = self._schedules[api_base]
        
        if active_count >= concurrency_limit if concurrency_limit > 0 else active_count > 0:
            # Must wait - block until a slot is available
            wait_queue.get()
        
        with self._lock:
            if api_base in self._schedules:
                active, _ = self._schedules[api_base]
                self._schedules[api_base] = (active + 1, self._schedules[api_base][1])
        
        # Return cleanup callback
        def release():
            with self._lock:
                if api_base in self._schedules:
                    active, wq = self._schedules[api_base]
                    self._schedules[api_base] = (active - 1, wq)
                    if active - 1 < (concurrency_limit if concurrency_limit > 0 else 1):
                        # Signal a waiting task
                        try:
                            wq.put_nowait(None)
                        except queue.Full:
                            pass
        return release
    
    def count_active(self, api_base: str) -> int:
        """Count active tasks on an endpoint."""
        with self._lock:
            entry = self._schedules.get(api_base)
            return entry[0] if entry else 0
```

**How it integrates**:
1. In `ParallelAgentManager.submit_task()`, before submitting to the thread pool, resolve the actual api_base and concurrency_limit for the agent class.
2. Acquire a slot on the endpoint scheduler. If concurrency=0, this blocks until no other agent is using that endpoint.
3. The release callback is called when the agent task completes (in the `finally` block of `task_wrapper`).
4. For unlimited endpoints (-1), no scheduling is needed.

**Lines affected**: 
- New class added to api_router.py
- `ParallelAgentManager.submit_task()` modified in agent_orchestrator.py (~lines 275-314)
- `ParallelAgentManager.__init__()` needs to receive the EndpointScheduler reference

### Phase 4: Remove Per-Call Semaphore for Serialized Endpoints (api_router.py)

**What**: Once Phase 3 is in place, the per-call semaphore in `call_with_fallback` becomes redundant for endpoints with concurrency=0 (the agent lifecycle is already serialized). The semaphore should be kept only for endpoints with N>0 (to limit parallel API calls within a running agent's lifetime), or removed entirely if the EndpointScheduler handles all cases.

**How**: 
- Option A: Keep the per-call semaphore but only for concurrency_limit > 0 (limit parallel calls WITHIN an agent's execution window).
- Option B: Remove the per-call semaphore entirely and rely on EndpointScheduler for all concurrency control.

**Recommendation**: Option B - the EndpointScheduler handles everything cleanly. The per-call semaphore was a band-aid; the queue-based approach is the real solution. This also eliminates the interleaving problem at its source.

---

## Implementation Order & Dependencies

```
Phase 1 (Read default concurrency) ──→ Phase 2 (Fix parallel launch check)
                                            ↓
                                       Phase 3 (EndpointScheduler queue)
                                            ↓
                                       Phase 4 (Clean up old semaphore)
```

Phases 1 and 2 can be done in parallel (they're both api_router.py changes but different methods). Phase 3 depends on Phase 2 being correct. Phase 4 is a cleanup step.

---

## Testing Strategy

### Test 1: Concurrency=0 Endpoint Serialization
- Configure default endpoint with concurrency_limit=0
- Launch 3 Security Advisor agents in parallel via `parallel_launch=true`
- Expected: Only one agent runs at a time, strictly sequential

### Test 2: Concurrency=N Endpoint Limiting  
- Configure an endpoint with concurrency_limit=2
- Launch 5 agents of that type in parallel
- Expected: At most 2 running simultaneously

### Test 3: Unlimited Endpoint (No Change)
- Configure endpoint with concurrency_limit=-1
- Launch multiple agents in parallel
- Expected: All can run simultaneously (no serialization)

### Test 4: Default Endpoint Concurrency Read
- Verify that `get_effective_concurrency('security_advisor')` returns the default endpoint's actual concurrency_limit, not -1

---

## Files to Modify

1. **api_router.py** (~3 changes):
   - Fix `call_with_fallback()` to read default endpoint concurrency (Phase 1)
   - Add `get_effective_concurrency()` method (Phase 2)
   - Add `EndpointScheduler` class and integrate it (Phase 3)
   - Remove/clean up per-call semaphore logic (Phase 4)

2. **agent_orchestrator.py** (~2 changes):
   - Replace `get_concurrency_limit()` with `get_effective_concurrency()` in parallel launch check (Phase 2)
   - Modify `ParallelAgentManager` to use EndpointScheduler (Phase 3)