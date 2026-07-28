# Root Cause Analysis: Approval Timeout Bug in Auto-Ask Mode

## Executive Summary

**Bug**: Approval timeout occurs even when explicitly disabled in options, when it was set on auto-ask mode.

**Root Cause**: The security advisor has a **hard-coded 180-second timeout** that runs independently of the user's approval timeout settings. When auto-ask mode is enabled, this security check timeout overrides the user's preference to disable approval timeouts.

## Key Findings

### 1. Approval Timeout Configuration

Location: `agent_cascade/operation_manager/approval.py`

```python
# Line 119
timeout_val = self.approval_timeout_seconds if self.enable_timeout else 3600
```

- When `enable_approval_timeout = true`: uses configured `approval_timeout_seconds` (default 300s)
- When `enable_approval_timeout = false`: uses 3600s (1 hour) — effectively "almost disabled"

**Config handlers** (`config_handlers.py:128-151`):
- `handle_approval_timeout()` → sets `operation_manager.approval_timeout_seconds`
- `handle_enable_approval_timeout()` → sets `operation_manager.enable_timeout`

### 2. Security Advisor Timeout (Hard-Coded)

Location: `agent_cascade/operation_manager/approval.py`

```python
# Lines 45-48
SECURITY_ADVISOR_TIMEOUT_SECONDS = 180   # 3 minutes — fixed constant
SECURITY_ADVISOR_WARNING_SECONDS = 120   # Warn at 2 minutes
```

**Critical**: This constant is **not configurable** and is imported directly by the security handler. It does NOT read from `operation_manager.enable_timeout` or `operation_manager.approval_timeout_seconds`.

### 3. Auto-Ask Mode Interaction

Location: `web_ui/app.js` (lines 2593-2598)

When a tool requires approval and auto-ask is enabled:
```javascript
if (pending.length > 0) {
    const ap = pending[0];
    state.activeSecurityChecks.add(ap.request_id);
    send({ type: 'ask_security', request_id: ap.request_id, auto_apply: true });
}
```

This triggers the security handler to run a background check with its own timeout.

### 4. Code Path to Bug

```
1. User sets enable_approval_timeout = false
   → operation_manager.enable_timeout = false
   → request_user_approval() uses timeout_val = 3600s

2. Auto-ask is ON (via UI toggle or --auto_security flag)
   → web UI sends 'ask_security' message
   → security_handler.run_check() spawns thread

3. Security thread uses fixed constant:
   args=(..., SECURITY_ADVISOR_TIMEOUT_SECONDS, ...)
   → timeout_seconds = 180

4. Security agent runs in background
   for resp in engine.run(sec_instance):
       elapsed = time.monotonic() - sec_start_time
       if elapsed > timeout_seconds:  # 180s
           sec_timeout_reached = True
           break

5. Timeout reached → _handle_timeout() calls user_reject()
   → approval.event.set() with rejected status
   → request_user_approval() returns (False, "SECURITY ADVISOR TIMEOUT")
```

## Root Causes Summary

| # | Cause | Location | Impact |
|---|-------|----------|--------|
| **RC1** | Security advisor timeout (180s) doesn't respect `enable_approval_timeout` | `security_handler.py:156` passes fixed constant | Overrides user's disabled timeout setting |
| **RC2** | Security advisor timeout doesn't respect `approval_timeout_seconds` either | Same — constant never overridden | Even when timeout is configured, security check still uses 180s |
| **RC3** | No timeout propagation from approval context to security handler | `run_check()` doesn't query operation manager settings | Two independent timeout mechanisms operate in isolation |

## Evidence

### Code References

- **Constants**: `operation_manager/approval.py:47-48` — 180s timeout, 120s warning
- **Approval wait logic**: `operation_manager/approval.py:119` — resolves to 3600s when disabled
- **Security check spawn**: `security_handler.py:152-159` — passes fixed constant to worker
- **Timeout check**: `security_handler.py:330-338` — breaks loop after 180s
- **Timeout rejection**: `security_handler.py:538` — calls `user_reject()` which kills the approval

### Configuration Flow

The UI settings are handled by `config_handlers.py`:
- `handle_approval_timeout()` sets `operation_manager.approval_timeout_seconds`
- `handle_enable_approval_timeout()` sets `operation_manager.enable_timeout`

However, the security handler (`security_handler.py`) never queries these values. It imports the fixed constant directly:

```python
from agent_cascade.operation_manager import (
    SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS,
)
```

## Impact Assessment

- **User Experience**: Users who explicitly disable approval timeouts still experience timeouts after 3 minutes when auto-ask is enabled.
- **Confusion**: The bug contradicts user expectations — "disabled" should mean no timeout (or very long timeout).
- **Consistency**: Two timeout mechanisms in the same system behave inconsistently.

## Recommended Fix

Propagate approval timeout settings to the security handler:

1. **Modify `security_handler.run_check()`** to query the operation manager's current settings:
   ```python
   enable_timeout = self.agent_pool.operation_manager.enable_timeout
   approval_timeout = self.agent_pool.operation_manager.approval_timeout_seconds
   
   # Use approval_timeout for security check as well, or respect disable setting
   if enable_timeout:
       timeout_seconds = approval_timeout
   else:
       timeout_seconds = 3600  # or some other large value
   ```

2. **Alternative**: Make `SECURITY_ADVISOR_TIMEOUT_SECONDS` configurable via environment variable (e.g., `SECURITY_ADVISOR_TIMEOUT_SECONDS`) and update the UI to control it.

## Conclusion

The bug is a clear case of **independent timeout mechanisms** that don't communicate. The security advisor's hard-coded 180-second timeout overrides the user's explicit preference to disable approval timeouts when auto-ask mode is enabled. The fix requires propagating the approval timeout settings to the security handler so that both mechanisms respect the same configuration.