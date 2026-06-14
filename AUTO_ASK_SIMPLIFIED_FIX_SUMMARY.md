# Auto-Ask Toggle Fix - Simplified Approach

## Executive Summary

The Auto-Ask toggle behavior has been simplified from a per-request cancellation model to a simple boolean state check. Instead of tracking which specific requests should be cancelled, we now just check if Auto-Ask is still enabled when the security check completes.

## Problem Statement (Same as Before)

When users toggled Auto-Ask OFF during an active security check, approvals were auto-applied regardless because:
1. Frontend cleared `state.approvals` immediately when sending `ask_security` with `auto_apply=true`
2. No mechanism existed to tell the backend "don't auto-apply"

## Simplified Solution

### Core Idea
Store the current Auto-Ask toggle state as a simple boolean on the server side (`app.current_auto_security`). Check this boolean right before auto-applying — if it's `False`, send to UI for manual confirmation instead.

### What Was Removed (Overengineering)
- Per-request `cancelled_auto_apply` set
- Locks for thread-safe set access  
- `cancel_auto_apply` messages from frontend to backend
- Defensive copies in JavaScript
- Cleanup logic for cancelled requests

### What Remains (Good Defensive Change)
- Frontend keeps approvals in state (doesn't clear immediately) — this is a good defensive change that should stay

## Changes Made

### Frontend (`web_ui/app.js`)

#### Toggle Handler (lines 826-834) - **Simplified**

**Before (complex):**
```javascript
if (autoSecurityToggle.checked) {
    // Turning ON: trigger renderApprovals which will launch security checks for pending approvals
    renderApprovals();
} else {
    // Turning OFF: send cancel messages to prevent auto-apply for active checks
    const toCancel = [...state.activeSecurityChecks];  // Defensive copy for iteration safety
    toCancel.forEach(request_id => {
        send({ type: 'cancel_auto_apply', request_id: request_id });
    });
    state.activeSecurityChecks.clear();
    // Re-render approvals so the user can manually decide
    renderApprovals();
}
```

**After (simple):**
```javascript
// Notify backend of toggle change and re-render approvals
send({ type: 'set_auto_security', enabled: state.autoSecurity });
renderApprovals();
```

**Lines saved:** ~10 lines

#### renderApprovals() - **Unchanged (good)**

The change to NOT clear `state.approvals` immediately remains. This is a good defensive change that allows approvals to be re-shown if the user toggles Auto-Ask off.

### Backend (`agent_cascade/api_server.py`)

#### 1. Removed: cancelled_auto_apply Initialization (was lines 1908-1912)

**Removed:**
```python
# Initialize cancelled_auto_apply alongside active_security_checks for consistency
if not hasattr(app, 'cancelled_auto_apply'):
    app.cancelled_auto_apply = set()
if not hasattr(app, 'cancelled_auto_apply_lock'):
    app.cancelled_auto_apply_lock = threading.Lock()
```

#### 2. Simplified: Auto-Apply Check (lines 2135-2139)

**Before (complex):**
```python
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
```

**After (simple):**
```python
# Check if Auto-Ask is still enabled BEFORE auto-applying
auto_ask_still_on = getattr(app, 'current_auto_security', True)

if auto_apply and auto_ask_still_on:
```

**Lines saved:** ~10 lines

#### 3. Replaced: cancel_auto_apply Handler → set_auto_security Handler (lines 2215-2218)

**Before (per-request tracking):**
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

**After (boolean state):**
```python
elif msg_type == 'set_auto_security':
    # User toggled Auto-Ask on/off — store current state for security checks to reference
    enabled = data.get('enabled', False)
    app.current_auto_security = enabled
```

**Lines saved:** ~5 lines

## Net Change

- **Frontend:** -10 lines (simplified toggle handler)
- **Backend:** -20 lines (removed set/locks, simplified logic)
- **Total:** ~30 lines removed, ~5 lines added = **~25 lines net reduction**

## How It Works

### Normal Flow (Auto-Ask ON throughout):
1. User has Auto-Ask ON → `app.current_auto_security = True` (default or set by message)
2. Approval appears → frontend sends `{ask_security, auto_apply: true}`
3. Backend runs security check
4. Security agent responds with verdict
5. Backend checks `auto_ask_still_on` → `True`
6. Backend auto-applies the decision ✓

### Toggle OFF During Check Flow (Fixed):
1. User has Auto-Ask ON
2. Approval appears → frontend sends `{ask_security, auto_apply: true}`
3. **User toggles Auto-Ask OFF**
4. Frontend sends `{set_auto_security, enabled: false}` to backend
5. Backend sets `app.current_auto_security = False`
6. Backend security check completes
7. Backend checks `auto_ask_still_on` → `False`
8. Backend sends `{security_response, ...}` to UI instead of auto-applying ✓
9. UI shows approval card with security verdict and approve/reject buttons

## Advantages of Simplified Approach

1. **Fewer moving parts**: No sets, no locks, no per-request tracking
2. **Easier to understand**: Boolean check is intuitive
3. **Less code**: ~25 lines removed
4. **No cleanup needed**: Don't need to remove items from cancelled set
5. **Global state makes sense**: Auto-Ask is a global toggle, not per-request

## Trade-offs

### What We Lost (Acceptable)
- Per-request granularity: Can't cancel individual requests, only global toggle
- Detailed logging: No "Cancelled auto-apply for request X" messages
- Defensive copy safety: JavaScript iteration without copy (but single-threaded, so safe)

### What We Gained (Valuable)
- Simplicity: Easier to maintain and debug
- Performance: Boolean check vs set lookup + lock acquisition
- Readability: Code is more self-documenting

## Testing Scenarios (Same as Before)

1. **Primary use case**: Toggle OFF during check → approval appears for manual review ✓
2. **Normal flow**: Don't toggle → auto-applies without intervention ✓
3. **Toggle before check starts**: No active checks, nothing to cancel ✓
4. **Toggle after check completes**: Boolean already checked, no-op ✓

## Backward Compatibility

- New message type `set_auto_security` is additive
- Backend defaults `current_auto_security` to `True` if not set (graceful degradation)
- No breaking changes to existing behavior

## Files Modified

1. **`web_ui/app.js`** - Simplified toggle handler (~10 lines removed)
2. **`agent_cascade/api_server.py`** - Removed cancelled infrastructure, added boolean check (~25 lines removed)

---

**Simplification Author:** AutoAskFixer (Coder Agent)  
**Date:** 2026-06-14  
**Status:** ✅ Simplified and ready for deployment