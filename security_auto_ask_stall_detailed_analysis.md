# Security Agent Auto-Ask Stall - Detailed Technical Analysis

## Overview

This document provides a detailed technical analysis of the Security agent auto-ask stall issue, examining the specific code paths and identifying potential blocking points.

## Code Path Analysis

### 1. Entry Point: ask_security Message Handler

**File:** `api_server.py`  
**Lines:** 1802-1822

```python
elif msg_type == 'ask_security':
    if not hasattr(app, 'security_check_lock'):
        app.security_check_lock = threading.Lock()
        
    rid = data.get('request_id')
    auto_apply = data.get('auto_apply', False)
    if rid and agent_pool:
        pending = agent_pool.operation_manager.list_pending_approvals()
        ap = next((a for a in pending if a['request_id'] == rid), None)
        if ap:
            if not hasattr(app, 'active_security_checks_lock'):
                app.active_security_checks_lock = threading.Lock()
            if not hasattr(app, 'active_security_checks'):
                app.active_security_checks = set()
                
            with app.active_security_checks_lock:
                if rid in app.active_security_checks:
                    logger.warning(f"Security check already active for request {rid}, ignoring duplicate.")
                    continue
                app.active_security_checks.add(rid)
```

**Analysis:**
- The handler receives an `ask_security` message with a `request_id` and `auto_apply` flag
- It checks if the request is still pending in `operation_manager`
- It uses a lock to prevent duplicate Security checks for the same request
- **Key Point:** The `rid` is added to `app.active_security_checks` to track active checks

---

### 2. Security Check Thread Launch

**File:** `api_server.py`  
**Lines:** 1823-1824, 2240

```python
loop = asyncio.get_running_loop()
def _security_check():
    # ... Security advisor code ...
    
# Line 2240: Thread is started here
threading.Thread(target=_security_check, daemon=True).start()
```

**Analysis:**
- The `_security_check()` function is defined as a nested function
- It's launched in a separate daemon thread
- **Key Point:** This thread runs independently of the main event loop
- **Potential Issue:** If this thread blocks or hangs, it won't affect the main loop, but the Security advisor state might not be cleaned up properly

---

### 3. Security Advisor State Setup

**File:** `api_server.py`  
**Lines:** 1863-1877

```python
# Register security advisor in instance_state so it shows a tab (Fix #4: thread-safe)
sec_state_key = 'Security'
with agent_pool._execution._state_lock:
    agent_pool.instance_state[sec_state_key] = {
        'active': True,
        'agent_name': f"Security Advisor (Security)",
        'messages': list(history),
    }
    if not any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
        agent_pool._execution.active_stack.append((sec_state_key, 1))
# Broadcast initial state so the tab appears immediately
asyncio.run_coroutine_threadsafe(
    send_queue.put({'type': 'stream_update', **build_stream_update([])}),
    loop
)
```

**Analysis:**
- Security advisor state is registered in `agent_pool.instance_state`
- Entry is added to `active_stack` with depth 1
- Initial state is broadcast to UI
- **Key Point:** The `active_stack` entry is added under lock, ensuring thread safety
- **Potential Issue:** If an exception occurs before the `finally` block, this entry might not be removed

---

### 4. LLM Call Execution

**File:** `api_server.py`  
**Lines:** 1898-2005

```python
# Fix #2: Route LLM call through API router instead of direct sec_agent.run()
api_router_sec = getattr(agent_pool, 'api_router', None)

if api_router_sec and hasattr(api_router_sec, 'call_with_fallback'):
    def _security_llm_call(llm_cfg: dict):
        # ... LLM call setup ...
        return sec_agent.llm.chat(...)
    
    run_gen = api_router_sec.call_with_fallback('Security', _security_llm_call)
else:
    # Fallback: direct LLM call
    run_gen = sec_agent.llm.chat(...)

# Line 1971-2005: Generator iteration with timeout
for partial in run_gen:
    # Check timeout
    elapsed = time.monotonic() - sec_start_time
    if elapsed > SECURITY_ADVISOR_TIMEOUT_SECONDS:
        sec_timeout_reached = True
        sec_elapsed_at_timeout = elapsed
        logger.warning(...)
        break
    
    final_msgs = partial
    # Update instance_state with lock protection
    with agent_pool._execution._state_lock:
        agent_pool.instance_state[sec_state_key]['messages'] = (...)
```

