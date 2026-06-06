# Code Interpreter Lessons Learned

## Fix: Container IP Binding with allow_remote_access=False (2026-05-27)

### Problem
After security hardening fixes, the code_interpreter tool was completely broken. The kernel started in Docker but refused to respond to shell channel requests from the host. `wait_for_ready()` failed with "Kernel died before replying to kernel_info".

### Root Cause
Two changes broke in combination:
1. **Fix B4a (line 686)**: Container connection file IP set to `"127.0.0.1"` — kernel binds only to localhost inside container
2. **Fix B4b (line 763)**: `allow_remote_access=False` — kernel rejects "remote" connections

On Windows Docker with port forwarding (`-p 127.0.0.1:PORT:PORT`), the host connects via 127.0.0.1, but when allow_remote_access=False is set AND the kernel binds to 127.0.0.1 inside the container, the ZMQ communication fails because ipykernel's internal check rejects what it perceives as a remote connection.

### Fix
Reverted B4a: Changed container IP back to `"0.0.0.0"` (line 686). The kernel now binds to all interfaces inside the container, but the host-side port forwarding is still restricted to 127.0.0.1 only (Fix C2 at line 756).

### Why This Is Safe
- **Host-side**: Ports are bound to `127.0.0.1` only (`-p 127.0.0.1:PORT:PORT`) — not accessible from outside the host
- **Container security**: Still has `--cap-drop=ALL`, `no-new-privileges`, memory/CPU/PID limits
- **allow_remote_access=False**: Still enforced on the kernel side for defense in depth
- The container IP being 0.0.0.0 only means the kernel listens on all interfaces *inside* the isolated container, which has no external network access

### Key Lesson
When using `allow_remote_access=False` with ipykernel inside Docker with port forwarding, the kernel must bind to `0.0.0.0` (not 127.0.0.1) on the container side. The security boundary is at the Docker port mapping layer, not at the kernel's bind address.