---
name: hermetic-subprocess-supervisor-testing
description: Patterns for hermetically testing process supervisors (spawn/watch/restart) without real subprocesses, covering watcher thread lifecycle and exit-code policies.
source: auto-generated
version: "1.0.0"
triggers:
  - "subprocess supervisor test"
  - "process watcher thread test"
  - "restart policy test"
  - "Popen mock test"
  - "child process lifecycle test"
generated_by: coder
generated_from_task: "Phase 3 AC Telegram bridge supervisor with exit-code-aware restart policy"
---

## Goal
Test subprocess supervisors (spawn, watch, restart-on-exit) hermetically — no real processes, no live services — while correctly modeling the Popen contract and watcher thread lifecycle.

## Procedure

### Step 1 — Patch Popen for the ENTIRE test duration (fixture, not context manager)

A `with patch(...)` block exits before async code (watcher threads) runs, so the supervisor spawns a REAL process. Use a pytest fixture that holds the patch open:

```python
@pytest.fixture
def popen_patched():
    import agent_cascade.telegram_bridge.supervisor as sup_mod
    popen = MagicMock()
    fakes = []
    popen.side_effect = lambda *a, **k: fakes.pop(0) if fakes else FakeProcess()
    with patch.object(sup_mod.subprocess, 'Popen', popen):
        yield {'popen': popen, 'queue_fake': lambda p: fakes.append(p)}
```

Key: patch `sup_mod.subprocess.Popen` (the module's reference), not `subprocess.Popen` globally.

### Step 2 — Model the real Popen contract in FakeProcess

A well-behaved child DIES on terminate(). If your fake doesn't model this, `stop()` paths that check `poll() is not None` will misbehave:

```python
class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code  # None = alive
        self.pid = 424242
        self.terminated = False
        self.killed = False
        self.wait_calls = []

    @property
    def returncode(self):
        return self.exit_code if self.poll() is not None else None

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.exit_code  # returns immediately (already dead) or blocks

    def terminate(self):
        self.terminated = True
        if self.exit_code is None:
            self.exit_code = 143  # process dies on SIGTERM/TerminateProcess

    def kill(self):
        self.killed = True
        if self.exit_code is None:
            self.exit_code = -9
```

For a child that IGNORES signals (to test the kill escalation path), subclass and override:

```python
class StubbornProcess(FakeProcess):
    def terminate(self):
        self.terminated = True  # record but do NOT reap
    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired(cmd='bridge', timeout=timeout)
```

### Step 3 — Use fast timers to avoid real sleeping

Pass zero/near-zero values for all timing knobs so the watcher loop spins without delay:

```python
supervisor = Supervisor(
    backoff_base=0.0,        # no sleep between restarts
    backoff_cap=0.0,
    max_restart_attempts=3,
    healthy_window_sec=0.0,
    stop_wait_sec=0.05,
    watch_interval_sec=0.01,  # poll every 10ms
)
```

### Step 4 — Handle the "already dead on entry" race

If the fake child reports a non-None `poll()` immediately (as all exit-code fakes do), the watcher's first iteration must process the death directly rather than trying to `wait()`. The supervisor code needs:

```python
with self._lock:
    proc = self._proc
    if proc is None:
        return
    already_dead = proc.poll() is not None

if already_dead:
    rc = proc.returncode if proc.returncode is not None else -1
    self._handle_child_exit(proc, rc)
    return
```

Without this, the watcher exits immediately without applying the restart policy.

### Step 5 — Clear the watcher reference before respawning

When the restart path calls `_spawn()` from within the old watcher thread, the old thread is still "alive" (in the middle of its function). If `_spawn()` checks `self._watcher.is_alive()` to decide whether to start a new watcher, it will see True and skip. Fix:

```python
self._watcher = None  # clear before spawn so _spawn() starts a fresh watcher
self._spawn()
```

### Step 6 — Clear stale state flags on re-enable

If `stop()` sets a `_stopping` flag, the enable path MUST clear it:

```python
if enabled and not self._enabled:
    self._enabled = True
    self._stopping = False  # critical: allows restarts after stop→enable cycle
    self._restart_attempts = 0
    self._spawn()
```

### Step 7 — Sanitize inherited env vars

`env = dict(os.environ)` inherits ALL host vars. If the host has stale `TG_TARGET_AGENT` or similar, it leaks into the child. Explicitly pop bridge-specific vars:

```python
for var in ('TG_BRIDGE_ENABLED', 'ALLOWED_USERS', 'AC_BASE_URL', 'TG_TARGET_AGENT'):
    env.pop(var, None)
# then set authoritative values
```

## Tips

- **Stale `.pyc` cache**: after editing a module that adds new methods, clear `__pycache__/*.pyc` before re-testing. Python may load the old bytecode and `hasattr()` will return False for new methods.
- **Don't use `with patch(...)` around async/threaded code** — the patch context exits before background threads run. Always use a fixture.
- **`proc.returncode` can be None after kill** if the process hasn't been reaped. Use `poll()` first, fall back to `getattr(proc, 'returncode', None)`.
- **Test the restart CAP explicitly**: queue N+1 fakes (N = max_restart_attempts + 1 initial), assert exactly N+1 spawns total, then verify the error message mentions "giving up".
- **`target_agent` or similar params with defaults** will always be set in env — don't assert their absence; assert their value instead.
- **Windows-specific**: `CREATE_NO_WINDOW = 0x08000000`. Assert `creationflags == 0x08000000` on win32, `== 0` elsewhere. Use `getattr(subprocess, 'CREATE_NO_WINDOW', 0)` for portability.
