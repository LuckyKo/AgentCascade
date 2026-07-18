# Compression Bug Fix - System Message Preservation

## Date: 2026-06-16

## Problem Summary

When `active_start_idx == 0`, the compression formula `history[:active_start_idx] + [marker] + history[insert_pos:]` produces an empty prefix (`history[:0] = []`), dropping the system message at index 0.

## Root Cause

The `apply_compression()` function in `core.py` was using `active_start_idx` directly without ensuring it includes at least the system message when present.

## Fixes Applied

### FIX 1: core.py - apply_compression() (lines 90-95)

**Location:** `N:\work\WD\AgentCascade\agent_cascade\compression\core.py`

**Solution:** Added logic to ensure `prefix_len` includes at least the system message:

```python
# FIX: Ensure system message is preserved even if active_start_idx=0
# When active_start_idx == 0, history[:0] returns empty list, dropping the system message.
# We calculate prefix_len to always include at least the system message when it exists.
prefix_len = active_start_idx
if history and get_role(history[0]) == SYSTEM and prefix_len < 1:
    prefix_len = 1
```

Then uses `prefix_len` instead of `active_start_idx` when building new_history:
```python
new_history = history[:prefix_len] + [marker_message] + history[insert_pos:]
```

### FIX 2: agent_pool.py - get_compression_target_set() (lines 686-699)

**Location:** `N:\work\WD\AgentCascade\agent_pool.py`

**Solution:** Changed the guard condition from `== 0` to `< 1` for a more defensive check:

```python
# FIX 2: Insurance guard — if history[0] is SYSTEM but active_start_idx would be 0,
# force it to at least 1. This catches corruption scenarios where start_idx logic
# was bypassed (e.g., pool sync lost system message), ensuring compression formula
# doesn't produce empty prefix and lose the system message.
# Kept at WARNING level because this indicates actual pool corruption.
# Changed from "== 0" to "< 1" for a more defensive check (handles edge cases).
if history:
    first_role = get_role(history[0])
    if first_role == SYSTEM and active_start_idx < 1:
        logger.warning(
            f"[COMPRESSION FIX] Forced active_start_idx from {active_start_idx} to 1 for '{agent_name}' "
            f"to preserve system message (pool may be partially corrupted)"
        )
        active_start_idx = 1
```

**Changes:**
- `== 0` → `< 1` (more defensive, handles potential negative indices)
- Log message updated to show actual value: `from {active_start_idx} to 1`

## Impact

These fixes ensure the system message is never lost during atomic compression operations, preventing context corruption that could cause agent behavior issues.

## Testing Recommendations

1. Test compression with exactly 2 messages (system + user) - should preserve both
2. Test forced compression at >95% context usage
3. Verify log and pool stay in sync after compression
4. Check for the warning message when active_start_idx is unexpectedly 0

## Related Files

- `agent_cascade/compression/core.py` - Main compression logic
- `agent_pool.py` - Agent pool and conversation management
- `agent_cascade/compression/helpers.py` - Helper functions including `get_role()`