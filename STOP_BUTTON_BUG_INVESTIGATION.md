# Stop Button Bug Investigation Report

## Executive Summary

The stop button issue is a **state synchronization and early termination problem** where clicking "Stop" sets flags but doesn't properly interrupt running operations, leaving the system in a locked state.

---

## 1. Complete Call Chain: UI Click → Backend Processing

### Frontend (web_ui/app.js)

**Line 3491**: Stop button click handler
```javascript
if (stopBtn) stopBtn.addEventListener('click', () => send({ type: 'stop' }));
```

**Lines 892-896**: The `send()` function
```javascript
function send(obj) {
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify(obj));
  }
}
```

### Backend (agent_cascade/api_server.py)

**Lines 1435-1439**: WebSocket message handler for 'stop' type
```python
elif msg_type == 'stop':
    with session_lock:
        session['stop_requested'] = True
    if agent_pool:
        agent_pool.stopped = True
```

**Key Issue**: The stop handler **only sets flags** but doesn't:
1. Broadcast a state update to the frontend
2. Clear the `session['generating']` flag immediately
3. Kill running tool executions (code_interpreter, shell_cmd)
4. Signal threads to exit properly

---

## 2. Where the Breakdown Occurs

### Problem A: No Immediate State Broadcast

After setting `agent_pool.stopped = True`, there's **no corresponding broadcast** to update the frontend state. Compare with other message types:

- **'continue' (line 1433)**: `await broadcast({'type': 'state', **build_state(generating=True)})`
- **'stop' (lines 1435-1439)**: No broadcast at all!

**Result**: Frontend shows "generating" state even after stop is clicked.

### Problem B: Thread Detection is Polling-Based, Not Interruptive

The execution thread in `run_agent_unified.py` checks the stopped flag only at specific points:

**Line 137-138** (run_agent_unified.py):
```python
if pool.stopped:
    break
```

This check happens **once per turn iteration**, meaning:
- If a tool is executing (code_interpreter, shell_cmd, grep), the thread won't check for 10-60+ seconds
- The Docker container continues running even after stop is clicked

### Problem C: Tool Execution Doesn't Check Stopped State

**Code Interpreter** (`tools/code_interpreter.py`):
- `_execute_code()` method (lines 864-995) runs in a blocking loop
- **No check for `pool.stopped` during the execution loop**
- Only checks for watchdog kill, not user-initiated stop

**Python Executor** (`tools/python_executor.py`):
- `batch_apply()` method (lines 219-270) runs code in a process pool
- **No stopped flag check** during execution

**Shell Command** (`tools/custom/shell_cmd.py`):
- Executes via `subprocess.run()` with timeout
- **No mechanism to kill subprocess on stop**

### Problem D: Operation Manager Approval Loops

**operation_manager.py lines 288-294**:
```python
while time.time() - start_time < timeout_val:
    if self.agent_pool and getattr(self.agent_pool, 'stopped', False):
        break
    
    # Wait in small increments to remain responsive to stopped flag
    if approval.event.wait(timeout=1.0):
        got_response = True
        break
```

**Issue**: The 1-second wait interval is too long for responsive stopping.

### Problem E: Frontend State Not Cleared

After stop, the backend sets `agent_pool.stopped = True` but:
- No `'done'` or `'state'` broadcast is sent to frontend
- Frontend's `state.generating` remains `True`
- UI shows "generating" state even though execution stopped

**Lines 1693-1701** (api_server.py) show proper cleanup happens on 'retry':
```python
with session_lock:
    session['generating'] = False
    session['stop_requested'] = False
    session['generation_id'] += 1
if agent_pool:
    agent_pool.stopped = True
    agent_pool.reset()
await broadcast({'type': 'done', **build_state()})
```

But **'stop' message handler (lines 1435-1439) doesn't do this cleanup**!

---

## 3. Root Cause Analysis

### Primary Root Cause: Incomplete Stop Handler

The stop message handler in api_server.py is **minimal and incomplete**:

```python
elif msg_type == 'stop':
    with session_lock:
        session['stop_requested'] = True
    if agent_pool:
        agent_pool.stopped = True
```

**What's missing:**
1. ❌ No broadcast to update frontend state
2. ❌ No `session['generating'] = False`
3. ❌ No thread joining or cleanup
4. ❌ No tool execution interruption
5. ❌ No Docker container termination

### Secondary Root Cause: Polling vs. Interruption

The system uses **polling** (checking flags at turn boundaries) instead of **interruption** (signaling mechanisms):

- Execution threads check `pool.stopped` only between turns
- Tool executions don't periodically check the flag
- No threading.Event or similar mechanism to wake up blocked threads

### Tertiary Root Cause: Sub-Agent Execution Loop

In `execution_engine.py`, sub-agent execution has stop checks:

