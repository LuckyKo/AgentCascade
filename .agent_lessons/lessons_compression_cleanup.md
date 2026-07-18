# Compression System Cleanup Lessons

**Date:** 2026-06-16  
**Related Files:** `COMPRESSION_CLEANUP_PLAN.md`, `COMPRESSION_SYSTEM_AUDIT.md`

## Key Learnings

### Architecture Decisions

1. **Single Source of Truth for Role Extraction**
   - Created `get_role()` and `get_content()` utilities in `helpers.py`
   - All compression code now uses these instead of inline checks
   - Handles both dict and Message objects consistently

2. **Constants Centralization**
   - All magic numbers moved to `constants.py`
   - Makes tuning system behavior easier
   - Prevents inconsistencies across files

3. **Phased Compression (2+1 Architecture)**
   - Split `compress_context()` into:
     - `_prepare_compression()`: Validation and calculation → returns `CompressionPreparation` dataclass
     - Inline summary generation: ~20 lines in orchestrator (too small for separate function)
     - `_apply_compression_phase()`: Atomic pool/log update → returns `CompressResult`
   - Each phase can be tested independently

4. **O(1) SYSTEM Message Check**
   - Replaced O(n) `any()` scan in `slice_history_for_llm()` with index check
   - Logic: markers inserted after SYSTEM (index 0), so `latest_summary_idx > 0` means SYSTEM not in slice

5. **Log Level Strategy**
   - FIX 2 (active_start_idx corruption guard): **WARNING** — indicates actual pool corruption
   - FIX 1 (working set system message mismatch): **DEBUG** — routine sync adjustment
   - FINAL GATE (apply_compression system message check): **ERROR** — prevents corrupted history

### Testing Strategy

- Test utilities independently before integrating
- Use dry_run mode for non-destructive testing
- Verify SYSTEM message preservation across all paths

### Common Pitfalls

1. **Don't mutate log_history directly** — always create a copy first
2. **Always validate insert_pos bounds** before slicing
3. **Keep force marker injection atomic** with summary marker
4. **Log updates must happen before pool updates** to prevent divergence

## Quick Reference

### Import Pattern
```python
from agent_cascade.compression import (
    compress_context,
    get_role,
    DEFAULT_COMPRESSION_FRACTION,
)
```

### Compression Flow
```
compress_context() → _prepare_compression() → [inline summary] → _apply_compression_phase() → apply_compression()
```

### Key Constants
- `DEFAULT_COMPRESSION_FRACTION = 0.5` — Default compression amount
- `FORCE_COMPRESSION_THRESHOLD = 95.0` — Trigger forced compression above this %
- `MIN_MESSAGES_TO_COMPRESS = 3` — Minimum messages before compression
- `COMPRESSION_AGENT_TIMEOUT = 300` — 5-minute timeout for large tasks
- `MAX_COMPRESSION_RETRIES = 3` — Max forced compression failures before skipping

## Future Improvements

- Add compression metrics/tracking
- Explore incremental compression (compress smaller chunks more frequently)
- Consider typed `Message` protocol for get_role/get_content instead of duck typing
