# Grep Tool Tuple Unpacking Error - Investigation Report

## Executive Summary
The grep tool in AgentCascade crashes with "too many values to unpack (expected 4)" because `_try_subprocess_grep()` returns a 5-tuple, but the caller unpacks into 4 variables.

## Root Cause Analysis

### Problem Location
- **File:** `N:\work\WD\AgentCascade\agent_cascade\operation_manager\grep.py`
- **Line 411:** Unpacks 4 values from `_try_subprocess_grep()`

### Problematic Code (Line 411)
```python
results, count, was_timed_out, _sub_truncated = self._try_subprocess_grep(
    pattern=pattern, path=resolved, include=include,
    char_limit=char_limit, timeout=timeout,
    agent_name=agent_name,
    exclude=exclude, ignore_vcs=ignore_vcs, context=context, smart_case=smart_case,
    spill_file_path=spill_file_path
)
```

### Expected Return (5 values)
The `_try_subprocess_grep()` method returns 5 values:
1. `results_list` - list of formatted match lines
2. `count` - number of matches found
3. `was_timed_out` - boolean indicating timeout occurred
4. `was_truncated` - boolean indicating result truncation
5. `original_output_size` - total output size before truncation

### Return Statements (grep.py)
- Line 282: `return formatted, count, False, _was_truncated, _original_output_size`
- Line 286: `return [], 0, False, False, 0`
- Line 291: `return None, 0, False, False, 0`

## Impact
- Occurs when grep fast path (subprocess-based) is active
- Affects all grep tool invocations using ripgrep or system grep
- Crashes with Python ValueError: "too many values to unpack (expected 4)"

## Recommended Fix
**Option A (cleanest):** Update the unpacking to match
```python
results, count, was_timed_out, _sub_truncated, _original_output_size = self._try_subprocess_grep(...)
```

**Option B (if original_output_size is unused):** 
```python
results, count, was_timed_out, _sub_truncated, *_ = self._try_subprocess_grep(...)
```

## Files Modified
- `agent_cascade/operation_manager/grep.py` - Line 411

## Verification Steps
1. Reviewed `_try_subprocess_grep()` method and its return statements
2. Located the unpacking at line 411 in the `grep` method
3. Confirmed mismatch between 5 returned values and 4 expected variables

---
Report generated: AgentCascade grep error investigation