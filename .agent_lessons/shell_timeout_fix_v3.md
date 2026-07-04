# Shell & Code Interpreter Timeout Fix V3 - Reviewer Corrections (2026-07-04)

## What Changed (from V2)
The reviewer found 5 issues in the timeout partial output fix. All have been addressed.

---

## File 1: `agent_cascade/operation_manager/shell.py`

### Fix 1: Named constants (module level, lines 12-14)
```python
PIPE_READ_SIZE = 4096           # Bytes per read call on stdout/stderr pipes
DRAIN_THREAD_JOIN_TIMEOUT = 3   # Seconds to wait for drain threads after process ends
```

### Fix 2: Exception propagation from drain threads (lines 164-181)
- Added `drain_errors: List[Exception]` shared list passed to both reader threads
- `_drain_pipe` now accepts a third `errors` parameter and appends caught exceptions into it
- After thread join, errors are checked and logged as warnings

### Fix 3: Race condition guard after thread join (lines 275-280)
```python
t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

# Ensure threads have fully terminated before accessing shared lists.
if t_out.is_alive() or t_err.is_alive():
    time.sleep(0.1)  # Brief grace period for thread cleanup
```

### Fix 4: Docstring accuracy
- Removed misleading "per-read timeout (1s)" claim from `_drain_pipe` docstring
- Simplified inline comment to match actual blocking read behavior

---

## File 2: `agent_cascade/tools/code_interpreter.py`

### Fix 1: Total drain timeout + message count limit (lines 391-417)
```python
DRAIN_TOTAL_TIMEOUT = 5.0       # Max seconds per drain call
DRAIN_MAX_MESSAGES = 100        # Max messages per drain cycle
DRAIN_MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB cap
```

### Fix 2: Watchdog check BEFORE blocking call (lines 419-422)
Moved `_WATCHDOG_KILLED` check to before `get_iopub_msg()` instead of after, reducing latency when kernel is killed.

### Fix 3: List-based accumulation (line 410, lines 442/447/451, line 461)
```python
parts: list[str] = []  # O(1) append instead of O(n²) string concat
...
partial_result = partial_result + ''.join(parts)  # Single join at end
```

### Fix 4: Size limit on accumulated output (lines 463-467)
Truncates to 10 MB with a clear marker message if exceeded.

### Fix 5: Unknown IOPub message type handling (lines 452-457)
Logs at debug level for unrecognized types (`'page_info'`, `'clear_output'`, etc.) instead of silently dropping them.

---

## Key Design Decisions
1. **Constants in shell.py**: Module-level for reuse across multiple command executions.
2. **Constants in code_interpreter.py**: Local scope inside timeout handler — only needed during error recovery, no need to pollute module namespace.
3. **Grace period (0.1s)**: Minimal sleep after `is_alive()` check; pipes close very quickly after process death on both Windows and Linux.
4. **Debug-level logging for unknown types**: Avoids noise in normal operation while still being traceable if needed.