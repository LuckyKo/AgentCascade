---
name: zmq-resource-management
description: Ensures deterministic cleanup of ZMQ and similar socket resources to prevent leaks under stress testing.
triggers:
  - When managing Jupyter kernels, long-lived socket connections, or any resource that must be explicitly closed.
---

# ZMQ Resource Management

**Name**: zmq-resource-management  
**Description**: Ensures deterministic cleanup of ZMQ and similar socket resources to prevent leaks under stress testing.  
**Triggers**: When managing Jupyter kernels, long-lived socket connections, or any resource that must be explicitly closed.

## Problem

Relying on `__del__` or `atexit` for socket cleanup leads to resource exhaustion under load because:
- `__del__` is non-deterministic and depends on GC timing
- `atexit` only runs on process exit, not during long-running tests
- Background threads holding sockets may not terminate cleanly

## Solution Pattern

### 1. Explicit Cleanup Methods
```python
def cleanup(self):
    if hasattr(self, 'socket') and self.socket:
        self.socket.close()
        self.socket = None
```

### 2. Tie to Lifecycle Events
- Call `cleanup()` on agent dismiss/terminate
- Don't rely solely on `__del__`
- Register callbacks with the system's lifecycle hooks

### 3. Use Context Managers
```python
with ZMQResource() as zmq:
    # use resource
    # cleanup happens automatically
```

### 4. Monitor Under Load
- Track open socket counts
- Alert on accumulation
- Test stress scenarios regularly

## Codebase Application

In `agent_cascade/tools/code_interpreter.py`:
- Add explicit cleanup when agents are dismissed
- Enhance watchdog to close ZMQ contexts properly
- Consider using Jupyter's `KernelManager.cleanup()` method

## Related
See `[[zmq-socket-leak-prevention]]` for prevention guidelines.