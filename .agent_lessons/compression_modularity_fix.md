# Compression Modularity Fix - Lessons Learned

## Summary of Changes (2026-01-21)

Addressed 4 REQUIRED and 6 RECOMMENDED changes from the Modularity Review for compression bug fixes.

### Files Modified:
1. **agent_pool.py** - Added shared helper, refactored _apply_context_compression, renamed params
2. **agent_cascade/tools/custom/compression_tools.py** - Eliminated duplication, added force param
3. **operation_manager.py** - Renamed parameter, added logging
4. **agent_orchestrator.py** - Added force=True to forced compression
5. **agent_cascade/prompts/dna.py** - Added COMPRESSION_NOTICE_TEMPLATE constant

## Key Discoveries

### 1. Safety Floor Must Be AFTER Clamps (Critical)
The safety floor (`target_discard_count = max(1, ...)`) must be applied AFTER all safety clamps. 
If applied before, the clamp `min(target_discard_count, len(messages_to_compress) - 2)` can reduce it back to 0 when there are exactly 3 active messages (3-2=1, but if target was already reduced).

**Pattern:** Always apply floors AFTER ceilings/clamps to prevent mutual defeat.

### 2. Shared Helpers Eliminate Critical Duplication
The compression marker scanning logic existed in both `compression_tools.py` and `agent_pool.py`. 
Both had the same pattern: scan backwards for COMPRESSION_MARKER, compute active_start_idx, extract messages_to_compress.
Extracting this into `get_compression_target_set()` means a future fix to marker detection only needs one change.

### 3. Force Parameter Needs Full Chain Propagation
Adding a `force` parameter to bypass guards requires careful consideration of the entire call chain:
- Tool accepts force → sets minimum discard count
- OperationManager passes through 
- _apply_context_compression applies its own clamps and floors

The safety floor in _apply_context_compression acts as the ultimate backstop, but it was positioned incorrectly (before clamps). Now fixed.

### 4. Naming Consistency Matters for Code Flow
Having `num_to_remove` → `num_to_summarize` → `target_discard_count` across files created cognitive overhead. 
Standardizing on `target_discard_count` everywhere makes the data flow obvious.

## Architecture Notes

### Call Chain:
```
agent_orchestrator._inject_compression_warning_for_agent()
  └→ compress_tool.call(params, force=True)          # compression_tools.py
       └→ agent_pool.get_compression_target_set()    # shared helper
       └→ operation_manager.apply_context_compression()
            └→ agent_pool._apply_context_compression()
                 └→ agent_pool.get_compression_target_set()  # shared helper again
```

### Safety Order:
1. Tool rejects small active sets (unless force=True)
2. Pool rejects if <= 2 messages in active set
3. Pool clamps to leave >= 2 tail messages
4. Pool enforces floor of 1 (after clamps, prevents no-op)

## Template Constants
- COMPRESSION_NOTICE_TEMPLATE: "A portion of earlier conversation history ({fraction}%) has been summarized..."
- Used via .format(fraction=int(fraction * 100)) in agent_pool.py