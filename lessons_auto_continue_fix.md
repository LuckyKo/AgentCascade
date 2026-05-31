# Lesson: Auto-Continue on Truncation Fix

## Problem
The `auto_continue` setting was sent from the WebUI via `getGenerateCfg()`, but the auto-continue logic in `execution_engine.py:825` was **unconditional** — it always continued after truncation regardless of the user's toggle.

## Root Cause
The `auto_continue` key was correctly stripped from LLM config (to prevent leaking to the API) via `NON_LLM_KEYS`, but it was never stored anywhere accessible to the execution engine. Other control settings like `max_turns` follow the pattern of being applied directly to `instance.max_turns`, but there was no equivalent storage for `auto_continue`.

## Fix Applied (3 files)

### 1. `agent_instance.py` — Added setting to PoolSettings
```python
@dataclass
class PoolSettings:
    ...
    auto_continue: bool = True  # Default True (backward compatible)
```

### 2. `api_integration.py` — Wired UI config → pool.settings
In `_apply_ui_config()`, after stripping from LLM keys:
```python
if 'auto_continue' in ui_cfg and hasattr(pool, 'settings'):
    pool.settings.auto_continue = bool(ui_cfg['auto_continue'])
```

### 3. `execution_engine.py` — Added conditional check
```python
# Before (unconditional):
if is_truncated and not self.pool.stopped and not self.pool.is_instance_halted(inst_name):

# After (conditional):
if is_truncated and not self.pool.stopped and not self.pool.is_instance_halted(inst_name) and self.pool.settings.auto_continue:
```

## Pattern for Future Non-LLM Settings
When adding new execution-control settings that should NOT leak to the LLM API:

1. **Add to `NON_LLM_KEYS`** in both `api_integration.py` and `api_server.py`
2. **Store on appropriate object**: `pool.settings`, `instance.attr`, or `pool.llm_cfg`
3. **Apply from ui_cfg** in `_apply_ui_config()` (or similar handler)
4. **Read from storage** where the logic needs it

## Backward Compatibility
Default is `True` so existing users who never toggled the setting keep the same behavior. Users who disabled it will now see their preference respected.