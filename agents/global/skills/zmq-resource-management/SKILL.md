---
name: zmq-resource-management
description: Ensures deterministic cleanup of ZMQ and similar socket resources to prevent leaks under stress testing.
triggers:
  - "When managing Jupyter kernels, long-lived socket connections, or any resource that must be explicitly closed."
---

# ZMQ Resource Management

Relying on `__del__` or `atexit` for socket cleanup leads to resource exhaustion under load because:
- `__del__` is non-deterministic and depends on GC timing.
- `atexit` only runs on process exit, not during long-running tests.
- Background threads holding sockets may not terminate cleanly.

## Solution pattern

**1. Explicit cleanup methods.**
```python
def cleanup(self):
    if hasattr(self, 'socket') and self.socket:
        self.socket.close()
        self.socket = None
```

**2. Tie to lifecycle events.** Call `cleanup()` on agent dismiss/terminate; don't rely solely on `__del__`; register callbacks with the system's lifecycle hooks. (For Jupyter, prefer `KernelManager.cleanup()`.)

**3. Use context managers.**
```python
with ZMQResource() as zmq:
    # use resource — cleanup happens automatically
```

**4. Monitor under load.** Track open socket counts; alert on accumulation; test stress scenarios regularly.

**Prevention rule of thumb:** every socket/context you create must have exactly one owned cleanup path (explicit `cleanup()`, a lifecycle callback, or a context manager) — never rely on GC/`__del__` alone.
