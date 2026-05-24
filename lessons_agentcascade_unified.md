# Lessons Learned — AgentCascade Unified Test Infrastructure

## 2026-05-24: Initial test setup

### Source file locations (absolute paths)
- `N:\work\WD\AgentCascade_unified\agent_cascade\tool_utils.py` → `resolve_prev_arg_placeholders()`
- `N:\work\WD\AgentCascade_unified\config\token_cache.py` → `AgentTokenCache`
- `N:\work\WD\AgentCascade_unified\config\unified.py` → feature flags (USE_UNIFIED_ARCHITECTURE, USE_UNIFIED_STATE, USE_UNIFIED_LOOP)

### Key behaviors discovered

#### `resolve_prev_arg_placeholders()`
- Returns `(resolved_args, error_message)` tuple — never raises exceptions.
- On error, returns the **original unmodified** tool_args (not partially-resolved).
- Resolution priority: tool-specific cache → `__GLOBAL__` fallback.
- Passes through non-dict inputs unchanged (str, list, int, None).
- Deep copies both the tool_args dict AND each resolved value to prevent cache mutation.
- When `lock=None`, caller is responsible for thread-safety — used when caller already holds the lock.
- AgentPool without `last_tool_args` attribute returns original args silently (defensive).

#### `AgentTokenCache`
- TTL defaults to 300s; uses lazy expiration on `get()` AND periodic `cleanup_expired()`.
- Background cleanup timer fires every 5 minutes and is a daemon thread.
- `get()` returns only `{'count': int, 'tokens': int}` — timestamp is internal.
- `size()` counts raw entries (does NOT lazily expire).

#### Feature flags (`config.unified`)
- All three default to `False`. Only env var value `"1"` enables a flag.
- Values like `"true"`, `"yes"`, `"2"` do **NOT** enable — it's a strict string comparison.
- Flags are read at import time → tests must reimport the module after changing env vars.

### Test infrastructure notes
- `conftest.py` provides `_FakeAgentPool` (minimal agent_pool mock), `short_ttl_cache` (1s TTL, timer cancelled), and `env_patch`/`clear_feature_env_vars` for flag tests.
- Feature flag tests use `__import__` with module cache clearing to re-read env vars — this pattern is needed because the flags are evaluated at import time.

## 2026-05-24: Integration test setup

### Key discovery: api_server.py uses closures, not module-level functions
- `get_session_history()`, `build_state()`, and `get_agent_state()` are defined inside the app factory function in api_server.py — they're closures that capture local variables like `session`, `agent_pool`, `agents`.
- **Cannot patch** these at module level (e.g., `patch('api_server.agent_pool', mock)` fails because `agent_pool` is not a module-level attribute).
- **Solution**: Test the *logic patterns* by recreating the same conditional branches with controlled data structures instead of trying to import the closures.

### Key discovery: AgentPool dependencies
- `OperationManager` is imported inside `AgentPool.__init__()` from `operation_manager` — patch at `'operation_manager.OperationManager'`.
- `TelemetryCollector` is imported at module level from `telemetry` — patch at `'telemetry.TelemetryCollector'`.
- `APIRouter` is imported at module level from `api_router` — patch at `'api_router.APIRouter'`.

### Streaming tool resolution (agent_orchestrator.py lines ~1385-1528)
- **Streaming path** (sub-agent calls, USE_UNIFIED_LOOP=True): resolves __USE_PREV_ARG__ via `resolve_prev_arg_placeholders()` with `lock=self.agent_pool._state_lock`.
- **Non-streaming path** (normal tools): always resolves placeholders regardless of USE_UNIFIED_LOOP flag.
- Both paths write resolved args to `last_tool_args` after successful execution (lines 1522-1528).