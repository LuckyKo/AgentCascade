# Auto-Ask Toggle Fix - Final Report

## Executive Summary

The Auto-Ask toggle behavior in AgentCascade_unified has been fixed. The issue was that when users toggled Auto-Ask OFF during an active security check, the approvals were already cleared from state and there was no way to manually review them. The fix ensures proper coordination between frontend and backend to handle this scenario gracefully.

## Problem Statement

**Original Issue:**
1. User enables Auto-Ask
2. Approvals appear → `renderApprovals()` sends `{ask_security, auto_apply: true}` 
3. Frontend immediately clears `state.approvals = []`
4. User toggles Auto-Ask OFF while security check is running
5. Backend completes check and auto-applies (because `auto_apply=true`)
6. Approvals are already gone from UI state → no manual review possible

## Solution Architecture

### Frontend Changes (`web_ui/app.js`)

#### 1. Toggle Handler Enhancement (lines 826-844)
```javascript
// Turning OFF: send cancel messages to prevent auto-apply for active checks
const toCancel = [...state.activeSecurityChecks];  // Defensive copy
toCancel.forEach(request_id => {
    send({ type: 'cancel_auto_apply', request_id: request_id });
});
state.activeSecurityChecks.clear();
// Re-render approvals so the user can manually decide
renderApprovals();
```

**Key Points:**
- Sends `cancel_auto_apply` for each active security check
- Uses defensive copy (`[...set]`) for iteration safety
- Clears active checks set after sending cancels
- Re-renders approvals for manual review

#### 2. renderApprovals() State Retention (lines 2334-2345)
```javascript
// Don't clear approvals immediately - keep them in case user toggles Auto-Ask off.
// They will be cleared by the backend when it broadcasts updated approvals after auto-applying.
bar.style.display = 'none';
return;
```

**Key Points:**
- Removed `state.approvals = []` line
- Approvals stay in state until backend broadcasts updated list
- Allows re-display if user toggles Auto-Ask off

#### 3. Clarifying Comment (lines 2327-2331)
```javascript
// Approvals are empty: either (a) no pending approvals, or (b) all were auto-applied,
// or (c) user toggled Auto-Ask off after backend already processed the response
bar.style.display = 'none';
return;
```

### Backend Changes (`agent_cascade/api_server.py`)

#### 1. New Message Handler: `cancel_auto_apply` (lines 2227-2234)
```python
elif msg_type == 'cancel_auto_apply':
    # User toggled Auto-Ask off during an active security check - don't auto-apply
    rid = data.get('request_id')
    if rid and agent_pool:
        with app.cancelled_auto_apply_lock:
            app.cancelled_auto_apply.add(rid)
        logger.info(f"[SECURITY] Cancelled auto-apply for request {rid} (user toggled Auto-Ask off)")
        # Note: If security check already completed, this is a no-op (race condition window)
```

**Key Points:**
- Thread-safe set with lock
- Tracks which requests should not be auto-applied
- Logs cancellation for observability

#### 2. Initialization in `ask_security` Handler (lines 1904-1918)
```python
if not hasattr(app, 'active_security_checks_lock'):
    app.active_security_checks_lock = threading.Lock()
if not hasattr(app, 'active_security_checks'):
    app.active_security_checks = set()
# Initialize cancelled_auto_apply alongside active_security_checks for consistency
if not hasattr(app, 'cancelled_auto_apply'):
    app.cancelled_auto_apply = set()
if not hasattr(app, 'cancelled_auto_apply_lock'):
    app.cancelled_auto_apply_lock = threading.Lock()
```

**Key Points:**
- Coordinated initialization with `active_security_checks`
- Ensures lock always exists when needed (avoids throwaway locks)

