# Compression Agent Overflow Fix — Lessons Learned

## Problem
When forced compression runs, it feeds the compression agent more messages than its context can handle. The compression agent overflows and spawns children (named `compression_agent_child1`, etc.), which then overflow too — creating a cascade.

## Root Causes
1. **Exact name matching**: The exemption check at `agent_orchestrator.py:2049` only exempted exact name `'compression_agent'`, not child variants like `'compression_agent_child1'`.
2. **No context window guard**: `core.py` had no logic to limit the number of messages sent to the compression agent based on its actual context window size.

## Fixes Applied

### Fix 1: Broadened exemption (agent_orchestrator.py line 2049)
```python
# Before:
if instance_name != 'compression_agent':

# After:
if not instance_name.startswith('compression_agent'):
```
This exempts ALL compression agent instances, including spawned children.

### Fix 2: Token cap guard (core.py lines 131-150)
After computing `target_discard_count`, the new code:
1. Looks up the compression agent in the pool via `agent_pool.get_agent('compression_agent')`
2. Reads its `max_input_tokens` from `llm.generate_cfg`
3. Caps `target_discard_count` so estimated tokens don't exceed 60% of the context window
4. Uses ~500 tokens/message as a rough estimate (conservative — better to under-compress than overflow)
5. Entire block is wrapped in try/except — failures silently fall through to original count

## Testing
- All existing compression tests pass (61/61, 1 pre-existing failure excluded)
- New test class `TestTokenCapGuard` covers:
  - Discard count properly capped when compression agent has small context window
  - No cap applied when compression agent not loaded
  - Graceful handling when get_agent raises exception
- New test `test_orchestrator_skips_inject_for_compression_agent_children` verifies child exemption

## Key Design Decisions
- **60% margin**: Reserves 40% for system prompt, existing summary, and output generation. For a 64K window that's ~25.6K — plenty of headroom.
- **500 tokens/message**: Conservative estimate. Short messages are ~50-100 tokens; long tool outputs can be 500-2000+. Better to under-count than overflow.
- **try/except around the whole block**: If any part fails (missing method, wrong attribute), we proceed with the original discard count rather than blocking compression entirely.