**Lines 2797-2800**:
```python
for resp in self.run(inst):
    if self.pool.stopped or self.pool.is_instance_halted(instance_name) or self.pool.is_instance_terminated(instance_name):
        break
    final_resp = resp
```

But this only breaks the **outer loop**, not the inner tool execution that may be blocking.

---

## 4. Why It Puts System in "Locked State"

The "locked state" occurs because:

1. **Frontend thinks it's still generating** (no broadcast updates state)
2. **Backend thread may be blocked** in a tool call (code_interpreter Docker container running)
3. **`session['generating']` is still True** (never cleared by stop handler)
4. **`agent_pool.stopped` is True** but threads haven't observed it yet
5. **New messages can't start** because generating=True blocks new sends
6. **User must refresh or wait** for the blocked tool to complete

This creates a **half-stopped state** where:
- Flags say "stopped" 
- But execution is still running
- And UI shows "generating"

---

## 5. File Paths and Line Numbers Summary

| Component | File Path | Lines | Issue |
|-----------|-----------|-------|-------|
| Stop Button Click Handler | `web_ui/app.js` | 3491 | Sends `{type: 'stop'}` via WebSocket |
| Send Function | `web_ui/app.js` | 892-896 | Simple WebSocket send |
| Stop Message Handler | `agent_cascade/api_server.py` | 1435-1439 | **SETS FLAGS ONLY, NO BROADCAST** |
| Execution Thread Loop | `agent_cascade/run_agent_unified.py` | 137-138 | Checks stopped flag once per turn |
| Code Interpreter Execute | `agent_cascade/tools/code_interpreter.py` | 864-995 | **NO STOP CHECK DURING EXECUTION** |
| Python Executor | `agent_cascade/tools/python_executor.py` | 219-270 | **NO STOP CHECK** |
| Sub-Agent Loop | `agent_cascade/execution_engine.py` | 2797-2800 | Breaks outer loop, not inner tool exec |
| Operation Manager | `agent_cascade/operation_manager.py` | 288-294 | 1-second polling interval |
| Proper Cleanup (retry) | `agent_cascade/api_server.py` | 1693-1701 | Shows what stop handler should do |

---

## 6. Hypothesis on Root Cause

**The stop button puts the system in a locked state because:**

1. The stop message handler **only sets flags** without broadcasting state updates to the frontend
2. Running tool executions (code_interpreter, shell_cmd) **don't check the stopped flag** during their execution loops
3. The execution thread only checks `pool.stopped` **at turn boundaries**, not during blocking operations
4. No cleanup or broadcast occurs after setting flags, leaving frontend in "generating" state
5. This creates a **race condition**: backend may stop but frontend doesn't know, or tools continue running even after stop

**The fundamental issue is that "stop" is implemented as a hint (flag) rather than an interrupt signal.**

---

## 7. Recommended Fixes

### Fix 1: Complete the Stop Handler (api_server.py lines 1435-1439)

```python
elif msg_type == 'stop':
    with session_lock:
        session['stop_requested'] = True
        session['generating'] = False  # Clear generating flag immediately
    if agent_pool:
        agent_pool.stopped = True
        # Broadcast state update to frontend
        await broadcast({'type': 'done', **build_state()})
```

### Fix 2: Add Stop Checks in Tool Executions

**code_interpreter.py**: Check `pool.stopped` in the execution loop (around line 905)

**python_executor.py**: Add stop flag check before processing results

**shell_cmd.py**: Use subprocess with proper interrupt handling

### Fix 3: Use Threading Events for Faster Response

Replace simple boolean flag with `threading.Event()` for faster thread wake-up:

```python
# In agent_pool.py
self._stopped_event = threading.Event()

@property
def stopped(self):
    return self._stopped_event.is_set()

@stopped.setter  
def stopped(self, value):
    if value:
        self._stopped_event.set()
    else:
        self._stopped_event.clear()
```

### Fix 4: Add Periodic Stop Checks in Long Operations

In code_interpreter's `_execute_code()` loop, add:
```python
if hasattr(self, 'agent_pool') and self.agent_pool.stopped:
    break
```

---

## 8. Testing Recommendations

1. **Test rapid stop**: Click stop immediately after sending a message that triggers code_interpreter
2. **Test nested agents**: Stop while a sub-agent is executing
3. **Test shell_cmd**: Stop during a long-running shell command (e.g., `sleep 60`)
4. **Verify UI state**: Ensure "generating" indicator clears immediately after stop
5. **Check Docker containers**: Verify stopped containers are cleaned up

---

## 9. Additional Notes

- The `resume` handler (lines 1441+) has similar issues - it sets flags but may not properly restart generation
- The `continue` handler properly broadcasts state, which is why it works better than stop
- There's existing infrastructure (`_stopped_event`) that could be used more effectively for thread signaling