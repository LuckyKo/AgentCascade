# Regression Test Console-Window Investigation

**Date:** 2026-08-17
**Requested by:** Maine (orchestrator)
**Related todo:** `todo.md` line 120 — "regression tests must NOT pop out cmd shell windows, pls make sure none of them do that."
**Mode:** Investigative (root cause)
**Investigator:** shell_window_researcher

---

## Executive Summary

Regression tests that call `AsyncShellTracker.launch()` **directly** with a **real
(non-mocked) process** and **no explicit `console_window` argument** will pop a visible
cmd window on Windows. The root cause is a two-part gap:

1. `AsyncShellTracker.launch()` defaults to `console_window=True`
   (`agent_cascade/async_shell.py:286`), which contradicts its own docstring
   ("Default is False", line 299) and the `AsyncShellTask` dataclass default
   (`console_window: bool = False`, line 175).
2. The existing test-harness opt-out env var
   `QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW` (set in `tests/conftest.py:42`)
   is **only read inside the `shell_cmd` tool**
   (`agent_cascade/tools/custom/shell_cmd.py:261`), **not** inside
   `AsyncShellTracker.launch()`. So the conftest protection does **not** cover direct
   `tracker.launch()` calls.

The fix is small and production-safe: (a) add the same env-var opt-out check inside
`launch()`, and/or (b) flip the `launch()` default to `False`. The `shell_cmd` tool
(the only production caller) always passes `console_window` explicitly
(`shell_cmd.py:272`), so neither change alters production behavior.

---

## 1. How console windows are currently created

All visible-window behavior lives in `agent_cascade/async_shell.py`, gated on the
`task.console_window` flag.

### 1a. Main tracked process (`async_shell.py:487-522`)
```python
creationflags = 0
env = None
if ON_WINDOWS:
    command, creationflags = configure_windows_utf8(command, create_new_console=task.console_window)
    env = _WIN_ENV
    ...
proc = subprocess.Popen(command, ..., creationflags=creationflags,
                        start_new_session=True, env=env)
```
`configure_windows_utf8` (`shell_utils.py:77-90`) returns
`flags = CREATE_NEW_PROCESS_GROUP`, and **ORs in `CREATE_NEW_CONSOLE` only when
`create_new_console` is True** (i.e. `task.console_window`).

Note: because the main process's stdout/stderr are **piped**
(`subprocess.PIPE`), `CREATE_NEW_CONSOLE` alone does **not** render a visible window —
the console has no attached console host to display into.

### 1b. The "viewer" process — the actual visible window (`async_shell.py:534-565`)
```python
if ON_WINDOWS and task.console_window:
    viewer_cmd, _ = configure_windows_utf8(original_command, create_new_console=True)
    viewer = subprocess.Popen(
        ['cmd.exe', '/c', viewer_cmd],
        cwd=str(cwd) if cwd else None,
        creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
        env=env,
        # No stdout/stderr args → inherit → shows in its own visible window
    )
    task.viewer_process = viewer
```
This secondary `cmd.exe` re-executes the command **without** pipes, so it inherits a
real console and is what the user actually sees. **This is the window that pops.** It
runs only when `ON_WINDOWS and task.console_window`.

> Known defect (see `.agent_lessons/lessons_async_console_viewer_duplicate.md`): the
> viewer is an independent re-execution of the same command (double side-effects,
> fast-fail flicker). That is a separate pre-existing issue; it does not change the
> conclusion here that the viewer is the visible-window source.

**Conclusion:** a visible cmd window appears **iff** `ON_WINDOWS and
task.console_window == True` at `launch()` time.

---

## 2. Existing suppression mechanisms

| Mechanism | Location | Default | Effect |
|---|---|---|---|
| Pool toggle `_enable_async_shell_console_window` | `agent_pool.py:272` | **False** | Drives the tool-level decision |
| Tool-level read of pool toggle | `shell_cmd.py:255-257` | — | `console_window = bool(pool._enable_async_shell_console_window)` (defaults False) |
| **Env-var opt-out** `QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW` | `shell_cmd.py:261-262` | unset | If truthy (`1`/`true`/etc.) → forces `console_window = False` regardless of pool |
| Conftest sets the env var for all tests | `tests/conftest.py:39-42` | `= "1"` | **Intended** to suppress windows in tests, but only works via the `shell_cmd` tool path |
| `AsyncShellTask.console_window` dataclass field | `async_shell.py:175` | `False` | Per-task flag |
| `AsyncShellTracker.launch()` parameter | `async_shell.py:286` | **`True`** | ⚠️ Default contradicts docstring ("Default is False", line 299) and the dataclass default (`False`) |

