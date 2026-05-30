# API Endpoint Assignment System — Audit Report

**Date:** 2026-05-30  
**Auditor:** ApiEndpointAudit  
**Branch:** `unified` (N:\work\WD\AgentCascade_unified\)  
**Scope:** All agent LLM calls, API routing, concurrency enforcement, token limits  

---

## Executive Summary

The API endpoint assignment system has **multiple critical gaps** where special agents (compression_agent and security_advisor) bypass the API router entirely. Additionally, there is a collision risk in sub-agent instance naming when the same instance name is reused across different sessions/callers, and synchronous calls bypass lifecycle-level concurrency enforcement. The core routing logic for agents going through ExecutionEngine is sound, but two execution paths circumvent it.

**Severity Breakdown:**
- 🔴 Critical: 5 findings (compression agent bypasses router, security advisor bypasses router, no concurrency check on compression/security, no retry/fallback for compression and security, race condition in compression state)
- 🟠 Major: 2 findings (sync path bypasses lifecycle slot acquisition, instance name reuse silently overwrites)
- 🟡 Moderate: 3 findings (count_by_class per-class semantics, sync calls not concurrency-limited at task level, default fallback concurrency=0 edge case)
- ✅ Positive: Multiple areas working correctly

---

## Finding 1 🔴: Compression Agent Bypasses API Router Entirely

**Severity:** Critical  
**Files:** `compression/agent_invoker.py:143`, `agent_cascade/agent.py:181`  

### What Happens
When forced compression triggers (at >95% context usage) or an agent calls the `compress_context` tool, the flow is:

```
ExecutionEngine._force_compression()  [execution_engine.py:367]
    → compress_context()              [compression/core.py:15]
        → invoke_compression_agent()  [compression/agent_invoker.py:62]
            → comp_agent.run()        [line 143]
                → Agent._run()        [agent_cascade/agents/fncall_agent.py:73]
                    → self._call_llm() [agent_cascade/agent.py:161]
                        → self.llm.chat() [line 181]  ← DIRECT LLM CALL, NO ROUTER
```

At `agent_invoker.py:143`, the compression agent is invoked via `comp_agent.run(comp_history, ...)`. This calls through to the base `Agent._call_llm()` at line 181 of `agent.py`, which does a **direct `self.llm.chat()` call** — no API router involvement.

### Impact
- The compression agent uses whatever LLM config was set when it was loaded (via `load_agent('compression_agent')` → `api_router.get_llm_config('compression_agent')`)
- If no specific endpoint is configured for `compression_agent`, it falls back to the default LLM config
- **No fallback chain** — if the selected endpoint fails, there's no retry on alternative endpoints. The entire compression operation fails and blocks the caller's execution thread (single point of failure).
- **No concurrency enforcement** — compression agent calls are not counted against any semaphore
- **No token limit enforcement** — the compression agent doesn't go through `_get_max_tokens()` which checks the router

### Reproduction
1. Configure a specific endpoint for `compression_agent` with `concurrency_limit=1`
2. Trigger two forced compressions simultaneously (e.g., from two parallel agents)
3. Both will fire LLM calls concurrently, ignoring the concurrency limit

---

## Finding 2 🔴: Security Advisor Bypasses API Router Entirely

**Severity:** Critical  
**Files:** `api_server.py:1949`  

### What Happens
When a tool requires security approval, the flow is:

```
api_server.py _security_check()          [line 1882]
    → agent_pool.load_agent('security_advisor')  [line 1890-1891]
    → sec_agent = agent_pool.get_agent(...)      [line 1892]
    → sec_agent.run(history, ...)                [line 1949]  ← DIRECT LLM CALL, NO ROUTER
```

At `api_server.py:1949`, the security advisor is invoked via `sec_agent.run(history, agent_instance_name='security_advisor', **llm_safe_cfg)`. Same as compression — it goes directly to `self.llm.chat()`, bypassing the router.

### Impact
- Same issues as Finding 1: no fallback chain, no concurrency enforcement, no token limit enforcement
- Additionally, at line 1929-1938, endpoint-identifying keys ARE excluded from `llm_safe_cfg` (good), but the LLM config used is still whatever was set at load time — with no per-call router mediation
- **Higher severity than compression** because the security advisor blocks tool execution entirely. If its endpoint is unavailable, tools requiring approval cannot execute, causing cascading timeouts and stuck agents

