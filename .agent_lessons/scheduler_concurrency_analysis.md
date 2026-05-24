# Scheduler Concurrency Analysis - Bug Investigation Report

## Executive Summary

The AgentCascade system has a concurrency handling bug where parallel agents are not properly serialized when landing on non-concurrent (concurrency=0) API endpoints. This is particularly affecting the Security Advisor agent, causing model trashing due to interleaved LLM calls.

**Root Cause**: A combination of three issues:
1. The default API endpoint's concurrency limit is never read in `call_with_fallback`
2. Parallel launch checks use agent-type priorities (which may be empty) instead of the actual endpoint being used
3. The semaphore only serializes individual LLM calls, not the full agent lifecycle

---

## 1. API Endpoint Concurrency Handling

### File: `api_router.py`

#### Data Model (Lines 27-66)
```python
@dataclass
class APIEndpoint:
    concurrency_limit: int = -1  # -1 = unlimited, 0 = sequential, 1+ = max parallel
```

**Key observations:**
- `concurrency_limit=-1` means unlimited (default when no specific config is set)
- `concurrency_limit=0` means fully sequential/serialized
- `concurrency_limit=N` (N>0) means at most N parallel requests

#### Concurrency Resolution (Lines 172-182)
```python
def get_concurrency_limit(self, agent_type: str) -> int:
    """Returns the concurrency_limit for the highest-priority enabled endpoint 
    of the given agent type. Returns -1 if unlimited (default)."""
    with self._lock:
        for eid in self.agent_priorities.get(agent_type, []):
            ep = self.endpoints.get(eid)
            if ep and ep.enabled:
                return ep.concurrency_limit
    return -1  # Default fallback is unlimited
```

**Critical finding**: If no custom endpoint priorities exist for an agent type, this returns `-1` (unlimited), regardless of what the default endpoint's actual concurrency limit is.

#### Semaphore Management (Lines 91-93)
```python
# Per-server semaphores for concurrency control: api_base -> (Semaphore, limit)
self._semaphores: Dict[str, Tuple[threading.Semaphore, int]] = {}
self._sem_lock = threading.Lock()
```

Semaphores are managed **per `api_base`** (server), not per agent type.

---

## 2. Parallel Agent Scheduling

### File: `agent_orchestrator.py`

#### ParallelAgentManager (Lines 256-314)
```python
class ParallelAgentManager:
    def __init__(self, agent_pool: AgentPool, max_workers: int = 10):
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        self.active_tasks = {}  # instance_name -> (Future, owner_session, agent_class)
```

Uses a standard `ThreadPoolExecutor` with no endpoint-aware queuing.

#### Concurrency Check in `_run` (Lines 1148-1175)
```python
if isinstance(parsed_args, dict) and parsed_args.get('parallel_launch') is True:
    # ── Check Concurrency Limits ──
    agent_class = (parsed_args.get('agent_class') or '').strip().lower()
    
    if agent_class and hasattr(self.agent_pool, 'api_router'):
        limit = self.agent_pool.api_router.get_concurrency_limit(agent_class)
        if limit == 0:
            is_parallel_allowed = False
        elif limit > 0:
            active_count = self.agent_pool.parallel_manager.count_active_tasks_by_class(agent_class)
            if active_count >= limit:
                is_parallel_allowed = False
    
    if is_parallel_allowed:
        # Submit to thread pool
        tool_result = self.agent_pool.parallel_manager.submit_task(...)
    else:
        # Fallback to sequential
        tool_result = yield from self._stream_sub_agent_call(...)
```

**Problem**: This check uses `get_concurrency_limit(agent_class)` which only looks at agent-specific endpoint priorities. If no custom endpoints exist for the agent class, it returns -1 (unlimited), allowing parallel execution even when the underlying default endpoint should serialize requests.

---

## 3. Security Advisor Endpoint Inheritance