**Analysis:**
- LLM call is made through API router (or direct fallback)
- Generator is iterated with timeout protection
- **Key Point:** The generator is properly closed in the `finally` block (line 2001-2004)
- **Potential Issue:** If the LLM call hangs beyond the timeout, the generator is closed but the HTTP connection might not be immediately terminated

---

### 5. Response Parsing

**File:** `api_server.py`  
**Lines:** 2006-2123

```python
display_response = ""
parsing_response = ""

new_msgs = final_msgs
for msg in new_msgs:
    # ... extract content, reasoning, function_call ...
    if role == 'assistant':
        # Deduplicate thinking blocks
        # ... complex parsing logic ...
        if content_str:
            parsing_response = strip_thinking_blocks(content_str).strip()

# Lines 2063-2123: Verdict extraction
clean_text = parsing_response
# ... remove thinking blocks ...
lines = [l.strip() for l in clean_text.split('\n') if l.strip()]
last_line = lines[-1] if lines else ""

is_yes = last_line_upper.startswith('[YES]')
is_no = last_line_upper.startswith('[NO]')
```

**Analysis:**
- Response is parsed to extract the Security advisor's verdict
- Multiple fallback strategies are used to find [YES] or [NO]
- **Key Point:** If parsing fails, `is_yes` and `is_no` remain False
- **Potential Issue:** Complex parsing logic could have edge cases that miss valid verdicts

---

### 6. Timeout Handling

**File:** `api_server.py`  
**Lines:** 2124-2167

```python
# Handle security advisor timeout
if sec_timeout_reached:
    elapsed = sec_elapsed_at_timeout
    logger.info(f"[SECURITY] Timeout after {elapsed:.0f}s for request {rid}. Auto-rejecting...")
    
    # Halt the security advisor instance
    agent_pool.halt_instance('Security')
    
    # Reject the approval
    reject_msg = "SECURITY ADVISOR TIMEOUT: ..."
    agent_pool.operation_manager.user_reject(rid, reject_msg)
    
    # Notify UI about timeout
    asyncio.run_coroutine_threadsafe(
        send_queue.put({
            'type': 'security_response',
            'request_id': rid,
            'response': response_text,
            'verdict': 'TIMEOUT'
        }),
        loop
    )
    
    # Broadcast updated approval list so stale card is removed
    asyncio.run_coroutine_threadsafe(
        send_queue.put({
            'type': 'approvals',
            'approvals': agent_pool.operation_manager.list_pending_approvals()
        }),
        loop
    )
```

**Analysis:**
- Timeout triggers auto-rejection
- Security advisor instance is halted
- **Key Point:** Approval list is broadcast to UI to remove the card
- **This is the CORRECT pattern that should be followed in other code paths**

---

### 7. Auto-Apply Handling (THE BUG)

**File:** `api_server.py`  
**Lines:** 2168-2177

```python
elif is_yes or is_no:
    if auto_apply:
        if is_yes:
            logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
            agent_pool.operation_manager.user_approve(rid, reason=justification)
            # MISSING: Broadcast updated approvals list!
        else:
            logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
            reject_msg = f"SECURITY REJECTED: {justification}" if justification else "..."
            agent_pool.operation_manager.user_reject(rid, reject_msg)
            # MISSING: Broadcast updated approvals list!
```

**Analysis:**
- When `auto_apply=True` and verdict is clear, approval/rejection is called
- **BUG:** No broadcast of updated approvals list to UI
- **Consequence:** UI still shows the approval card even though it's resolved
- **This is likely the main cause of the "stall" appearance**

