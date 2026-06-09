# Endpoint Assignment Refactoring — Lessons Learned

## Summary
Refactored the API router's endpoint assignment logic to simplify the three-tier fallback chain, remove the orchestrator-specific hack, and add a "last successful endpoint" tracking mechanism for improved reliability.

## Changes Applied

### Change #1: Simplified `get_endpoint_chain()` Tier Logic
The three-tier endpoint selection chain was simplified:

| Tier | Old Behavior | New Behavior |
|------|-------------|--------------|
| Tier 1 | Agent-specific endpoints with normalization | Direct lookup: `self.agent_priorities.get(agent_type, [])` — no normalization |
| Tier 2 | Orchestrator fallback hack (hardcoded logic) | `_last_successful_endpoint_cfg` — validated endpoint that previously succeeded |
| Tier 3 | Default endpoint | Unchanged: `default_llm_cfg` as last resort |

**File**: `agent_cascade/api_router.py` (lines ~410-465)

### Change #2: Added Last Successful Endpoint Tracking
Added `_last_successful_endpoint_cfg` attribute to track the most recently successful endpoint configuration.

**Initialization**: In `__init__()`:
```python
self._last_successful_endpoint_cfg: Optional[Dict[str, Any]] = None
```

**Update Logic**: In `call_with_fallback()`, after a successful LLM call completes (including all retries):
```python
with self._lock:
    self._last_successful_endpoint_cfg = copy.deepcopy(llm_cfg)
```

**Usage**: In `get_endpoint_chain()` as Tier 2 fallback when agent-specific endpoints are unavailable.

### Change #3: Thread Safety Validation
All accesses to `_last_successful_endpoint_cfg` are protected by `self._lock`:
- **Write**: In `call_with_fallback()` — wrapped with `with self._lock:` ✅
- **Read (condition + data)**: In `get_endpoint_chain()` — entire Tier 2 block inside single lock to prevent TOCTOU race ✅

**Thread Safety Design:**
- The condition check (`if not configs and self._last_successful_endpoint_cfg is not None`) and the subsequent data access are kept within the same lock scope to prevent a TOCTOU race where another thread could set `_last_successful_endpoint_cfg` to `None` between the check and the read.

## Benefits

1. **Improved Reliability**: Automatic recovery when an agent's configured endpoints become unavailable
2. **Simplified Codebase**: Removed the fragile orchestrator fallback hack
3. **Better Fallback Logic**: Uses a validated endpoint that previously succeeded, rather than arbitrary orchestrator endpoints
4. **Thread Safety**: All shared state accesses are properly synchronized

## Remaining Gaps

1. **Stale Documentation**: Other documentation files may still reference the old `_normalize_agent_type_lookup` function
2. **Telemetry**: Consider adding metrics for fallback chain tier usage (how often Tier 2 vs Tier 3 kicks in)
3. **Endpoint Validation**: The `_last_successful_endpoint_cfg` could become stale if the endpoint was deleted/disabled — validation exists but no warning is logged

## Testing Recommendations

1. Test with an agent that has no configured endpoints to verify Tier 2 fallback works
2. Test with all endpoints disabled to verify Tier 3 fallback works
3. Test concurrent access to verify thread safety
4. Test endpoint removal while tracking is active to verify stale config handling

## Related Files

- `agent_cascade/api_router.py` — Main implementation
- `config/api_endpoints.json` — Endpoint configuration