### File: `agent_factory.py` (Lines 169-227)
```python
def load_agent(agent_pool, agent_name: str, llm_cfg: dict = None) -> Assistant:
    if hasattr(agent_pool, 'api_router'):
        agent_llm_cfg = agent_pool.api_router.get_llm_config(agent_name)
    else:
        agent_llm_cfg = llm_cfg or agent_pool.llm_cfg
```

### File: `api_router.py` (Lines 186-195)
```python
def get_llm_config(self, agent_type: str) -> dict:
    chain = self.get_endpoint_chain(agent_type)
    if chain:
        return chain[0]
    return copy.deepcopy(self.default_llm_cfg)
```

**How it works:**
1. `load_agent` calls `api_router.get_llm_config('security_advisor')`
2. `get_llm_config` checks if 'security_advisor' has custom endpoint priorities
3. If no priorities exist, returns the **default LLM config** (`default_llm_cfg`)
4. The Security Advisor then uses this default config (same as any other agent without custom endpoints)

### File: `agents/security_advisor_soul.md`
No endpoint configuration in the soul file. Security Advisor relies entirely on the APIRouter for endpoint assignment.

---

## 4. The Core Problem - Three Interconnected Issues

### Issue A: Default Endpoint Concurrency Not Read in `call_with_fallback`

**File**: `api_router.py`, Lines 266-278
```python
for cfg_idx, llm_cfg in enumerate(chain):
    max_retries = 2
    concurrency_limit = 0       # <-- Always initialized to 0
    is_default = (cfg_idx == len(chain) - 1)
    
    endpoint_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')
    if not is_default:          # <-- Only reads concurrency for NON-default endpoints
        for ep in self.endpoints.values():
            if ep.api_base == endpoint_base:
                max_retries = ep.max_retries
                concurrency_limit = ep.concurrency_limit  # <-- Never read for default!
```

**Impact**: For the default endpoint (the last item in the chain), `concurrency_limit` **always stays at 0**, regardless of what the actual endpoint configuration says. This creates a semaphore of size 1 for all agents using the default endpoint.

### Issue B: Parallel Launch Check Doesn't Account for Default Endpoint Concurrency

**File**: `agent_orchestrator.py`, Lines 1153-1154
```python
if agent_class and hasattr(self.agent_pool, 'api_router'):
    limit = self.agent_pool.api_router.get_concurrency_limit(agent_class)
```

When 'security_advisor' has no custom endpoints:
- `get_concurrency_limit('security_advisor')` returns `-1` (unlimited)
- `is_parallel_allowed = True` → agents launched in parallel
- But they all share the same default endpoint, which may have concurrency=0

### Issue C: Semaphore Serializes Individual LLM Calls, Not Agent Lifecycle

**File**: `api_router.py`, Lines 292-328
```python
def execute_with_sem(current_agent_name=None):
    sem.acquire()
    try:
        result = call_fn(*args, **kwargs)  # <-- One LLM call
        if hasattr(result, '__iter__') and not isinstance(result, (list, dict, str)):
            def sem_generator_wrapper(gen):
                try:
                    yield from gen              # <-- Waits for generator exhaustion
                finally:
                    sem.release()             # <-- Releases after THIS LLM call's stream
            return sem_generator_wrapper(result)
        else:
            sem.release()                   # <-- Releases immediately
```

**The problem**: When an agent makes multiple sequential LLM calls (e.g., in its run loop), each one acquires and releases the semaphore independently. Between two LLM calls from Agent A, a parallel Agent B can acquire the same semaphore and make its own LLM call. This allows **interleaving of LLM calls across agents**, which causes model trashing.

**Example interleaving scenario:**
```
Time  Agent A                    Agent B
----  -------                    -------
T1    [Acquire semaphore]
T2    [LLM Call #1 - streaming]
T3    [LLM Call #1 - complete, tool decision]
T4    [Release semaphore]          ← Agent A releases after turn completes
T5                                          [Acquire semaphore]  ← Agent B can now start
T6                                          [LLM Call #1 - streaming]
T7    [Tool execution wait...]
T8    [Wait for tool result]
T9                                          [LLM Call #1 - complete]
T10   [Acquire semaphore]           ← Agent A acquires again for next turn
T11   [LLM Call #2 - streaming]     ← INTERLEAVED with Agent B!
```

