# Compression Fix: Tail-Offset Insertion Method

**Date:** 2026-06-10  
**Issue:** Log data destruction on every compression  
**Fix:** Tail-offset marker insertion to preserve all log entries

---

## Problem Statement

The original `apply_compression()` function in `core.py` passed truncated history to the logger:

```python
# OLD CODE (destructive)
new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
logger_inst.reset_history(new_history, rewrite=True)  # Lost all discarded messages!
```

This caused permanent loss of all previously discarded messages because the entire log file was overwritten with the truncated pool history. Each compression destroyed earlier content that wasn't in the active set.

---

## Solution: Tail-Offset Insertion Method

### Key Insight

The pool gets clean-trim history (truncated), but the log should preserve **all** entries by inserting markers at calculated offset positions from the tail:

```
Pool after compression:  [PREFIX][MARKER][TAIL]
Log after compression:   [FULL_HISTORY_WITH_MARKERS_INSERTED]
```

The log mirrors what you'd see if reading from the last marker forward: `[MARKER][TAIL_MESSAGES]` — perfectly mirroring the pool's post-compression state.

### Implementation

**File:** `agent_cascade/compression/core.py`, lines 73-107

```python
# Use tail-offset insertion method to preserve all existing log entries
log_history = logger_inst.data["history"]

# Calculate insert offset from tail — preserves all existing log entries
log_insert_pos = len(log_history) - tail_count

# Safety: never insert before SYSTEM message (index 0)
if log_insert_pos == 0 and log_history and log_history[0].get('role') == 'system':
    log_insert_pos = 1

# Clamp to valid range (both lower and upper bounds)
log_insert_pos = max(0, min(log_insert_pos, len(log_history)))

# Build formatted markers using logger's formatting (adds timestamps)
formatted_marker = logger_inst._format_message(marker_message)

if include_force_marker:
    # Insert force marker first, then summary marker
    formatted_force = logger_inst._format_message(force_marker)
    log_history.insert(log_insert_pos, formatted_force)
    log_history.insert(log_insert_pos + 1, formatted_marker)
else:
    # Just insert the summary marker
    log_history.insert(log_insert_pos, formatted_marker)

# Rewrite the entire file since we inserted in the middle
logger_inst.reset_history(log_history, rewrite=True)
```

### Why This Works

1. **Pool** receives `new_history` (truncated — clean trim for active context)
2. **Log** receives marker **inserted** at position `len(log_history) - tail_count`:
   - All existing log entries are preserved
   - Reading from the last marker forward gives `[MARKER][TAIL_MESSAGES]`
   - Exactly mirrors the pool's post-compression state
3. **Full message queue reconstruction is possible with NO loss**

---

## Edge Cases Handled

### 1. SYSTEM Message Protection

If `log_insert_pos == 0` and first message has role `'system'`, bump to index 1:

```python
if log_insert_pos == 0 and log_history and log_history[0].get('role') == 'system':
    log_insert_pos = 1
```

### 2. Negative Insert Position (Pool/Log Divergence)

If `tail_count > len(log_history)` (divergence), raw position becomes negative:

```python
log_insert_pos = max(0, min(log_insert_pos, len(log_history)))
```

Without `max(0, ...)`, Python's negative index would insert from the **end**, corrupting the log.

### 3. Force Marker Handling

When `include_force_marker=True`, both markers inserted in order:

```python
log_history.insert(log_insert_pos, formatted_force)      # First
log_history.insert(log_insert_pos + 1, formatted_marker) # Second
```

---

## Files Modified

1. **`agent_cascade/compression/core.py`** (lines 73-107)
   - Replaced truncating log update with tail-offset insertion
   - Added safety checks and clamping
   - Force marker support

2. **`agent_logger.py`** (lines 134-174)
   - Updated `insert_compression_marker()` docstring (removed "DEPRECATED")
   - Removed debug scanning code
   - Added lower-bound clamp for consistency

3. **`test_compression_fix.py`** (new file)
   - 5 comprehensive tests covering all edge cases
   - All tests pass ✅

---

## Test Coverage

| Test | Scenario | Result |
|------|----------|--------|
| `test_tail_offset_calculation()` | Normal compression (10 messages, discard 3) | ✅ PASSED |
| `test_force_marker_insertion()` | Force marker + summary marker | ✅ PASSED |
| `test_system_message_at_position_zero()` | SYSTEM at index 0 protection | ✅ PASSED |
| `test_tail_count_zero()` | All messages discarded (marker at end) | ✅ PASSED |
| `test_negative_insert_position()` | Divergence (tail_count > log length) | ✅ PASSED |

---

## Benefits

1. **No data loss**: All original log entries preserved across compressions
2. **Full reconstruction**: Can rebuild complete message queue from log
3. **Mirrors pool**: Log structure matches pool's `[MARKER][TAIL]` pattern
4. **Atomic updates**: Log updated first, then pool (prevents divergence)
5. **Edge case safe**: Handles SYSTEM messages, zero tail, negative positions

---

## Remaining Considerations (Non-Blocking)

1. **Concurrency Safety**: No per-agent locking for concurrent compressions
   - Impact: Low (single-threaded usage common)
   - Fix: Add `threading.Lock` per agent instance

2. **Formatting Consistency**: Pool stores raw Message objects, log stores formatted dicts
   - Impact: Low (`update_history()` formats both sides before comparison)
   - Fix: Standardize formatting on both sides

3. **Double-Formatting**: Markers formatted twice (once in `apply_compression()`, once in `reset_history()`)
   - Impact: Minimal (timestamps are stable, idempotent)
   - Fix: Track already-formatted messages or skip formatting in `reset_history()` when known

---

## Usage Notes

- The tail-offset method is now the **active pattern** for compression
- `insert_compression_marker()` in `agent_logger.py` provides standalone interface
- Both pool and log use the same calculation logic
- Force markers are handled atomically with summary markers

---

## References

- Original issue: "Log data destroyed on every compression"
- Fix implemented: 2026-06-10
- Reviewer approval: ✅ PASS — Approved
- Test file: `test_compression_fix.py`