# UI Tab Stall Bug Fixes - Summary

**Date:** 2026-06-13  
**Issue:** UI sub-agent tab stall bug - sub-agent tabs would freeze when the main agent halted or during generation cycles.

## Changes Made

### Fix 1 (HIGH): Frontend drops ALL updates when instance_halted=True
**File:** `web_ui/app.js`  
**Line:** ~869-882

**Problem:** The `instance_halted` check was placed at the start of the `stream_update` handler, blocking ALL stream updates including sub-agent tab data.

**Solution:** Moved the `instance_halted` check to only guard the main chat history processing (where `state.messages` is updated), allowing sub-agent state updates to flow through regardless.

**Code Change:**
```javascript
// Before: if (state.instance_halted) break; at line 871

// After: Wrapped main chat history update in conditional
if (!state.instance_halted) {
  // Merge: keep stable history, replace streaming response tail
  if (historyCount <= state.messages.length) {
    state.messages.length = historyCount;
  }
  state.messages.push(...responseMsgs);
}
```

---

### Fix 2 (HIGH): `_last_sa_msg_counts` never cleaned up between generations
**File:** `api_server.py`  
**Line:** ~1403-1404

**Problem:** The `_last_sa_msg_counts` session key was not being cleared in the finally block, causing stale state to persist between generation cycles.

**Solution:** Added cleanup of `_last_sa_msg_counts` alongside other session cache keys.

**Code Change:**
```python
# FIX6: Clean up sub-agent message counts tracker to prevent staleness between generations
session.pop('_last_sa_msg_counts', None)
```

---

### Fix 3 (MEDIUM): `_last_sa_msg_counts` not reset on retry
**File:** `api_server.py`  
**Line:** ~1071

**Problem:** When the generation loop retried, `_last_sa_msg_counts` was not being reset, causing change detection to fail.

**Solution:** Added reset of `_last_sa_msg_counts` in the retry loop initialization.

**Code Change:**
```python
session.pop('_last_sa_msg_counts', None)  # Reset sub-agent message counts on retry
```

---

### Fix 4 (MEDIUM): Add periodic forced sub-agent state refresh
**File:** `api_server.py`  
**Line:** ~1171-1173

**Problem:** The change detection for sub-agent states relied on message count and content length tracking, which could miss some content changes if only string length was tracked.

**Solution:** Added periodic forced refresh every 20 ticks to ensure sub-agent state is recomputed even when change detection misses updates.

**Code Change:**
```python
# Force recompute sub-agent state every N ticks to prevent staleness from missed change detection.
# The _sa_changed flag relies on message count/content length tracking, which can miss some changes.
if _sa_changed or any_sa_active or tick_num % 20 == 0:
    sub_agents_cache = get_sub_agent_state(streaming=True)
```

---

### Fix 5 (LOW): Sender loop exception logging
**File:** `api_server.py`  
**Line:** ~1451-1452

**Problem:** The sender loop was silently swallowing exceptions with `pass`, making debugging difficult.

**Solution:** Added proper exception logging to capture sender loop errors.

**Code Change:**
```python
# Before: except Exception: pass

# After:
except Exception as e:
    logger.error(f"Sender loop exception: {e}")
```

---

## Testing

All compression tests pass after changes:
```
============================= 74 passed in 4.05s ==============================
```

## Files Modified

1. `N:\work\WD\AgentCascade\web_ui\app.js` - Fix 1
2. `N:\work\WD\AgentCascade\api_server.py` - Fixes 2, 3, 4, 5

## Impact

- Sub-agent tabs will now continue receiving updates even when the main agent halts
- Change detection for sub-agents is more reliable with periodic forced refresh
- Better debugging capability with sender loop exception logging
- No stale state between generation cycles or retries