# Bug Fix Summary: disabled_tools Filtering in get_agent_info()

## Root Cause
The `get_agent_info()` method in `agent_pool.py` was reading `template.function_map.keys()` directly instead of using `_get_active_functions()`, which meant it returned ALL tools regardless of the `disabled_tools` configuration from UI settings.

## Files Modified

### 1. agent_cascade/agent_pool.py (Line 576)
**Before:**
```python
'tools': list(getattr(template, 'function_map', {}).keys()),
```

**After:**
```python
# Get active functions using the same logic as agents use at runtime
# This respects disabled_tools configuration from UI settings
active_functions = template._get_active_functions()
active_tool_names = [f['name'] for f in active_functions]

return {
    'name': getattr(template, 'name', name),
    'tagline': getattr(template, 'description', ''),
    'tools': active_tool_names,  # Now filtered by disabled_tools config
}
```

### 2. agent_cascade/api_server.py (Line 979)
**Before:**
```python
'tools': list(a.function_map.keys()) if hasattr(a, 'function_map') else [],
```

**After:**
```python
# Use _get_active_functions() to respect disabled_tools configuration
'tools': [f['name'] for f in a._get_active_functions()] if hasattr(a, '_get_active_functions') else [],
```

### 3. agent_cascade/execution_engine.py (Line 163)
**Before:**
```python
enabled_tools = sorted(t_name for t_name in template.function_map.keys() if t_name not in disabled_tools)
```

**After:**
```python
active_functions = _get_active_functions_from_template(template, instance)
enabled_tools = sorted(f['name'] for f in active_functions)
```

## Additional Findings

### system_info.py (Lines 123, 126) - Already Correct
This file was already using both approaches correctly:
- `function_map.keys()` to get ALL tools (for display purposes)
- `_get_active_functions()` to get ACTIVE/ENABLED tools (respecting disabled_tools config)

No changes needed.

## Verification
All three modified files passed Python syntax validation using AST parsing.

## Impact
After this fix:
1. `list_agents` tool will show only enabled tools per agent type
2. `/api/agents` endpoint will return filtered tool lists
3. Default prompt builder's "CURRENT AVAILABLE RESOURCES" section will list correct enabled tools
4. All locations now use the same `_get_active_functions()` logic as agents do at runtime

## Related Code
- `agent.py:189-209`: Definition of `Agent._get_active_functions()` - the source of truth for tool filtering
- `execution_engine.py:43-86`: Helper function `_get_active_functions_from_template()` that mirrors Agent logic