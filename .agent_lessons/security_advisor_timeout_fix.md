# Security Advisor Timeout Fix - Lessons & Notes

## Problem (todo.md line 46)
The security advisor could take longer to respond than the user approval timeout (APPROVAL_TIMEOUT_SECONDS = 300s), causing an AFK rejection cascade where the operation gets rejected because the system thinks no one is responding — not because of actual inactivity, but because the security advisor thread is still thinking.

## Root Cause
In `api_server.py`, the `_security_check()` function spawns a thread that runs `sec_agent.run(...)` without any timeout protection. Meanwhile, the approval request it's evaluating has its own timeout timer in `operation_manager.request_user_approval()`. If the security advisor takes longer than the approval timeout (300s), the approval times out and returns "User is AFK" — a false rejection.

## Solution
Added a **two-phase timeout mechanism** with early warning:

### Phase 1: Warning (at ~120 seconds)
- A `threading.Timer` injects a warning message into the security advisor's message queue using `agent_pool.enqueue_message('security_advisor', ...)`.
- The security advisor picks this up on its next iteration via `drain_queue(self.session_name)` and is prompted to hurry.
- Gives slow models (3-5 second response times) a fighting chance to finish.

### Phase 2: Termination (at ~180 seconds)  
- Every iteration of the `for partial in sec_agent.run(...)` loop checks elapsed time using `time.monotonic()`.
- If exceeded, we set `sec_timeout_reached = True` and break the loop.
- The generator is properly closed via `run_gen.close()` to abort any active LLM call / HTTP connection.
- The security advisor instance is halted via `agent_pool.halt_instance('security_advisor')` (best-effort between turns).
- The operation is auto-rejected with a clear message suggesting resubmission with better justification.
- UI is notified via a `security_response` message with verdict='TIMEOUT'.

### Configuration Constants (in operation_manager.py)
```python
SECURITY_ADVISOR_TIMEOUT_SECONDS = 180   # 3 minutes — gives slow models breathing room
SECURITY_ADVISOR_WARNING_SECONDS = 120   # Warn at 2 minutes — agent gets a nudge via message queue
```

### Key Design Decisions
1. **Monotonic clock** (`time.monotonic()`) instead of wall clock — immune to system time changes
2. **Warning first, kill second** — most timeouts can be avoided if the agent is prompted to hurry
3. **Halt the instance** — `halt_instance()` makes `_run` exit cleanly on its next iteration check (best-effort between turns only)
4. **Generator close** — `run_gen.close()` aborts active LLM calls / HTTP connections on timeout
5. **Clear feedback** — rejection message tells the orchestrator exactly what happened and how to fix it (resubmit with better justification)
6. **Both auto-apply and manual mode supported** — timeout handling works in both modes

### Edge Cases Handled
1. **Generator creation failure**: Timer is only started AFTER successful generator creation, so no timer leak if `sec_agent.run()` raises
2. **Timeout during LLM streaming**: `break` + `run_gen.close()` aborts the active HTTP call (best-effort)
3. **Warning timer fires after completion**: `finally` block calls `timer.cancel()` in all exit paths
4. **Exception during generator close**: Caught and swallowed — verdict path still executes normally
5. **Elapsed time accuracy**: Stored at break point (`sec_elapsed_at_timeout`) instead of recalculated later

### Code Pattern for Future Reference
```python
# Extract generator to properly close it on timeout
run_gen = agent.run(history, ...)
timer = threading.Timer(WARNING_SECONDS, warning_func)
try:
    for partial in run_gen:
        if time.monotonic() - start > TIMEOUT_SECONDS:
            break  # Timeout enforcement
        ...
finally:
    timer.cancel()
    try:
        run_gen.close()  # Abort active LLM call
    except Exception:
        pass  # Best-effort cleanup
```

## Files Modified
- `N:\work\WD\AgentCascade\operation_manager.py` — Added 2 new constants (lines 64-65)
- `N:\work\WD\AgentCascade\api_server.py` — Module-level import of constants (line 55), timeout protection in `_security_check()` (lines ~2187-2399)

## Potential Follow-ups
- Consider making these timeouts configurable via the WebUI settings panel
- Monitor how often timeouts occur to tune the values (may need longer for slower models like local ones)
- Could add telemetry tracking for security advisor completion times to proactively adjust timeouts