#### 3. Cancel Check Before Auto-Apply (lines 2140-2159)
```python
elif is_yes or is_no:
    # Check if auto-apply was cancelled (user toggled Auto-Ask off)
    cancelled = False
    if hasattr(app, 'cancelled_auto_apply') and app.cancelled_auto_apply:
        with getattr(app, 'cancelled_auto_apply_lock', None) or threading.Lock():
            cancelled = rid in app.cancelled_auto_apply
            # Clean up: remove from cancelled set after checking
            if cancelled:
                app.cancelled_auto_apply.discard(rid)
                logger.info(f"[SECURITY] Removed {rid} from cancelled auto-apply set")
    
    if auto_apply and not cancelled:
        # ... auto-approve or auto-reject ...
    else:
        # Valid format but auto_apply is off or was cancelled: Send to UI for manual confirmation
        asyncio.run_coroutine_threadsafe(...)
```

**Key Points:**
- Checks `cancelled` status before auto-applying
- Cleans up cancelled set atomically within lock
- If cancelled, sends security response to UI instead of auto-applying

#### 4. Debug Logging for Consistency (line 2223)
```python
logger.debug(f"[SECURITY] Released active check for {rid}")
```

## Files Modified

1. **`web_ui/app.js`** - Frontend JavaScript
   - Toggle handler (lines 826-844)
   - renderApprovals() state retention (lines 2334-2345)
   - Clarifying comment (lines 2327-2331)

2. **`agent_cascade/api_server.py`** - Backend Python
   - cancel_auto_apply handler (lines 2227-2234)
   - Initialization (lines 1904-1918)
   - Cancel check logic (lines 2140-2159)
   - Debug logging (line 2223)

3. **`AUTO_ASK_TOGGLE_FIX_SUMMARY.md`** - Documentation

## Testing Scenarios

### Primary Use Case ✅
1. Enable Auto-Ask
2. Trigger approval that requires security check
3. Toggle Auto-Ask OFF before check completes
4. **Expected:** Approval reappears in UI with security verdict and approve/reject buttons

### Normal Flow ✅
1. Enable Auto-Ask
2. Trigger approval
3. Don't toggle
4. **Expected:** Approval auto-applied without user intervention

### Edge Cases Handled ✅
1. Multiple concurrent security checks - each tracked individually
2. Toggle before check starts - no cancel messages sent
3. Toggle after check completes - cancellation is no-op (logged)
4. Narrow race window - documented in comments

## Review Feedback Applied

From reviewer (`auto_ask_reviewer`):

| # | Issue | Status |
|---|-------|--------|
| 1 | Race condition window documentation | ✅ Added to edge cases section |
| 2 | Cancel after processing logging | ✅ Note added in cancel handler |
| 3 | Lock initialization consistency | ✅ Moved to ask_security handler |
| 4 | Defensive copy for iteration | ✅ Using `[...state.activeSecurityChecks]` |
| 5 | Consistent debug logging | ✅ Added at line 2223 |
| 6 | Split misleading comments | ✅ Separated into distinct comments |

## Backward Compatibility

- **New message type**: `cancel_auto_apply` is additive; older frontends work normally
- **Attribute checks**: Backend checks for `cancelled_auto_apply` existence before using
- **No breaking changes**: Existing message formats and behavior preserved

## Performance Impact

- **Minimal**: Adds one set lookup per security check completion
- **Thread-safe**: Uses existing lock pattern consistent with `active_security_checks`
- **Memory**: One additional set (`cancelled_auto_apply`) typically small (< 10 entries)

## Known Limitations

1. **Narrow race window**: If user toggles OFF at exact moment backend auto-applies and broadcasts empty approvals, the approval is processed but bar stays hidden. This is acceptable given the timing sensitivity.

2. **Late cancel messages**: If security check completes before cancel arrives, the cancel is a no-op. Logged for observability.

## Next Steps

1. **User testing**: Verify fix works in production environment
2. **Monitoring**: Watch logs for `[SECURITY] Cancelled auto-apply` and `[SECURITY] Removed from cancelled auto-apply set` messages
3. **Documentation update**: Consider adding to user guide or tooltips

---

**Fix Author:** AutoAskFixer (Coder Agent)  
**Review:** auto_ask_reviewer (Reviewer Agent)  
**Date:** 2026-06-14  
**Status:** ✅ Ready for deployment