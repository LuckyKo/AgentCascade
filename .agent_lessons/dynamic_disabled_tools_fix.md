# Dynamic disabled_tools Fix — Lessons Learned

## Problem
When a user changes `disabled_tools` via the UI, the "Enabled Tools" list in the agent's system message (m0) became stale because it was injected once during `_setup_turn()` and never updated. The guard `if '--- CURRENT AVAILABLE RESOURCES' not in m0_content` prevented any refresh.

## Root Cause
In `execution_engine.py:_setup_turn()`, the resources block was injected conditionally — only if absent. This meant:
1. First turn: tools list injected correctly
2. User changes disabled_tools via UI → `_apply_ui_config()` updates `template.llm.generate_cfg['disabled_tools']`
3. Next turn: `_setup_turn()` sees `'--- CURRENT AVAILABLE RESOURCES'` already in m0 → skips injection → agent sees stale tool list

## Solution (Two-Pronged)

### 1. Always rebuild the resources block each turn (execution_engine.py)
Changed from "inject once if not present" to "always rebuild":
- Created `_build_resources_block(pool, template)` — builds the full resources section reflecting current disabled_tools state
- Created `_replace_resources_block(m0_content, new_block)` — uses regex to find and replace the existing block in m0
- In `_setup_turn()`: always call `_build_resources_block()` and either replace (if present) or append (if first injection)

### 2. Immediately refresh m0 when disabled_tools changes (api_integration.py)
In `_apply_ui_config()`, after writing new disabled_tools to the template:
- Track `disabled_tools_changed = True` flag
- After the lock is released, refresh m0 by calling `_build_resources_block()` and `_replace_resources_block()`
- Uses `instance._compression_lock` for thread safety
- Wrapped in try/except so m0 refresh failure doesn't break the main flow

## Key Design Decisions

### Why rebuild every turn AND on config change?
The "rebuild every turn" approach ensures correctness regardless of when disabled_tools changes — even if changes come from sources other than `_apply_ui_config()`. The "on config change" approach provides immediate consistency without waiting for the next turn. Together they provide defense in depth.

### Why extract to helper functions?
- Avoids code duplication between `_setup_turn()` and `_apply_ui_config()`
- Makes the resources block generation independently testable
- Reduces cognitive load when maintaining either call site

### Thread safety
- disabled_tools write: protected by `pool._execution._state_lock` (existing)
- m0 update: protected by `instance._compression_lock` (same lock used for all conversation modifications)
- m0 refresh happens AFTER the state_lock is released to avoid deadlock risk

### Regex pattern for block replacement
```python
pattern = r'--- CURRENT AVAILABLE RESOURCES.*?(?=\n\n###|\Z)'
```
- Matches from the resources header through everything until the next top-level section (`###`) or end of string
- `re.DOTALL` flag makes `.` match newlines
- `count=1` ensures only the first occurrence is replaced

## Files Modified
- `agent_cascade/execution_engine.py`: Added `_build_resources_block()`, `_replace_resources_block()`, refactored resources injection in `_setup_turn()`
- `agent_cascade/api_integration.py`: Added m0 refresh after disabled_tools change in `_apply_ui_config()`