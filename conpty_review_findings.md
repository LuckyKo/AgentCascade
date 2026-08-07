# ConPTY Async Shell Code Review - Detailed Findings

## Executive Summary
**VERDICT: NEEDS WORK**

The ConPTY implementation is architecturally sound and correctly implements the single-process console + output capture model. However, several critical issues prevent it from being production-ready:

- **Critical**: Ctrl+C command does not work with ConPTY mode.
- **Major**: Handle leaks on error paths (3 locations).
- **Major**: Reader thread can deadlock if relay process crashes.

## Detailed Findings

### 🔴 CRITICAL: Ctrl+C Not Working with ConPTY

**Location**: `send_ctrl_c` method (L1836-1872)

**Problem**: The current implementation uses `AttachConsole(pid)` and `GenerateConsoleCtrlEvent` to send Ctrl+C. This approach relies on the target process being attached to a real console window. With ConPTY, the user's command runs inside a pseudoconsole, which does not have a conventional console PID that can be targeted by these API calls. The relay process has its own console, so `AttachConsole` may attach to the wrong process.

**Evidence**:
```python
if ON_WINDOWS:
    # proc.send_signal(CTRL_C_EVENT) fails for cmd.exe commands running with
    # CREATE_NEW_CONSOLE. Use GenerateConsoleCtrlEvent via helper subprocess.
    success = _send_windows_ctrl_c(pid)  # L1861
```

**Impact**: Users cannot interrupt long-running commands via Ctrl+C in ConPTY mode, making the feature incomplete.

**Recommended Fix**:
- **Option A (Preferred)**: For ConPTY tasks, call `_kill_process_tree` instead of sending Ctrl+C. This is more reliable and aligns with `kill_task` behavior.
- **Option B**: Write `\x03` (Ctrl+C character) to the pseudoconsole input handle via `WriteFile`. This may work if the console mode has `ENABLE_PROCESSED_INPUT` set, but it's not guaranteed.

**Suggested Code Change**:
```python
if ON_WINDOWS and task.console_window:
    # ConPTY mode: cannot send console Ctrl+C event; use process tree kill
    with task._lock:
        proc = task.process
    if proc and proc.poll() is None:
        self._kill_process_tree(proc, agent_name, tool_id)
        return f"Ctrl+C sent (via kill) [Tool ID: {tool_id}, PID: {task.pid}]."
    else:
        return f"Shell already finished [Tool ID: {tool_id}]."
```

---

### 🟠 MAJOR: Handle Leaks on Error Paths

**Location**: `_spawn_conpty_with_relay` function (multiple error paths)

**Problem**: On three error paths, the `pty_out_write` handle is not closed before raising an exception. This handle is created at L408 and is not passed to any other cleanup code in these scenarios.

**Affected Paths**:

1. **Relay spawn failure** (L489-496):
   ```python
   except Exception as e:
       logger.debug(f"[AsyncShell] Failed to spawn relay: {e}")
       _kernel32.DeleteProcThreadAttributeList(attr_buf_relay)
       _close_handle(relay_read)
       _close_handle(relay_write)
       _kernel32.ClosePseudoConsole(pty_handle)
       _close_handle(stdin_write.value)
       raise  # pty_out_write NOT closed!
   ```

2. **UpdateProcThreadAttribute failure** (L527-534):
   ```python
   if not _kernel32.UpdateProcThreadAttribute(...):
       err = _WinError()
       logger.debug(f"[AsyncShell] UpdateProcThreadAttribute failed: {err}")
       _kernel32.DeleteProcThreadAttributeList(attr_buf2)
       _close_handle(relay_write)
       _kernel32.ClosePseudoConsole(pty_handle)
       _close_handle(stdin_write.value)
       raise RuntimeError(...)  # pty_out_write NOT closed!
   ```

3. **User command Popen failure** (L552-558):
   ```python
   try:
       proc = subprocess.Popen(...)
   except Exception as e:
       logger.debug(f"[AsyncShell] Failed to spawn ConPTY process: {e}")
       _kernel32.DeleteProcThreadAttributeList(attr_buf2)
       _close_handle(relay_write)
       _kernel32.ClosePseudoConsole(pty_handle)
       _close_handle(stdin_write.value)
       raise  # pty_out_write NOT closed!
   ```

**Impact**: Repeated failures to spawn ConPTY tasks will leak handles, eventually leading to `ERROR_NOT_ENOUGH_MEMORY` or similar resource exhaustion errors.

**Recommended Fix**: Add `_close_handle(pty_out_write)` to each error block before raising.

---

### 🟠 MAJOR: Reader Thread Deadlock Risk

**Location**: `_conpty_reader_thread_fn` (L576-664)

**Problem**: The reader thread performs a blocking `ReadFile` on the ConPTY output pipe and then a blocking `WriteFile` to the relay pipe. If the relay process crashes or hangs, it stops reading from its stdin, causing the pipe to fill up. Subsequent `WriteFile` calls block, which prevents the thread from reading more data. If the ConPTY output buffer also fills, the `ReadFile` blocks as well, creating a deadlock.