---

### 8. Manual Mode Handling

**File:** `api_server.py`  
**Lines:** 2178-2208

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
# Ambiguous response handling (lines 2190-2208)
else:
    if auto_apply:
        # Auto-reject ambiguous responses
        agent_pool.operation_manager.user_reject(rid, reject_msg)
        # Notify UI
        asyncio.run_coroutine_threadsafe(...)
    else:
        # Manual mode: Let user decide
        asyncio.run_coroutine_threadsafe(...)
```

**Analysis:**
- When `auto_apply=False`, Security advisor's response is sent to UI but approval is not auto-applied
- User must manually click Approve/Reject
- **This is intentional behavior for manual mode**

---

### 9. Cleanup in Finally Block

**File:** `api_server.py`  
**Lines:** 2218-2238

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
    
    # Fix #3: Release endpoint slot when done
    if sec_endpoint_release is not None:
        try:
            sec_endpoint_release()
        except Exception as e:
            logger.warning(f"Failed to release endpoint slot for Security: {e}")
    
    if hasattr(app, 'active_security_checks') and rid:
        with app.active_security_checks_lock:
            app.active_security_checks.discard(rid)
```

**Analysis:**
- State cleanup happens in `finally` block
- `active_stack` entry is removed under lock
- Endpoint slot is released
- `rid` is removed from `active_security_checks`
- **Key Point:** This should execute even if exceptions occur
- **Potential Issue:** If the thread crashes before reaching `finally`, cleanup might not happen

---

## Root Cause Analysis

### Primary Issue: Missing UI Broadcast After Auto-Apply

**Symptom:** After Security advisor auto-applies an approval/rejection, the UI still shows the approval card.

**Root Cause:** Lines 2168-2177 call `user_approve()` or `user_reject()` but don't broadcast the updated approvals list to the UI.

**Impact:**
1. User sees the approval card still pending
2. User might think the system is stalled
3. User might try to approve/reject again, getting "already resolved" error

**Fix:** Add broadcast after auto-apply, matching the timeout handling pattern.

---

### Secondary Issue: Potential Race Condition in active_stack Management

**Symptom:** Security advisor tab might remain visible after completion.

**Root Cause:** The `active_stack` entry is added at line 1872 and removed in the `finally` block at line 2224-2226. If an exception occurs between these points and the `finally` block doesn't execute properly, the entry could remain.

**Impact:**
1. Security advisor tab remains in UI
2. `active_stack` grows with stale entries
3. Memory leak over time

**Fix:** Add logging to verify cleanup, ensure `finally` block always executes.

---

### Tertiary Issue: Thread Safety of Lock Ordering

**Symptom:** Occasional deadlocks or hangs.

**Root Cause:** Multiple locks are acquired in `_security_check()`:
1. `app.security_check_lock` (line 1832)
2. `agent_pool._execution._state_lock` (line 1865, 1986, 2221)
3. `app.active_security_checks_lock` (line 1817, 2237)

If these locks are acquired in different orders elsewhere, deadlock could occur.

**Impact:** Intermittent hangs that are hard to reproduce.

**Fix:** Document lock ordering, review all code paths that acquire these locks.

---

## Recommended Fixes

### Fix 1: Add Broadcast After Auto-Apply (HIGH PRIORITY)

**File:** `api_server.py`  
**Location:** After lines 2172 and 2177