**Key asymmetry (the bug):** the env-var opt-out is wired into the `shell_cmd` tool
(`shell_cmd.py:261`) but **not** into `AsyncShellTracker.launch()`. Any caller that
bypasses the tool and calls `launch()` directly is **not** protected by the conftest
env var.

### Why production is safe
The **only production caller** of `launch()` is the `shell_cmd` tool
(`shell_cmd.py:266-273`), and it **always passes `console_window` explicitly**
(line 272). So the `launch()` default value is irrelevant in production. The env var
is set **only** in `tests/conftest.py`, so it has zero effect on production.

---

## 3. Test files that invoke shell/subprocess

### 3a. Tests that call `tracker.launch()` with a REAL process (⚠️ pop a window on Windows)

These bypass the `shell_cmd` tool, call `launch()` directly with **no
`console_window` argument**, and execute a **real** (non-mocked) command. They fall
through to the `launch()` default `True` → spawn the viewer window.

**`tests/test_async_shell_kill.py`**
- `TestKillTaskWithRealProcess.test_kill_terminates_long_running_process` — `launch()` at line 169 (real `ping`/`sleep`)
- `TestKillTaskWithRealProcess.test_kill_returns_only_after_process_dead` — `launch()` at line 222 (real `ping -t`)

**`tests/test_async_shell_failure_scenarios.py`** (all real processes)
- `TestKilledProcessCleanup.test_killed_process_is_removed_from_active_tasks` — line 69
- `TestKilledProcessCleanup.test_killed_process_no_longer_sends_heartbeats` — line 103
- `TestKilledProcessCleanup.test_killed_process_actually_terminated_on_os` — line 133 (Windows-only)
- `TestTimeoutBehavior.test_timeout_kills_long_running_process` — line 173
- `TestTimeoutBehavior.test_timeout_completion_message_sent` — line 206
- `TestTimeoutBehavior.test_normal_completion_produces_single_merged_message` — line 245
- `TestStderrCapture.test_stderr_captured_on_failure` — line 293
- `TestStderrCapture.test_nonzero_exit_code_recorded` — line 323
- `TestKillEdgeCases.test_kill_already_finished_task` — line 364

**`tests/test_async_shell_cmd.py`** — `TestRealExecution` (all real processes)
- `test_real_echo_output_captured` — `launch()` at line 630
- `test_real_python_exit_code_zero` — line 662
- `test_real_command_nonzero_exit` — line 694

**Total: 15 real-launch call sites across 3 files → each pops a window on Windows.**

### 3b. Tests that call `launch()` but are MOCKED (no window)
- `test_async_shell_cmd.py` lines 435, 453, 473, 523 — `tracker.launch = MagicMock(...)`
- `test_async_shell_kill.py` line 320 — `with patch('subprocess.Popen', mock_popen)`

### 3c. Tests using `subprocess.run` / `Popen` directly (no window)
All use `capture_output=True` and/or only `CREATE_NEW_PROCESS_GROUP` (no
`CREATE_NEW_CONSOLE`), so no visible window:
- `test_async_shell_kill.py:203, 251` — `tasklist` verification (`capture_output`)
- `test_async_shell_kill.py:523` — `subprocess.Popen(['cmd','/c','start /B ping ...'], creationflags=CREATE_NEW_PROCESS_GROUP)` (no NEW_CONSOLE)
- `test_async_shell_kill.py` lines 558-716 — all `patch('subprocess.run')` (mocked)
- `test_async_shell_failure_scenarios.py:153` — `tasklist` (`capture_output`)
- `test_greptool.py:26`, `tests/scripts/grep_compare.py:44,88,111` — `grep`/`find` (`capture_output`)
- `test_code_interpreter_extra_mounts.py` — `patch('subprocess.run')` (mocked; Docker)
- `test_zmq_cleanup.py:148,239,415` — `patch('subprocess.run')` (mocked)
- `test_safe_shell_cmd.py:212` — only a **string** containing `os.system` (classification test, never executed)

**No test file references `CREATE_NEW_CONSOLE`, `CREATE_NO_WINDOW`, or sets
`creationflags` to pop a window** — the only window source is the tracker's viewer path.

---

## 4. Which tests pop windows and why (root cause)