**Evidence**:
```python
while True:
    bytes_read = wintypes.DWORD()
    success = _kernel32.ReadFile(...)  # Could block if ConPTY buffer full
    if not success or bytes_read.value == 0:
        break
    chunk = buffer.raw[:bytes_read.value]
    # WriteFile could block if relay pipe full
    written = wintypes.DWORD()
    _kernel32.WriteFile(relay_write_handle, chunk, len(chunk), ctypes.byref(written), None)
```

**Impact**: If the relay process crashes, the entire async shell task hangs indefinitely, consuming a thread and preventing cleanup.

**Recommended Fix**:
1. **Monitor relay process**: In the reader thread loop, periodically check if the relay process is still alive. If it has terminated, close the relay_write handle and break the loop.
2. **Use non-blocking write or timeout**: This is more complex but could involve overlapped I/O with `CreateEvent`.

**Simplest Fix - Add Relay Monitoring**:
```python
relay_proc = getattr(current_thread, '_relay_proc', None)  # Set before start
while True:
    # Check if relay died
    if relay_proc and relay_proc.poll() is not None:
        logger.debug(f"[AsyncShell] Relay process died, exiting reader thread")
        break
    
    # ... rest of read/write loop
```

Then set `reader_thread._relay_proc = relay_proc` before starting the thread.

---

### 🟡 MINOR: Relay Cleanup Timeout

**Location**: `_cleanup_conpty` (L1222-1229)

**Problem**: The relay process is given only 2 seconds to terminate after `terminate()` is called. If the relay is blocked on I/O, it may not exit in time, but the code proceeds without forcing a kill. This can leave zombie processes if the main program exits while the relay is still running.

**Recommended Fix**: Add a second stage with `terminate()` followed by `wait()` for a longer period, or call `kill()` if termination fails.

---

## ✅ Correctly Implemented Aspects

1. **ConPTY Setup**: `CreatePseudoConsole` usage, handle directions, and `STARTUPINFOEXW` configuration are all correct.
2. **Single Execution**: User command runs exactly once, attached to the pseudoconsole.
3. **Output Capture**: Reader thread correctly reads from ConPTY output and distributes to task buffers and relay pipe.
4. **Heartbeats**: Integration with heartbeat mechanism is thread-safe and works correctly.
5. **Cleanup Flow**: `_cleanup_conpty` is called in the `finally` block of the tracking thread.
6. **Fallback Behavior**: ConPTY spawn failure falls back to piped mode.

---

## 🛠️ Required Code Changes (Priority Order)

### 1. Fix Handle Leaks
Add these lines to error blocks:

**At L489-496** (after `_close_handle(relay_write)`):
```python
_close_handle(pty_out_write)
```

**At L527-534** (after `_close_handle(stdin_write.value)`):
```python
_close_handle(pty_out_write)
```

**At L552-558** (after `_close_handle(stdin_write.value)`):
```python
_close_handle(pty_out_write)
```

### 2. Fix Ctrl+C for ConPTY Mode
Modify `send_ctrl_c` method:

```python
if ON_WINDOWS and task.console_window:
    # ConPTY mode: cannot send console Ctrl+C event; use process tree kill
    with task._lock:
        proc = task.process
        pid = task.pid
    if proc and proc.poll() is None:
        self._kill_process_tree(proc, agent_name, tool_id)
        return f"Ctrl+C sent (via kill) [Tool ID: {tool_id}, PID: {pid}]."
    else:
        return f"Shell already finished [Tool ID: {tool_id}]."
```

Keep the existing `GenerateConsoleCtrlEvent` logic for piped mode.

### 3. Add Relay Monitoring to Reader Thread
- Before starting the reader thread (L571-572), add:
  ```python
  reader_thread._relay_proc = relay_proc
  ```
- In `_conpty_reader_thread_fn`, at the start of the loop (L609), add:
  ```python
  # Check if relay process died
  relay_proc = getattr(current_thread, '_relay_proc', None)
  if relay_proc is not None and relay_proc.poll() is not None:
      logger.debug(f"[AsyncShell] Relay process died, exiting reader thread")
      break
  ```

### 4. Improve Relay Cleanup (Optional but Recommended)
In `_cleanup_conpty` (L1222-1229), enhance termination logic:
```python
if relay_proc is not None:
    try:
        if relay_proc.poll() is None:
            relay_proc.terminate()
            try:
                relay_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Force kill if termination fails
                relay_proc.kill()
                relay_proc.wait(timeout=1)
    except Exception as e:
        logger.debug(f"[AsyncShell] Error cleaning up relay proc tool_id={tool_id}: {e}")
```

---

## Final Notes

The ConPTY implementation represents a significant architectural improvement over the previous log-file/viewer approach. The core design is correct and follows Windows best practices. However, the identified issues must be fixed to ensure robustness and functionality. After applying these changes, thorough testing with various failure scenarios (relay crash, ConPTY creation failure, etc.) is recommended.