```python
# After line 2172
elif is_yes or is_no:
    if auto_apply:
        if is_yes:
            logger.info(f"[SECURITY] Automatic Approval for {rid} with justification: {justification[:50]}...")
            agent_pool.operation_manager.user_approve(rid, reason=justification)
            # ADD BROADCAST:
            asyncio.run_coroutine_threadsafe(
                send_queue.put({
                    'type': 'approvals',
                    'approvals': agent_pool.operation_manager.list_pending_approvals()
                }),
                loop
            )
        else:
            logger.info(f"[SECURITY] Automatic Rejection for {rid} with reason: {justification[:50]}...")
            reject_msg = f"SECURITY REJECTED: {justification}" if justification else "SECURITY REJECTED: The security advisor flagged this operation as unsafe."
            agent_pool.operation_manager.user_reject(rid, reject_msg)
            # ADD BROADCAST:
            asyncio.run_coroutine_threadsafe(
                send_queue.put({
                    'type': 'approvals',
                    'approvals': agent_pool.operation_manager.list_pending_approvals()
                }),
                loop
            )
```

### Fix 2: Add Debug Logging for Cleanup (MEDIUM PRIORITY)

**File:** `api_server.py`  
**Location:** Lines 2218-2238

```python
finally:
    logger.debug(f"[SECURITY] Cleanup starting for request {rid}, sec_state_key={sec_state_key}")
    
    # Always clean up security advisor state when done (Fix #4: thread-safe)
    if sec_state_key and sec_state_key in agent_pool.instance_state:
        with agent_pool._execution._state_lock:
            agent_pool.instance_state[sec_state_key]['active'] = False
            logger.debug(f"[SECURITY] Set Security active=False")
            if any(n == sec_state_key for n, _depth in agent_pool._execution.active_stack):
                for i, (n, _depth) in enumerate(agent_pool._execution.active_stack):
                    if n == sec_state_key:
                        agent_pool._execution.active_stack.pop(i)
                        logger.debug(f"[SECURITY] Removed Security from active_stack at index {i}")
                        break
            else:
                logger.warning(f"[SECURITY] Security not found in active_stack during cleanup")
    
    # Fix #3: Release endpoint slot when done
    if sec_endpoint_release is not None:
        try:
            sec_endpoint_release()
            logger.debug(f"[SECURITY] Released endpoint slot")
        except Exception as e:
            logger.warning(f"Failed to release endpoint slot for Security: {e}")
    
    if hasattr(app, 'active_security_checks') and rid:
        with app.active_security_checks_lock:
            app.active_security_checks.discard(rid)
            logger.debug(f"[SECURITY] Removed {rid} from active_security_checks")
    
    logger.debug(f"[SECURITY] Cleanup complete for request {rid}")
```

### Fix 3: Verify Lock Ordering (LOW PRIORITY)

**Action:** Search for all places where these locks are acquired and ensure consistent ordering:
- `app.security_check_lock`
- `agent_pool._execution._state_lock`
- `app.active_security_checks_lock`

**Recommended Order:** Always acquire in this order:
1. `app.active_security_checks_lock` (outermost)
2. `app.security_check_lock`
3. `agent_pool._execution._state_lock` (innermost)

---

## Testing Recommendations

1. **Test Auto-Apply Mode:**
   - Enable `auto_apply=True`
   - Trigger a security check
   - Verify approval card disappears from UI after Security responds
   - Check logs for cleanup messages

2. **Test Manual Mode:**
   - Enable `auto_apply=False`
   - Trigger a security check
   - Verify Security advisor's recommendation appears in UI
   - Manually approve/reject
   - Verify approval card disappears

3. **Test Timeout:**
   - Set `SECURITY_ADVISOR_TIMEOUT_SECONDS` to a low value (e.g., 5 seconds)
   - Trigger a security check with complex justification
   - Verify timeout handling works correctly
   - Verify approval card disappears after timeout

4. **Test Concurrent Security Checks:**
   - Trigger multiple security checks simultaneously
   - Verify no deadlocks occur
   - Verify all cleanup happens correctly

---

## Summary

The primary issue causing the Security agent auto-ask stall is the missing broadcast of updated approvals list after auto-apply approval/rejection. This causes the UI to still show the approval card even though it's been resolved, making it appear as if the system is stalled.

Secondary issues include potential race conditions in `active_stack` management and lock ordering concerns that could cause intermittent problems.

The recommended fixes address these issues with minimal code changes and should resolve the stall issue.