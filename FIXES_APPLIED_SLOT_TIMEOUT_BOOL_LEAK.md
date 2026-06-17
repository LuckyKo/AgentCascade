# Fixes Applied: Slot Timeout & Bool/List Leak

## Summary

Applied fixes for two critical issues based on deep debugging analysis and reviewer feedback.

---

## Fix 1: Slot Timeout Bug (CRITICAL)

### Root Cause
Line 2075 in `execution_engine.py` was setting `_slot_release = None` when reacquire failed after sync child execution. This destroyed the original slot reference, causing zombie slots that blocked all subsequent instances on sequential endpoints.

### Changes Made

**File: `agent_cascade/execution_engine.py`**

1. **Line ~2075**: Removed `slot_holder._slot_release = None` from `_reacquire_slot` helper
   - The finally block at line 624 handles cleanup via `_release_slot`
   - The closure's `_released` flag prevents double-release
   - If reacquire succeeds, it overwrites with new callback; if fails, original remains

2. **Lines ~2090, ~2119**: Updated comments to reflect the fix

### Before:
```python
def _reacquire_slot(slot_holder, slot_holder_name, context_label):
    # ... retry logic ...
    slot_holder._slot_release = None  # ← DESTROYS original reference
    return False
```

### After:
```python
def _reacquire_slot(slot_holder, slot_holder_name, context_label):
    """FIX SLOT_TIMEOUT: On failure, we no longer set _slot_release = None.
    The finally block at line 624 will release whatever slot is currently held.
    If reacquire succeeds, it overwrites with a new callback. If it fails,
    the old callback remains (if any) and the closure's _released flag prevents double-release."""
    # ... retry logic ...
    # Removed: slot_holder._slot_release = None
    return False
```

### Impact
- Fixes zombie slot issue where failed reacquire left slots unreleased
- Prevents cascade effect where one failed reacquire blocks ALL subsequent instances on that endpoint
- Explains why PortPlanReviewer was timing out - the original slot from a previous instance was never released

---

## Fix 2: Bool/List Leak (MAJOR)

### Root Cause
JSON parsing in `load_session_from_log` and LLM response processing were appending raw values (booleans, lists) to conversation history instead of only Message objects or dicts.

### Changes Made

**File: `agent_cascade/agent_pool.py`**

1. **Lines ~829-837**: Added type validation for JSONL file parsing
   - Only accepts `dict` items as message objects
   - Logs skipped non-dict entries (bool, list, str, etc.)

2. **Lines ~843-876**: Added type validation for inline JSON parsing  
   - Filters lists to only include dict messages
   - Logs filtered items count

3. **Lines ~878-890**: Added comprehensive filtering for JSON block parsing
   - Handles `list`, `dict with "history" key`, and standalone `dict` cases
   - Filters each case to only accept dict messages
   - Logs filtered items count

**File: `agent_cascade/execution_engine.py`**

4. **Lines ~563-574**: Added type validation before appending to `turn_output`
   - Only accepts `Message` or `dict` objects
   - Logs warning for non-Message types with preview of value

### Before (agent_pool.py):
```python
item = json.loads(line)
if "metadata" in item:
    metadata.update(item["metadata"])
else:
    messages.append(item)  # ← Could append True/False, [], etc.
```

### After (agent_pool.py):
```python
item = json.loads(line)
# FIX BOOL_LEAK: Only accept dict as message objects from JSONL
if isinstance(item, dict):
    if "metadata" in item:
        metadata.update(item["metadata"])
    else:
        messages.append(item)
else:
    logger.debug(f"load_session_from_log: skipping non-dict JSONL entry of type {type(item).__name__}")
```

### Before (execution_engine.py):
```python
for msg in self._call_llm_with_injection(instance, llm_messages):
    if msg is None:
        yield (response + turn_output + partial_msgs, True)
        continue
    turn_output.append(msg)  # ← Could append any type
```

### After (execution_engine.py):
```python
for msg in self._call_llm_with_injection(instance, llm_messages):
    if msg is None:
        yield (response + turn_output + partial_msgs, True)
        continue
    # FIX BOOL_LEAK: Validate message type before appending
    if isinstance(msg, (Message, dict)):
        turn_output.append(msg)
    else:
        logger.warning(f"[MSG_VALIDATION] Skipping non-Message in LLM response for {instance.instance_name}: type={type(msg).__name__}, value={str(msg)[:100]}")
```

### Impact
- Prevents `True`/`False` booleans from appearing in conversation history
- Prevents raw lists from being extended into conversation  
- Provides debug logging to track filtered items
- Fixes the "skipping unexpected bool/list item" warnings in `get_history_stats`

---

## Files Modified

1. **agent_cascade/execution_engine.py** (4 changes)
   - Line ~2075: Removed `_slot_release = None` on failed reacquire
   - Lines ~2090, ~2119: Updated comments
   - Lines ~574: Added message type validation

2. **agent_cascade/agent_pool.py** (3 changes)
   - Lines ~829-837: JSONL file parsing validation
   - Lines ~843-876: Inline JSON parsing validation  
   - Lines ~878-890: JSON block parsing validation

---

## Testing Recommendations

1. **Test slot timeout fix**:
   - Run workflow that triggers sync call_agent with potential reacquire failure
   - Verify no "Failed to acquire endpoint slot" timeout errors
   - Check logs for proper slot release messages

2. **Test bool/list leak fix**:
   - Load session from JSONL log file with mixed content
   - Verify no "skipping unexpected bool/list item" warnings
   - Check that conversation only contains Message objects or dicts

3. **Regression testing**:
   - Test normal agent execution paths
   - Test async and sync call_agent paths
   - Test SLEEPING → RUNNING transitions
   - Test error handling in all modified code paths

---

## Related Documents

- `.agent_lessons/lessons_slot_timeout_debug.md` - Detailed investigation notes
- `SLOT_TIMEOUT_AND_BOOL_LEAK_FIX.md` - Initial fix analysis
- Reviewer feedback from `slot_debug_reviewer` agent

---

## Next Steps

1. ✅ Apply fixes (DONE)
2. ⏳ Test fixes with actual workflow
3. ⏳ Monitor logs for slot timeout and bool/list leak messages
4. ⏳ If issues persist, add more detailed debug logging per reviewer's suggestions