# Compressor disabled_tools Filtering — Investigation Findings
# Discovered: 2026-06-25

## Bug Hypothesis (CONFIRMED)
The Compressor agent's `disabled_tools` filtering doesn't properly propagate UI settings
because `operation_manager._current_session` is **never set** anywhere in the codebase.

## Evidence Chain

### 1. The Read Path (agent_invoker.py:165-182)
```python
if hasattr(agent_pool, 'operation_manager'):
    session = getattr(agent_pool.operation_manager, '_current_session', None)
    if session:
        # → Use UI config from session['generate_cfg']
        ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
        ...
```

### 2. The Write Path — NOWHERE
Searched entire codebase for `_current_session = ` (assignment): **0 matches**.
The `operation_manager` is never told about the session dict.

### 3. What happens instead?
- When `session` is None (always), the code falls through to line 182:
  ```python
  cfg['disabled_tools'] = sorted(DEFAULT_COMPRESSOR_DISABLED_TOOLS)
  ```
- This means **only** the hardcoded defaults are applied, never the UI-configured disabled tools.

### 4. DEFAULT_COMPRESSOR_DISABLED_TOOLS (constants.py:45-51)
Contains ALL_USER_APPROVAL_TOOLS + sub-agent management tools:
```python
ALL_USER_APPROVAL_TOOLS = frozenset({
    'shell_cmd', 'code_interpreter', 'write_file', 'edit_file',
    'delete_file', 'copy_file', 'move_file'
})
DEFAULT_COMPRESSOR_DISABLED_TOOLS = ALL_USER_APPROVAL_TOOLS | {
    'call_agent', 'dismiss_agent', 'list_agents'
}
```

### 5. Comparison: Security Agent (api_server.py:2107-2134)
Security agent reads `session` directly from the api_server closure:
```python
ui_cfg = copy.deepcopy(session.get('generate_cfg', {}))
```
This works because the Security code runs INSIDE the WebSocket handler where `session` is in scope.

Compressor runs inside `compression/core.py` → `agent_invoker.py`, which has NO access to
the api_server session closure. It tries to get it via `operation_manager._current_session`.

### 6. Session Flow for Regular Agents
api_server.py line 514: `session = {'generate_cfg': ..., ...}` (local dict)
→ Passed as `ui_cfg` to `run_agent_thread_unified()` at line 809
→ Applied via `_apply_ui_config(pool, instance_name, ui_cfg)` at run_agent_unified.py:106
→ Stored on instance as `instance._generate_cfg_override`

### Conclusion
The Compressor agent works correctly for defense-in-depth defaults (hardcoded tools are disabled),
but UI-configured disabled_tools settings are NEVER applied because `_current_session` is never set.

## Fix Applied (2026-06-25)
Option 3 was implemented: read from caller instance's `_generate_cfg_override`.

### Changes in `agent_cascade/compression/agent_invoker.py` (lines 164–207):
- Read user's disabled_tools from `caller_inst._generate_cfg_override['disabled_tools']`
- Handle both flat list format and per-agent dict format (with case-insensitive key lookup)
- Merge with `DEFAULT_COMPRESSOR_DISABLED_TOOLS` via `merge_disabled_tools_for_auto_agent()`
- Cleaned up unused imports: removed `copy` and `NON_LLM_KEYS`

### Key details:
- The caller agent's `_generate_cfg_override` is populated by `propagate_settings()` during `_create_system_agent()`, which receives UI config from the session.
- Type validation added: only `(list, tuple)` or `dict` are accepted for disabled_tools values (prevents string-to-char-list corruption).
- Case-insensitive fallback for dict key lookup (`'Compressor'` → `'compressor'`).