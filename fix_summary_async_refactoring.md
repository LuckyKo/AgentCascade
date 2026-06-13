# Async call_agent Refactoring - Fix Summary (REVISED)

**Date:** 2026-01-12  
**Task:** Fix 3 CRITICAL and 2 MAJOR issues found by reviewer in async call_agent refactoring  
**Status:** ✅ **APPROVED FOR COMMIT**

## Files Modified

1. `agent_cascade/execution_engine.py` - FIX 1 (Parts A, B, C)
2. `agent_cascade/api_router.py` - FIX 2 + MINOR FIX
3. `agent_cascade/agent_pool.py` - FIXES 3, 4, 5

---

## 🔴 CRITICAL FIX 1: Result-Loss Bug — Fast-Completing Child Causes Premature Agent Exit

**Location:** `execution_engine.py`, `_post_turn_checks()` line ~1602

**Problem:** If a child completes between `register_async_call()` and the `has_pending()` check, the parent exits without draining the child's result.

### Part A & B: Add Missing Parameters to Function Signature

**Sub-Problem:** Original fix had undefined variables `llm_messages` and `response` causing NameError at runtime.

**Solution:** 
1. Added `llm_messages` and `response` parameters to function signature
2. Updated call site to pass these parameters

**Code Changes (Function Signature):**
```python
# BEFORE:
def _post_turn_checks(self, instance: AgentInstance, messages: List[Message]) -> bool:

# AFTER:
def _post_turn_checks(
    self, 
    instance: AgentInstance, 
    messages: List[Message],
    llm_messages: List[Message],
    response: List[Message]
) -> bool:
```

**Code Changes (Call Site):**
```python
# BEFORE:
if not self._post_turn_checks(instance, messages):

# AFTER:
if not self._post_turn_checks(instance, messages, llm_messages, response):
```

### Part C: Add Safety Drain with Error Handling

**Solution:** Added safety drain before final `return False` wrapped in try-except:

```python
try:
    final_drain = self.pool.drain_async_results(inst_name)
    if final_drain:
        logger.debug(
            f"[CALL_AGENT_DEBUG] _post_turn_checks — safety drain caught {len(final_drain)} "
            f"result(s) for {inst_name}"
        )
        for result in final_drain:
            result_msg = Message(role=USER, content=f"[BACKGROUND TOOL RESULT]: {result}")
            messages.append(result_msg)
            llm_messages.append(result_msg)
            response.append(result_msg)
            with instance._compression_lock:
                instance.conversation.append(result_msg)
        _invalidate_token_cache(instance)
        return True  # Continue loop to process drained results
except Exception as e:
    logger.error(
        f"[CALL_AGENT_DEBUG] _post_turn_checks — safety drain failed for {inst_name}: {e}"
    )
```

---

## 🔴 CRITICAL FIX 2: Zombie Threads from Unbounded Slot Acquisition Blocking

**Location:** `api_router.py`, `EndpointScheduler.acquire()` line ~181

**Problem:** `semaphore.acquire()` blocks forever with no timeout. If a child's slot never becomes available, the worker thread is stuck forever.

**Solution:** Added 5-minute timeout to semaphore acquisition using named constant:

```python
# Module-level constant (line 25)
ENDPOINT_SLOT_ACQUIRE_TIMEOUT: int = 300

# Usage in acquire() method (line 181-184)
if not sched['sem'].acquire(timeout=ENDPOINT_SLOT_ACQUIRE_TIMEOUT):
    raise TimeoutError(
        f"Timed out after {ENDPOINT_SLOT_ACQUIRE_TIMEOUT}s waiting for endpoint slot on {api_base}. "
        f"Current active count: {sched['active_count']}, max allowed: {sched['max_active']}"
    )
```

---

## 🔴 CRITICAL FIX 3: Non-Atomic Error Notification in _notify_async_error

**Location:** `agent_pool.py`, `_notify_async_error()` line ~1529-1555 AND task_wrapper success path lines ~1680-1702

**Problem:** If `add_async_result()` raises, `complete_async_call()` is skipped and the parent hangs forever in SLEEPING.

**Solution:** Made each operation individually safe with try-except blocks:
- Wrapped `add_async_result` in try-except with error logging
- Wrapped `complete_async_call` in try-except with error logging  
- Applied same pattern to success path in task_wrapper
- Ensures `complete_async_call` always runs even if `add_async_result` fails

**Code Changes (_notify_async_error):**
```python
# Ensure complete_async_call is called even if add_async_result fails
try:
    self.pool.add_async_result(caller, error_msg)
except Exception as e:
    logger.error(
        f"[CALL_AGENT_DEBUG] _notify_async_error — failed to add async result "
        f"for caller={caller}: {e}"
    )

if call_id:
    try:
        self.pool.complete_async_call(caller, call_id)
        logger.debug(...)
    except Exception as e:
        logger.error(
            f"[CALL_AGENT_DEBUG] _notify_async_error — failed to complete async call "
            f"{call_id} for caller={caller}: {e}"
        )
```