### Note on Positive Design
At `api_server.py:1935`, the code correctly excludes `'model', 'model_server', 'api_base', 'base_url', 'api_key', 'model_type'` from the UI config passed to the security advisor. This means the security advisor uses its own assigned endpoint, not one overridden by UI settings. However, this only works if `get_llm_config('security_advisor')` was called during load — which it is at `agent_factory.py:189`.

---

## Finding 3 🔴: No Concurrency Enforcement for Compression/Security Agents

**Severity:** Critical (derivative of Findings 1 and 2)  
**Files:** `compression/agent_invoker.py`, `api_server.py:1949`  

### What Happens
The `EndpointScheduler` in `api_router.py:70-204` operates at the **task lifecycle level** — it acquires a slot when a task is submitted and releases when complete. But compression and security agents are NOT submitted as tasks through `_acquire_slot()` / `submit_task()`. They're called directly from within other agents' execution loops.

### Impact
- If `compression_agent` is assigned to an endpoint with `concurrency_limit=1`, forced compression can still run concurrently with the main agent on that same endpoint
- If `security_advisor` shares an endpoint with the orchestrator, security checks can happen simultaneously with orchestrator LLM calls, exceeding concurrency limits

### Root Cause
The EndpointScheduler acquires slots at task submission time (`_acquire_slot()` in `agent_pool.py:1163`). Neither compression nor security agents go through this path.

---

## Finding 4 🔴: Compression and Security Agents Have No Retry/Fallback Path (Single Point of Failure)

**Severity:** Critical  
**Files:** `compression/agent_invoker.py:122-192`, `compression/core.py:195-211`, `api_server.py:1882-1996`  

### What Happens — Compression Agent
The compression agent's `invoke_compression_agent()` has a try/except that catches exceptions and re-raises as `RuntimeError`:

```python
# agent_invoker.py:188-192
except RuntimeError:
    raise  # Re-raise our own errors as-is
except Exception as e:
    raise RuntimeError(f"Exception occurred while generating summary: {e}") from e
```

And in `core.py:195-211`, this propagates back to the caller:
```python
try:
    generated_summary = invoke_compression_agent(...)
except Exception as e:
    return CompressResult(success=False, error=f"Compression Agent failed: {e}")
```

There is **no endpoint fallback chain**. If the configured LLM endpoint for the compression agent fails, the entire compression operation fails. Critically, forced compression happens in `_force_compression()` which runs on the caller's execution thread — meaning a failed compression endpoint **blocks the entire agent lifecycle**.

### What Happens — Security Advisor
The security advisor has the same issue. At `api_server.py:1949`, it calls `sec_agent.run()` directly without going through `api_router.call_with_fallback()`. If the endpoint fails, the generator raises an exception, and at lines 2056-2071 of `api_server.py` the security check is treated as a failure — the tool approval request is denied.

There is no retry mechanism or alternative endpoint chain for the security advisor. A single endpoint failure means all tool approvals fail, which can cause cascading timeouts and stuck agents.

### Impact (Both)
- Single point of failure: one bad endpoint = all compressions or security checks fail
- No degraded mode (e.g., fallback to a different endpoint)
- The forced compression path at >95% context usage is especially dangerous — if the LLM call fails, the agent cannot recover from its context bloat
- For security advisor: tool approvals fail, causing agents to be unable to execute mutating operations

---

## Finding 5 🔴: Race Condition in Compression Agent State Setup

**Severity:** Critical  
**Files:** `compression/agent_invoker.py:129-136`  

### What Happens
At lines 129-136, the compression agent sets up state in shared pool dictionaries with **no lock protection**:

```python
agent_pool.instance_state[comp_state_key] = {
    'active': True,
    'agent_name': f"Compression Agent (compression_agent)",
    'messages': list(comp_history),
}
if comp_state_key not in agent_pool.active_stack:
    agent_pool.active_stack_append(comp_state_key)
agent_pool.instance_conversations[comp_state_key] = list(comp_history)
```

