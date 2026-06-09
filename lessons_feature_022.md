# Feature Plan #022 — Compression Endpoint Fix Implementation Notes

## Summary
Fixed endpoint routing for dynamically-sized agents (e.g., compression agent) by adding `allocated_tokens` parameter to the API router's `call_with_fallback()` method. This ensures agents with variable context sizes are routed to endpoints with sufficient token capacity.

## Problem
When `call_with_fallback()` was called from `_execute_llm_call()`, it only received the `agent_type` string. The endpoint chain resolution (`get_endpoint_chain()`) used this to look up configured endpoints, but for agents whose context size was dynamically allocated (e.g., compression agent with variable max_input_tokens), the wrong endpoint could be selected — one with insufficient token capacity.

## Solution
Extended the API router to accept and use `allocated_tokens` parameter:

1. **execution_engine.py**: Calculate `allocated_tokens` from instance override or template config BEFORE calling `call_with_fallback()`, then pass it as a named parameter.

2. **api_router.py**: 
   - Added `allocated_tokens: Optional[int] = None` to both `call_with_fallback()` and `get_endpoint_chain()` signatures
   - When provided, adjust endpoint configs to ensure they can accommodate the agent's actual token requirements
   - Maintains backward compatibility via optional parameter default

## Key Implementation Details

### Token Calculation Priority (execution_engine.py)
```python
allocated_tokens = None
if instance._generate_cfg_override is not None and 'max_input_tokens' in instance._generate_cfg_override:
    val = instance._generate_cfg_override['max_input_tokens']
    if isinstance(val, int) and val > 0:
        allocated_tokens = val
elif hasattr(llm, 'generate_cfg') and 'max_input_tokens' in llm.generate_cfg:
    val = llm.generate_cfg['max_input_tokens']
    if isinstance(val, int) and val > 0:
        allocated_tokens = val
```

Priority order: instance override → template config → None (use existing behavior)

### Endpoint Adjustment Logic (api_router.py)
When `allocated_tokens` is provided:
- Check if endpoint's `max_input_tokens` < `allocated_tokens`
- If so, adjust the config to `allocated_tokens` to ensure sufficient capacity
- Skip adjustment if limit is 0 (means "unlimited")

### Review Process
- **Reviewer**: reviewer_022
- **Rounds**: 2 review iterations
- **Findings Fixed**:
  1. Duplicate `agent_type` assignment removed
  2. Inefficient `max()` calls replaced with direct assignments
  3. Enhanced inline documentation for maintainability
  4. Clarified handling of "unlimited" endpoints (limit=0)

## Files Modified
| File | Changes |
|------|---------|
| `agent_cascade/execution_engine.py` | Calculate and pass allocated_tokens (~15 lines added) |
| `agent_cascade/api_router.py` | Accept and use allocated_tokens parameter (~25 lines modified) |

## Commit Information
- **Hash**: `f2c202452097885e4a12504dfd42224f5c9af40a`
- **Message**: "fix: compression endpoint fix — allocated tokens parameter in call_with_fallback"
- **Stats**: 2 files changed, 56 insertions(+), 4 deletions(-)

## Testing Notes
- Backward compatible: `allocated_tokens=None` maintains existing behavior
- All existing callers unaffected (api_router.py:380, api_server.py:1925)
- No new regressions introduced

## Future Enhancements (Noted but Deferred)
- Add telemetry for `allocated_tokens` parameter usage frequency
- Consider warning log when `allocated_tokens` exceeds common API hard caps (~128K)
- Extend to other hot-path LLM call sites beyond `_execute_llm_call()`

## Related Features
- **Feature #021**: Simplified endpoint selection (companion fix)
- **Feature #020**: Compression session lock and cooldown
- **Bug 42**: Retry path fix in api_integration.py