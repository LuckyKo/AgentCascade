# Lessons: Max Parallel Agents Fix (Bug #16)

## Problem
The WebUI sends `max_parallel_agents` via `getGenerateCfg()` as part of the UI config payload, but the `ThreadPoolExecutor` in `ParallelAgentManager.__init__()` was hardcoded to `max_workers=10`. The setting value was never applied.

## Root Cause
The `max_parallel_agents` setting flowed through the WebSocket handler in `api_server.py` but was never extracted from `ui_cfg` and applied to any backend component. Similar settings like `idle_timeout_seconds`, `max_auto_rollbacks`, etc. all had their own wiring paths, but `max_parallel_agents` was missing one.

## Fix Applied
4 files modified:

### 1. `agent_instance.py` — PoolSettings
Added `max_workers: int = 10` attribute to `PoolSettings` class. This is the canonical storage location for the setting on the pool, matching how `idle_timeout_seconds` is already handled.

### 2. `agent_pool.py` — ParallelAgentManager
- **Line 1156**: Changed from hardcoded `max_workers=10` to `getattr(pool.settings, 'max_workers', 10)` — reads from the pool's settings with a safe fallback.
- **Lines 1162-1197**: Added `resize_executor(max_workers: int) -> bool` method that shuts down the old executor and creates a new one. Supports runtime configuration changes without restart.

### 3. `api_server.py` — WebSocket handler
Added wiring at lines 1844-1850 (right after `idle_timeout_seconds` handler):
```python
if 'max_parallel_agents' in ui_cfg and agent_pool and hasattr(agent_pool, 'settings'):
    val = int(ui_cfg['max_parallel_agents'])
    agent_pool.settings.max_workers = max(1, val)  # Clamp to at least 1
    if hasattr(agent_pool._execution, 'executor') and agent_pool._execution.executor is not None:
        agent_pool._execution.resize_executor(agent_pool.settings.max_workers)
```

### 4. `api_integration.py` — _apply_ui_config
Added `'max_parallel_agents'` to `NON_LLM_KEYS` tuple so it doesn't leak into the LLM API config (it's an execution control parameter, not an LLM parameter).

## Design Decisions

### Why PoolSettings instead of passing through constructor?
- `PoolSettings` is already used for similar runtime-wired settings (`idle_timeout_seconds`).
- The `ParallelAgentManager` receives a reference to the `AgentPool`, which has `pool.settings`. This avoids changing the constructor signature.
- Setting on `PoolSettings` persists across sessions and is accessible from anywhere that has access to the pool.

### Why resize_executor() instead of just recreating at init?
- The setting can be changed at runtime via WebSocket (same pattern as `idle_timeout_seconds`).
- `resize_executor()` cleanly shuts down the old executor with `cancel_futures=True` and creates a new one.
- **Caveat**: Cancelled futures lose their pending tasks. This is acceptable because the setting change happens between user interactions, not during active agent execution.

### Why clamp to min(1)?
- `ThreadPoolExecutor(max_workers=0)` raises `ValueError`.
- `max_workers < 1` would make the thread pool non-functional.
- Using `max(1, val)` ensures at least one worker is always available.

### Thread Safety
- The resize operation acquires no lock explicitly — it replaces `self.executor` atomically in Python (GIL ensures this).
- Running tasks on the old executor continue until they complete or are cancelled.
- New tasks submitted after the swap go to the new executor.

## Flow Summary
```
WebUI (#setting-max-parallel, default=3)
  → getGenerateCfg() sends max_parallel_agents in JSON payload
  → WebSocket handler receives {type: 'update_config', generate_cfg: {...}}
  → api_server.py extracts ui_cfg['max_parallel_agents']
  → pool.settings.max_workers = max(1, val)
  → pool._execution.resize_executor(pool.settings.max_workers)
    → old executor.shutdown(wait=False, cancel_futures=True)
    → new ThreadPoolExecutor(max_workers=new_val)
```

## Testing
- All Python files pass syntax check (AST parse).
- Source-level verification confirms:
  - PoolSettings has `max_workers: int = 10`
  - ThreadPoolExecutor reads from `pool.settings.max_workers` (no hardcoded 10)
  - resize_executor() method with correct shutdown+recreate logic
  - api_server.py wires the full chain
  - max_parallel_agents filtered from NON_LLM_KEYS

## Related Issues to Consider
- **Auto-Continue (`setting-auto-continue`)**: Same pattern — setting is stripped but never checked as a condition. Fix needed in execution_engine.py.
- **Show Active Window Only (`setting-show-active-only`)**: Dead setting — either remove from WebUI or wire it to the appropriate logic.
- **Vision Enabled (`setting-vision-enabled`)**: Stored in WebUI local state but NOT sent via getGenerateCfg().