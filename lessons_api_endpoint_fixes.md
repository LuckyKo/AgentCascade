# Lessons Learned: API Endpoint Routing Fixes

## Date: 2026-05-30

## Audit Source
`audit_api_endpoint_system.md` — identified 4 critical (🔴) and 2 major (🟠) issues with the API endpoint routing system.

## Key Discoveries

### 1. Config Merge Order Matters
When replacing `agent.run()` with direct `llm.chat()` calls, you must replicate the config merge chain from `Agent._call_llm()`:
```
extra_generate_cfg (agent-level) → generate_cfg (LLM-level) → router llm_cfg (endpoint-level)
```
The `extra_generate_cfg` contains agent-specific settings (seed, lang, custom params) that would be silently dropped if not merged. This was a critical oversight in the initial implementation.

### 2. System Message Prepending is Essential
When bypassing `Agent.run()` and calling `llm.chat()` directly, you MUST prepend the agent's system message (`sec_agent.system_message`). The `llm.chat()` method has a guard that prepends a generic "You are a helpful assistant." if no system message exists. Without our explicit prepend, the security advisor would receive the wrong system prompt.

### 3. Heuristic Edit Mode is Unreliable for Deeply Nested Code
The `edit_file` tool's heuristic match mode produced indentation drift in `api_server.py` (lines ~2040-2047). The deeply nested code inside `_security_check()` (inside a try/except inside an if block inside a WebSocket handler) made it nearly impossible to get correct indentation via heuristic edits. **Lesson:** For deeply nested Python code, use `code_interpreter` to fix indentation issues with precise space counts.

### 4. Fallback Paths Must Match Router Paths
Both the API router path AND the fallback (direct LLM call) path must have identical config merging and system message prepending logic. Otherwise, behavior differs between normal operation and degraded mode.

### 5. Thread Safety in Streaming State Updates
The `_state_lock` (RLock) protects shared state writes during streaming. This is critical because:
- Compression can run in the same thread as `ExecutionEngine.run()` which may already hold the lock
- The security advisor runs in a separate thread (`threading.Thread`)
- RLock (re-entrant lock) prevents deadlocks when the same thread acquires the lock multiple times

### 6. Endpoint Slot Acquisition Pattern
The pattern for acquiring and releasing endpoint scheduling slots:
```python
endpoint_release = None
if hasattr(pool, '_execution') and hasattr(pool._execution, '_acquire_slot'):
    try:
        endpoint_release = pool._execution._acquire_slot(agent_class, instance_name)
    except Exception as e:
        logger.warning(f"Failed to acquire endpoint slot for {instance_name}: {e}")

try:
    # ... execute agent ...
finally:
    if endpoint_release is not None:
        try:
            endpoint_release()
        except Exception as e:
            logger.warning(f"Failed to release endpoint slot for {instance_name}: {e}")
```

## Files Modified
1. `agent_cascade/compression/agent_invoker.py` — Compression agent routing via API router
2. `agent_cascade/api_server.py` — Security advisor routing via API router (lines ~1883-2060)
3. `agent_cascade/execution_engine.py` — Sync path slot acquisition + instance reuse warning

## Review Process
Two review rounds were needed:
- **Round 1:** FAIL — identified 2 critical issues (missing system message, indentation corruption) and 4 major issues (missing agent_name in fallback paths, missing extra_generate_cfg merge)
- **Round 2:** PASS ✅ — all issues resolved