**Code Changes (task_wrapper success path):** Same try-except pattern applied.

---

## 🟠 MAJOR FIX 4: Orphan Queue Entries on Slot Failure

**Location:** `agent_pool.py`, `_notify_async_error()` line ~1554 AND task_wrapper line ~1704

**Problem:** When slot acquisition fails, `send_message(instance_name, caller, error_msg)` creates a message queue for the child agent that never started.

**Solution:** Removed `send_message` calls from both error and success notification paths:
- The async result buffer is sufficient for error/completion notification
- Parent receives the error via `add_async_result` when it wakes from SLEEPING
- Message queue was redundant here

**Code Changes:** Removed these lines:
```python
# REMOVED from _notify_async_error:
self.pool.send_message(instance_name, caller, error_msg)

# REMOVED from task_wrapper success path:
self.pool.send_message(instance_name, caller, completion_msg)
```

---

## 🟠 MAJOR FIX 5: Empty Conversation Treated as Success in task_wrapper

**Location:** `agent_pool.py`, task_wrapper lines ~1650-1658

**Problem:** If `_create_and_run_agent` returns `(inst, [])` (empty conversation), it's treated as a successful completion with no output instead of an error.

**Solution:** Added check for empty conversation after the None check:
- Checks `if not conv:` after validating inst and conv are not None
- Logs warning about aborted/terminated execution
- Calls `_notify_async_error` to properly notify caller of failure

**Code Changes:**
```python
# Check for empty conversation — indicates aborted/terminated execution
if not conv:
    logger.warning(
        f"[CALL_AGENT_DEBUG] task_wrapper — empty conversation for {instance_name}, "
        f"treating as error (agent may have been terminated or aborted)"
    )
    error_msg = f"[Parallel Agent '{instance_name}' Failed]: Execution terminated with no output."
    self._notify_async_error(instance_name, caller, error_msg, call_id)
    return
```

---

## Verification

### Syntax Validation
All three modified files pass Python syntax validation:
- ✅ `agent_cascade/execution_engine.py` - Valid
- ✅ `agent_cascade/api_router.py` - Valid  
- ✅ `agent_cascade/agent_pool.py` - Valid

### Review Status
**Reviewer:** reviewer_async_fixes  
**Verdict:** ✅ **PASS — Ready for Commit**

All issues from the review have been resolved:
- [x] CRITICAL #1 (undefined variables) — Fixed by adding parameters
- [x] MAJOR #2 (error handling) — Fixed by wrapping in try-except
- [x] MINOR #3 (named constant) — Fixed by adding ENDPOINT_SLOT_ACQUIRE_TIMEOUT
- [x] All other fixes verified intact

---

## Testing Recommendations

1. **Test FIX 1:** Create a scenario where a child agent completes very quickly after being spawned via call_agent, verify parent receives the result without exiting prematurely.

2. **Test FIX 2:** Fill all endpoint slots and spawn an additional agent, verify it times out after ~300s with proper error message rather than hanging forever.

3. **Test FIX 3:** Simulate `add_async_result` raising an exception (e.g., buffer full), verify `complete_async_call` still executes and parent doesn't hang in SLEEPING.

4. **Test FIX 4:** Trigger slot acquisition failure, verify no orphan message queue entries are created for agents that never started.

5. **Test FIX 5:** Create a scenario where an agent terminates early with empty conversation (e.g., exception during initialization), verify parent receives error notification rather than empty success.

---

## Notes for Commit

- All fixes follow the exact specifications from the reviewer's report
- Added comprehensive debug logging with `[CALL_AGENT_DEBUG]` prefix for tracing
- Used `edit_file` for surgical changes, preserving existing code structure
- Backups automatically created for all modified files in `logs/backups/coder/`
- **Ready for commit after this review approval**

## Backup Files Created

All backups are in `logs/backups/coder/`:
- `execution_engine.py.1781223672.bak` (original FIX 1)
- `execution_engine.py.1781224133.bak` (FIX 1 Part A - signature)
- `execution_engine.py.1781224141.bak` (FIX 1 Part B - call site)
- `execution_engine.py.1781224170.bak` (FIX 1 Part C - try-except)
- `api_router.py.1781223682.bak` (original FIX 2)
- `api_router.py.1781224189.bak` (named constant definition)
- `api_router.py.1781224206.bak` (use named constant)
- `agent_pool.py.1781223708.bak` (FIX 3 - _notify_async_error)
- `agent_pool.py.1781223738.bak` (FIX 3 part 2 & FIX 4 - task_wrapper)
- `agent_pool.py.1781223763.bak` (FIX 5 - empty conv check)

---

**Total Files Modified:** 3  
**Total Fixes Applied:** 5 (3 CRITICAL, 2 MAJOR) + 1 MINOR  
**Review Status:** ✅ APPROVED FOR COMMIT