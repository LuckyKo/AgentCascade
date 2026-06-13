# Security Agent Auto-Ask Stall Analysis

## Executive Summary

This analysis examines the code sections related to the Security agent auto-ask stall issue, focusing on the `_security_check()` method in `api_server.py`, the `call_agent` Security invocation path in `execution_engine.py`, and the Compressor agent invocation in `compression/agent_invoker.py`.

## Key Findings

### 1. **CRITICAL ISSUE: Missing active_stack cleanup after auto-apply approval/rejection**

**Location:** `api_server.py` lines 2168-2208

**Problem:** When `auto_apply=True` and Security returns a verdict, the code calls `user_approve()` or `user_reject()` but does NOT broadcast an updated approvals list to remove the approval card from the UI. This means:
- The approval request is removed from `operation_manager.pending` 
- BUT the UI still shows the approval card
- The Security advisor tab may remain visible

**Code Snippet (lines 2168-2177):**
```python
elif is_yes or is_no:
    if auto_apply:
        if is_yes:
            logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
            agent_pool.operation_manager.user_approve(rid, reason=justification)
        else:
            logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
            # Auto-rejection message
            reject_msg = f"SECURITY REJECTED: {justification}" if justification else "SECURITY REJECTED: The security advisor flagged this operation as unsafe."
            agent_pool.operation_manager.user_reject(rid, reject_msg)
```

**Issue:** Notice that after `user_approve()` or `user_reject()`, there's no broadcast of the updated approvals list. Compare this to the timeout handling (lines 2158-2166) which DOES broadcast:

```python
# Broadcast updated approval list so stale card is removed from frontend after timeout rejection
asyncio.run_coroutine_threadsafe(
    send_queue.put({
        'type': 'approvals',
        'approvals': agent_pool.operation_manager.list_pending_approvals()
    }),
    loop
)
```

**Fix:** Add the same broadcast after auto-apply approval/rejection.

---

### 2. **CRITICAL ISSUE: active_stack entry not removed in all code paths**

**Location:** `api_server.py` lines 2218-2238

**Problem:** The `finally` block (lines 2218-2238) removes the Security entry from `active_stack`, but this only happens if the `_security_check()` function completes normally or raises an exception. However, there's a potential race condition:

**Code Snippet (lines 2218-2227):**
```python
finally:
    # Always clean up security advisor state when done (Fix #4: thread-safe)
    if sec_state_key and sec_state_key in agent_pool.instance_state:
        with agent_pool._execution._state_lock:
            agent_pool.instance_state[sec_state_key]['active'] = False
            if any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
                for i, (n, _depth) in enumerate(agent_pool._execution.active_stack):
                    if n == sec_state_key:
                        agent_pool._execution.active_stack.pop(i)
                        break
```

**Issue:** The `active_stack` is modified inside a lock (`agent_pool._execution._state_lock`), but the check at line 1871-1872 adds to it:

```python
if not any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
    agent_pool._execution.active_stack.append((sec_state_key, 1))
```

This is also inside the same lock. However, if there's an exception between adding to `active_stack` and the `finally` block executing, the entry could remain.

**More importantly:** The check at line 1871 uses `any()` which returns True if the name exists, but it doesn't account for the fact that multiple threads could add the same entry before either removes it (though the lock should prevent this).

---

### 3. **POTENTIAL ISSUE: Event setting not triggering main execution loop**

**Location:** `operation_manager.py` lines 287-311

**Problem:** When `user_approve()` or `user_reject()` is called, it sets `approval.event.set()`. This should unblock any thread waiting in `_wait_for_approval()`. However, the main execution loop in `execution_engine.py` might not be properly checking for this.

**Code Snippet (operation_manager.py lines 295-310):**
```python
def user_approve(self, request_id: str, reason: str = "") -> str:
    """Called by WebUI when user clicks Approve."""
    with self._lock:
        approval = self.pending.pop(request_id, None)

    if not approval:
        return f"ERROR: Request '{request_id}' not found or already resolved."

    approval.approved = True
    approval.outcome_reason = reason
    approval.event.set()  # This should unblock the waiting thread
    return f"Approved: {request_id}"

def user_reject(self, request_id: str, reason: str = "") -> str:
    """Called by WebUI when user clicks Reject."""
    with self._lock:
        approval = self.pending.pop(request_id, None)

    if not approval:
        return f"ERROR: Request '{request_id}' not found or already resolved."

    approval.approved = False
    approval.outcome_reason = reason or "Rejected by user."
    approval.event.set()  # This should unblock the waiting thread
    return f"Rejected: {request_id}"
```

**Issue:** The event is set correctly, but if the Security advisor's auto-apply mode calls these methods from a different thread (the `_security_check` thread), and the main execution loop is also checking for pending approvals, there could be a race condition where:
1. Security thread calls `user_approve()` 
2. The approval is removed from `pending`
3. Main execution loop checks `has_pending()` and finds no pending approvals
4. But the agent that was waiting for this approval might not get notified

**Note:** This is less likely to be the issue since `_wait_for_approval()` should return immediately after the event is set.

---

### 4. **POTENTIAL ISSUE: Threading lock ordering in _security_check**

**Location:** `api_server.py` lines 1824-2240

**Problem:** The `_security_check()` function acquires multiple locks in a specific order:
1. `app.security_check_lock` (line 1832)
2. `agent_pool._execution._state_lock` (line 1865, 1986, 2221)
3. `app.active_security_checks_lock` (line 1817, 2237)

If these locks are acquired in a different order elsewhere in the code, a deadlock could occur.