For concurrency=0, the requirement is **strict serialization**: Agent A must complete ALL its work (all LLM calls, all tool waits) before Agent B can start ANY of its work. The current semaphore-based approach only serializes individual API calls.

---

## 5. What's Missing: Per-Endpoint Serialization Queue

### Requirement
For `concurrency=0` endpoints:
- Agents must be **strictly serialized** from task submission to completion
- One agent at a time, waiting for full completion including streaming AND tool call waits
- No interleaving of any kind between agents on the same endpoint

### What Currently Exists
1. **API-level semaphore** (`api_router.py`): Serializes individual LLM API calls per `api_base`
2. **Agent-type concurrency check** (`agent_orchestrator.py`): Checks if parallel is allowed based on agent type priorities
3. **ThreadPoolExecutor**: No endpoint awareness, unlimited workers

### What's Missing
1. **No per-endpoint task queue**: There's no queue that serializes entire agent lifecycles per endpoint
2. **No endpoint-level lock at submission time**: `submit_task` doesn't check if the target endpoint requires serialization
3. **No lifecycle-aware serialization**: The semaphore is released after each LLM call, not after the full agent execution

---

## 6. Summary Table

| Component | File | Lines | Issue |
|-----------|------|-------|-------|
| `get_concurrency_limit()` | `api_router.py` | 172-182 | Returns -1 for agents without custom endpoints, ignoring default endpoint settings |
| `_run` parallel check | `agent_orchestrator.py` | 1148-1175 | Uses agent-type priority (may be empty) instead of actual endpoint concurrency |
| `call_with_fallback` | `api_router.py` | 266-278 | Never reads concurrency_limit from the default endpoint |
| Semaphore wrapping | `api_router.py` | 292-328 | Serializes individual LLM calls, not agent lifecycle |
| `ParallelAgentManager.submit_task()` | `agent_orchestrator.py` | 275-314 | No endpoint awareness when submitting to thread pool |

---

## 7. Recommended Fixes

### Fix 1: Read Default Endpoint Concurrency in `call_with_fallback` (api_router.py)
At line 268-278, also read concurrency from the default endpoint when `is_default=True`:
```python
if is_default and self.default_llm_cfg:
    # Try to find if the default config corresponds to a configured endpoint
    default_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', '')
    for ep in self.endpoints.values():
        if ep.api_base == default_base:
            concurrency_limit = ep.concurrency_limit
            max_retries = ep.max_retries
            break
```

### Fix 2: Endpoint-Level Serialization at Task Submission (agent_orchestrator.py)
In `ParallelAgentManager.submit_task()`, before submitting to the thread pool, acquire a per-endpoint lock if the endpoint requires serialization:
```python
# Before submit_task creates the future:
if concurrency_limit == 0:
    endpoint_lock = self._get_endpoint_lock(api_base)
    endpoint_lock.acquire()  # Blocks until previous agent finishes

def task_wrapper():
    try:
        ...execute...
    finally:
        if concurrency_limit == 0:
            endpoint_lock.release()
```

### Fix 3: Per-Endpoint Serialization Queue
Implement a proper queue per `api_base` that respects concurrency settings:
- For `concurrency=0`: Strict FIFO queue, one agent at a time (full lifecycle)
- For `concurrency=N`: Queue with N parallel slots
- For `concurrency=-1`: No queue needed (unlimited)

---

## 8. Verification Steps

To verify the bug:
1. Configure any endpoint with `concurrency_limit=0`
2. Have the orchestrator call Security Advisor twice with `parallel_launch=true`
3. Observe that both agents start their LLM calls simultaneously (model trashing)
4. Check logs for interleaved streaming output from both agents

To verify fixes:
1. Same setup as above
2. Only one agent should stream at a time
3. The second agent waits until the first completes ALL work (including tool calls)