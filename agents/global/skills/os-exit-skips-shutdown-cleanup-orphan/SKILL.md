---
name: os-exit-skips-shutdown-cleanup-orphan
description: Diagnose "restart leaves orphaned/duplicate child processes" (e.g. Telegram bridge duplicated after /restart) where a hard exit (os._exit / spawn-child-then-exit) skips the signal-handler or atexit cleanup that was supposed to stop them.
source: auto-generated
version: "1.0.0"
triggers:
  - "/restart kills the server instead of restarting"
  - "duplicating the telegram bridge on restart"
  - "orphan process after restart"
  - "duplicate child processes after restart"
  - "os._exit skips shutdown cleanup"
  - "restart leaves two instances running"
  - "child survives parent os._exit"
  - "atexit not called on restart"
generated_by: orchestrator
generated_from_task: "TG /restart killed the server instead of restarting; then user flagged we are now duplicating the Telegram bridge on restart."
---

## Goal
Find and fix bugs where a process "restart" (spawn a fresh child, then hard-exit the parent) leaves **orphaned child processes** behind — producing duplicate instances (e.g. two Telegram bridges polling the same bot token), double-handling of work, or leaked resources — because the cleanup that was supposed to stop those children lives in a code path the hard exit never runs.

## The core gotcha
`os._exit(n)` (and `os.kill(os.getpid(), SIGKILL)`) terminate the process **immediately**: they do NOT run `atexit` handlers, do NOT deliver Python-level signal handlers, and do NOT unwind the stack. Any cleanup registered via a **signal handler** (`signal.signal(SIGTERM, ...)`, uvicorn/ASGI shutdown hooks, FastAPI `@app.on_event('shutdown')`) or `atexit.register(...)` is **silently skipped**.

So if your server stops its child processes (a sidecar daemon, a worker pool, a spawned bridge) inside such a handler, and your restart path does:
```python
subprocess.Popen([sys.executable, *sys.argv], detached=...)  # spawn replacement
os._exit(0)                                                  # <-- skips the shutdown handler
```
then the OLD children are never stopped. A non-`DETACHED_PROCESS` child often still **survives** (the parent just vanishes; nothing explicitly kills it), and the freshly-spawned replacement then starts its OWN copy → **two live instances** (duplicate replies, double work).

## Procedure
### Step 1 — Confirm the restart path hard-exits
Locate the restart/re-exec code. Look for `os._exit(`, `os.kill(os.getpid(), ...)`, or "spawn child + exit" patterns. If it uses `os.execl` (POSIX in-place re-exec) that's also a hard transition — on POSIX `execl` replaces the image so old children become orphans too unless explicitly stopped first.

### Step 2 — Find where the children are actually stopped
Grep for the stop/cleanup: `.stop()`, `terminate()`, `shutdown`, `atexit.register`, signal handlers, ASGI/FastAPI shutdown hooks. Note WHICH mechanism runs it (signal handler? atexit? explicit call?).

### Step 3 — Check the two paths agree
If the cleanup lives in a **signal handler or atexit** but the restart uses **`os._exit`**, they disagree → orphan bug. Reproduce mentally: parent exits via os._exit → handler never fires → child not stopped → replacement spawns a second one.

### Step 4 — Pick the fix (prefer removing the root cause)
- **Best (removes the class of bug):** if the "child" is really just long-running in-process work (a polling loop, a bot), run it as a **daemon thread in the same process** instead of a separate OS process. Then there is nothing to orphan — it dies with the parent by construction and restarts cleanly. Trade-off: you lose crash/memory isolation; mitigate by wrapping the thread body in a broad try/except that logs + (optionally) restarts the loop rather than letting a fatal error kill the host.
- **If it must stay a separate process:** stop the children **explicitly BEFORE** the hard exit (call the same stop routine the shutdown handler uses, directly, in the restart path), AND/OR add an **idempotent "kill stale instance on startup"** guard: on boot, before spawning your own child, detect + terminate any leftover from a prior run. For robust detection use a PID file written by the spawner + a **command-line verification** of the candidate PID (guard against OS PID reuse — never kill a recycled PID that isn't actually yours). A best-effort full process-scan fallback catches pre-fix orphans.

## Tips
- The bug is invisible in unit tests: nothing in a test does a real `os._exit` + respawn. Verify with a **real** restart (or an e2e that spawns a fake child, hard-exits a wrapper, and asserts the child count afterward).
- Distinguish from related-but-different skills: `uvicorn-port-rebind-restart` = the NEW process can't rebind its port (EADDRINUSE); `debug-ac-supervisor-child-not-starting` = child won't stay up. This one is about OLD children SURVIVING.
- On Windows, a non-detached child's survival after parent `os._exit` depends on job/console semantics — don't assume it dies; verify empirically.
- Keep the "stop" logic in ONE place and call it from both the graceful-shutdown path and the restart path (or rely on startup cleanup) so the two can't drift.