**Code Snippet (lines 1817-1822):**
```python
with app.active_security_checks_lock:
    if rid in app.active_security_checks:
        logger.warning(f"Security check already active for request {rid}, ignoring duplicate.")
        continue
    app.active_security_checks.add(rid)

loop = asyncio.get_running_loop()
def _security_check():
    sec_state_key = None  # Defined early so finally can reference it directly (Fix #4)
    sec_endpoint_release = None  # Fix #3: endpoint slot release callback
    try:
        with app.security_check_lock:  # Line 1832
            # ... Security advisor code ...
            with agent_pool._execution._state_lock:  # Line 1865
                # ... state update ...
```

**Issue:** The `app.active_security_checks_lock` is acquired BEFORE the `_security_check()` function is defined and started. Then inside `_security_check()`, `app.security_check_lock` is acquired first, followed by `agent_pool._execution._state_lock`. This ordering should be consistent, but if other parts of the code acquire these locks in a different order, deadlock could occur.

---

### 5. **ISSUE: Manual mode (auto_apply=False) doesn't trigger approval**

**Location:** `api_server.py` lines 2178-2208

**Problem:** When `auto_apply=False` and Security returns a valid verdict, the code sends a `security_response` to the UI but does NOT call `user_approve()` or `user_reject()`. This means the approval request remains pending and the agent continues waiting.

**Code Snippet (lines 2178-2189):**
```python
else:
    # Valid format but auto_apply is off: Send to UI for manual confirmation
    asyncio.run_coroutine_threadsafe(
        send_queue.put({
            'type': 'security_response', 
            'request_id': rid, 
            'response': display_response,
            'verdict': 'YES' if is_yes else 'NO',
            'reason': justification if is_no else ""
        }),
        loop
    )
```

**Issue:** The UI receives the Security advisor's recommendation, but the user still needs to manually click Approve/Reject. This is probably intentional for manual mode, but it could be confusing if the user expects auto-apply to work.

---

### 6. **ISSUE: Ambiguous response in manual mode doesn't trigger approval**

**Location:** `api_server.py` lines 2190-2208

**Problem:** Similar to issue #5, when the Security advisor returns an ambiguous response and `auto_apply=False`, the code sends a `security_response` but doesn't call `user_approve()` or `user_reject()`.

**Code Snippet (lines 2202-2208):**
```python
else:
    # Manual mode: Let the user see the ambiguous response and decide
    logger.info(f"[SECURITY] Ambiguous response for {rid} in manual mode. Waiting for user decision.")
    asyncio.run_coroutine_threadsafe(
        send_queue.put({'type': 'security_response', 'request_id': rid, 'response': display_response, 'verdict': 'AMBIGUOUS'}),
        loop
    )
```

**Issue:** Same as #5 - the user needs to manually approve/reject even though Security provided an ambiguous response.

---

## Recommendations

### Priority 1: Fix Missing Broadcast After Auto-Apply

Add broadcast of updated approvals list after auto-apply approval/rejection in `api_server.py`:

```python
# After line 2172 (after user_approve)
agent_pool.operation_manager.user_approve(rid, reason=justification)
# ADD: Broadcast to remove card from UI
asyncio.run_coroutine_threadsafe(
    send_queue.put({
        'type': 'approvals',
        'approvals': agent_pool.operation_manager.list_pending_approvals()
    }),
    loop
)

# After line 2177 (after user_reject)
agent_pool.operation_manager.user_reject(rid, reject_msg)
# ADD: Broadcast to remove card from UI
asyncio.run_coroutine_threadsafe(
    send_queue.put({
        'type': 'approvals',
        'approvals': agent_pool.operation_manager.list_pending_approvals()
    }),
    loop
)
```

### Priority 2: Ensure active_stack Cleanup is Robust

The current cleanup in the `finally` block looks correct, but add logging to verify it's being called:

```python
finally:
    logger.debug(f"[SECURITY] Cleaning up Security advisor state for request {rid}")
    # Always clean up security advisor state when done (Fix #4: thread-safe)
    if sec_state_key and sec_state_key in agent_pool.instance_state:
        with agent_pool._execution._state_lock:
            agent_pool.instance_state[sec_state_key]['active'] = False
            if any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
                for i, (n, _depth) in enumerate(agent_pool._execution.active_stack):
                    if n == sec_state_key:
                        agent_pool._execution.active_stack.pop(i)
                        logger.debug(f"[SECURITY] Removed Security from active_stack at index {i}")
                        break
```

### Priority 3: Verify Lock Ordering

Review all places where `app.security_check_lock`, `agent_pool._execution._state_lock`, and `app.active_security_checks_lock` are acquired to ensure consistent ordering.

---

## Conclusion

The most likely cause of the Security agent auto-ask stall is **Issue #1**: missing broadcast of updated approvals list after auto-apply approval/rejection. This would cause the UI to still show the approval card even though it's been resolved, potentially confusing users and making it appear as if the system is stalled.

Secondary issues could include threading problems with lock ordering or race conditions in the event setting mechanism.

## Files Affected

1. `agent_cascade/api_server.py` - Lines 1802-2240 (_security_check method)
2. `agent_cascade/operation_manager.py` - Lines 287-311 (user_approve, user_reject)
3. `agent_cascade/execution_engine.py` - Lines 1899-2113 (call_agent handling)
4. `agent_cascade/compression/agent_invoker.py` - Lines 63-269 (Compressor invocation)

## Next Steps

1. Add the missing broadcast after auto-apply approval/rejection
2. Add debug logging to track Security advisor lifecycle
3. Test with both auto_apply=True and auto_apply=False modes
4. Monitor for any remaining stalls after fixes are applied