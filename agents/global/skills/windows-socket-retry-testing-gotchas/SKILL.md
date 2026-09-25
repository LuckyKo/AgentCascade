---
name: windows-socket-retry-testing-gotchas
description: Hermetic testing of port-bind/EADDRINUSE retry logic on Windows — fake sockets, WinError 10013 trap, early-sleep timing bounds, patching os.execl.
source: auto-generated
version: "1.0.0"
triggers:
  - "EADDRINUSE test"
  - "bind retry test"
  - "port in use test"
  - "SO_REUSEADDR test"
  - "WinError 10013"
generated_by: coder
generated_from_task: "Testing _bind_socket_with_retry EADDRINUSE retry + restart_server_process os.execl branch on Windows host"
---

## Goal
Write deterministic, hermetic tests for socket bind-retry (EADDRINUSE) and process re-exec logic that run correctly on a Windows host.

## Procedure

### Step 1 — Simulate EADDRINUSE with a fake socket class; never hold the port for real
On Windows a second socket binding the SAME port WITH `SO_REUSEADDR` set raises **WinError 10013 (WSAACCESS)**, not 10048 — so "hold the port in a real socket" does NOT produce EADDRINUSE. Instead subclass `socket.socket`, override `bind()` to raise the error, and patch the constructor:

```python
def _eaddrinuse_error():
    return OSError(10048, 'Address already in use')  # errno 98 on POSIX, 10048 on Windows

class _FlakySocket(socket.socket):
    def bind(self, addr):
        attempts['n'] += 1
        if attempts['n'] <= 2:          # first N attempts: "held"
            raise _eaddrinuse_error()
        return super().bind(addr)       # then succeed on a real free port

with patch('socket.socket', _FlakySocket):
    sock = _bind_socket_with_retry('127.0.0.1', port, max_attempts=5, delay=0.05)
```
Pick the port with a throwaway bind-to-0 + close (`_free_port()` helper). Assert `attempts['n'] == N+1` to prove retries happened, not just that it eventually bound.

### Step 2 — Use loose timing bounds for sleep-based assertions
`time.sleep(0.05)` can return ~6% early on Windows (timer resolution). For "two delays elapsed" use `elapsed >= 0.09`, NOT `>= 0.1`. Keep the assertion (it proves retries actually slept) but bound it generously.

### Step 3 — Patching os.execl / os._exit for re-exec branch tests
`patch.object(os, 'execl')` **silently no-ops on Windows** because `os.execl` doesn't exist there — the code then hits the real missing attribute. When testing the POSIX branch on any platform: also patch `os._exit` (so a defensive post-execl exit path is inert) and monkeypatch `os.name` for the Windows/POSIX switch read at call time.

```python
monkeypatch.setattr(os, 'name', 'posix')
with patch.object(os, 'execl') as m_execl, patch.object(os, '_exit') as m_exit:
    restart_server_process()
m_execl.assert_called_once_with(sys.executable, sys.executable, *sys.argv)
```

### Step 4 — Verify uvicorn pre-bound socket integration for real
For `server.run(sockets=[pre_bound_sock])`: run a real `uvicorn.Server` in a thread with `install_signal_handlers = lambda: None`, do an HTTP round-trip against the served port, then stop via `server.should_exit = True`. This proves no double-bind and clean shutdown — far stronger than mocking uvicorn.

## Tips
- Non-EADDRINUSE OSError must be re-raised immediately (no retry) — test that too with a distinct errno (e.g. 98 vs 13).
- Budget-exhaustion tests: fake socket whose `bind()` ALWAYS raises EADDRINUSE; assert RuntimeError and exact attempt count (`max_attempts`).
- Recording-socket pattern: override `setsockopt` to append `(level, optname, value)` so you can assert `SO_REUSEADDR=1` was set BEFORE bind.
