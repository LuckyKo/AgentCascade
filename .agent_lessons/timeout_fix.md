# Lessons: Timeout and Process Killing Fixes (2026-05-22)

## Bug: cmd_shell timeout doesn't kill Python child processes

### Root Cause
`subprocess.run(..., shell=True, timeout=120)` on Windows kills only the shell process (cmd.exe). Child processes like python.exe survive because they are not in the same process group by default on Windows.

### Fix Applied
1. Use `Popen` + `communicate(timeout=...)` instead of `subprocess.run(..., timeout=...)`.
2. On Windows: set `CREATE_NEW_PROCESS_GROUP` creation flag so all children belong to one process group.
3. Three-pass kill strategy on Windows:
   - First pass: `taskkill /F /T /PID <pid>` kills the process tree
   - Second pass: 0.5s delay, then repeat `taskkill /F /T` — catches processes that orphaned between passes
   - Third pass: Recursive WMIC sweep (up to 4 levels deep) to find and kill any remaining descendant PIDs
4. On Unix: set `start_new_session=True` (Python 3.8+) so the shell and all children share a session/process group, then use `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)`.
5. After killing, drain partial output before reporting timeout.

### Key Insight
On Windows, `subprocess.run(..., timeout=N)` only sends CTRL_BREAK_EVENT to the shell process. Child processes ignore it. You MUST use `CREATE_NEW_PROCESS_GROUP` + `taskkill /F /T` for complete cleanup. A single taskkill pass is NOT enough — you need multiple passes because grandchildren can orphan between passes.

## Bug: Watchdog kills container but error doesn't propagate back to agent

### Root Cause
The watchdog thread removed kernels from `_KERNEL_CLIENTS` and `_DOCKER_CONTAINERS`, but the main thread had no way to detect this mid-execution. It would get a raw exception (connection closed, etc.) instead of a proper error message. Also, shared state was accessed without locking, causing race conditions between the watchdog thread and the main execution thread.

### Fix Applied
1. Added `_KERNEL_LOCK = threading.Lock()` — all mutations to `_KERNEL_CLIENTS`, `_DOCKER_CONTAINERS`, `_KERNEL_ACTIVITY`, and `_WATCHDOG_KILLED` are now protected.
2. Added `_WATCHDOG_KILLED: set()` — watchdog marks killed kernels here before removing them from tracking. Main thread checks this set at multiple points: before starting, inside the execution loop (twice — at top of iteration AND immediately after get_iopub_msg returns), and in exception handlers.
3. Watchdog holds lock during entire kill sequence (mark + remove client + remove container + remove activity), but releases it for slow Docker stop/rm operations.
4. `_execute_code()` receives `kernel_id` as a parameter instead of recomputing it — prevents stale references.
5. When watchdog kill is detected mid-execution, discard partial output to avoid confusing the agent with mixed valid-looking + error output.
6. After watchdog kills a kernel, subsequent calls detect this via `_WATCHDOG_KILLED`, discard the flag, and start a fresh kernel/container automatically.

### Key Insight
Thread-safe state management requires: (a) a lock for all shared mutable state, (b) slow I/O operations outside the lock, (c) a signaling mechanism (like a killed-set) so in-flight calls can detect cleanup by another thread. Always read shared state into a local variable under the lock, then use the local variable — never hold the lock while doing I/O or heavy processing.

### Critical Bug: Docstring split across code
When editing docstrings surgically with edit_file, be very careful about triple-quote boundaries. A misplaced closing `"""` can cause Returns/Raises sections to end up as bare code outside the docstring, creating a syntax error that prevents module loading. Always verify with python_compiler after edits.

## Testing Notes
- These changes are hard to unit-test in isolation because they require actual Docker containers and process spawning
- Manual testing should verify: (1) cmd_shell timeout kills Python children on Windows, (2) code interpreter recovers after watchdog kill with a proper error message, (3) subsequent code_interpreter calls start fresh after watchdog kill