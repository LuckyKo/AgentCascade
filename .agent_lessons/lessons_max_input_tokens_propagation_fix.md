# max_input_tokens Propagation Bug Fix - 2026-06-22

## Problem
All sub-agents were using the general UI "Context Size (Tokens)" value instead of their per-endpoint values from `api_endpoints.json`. This caused context size misalignment where agents would inherit a generic context limit rather than their endpoint-specific configured limits.

## Root Cause
The `max_input_tokens` key was being propagated via `_generate_cfg_override` to all sub-agents. The issue occurred because:

1. UI sets a general "Context Size (Tokens)" value which populates `max_input_tokens` in the config
2. During LLM config sanitization, `NON_LLM_KEYS` is used to filter out operational settings that shouldn't be passed to the LLM API
3. `max_input_tokens` was NOT in `NON_LLM_KEYS`, so it was included in the sanitized LLM-safe config
4. This sanitized config gets stored in `_generate_cfg_override` and propagated to sub-agents via `call_agent`
5. Sub-agents then inherited this overridden value instead of using their own per-endpoint values from `api_endpoints.json`

## Fix Applied
Added `'max_input_tokens'` to the `NON_LLM_KEYS` tuple in `agent_cascade/constants.py`.

### Change Details
**File**: `agent_cascade/constants.py` (line 60)

```python
NON_LLM_KEYS: tuple[str, ...] = (
    # Execution control settings
    'max_auto_rollbacks',
    'auto_rollback_on_loop',
    'auto_continue',
    'max_turns',
    'max_parallel_agents',
    'max_input_tokens',  # Context size setting (per-endpoint, not LLM parameter)
    
    # ... rest of keys
)
```

## Why This Works
By adding `max_input_tokens` to `NON_LLM_KEYS`:

1. It gets filtered out during LLM config sanitization in the execution engine
2. It's NOT included in `_generate_cfg_override` that gets propagated to sub-agents
3. Each agent instance can now use its own per-endpoint `max_input_tokens` value from `api_endpoints.json` via the API Router configuration
4. The propagation chain is broken, preventing the UI's general context size from overriding endpoint-specific values

## Files Modified
- `agent_cascade/constants.py` — Added `'max_input_tokens'` to `NON_LLM_KEYS` tuple (line 60)

## Related Concepts
- `_generate_cfg_override`: Dict that stores per-instance LLM config overrides, propagated via `call_agent` tool
- `NON_LLM_KEYS`: Tuple of config keys that are operational settings (not model parameters) and should be excluded from LLM API calls
- Per-endpoint configuration: Each endpoint in `api_endpoints.json` can have its own `max_input_tokens` value based on the underlying model's context window

## Testing Considerations
After this fix, verify that:
1. Different agent types (coder, researcher, security, etc.) use their configured endpoint-specific context sizes
2. The UI "Context Size (Tokens)" setting still works for the main agent instance
3. Sub-agents spawned via `call_agent` don't inherit the parent's `max_input_tokens` value

## Impact
- **Minimal code change**: Single line addition to a tuple
- **No breaking changes**: Only affects config propagation behavior
- **Fixes context size alignment**: Agents now respect their endpoint-specific limits from `api_endpoints.json`