If two forced compressions happen simultaneously (which Finding 3 says they can — since there's no concurrency enforcement), these shared dicts could be corrupted. Both would write to `instance_state['compression_agent']` and `instance_conversations['compression_agent']` concurrently.

### Impact
- Silent conversation history corruption
- Active stack inconsistency — one compression might remove the other's entry
- WebUI state desynchronization — the stream update at line 157-162 reads from these same dicts

---

## Finding 6 🟠: Synchronous call_agent Bypasses Lifecycle-Level Slot Acquisition

**Severity:** Major  
**Files:** `execution_engine.py:979`, `agent_pool.py:1163-1180`  

### What Happens
Parallel agents acquire an EndpointScheduler slot before execution (`_acquire_slot()` at line 1198 in `submit_task()`). But synchronous calls go through `_execute_agent_sync()` which eventually calls `_create_and_run_agent()` → `engine.run(inst)`. The sync path does **NOT** call `_acquire_slot()`.

This means:
- **Parallel agents:** acquire slot → run → release slot (lifecycle-level serialization)
- **Sync agents:** run directly, only get per-call semaphore throttling (Layer 2 in `call_with_fallback`)

### Impact
On concurrency=0 endpoints, the EndpointScheduler ensures only one agent runs at a time. But if an agent makes a synchronous call_agent to another agent on the same endpoint, both are "running" simultaneously — their LLM calls are serialized by Layer 2 semaphores, but their agent lifecycles overlap. This is inefficient and can lead to unexpected behavior where agents interleave tool results with each other's conversations.

### Distinction from Finding 7
This is separate from the `is_parallel_allowed` flag check — the sync path bypasses slot acquisition entirely, not just the parallel launch gate.

---

## Finding 7 🟠: Instance Name Reuse Silently Overwrites Existing Instances

**Severity:** Major  
**Files:** `execution_engine.py:1279`  

### What Happens
At `execution_engine.py:1279`:
```python
self.pool.instances[instance_name] = inst
```

This unconditionally overwrites any existing instance with the same name. The collision detection at lines 940-954 only catches:
1. **Recursive self-calls** (instance in active_stack) → clones to `name_child{N}`
2. **Class mismatch** (existing instance has different agent_class) → returns error

But if an inactive instance with the SAME class exists, it's silently overwritten. The old conversation is lost.

### Impact
- If "worker1" was used as a coder in a previous task and completed, then another call_agent creates "worker1" as a coder again — the new instance replaces the old one
- Not necessarily a bug (instance reuse is sometimes desired), but it's undocumented behavior with potential data loss

### Suggested Fix
Add a check: if an inactive instance exists with the same name AND class, either clear its conversation explicitly or document this as intentional reuse behavior.

---

## Finding 8 🟡: count_by_class Only Counts Active Stack Members (Per-Class, Not Per-Endpoint)

**Severity:** Moderate  
**Files:** `agent_pool.py:1157-1161`  

### What Happens
```python
def count_by_class(self, agent_class: str) -> int:
    with self._state_lock:
        return sum(1 for name in self.active_stack if self.pool.get_instance(name) and
                   self.pool.get_instance(name).agent_class.lower() == agent_class.lower())
```

This counts agents currently in the `active_stack` **of a specific class**. The concurrency check at `execution_engine.py:968` uses this to decide whether parallel launch is allowed for that class.

### Impact
- `count_by_class('compression_agent')` only affects other `compression_agent` instances — it does NOT affect coder, researcher, etc.
- However, the EndpointScheduler's semaphore is per-*api_base*, not per-class. So even if `count_by_class` says "OK to launch another coder", the endpoint might actually be at capacity
- The real issue: compression/security agents are added to `active_stack` but they're NOT counted against any semaphore (because they bypass `_acquire_slot()`). So they can consume endpoint capacity without the scheduler knowing

### Clarification
The concurrency check is per-class (via `count_by_class`), but the actual capacity control is per-endpoint (via EndpointScheduler semaphores). These two mechanisms are not perfectly aligned — compression/security agents break the alignment because they bypass both.

---

## Finding 9 🟡: Synchronous call_agent Calls Are Not Concurrency-Limited

**Severity:** Moderate  
**Files:** `execution_engine.py:956-979`  

### What Happens
At lines 956-970, concurrency checking only affects whether `parallel_launch=True` is allowed:

```python
if effective_concurrency == 0:
    is_parallel_allowed = False
elif effective_concurrency > 0:
    active_count = self.pool._execution.count_by_class(agent_class)
    if active_count >= effective_concurrency:
        is_parallel_allowed = False

# Parallel launch path
if is_parallel_allowed and args.get('parallel_launch') is True:
    return self.pool.submit_parallel(...)

# Synchronous execution — the unified path (no concurrency check here)
return self._execute_agent_sync(...)
```

Synchronous calls ALWAYS proceed regardless of concurrency limits. If `concurrency_limit=1`, a sync call will still execute even if another agent is already using that endpoint.

### Impact
- The EndpointScheduler Layer 1 (task-level) would block at task submission for parallel agents
- But synchronous calls happen within the caller's execution thread — they don't go through `_acquire_slot()`
- The Layer 2 semaphore in `call_with_fallback` (at `api_router.py:454-469`) DOES limit per-call concurrency, but it doesn't prevent the call from being initiated — it just blocks until a slot is available

### Mitigation
Layer 2 semaphores DO provide some protection — individual LLM calls will block if at capacity. But the agent lifecycle isn't serialized; multiple agents could be "running" simultaneously even on concurrency=0 endpoints, with their LLM calls queued behind each other. This is inefficient but not dangerous.

---

## Finding 10 🟡: Default Fallback Concurrency Returns 0 for Unmatched Endpoints

**Severity:** Moderate  
**Files:** `api_router.py:318-350`  

### What Happens
At `get_effective_concurrency()`, if the default LLM config has an api_base but no matching endpoint exists in `self.endpoints`:

```python
if defaults.get('api_base') or defaults.get('model_server'):
    return 0  # Sequential — conservative safety measure
```

This is intentional (comment says "conservative safety measure"), but it means that if you have an api_base configured and then delete the endpoint, ALL agent types fall back to sequential mode.

### Impact
- Unexpected behavior: deleting an endpoint makes everything sequential rather than unlimited
- This is actually a good safety default, but operators might be confused by the behavior change

---

## Positive Findings (Working Correctly)

### 1. ExecutionEngine LLM Routing ✅
**Files:** `execution_engine.py:539-564`

All agents going through `ExecutionEngine.run()` correctly route through `api_router.call_with_fallback()`:
```python
agent_type = instance.agent_class.lower()
return self.pool.api_router.call_with_fallback(agent_type, _do_call)
```

This includes orchestrator, coder, researcher, reviewer, writer, generalist — all agent types that are spawned via `call_agent`. Each gets its own endpoint chain based on `agent_class.lower()`.

### 2. Two-Layer Concurrency Control ✅
**Files:** `api_router.py:70-556`

The system uses two layers of concurrency control:
- **Layer 1 (EndpointScheduler):** Task-level serialization — acquires a slot at task submission, holds it for the entire agent lifecycle. Prevents interleaving of LLM calls between different agents on the same endpoint.
- **Layer 2 (Per-call semaphore):** Call-level throttling within an agent's execution window. Even if Layer 1 allows multiple agents, this prevents any single agent from making more than N concurrent API calls.

### 3. Token Limit MIN Logic ✅
**Files:** `api_router.py:365-417`

The `get_effective_max_tokens()` and `get_endpoint_chain()` methods correctly apply MIN logic:
```python
if general_limit > 0:
    if ep_limit <= 0 or ep_limit > general_limit:
        cfg['max_input_tokens'] = general_limit
```

This ensures that per-endpoint limits never exceed the general settings limit.

### 4. Agent Template LLM Config Resolution ✅
**Files:** `agent_factory.py:188-201`

When loading agents, the factory correctly resolves per-agent-type LLM config:
```python
if agent_pool.api_router is not None:
    agent_llm_cfg = agent_pool.api_router.get_llm_config(agent_name)
else:
    agent_llm_cfg = llm_cfg or agent_pool.llm_cfg

agent_llm_cfg = copy.deepcopy(agent_llm_cfg)  # Prevents shared reference issues
```

### 5. Parallel Task Endpoint Slot Management ✅
**Files:** `agent_pool.py:1163-1245`

Parallel task submission correctly acquires a slot BEFORE submitting to the thread pool, and releases in the finally block:
```python
endpoint_release = self._acquire_slot(agent_class, instance_name)
# ... submit to executor ...
finally:
    if endpoint_release is not None:
        endpoint_release()
```

### 6. Result Delivery to Correct Caller ✅
**Files:** `agent_pool.py:1215-1222` (parallel), `execution_engine.py:1517-1528` (sync)

- **Parallel agents:** Results are sent via `pool.send_message(instance_name, caller, completion_msg)` — the message queue routes to the correct caller
- **Synchronous agents:** The result string is returned directly from `_execute_agent_sync()` which is called within the caller's execution thread

### 7. Recursive Self-Call Protection ✅
**Files:** `execution_engine.py:940-945`

Recursive self-delegation is detected and prevented by cloning the instance name:
```python
if instance_name in self.pool._execution.active_stack:
    count = self.pool._execution.active_stack.count(instance_name)
    instance_name = f"{instance_name}_child{count}"
```

### 8. Endpoint Scheduler Dynamic Resize ✅
**Files:** `api_router.py:119-140`

If concurrency limits change while agents are running, the semaphore is safely resized without disrupting active agents.

---

## Summary of Issues by Category

### API Router Bypass (Critical)
| Agent | File | Line | Bypasses Router? | Has Concurrency Control? |
|-------|------|------|-----------------|-------------------------|
| Compression Agent | `compression/agent_invoker.py` | 143 | 🔴 Yes | ❌ No |
| Security Advisor | `api_server.py` | 1949 | 🔴 Yes | ❌ No |
| All other agents (via ExecutionEngine) | `execution_engine.py` | 544-564 | ✅ No — uses router | ✅ Yes (both layers) |

### Instance Management
| Issue | Severity | File | Line |
|-------|----------|------|------|
| Silent instance overwrite on name reuse | 🟠 Major | `execution_engine.py` | 1279 |
| Sync calls not concurrency-limited | 🟡 Moderate | `execution_engine.py` | 956-979 |

### Lifecycle Slot Acquisition Gaps
| Issue | Severity | File | Line |
|-------|----------|------|------|
| Compression agent no retry/fallback (SPOF) | 🔴 Critical | `compression/agent_invoker.py` | 122-192 |
| Security advisor no retry/fallback (SPOF) | 🔴 Critical | `api_server.py` | 1882-1996 |
| Compression state setup race condition | 🔴 Critical | `compression/agent_invoker.py` | 129-136 |
| Sync path bypasses lifecycle slot acquisition | 🟠 Major | `execution_engine.py` | 979 |

### Concurrency Enforcement Gaps
| Issue | Severity | File | Line |
|-------|----------|------|------|
| No concurrency check on compression/security | 🔴 Critical | Multiple | — |
| count_by_class per-class vs per-endpoint misalignment | 🟡 Moderate | `agent_pool.py` | 1157-1161 |

---

## Recommendations (For Fix, Not This Audit)

### Priority 1: Route Compression Agent Through API Router
Modify `compression/agent_invoker.py:143` to use the ExecutionEngine or directly call `api_router.call_with_fallback()`:

```python
# Instead of: comp_agent.run(comp_history, ...)
# Use: api_router.call_with_fallback('compression_agent', lambda llm_cfg: comp_agent.llm.chat(...))
```

### Priority 2: Route Security Advisor Through API Router  
Same fix needed at `api_server.py:1949`.

### Priority 3: Add Concurrency Slot Acquisition for Compression/Security
Before invoking compression or security agents, acquire an endpoint slot via `_acquire_slot()` and release in finally.

### Priority 4: Add Retry/Fallback Path for Compression Agent
If the primary endpoint fails during compression, fall back to alternative endpoints (same mechanism as `call_with_fallback`). At minimum, provide a degraded mode that uses a simpler summarization strategy.

### Priority 5: Fix Race Condition in Compression State Setup
Add lock protection around the state setup at `agent_invoker.py:129-136`. Consider using a per-endpoint mutex or the existing `_state_lock` from ParallelAgentManager.

### Priority 6: Add Endpoint Slot Acquisition for Sync Path
The synchronous call path (`_execute_agent_sync`) should acquire an EndpointScheduler slot before execution, matching what parallel tasks do in `submit_task()`.

### Priority 7: Document Instance Reuse Behavior
At minimum, document that reusing an instance name with the same class silently replaces the old instance. Optionally add a warning log.

---

## Files Examined (Complete List)

| File | Purpose | Key Lines |
|------|---------|-----------|
| `api_router.py` | Endpoint routing, concurrency, token limits | 70-556 |
| `execution_engine.py` | LLM call path, tool handling | 539-564, 914-979, 1252-1279 |
| `compression/agent_invoker.py` | Compression agent invocation | 62-198 |
| `compression/core.py` | Compression orchestration | 15-338 |
| `agent_pool.py` | Instance management, parallel execution | 1089-1245, 1157-1161 |
| `api_integration.py` | API server bridge | Full file |
| `agent_factory.py` | Agent loading with LLM config | 168-231 |
| `agent.py` | Base Agent class, _call_llm | 161-187 |
| `agents/fncall_agent.py` | FnCallAgent._run (compression agent base) | 73-120 |
| `api_server.py` | Security advisor invocation | 1882-1996 |