The 15 real-launch call sites in §3a pop windows **because**:
1. They call `tracker.launch()` directly (not via the `shell_cmd` tool).
2. They omit the `console_window` argument → the `launch()` default **`True`**
   (`async_shell.py:286`) applies.
3. The conftest env var (`QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW=1`) is **not
   consulted inside `launch()`**, so it cannot suppress these.

On non-Windows (`ON_WINDOWS` is False) these same tests do **not** pop windows; the
issue is Windows-specific, which matches the dev environment (Windows, `N:\work`).

---

## 5. Recommended fix (production-safe, minimal)

**Primary — make the existing test opt-out apply at the `launch()` boundary.**
Add the env-var check inside `AsyncShellTracker.launch()` (after the signature, before
task construction, ~`async_shell.py:327`):
```python
# Respect the test-harness opt-out so direct launch() calls (e.g. regression tests)
# never pop a visible window. Only takes effect when the env var is set truthy.
if console_window and os.getenv("QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW", "").strip() not in ("", "0", "false", "False"):
    console_window = False
```
Since `tests/conftest.py:42` already sets this env var for every pytest run, this
single change fixes **all** §3a tests at once, without touching each test file.
(`os` is already imported in `async_shell.py`.)

**Secondary (belt-and-suspenders) — fix the inconsistent default.**
Change `launch()` signature default from `console_window: bool = True` to
`console_window: bool = False` (`async_shell.py:286`). This aligns the signature with
the docstring (line 299) and the `AsyncShellTask` dataclass default (line 175), and
makes direct `launch()` calls window-free even if the env var were ever unset.

**Why both are production-safe:**
- The `shell_cmd` tool (the only production caller) passes `console_window` explicitly
  (`shell_cmd.py:272`), so the `launch()` default is irrelevant in production.
- The env var is set only in `tests/conftest.py`, so it has zero production effect.
- The `shell_cmd` tool keeps its own env-var check (lines 261-262) unchanged, so the
  tool path behaves exactly as before.

**Do NOT** add `CREATE_NO_WINDOW` globally — the viewer intentionally re-executes the
command to provide a *visible* window for user inspection (todo #21). Suppression must
stay opt-out, not a hard-coded flag, so the production "inspect/interact" feature
still works when the pool toggle is on.

### Verification after fix
- Run the three files on Windows and confirm **no** cmd window appears:
  `pytest tests/test_async_shell_kill.py tests/test_async_shell_failure_scenarios.py tests/test_async_shell_cmd.py`
- Assert in a new regression test that a direct `tracker.launch()` with the conftest
  env var set produces `task.console_window is False` (guards against regression).

---

## Confidence Level

| Claim | Level |
|---|---|
| Window source is the viewer `cmd.exe` (CREATE_NEW_CONSOLE) gated on `task.console_window` | **Confirmed** (read `async_shell.py:487-565`) |
| `launch()` default is `True`, contradicting docstring + dataclass default | **Confirmed** (`async_shell.py:286` vs 299/175) |
| Env-var opt-out is only in `shell_cmd.py`, not `launch()` | **Confirmed** (grep shows only `shell_cmd.py:261` + `conftest.py:42`) |
| The 15 listed real-launch tests pop windows on Windows | **High** (direct `launch()` + real process + no arg → default `True`) |
| Fix is production-safe | **High** (tool always passes explicit value; env var test-only) |

## Open Questions / Assumptions
- **Assumption:** "regression tests" in todo #120 = the pytest suite under `tests/`.
  Verified by code reading; I did **not** execute the suite (running it would itself
  pop the windows and involves multi-second sleeps). The window-popping conclusion is
  from static analysis, which is conclusive given the deterministic flag logic.
- **Assumption:** dev/CI test host is Windows (matches `N:\work`). On Linux/mac these
  tests already do not pop windows.
- **Not investigated (out of scope, pre-existing):** the viewer double-execution /
  fast-fail flicker defect (`lessons_async_console_viewer_duplicate.md`) — separate
  from window suppression.

## Suggested Next Actions
1. Apply the **primary** env-var check inside `launch()` (one block, ~4 lines).
2. Optionally apply the **secondary** default flip to `False` for consistency.
3. Add a regression test asserting `task.console_window is False` under the conftest
   env var.
4. Run the three affected test files on Windows and visually confirm no window pops.
5. Delegate implementation to a **coder** agent (with `testing-best-practices` skill),
   then have a **reviewer** verify the diff is production-safe.
