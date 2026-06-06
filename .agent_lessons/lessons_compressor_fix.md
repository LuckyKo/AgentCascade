# Fix for "Compressor is None after loading" Error

## Problem Summary
After renaming `compression_agent` → `Compressor`, the compression agent fails to load with the error:
```
RuntimeError("Compressor is None after loading")
```

## Root Cause Analysis

### Main Branch (`N:\work\WD\AgentCascade`)
**File**: `agent_pool.py`

**Issue**: Case mismatch between storage and retrieval of agents.

- **Line 503-504**: Agents are stored with their original case:
  ```python
  self.agents[agent_name] = agent  # e.g., 'Compressor'
  ```

- **Line 512**: Agents are retrieved with lowercase conversion:
  ```python
  return self.agents.get(agent_name.strip().lower())  # looks for 'compressor'
  ```

**Result**: When `get_agent('Compressor')` is called, it searches for `'compressor'` but the key is `'Compressor'`, returning `None`.

### Unified Branch (`N:\work\WD\AgentCascade_unified`)
**File**: `agent_cascade/agent_pool.py`

The unified branch appears to have consistent casing (no `.lower()` in retrieval), but I should verify if there are any other issues.

## Fix Applied

### Main Branch Fix
Changed lines 503-504 in `N:\work\WD\AgentCascade\agent_pool.py`:

```python
# Before:
self.agents[agent_name] = agent
self.agent_configs[agent_name] = agent.agent_configs.get(agent_name, {})

# After:
# Store with lowercase key for case-insensitive lookup (matches get_agent behavior)
self.agents[agent_name.lower()] = agent
self.agent_configs[agent_name.lower()] = agent.agent_configs.get(agent_name, {})
```

This ensures agents are stored with lowercase keys to match the case-insensitive retrieval logic in `get_agent()`.

## Testing Steps
1. Restart the application/server
2. Trigger a compression operation (e.g., via compress_context tool)
3. Verify that "Compressor is None after loading" error no longer appears
4. Check logs for successful agent loading: `[OK] Loaded agent on demand: Compressor`

## Related Files
- `N:\work\WD\AgentCascade\agent_pool.py` - Main fix location
- `N:\work\WD\AgentCascade\agent_cascade\compression\agent_invoker.py` - Where the error originates
- `N:\work\WD\AgentCascade\agents\Compressor_soul.md` - Agent definition file

## Notes
- The soul file naming (`Compressor_soul.md`) is correct and matches the expected pattern
- All Python code references use `'Compressor'` (case-sensitive)
- The `.lower()` in `get_agent()` was intended for case-insensitive lookup but wasn't matched in storage