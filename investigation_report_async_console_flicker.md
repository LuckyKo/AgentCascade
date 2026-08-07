# Investigation Report — Async Shell Console Window Flickers & Closes Immediately

**Date:** 2026-08-07
**Investigators:** rc_shell_console_investigation2 (researcher)
**Status:** Root cause analysis complete. No code changes made.

---

## 1. Confirmed: The Fix IS Present

The applied fix is confirmed **present and uncommitted** in the working tree
(`git diff` on `N:\work\WD\AgentCascade\agent_cascade\async_shell.py` shows
both hunks as `Modified, not committed`):

| Location | Before | After |
|---|---|---|
| `async_shell.py:472` | `configure_windows_utf8(command, create_new_console=task.console_window)` | `create_new_console=False` |
| `async_shell.py:487` (PowerShell re-apply) | same `create_new_console=task.console_window` | `create_new_console=False` |
| `async_shell.py:721-731` | `_cleanup_viewer(viewer, tool_id)` on normal completion | detach-only (`task.viewer_process = None; no wait/kill`) |

`_cleanup_viewer()` (line 631) still exists but is **no longer called anywhere**
(verified by grep — only its definition matches).

Runtime confirmation (console.log, post-restart, current code line numbers):
- `async_shell.py:509` Launched main process (create_new_console=False)
- `async_shell.py:537` Viewer spawned (create_new_console=True)
- `async_shell.py:728` "Viewer detached ... allowing natural completion"

So the intended fix was applied and IS running. The problem persists for a
different reason than the one the fix addressed.

## 2. How console_window Is Determined & Passed

- `shell_cmd.py:92` parses `async_mode` from params.
- `shell_cmd.py:133-144` auto-async triggers for `timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD` (with default heartbeat) OR explicit `async_mode:true`.
- `shell_cmd.py:253-255` → `console_window = True` unless
  `agent_pool._enable_async_shell_console_window` is falsy (default is **True**, `agent_pool.py:320`).
- `shell_cmd.py:265` → `tracker.launch(..., console_window=console_window)`.
- `async_shell.py:309-315` → stored on `AsyncShellTask.console_window`.
- `async_shell.py:521` `if ON_WINDOWS and task.console_window:` → spawn viewer.

No path re-enables `create_new_console=True` for the **main** process after the fix
(only `_spawn_process` at 472/487 sets main's flags; both are now `False`).

## 3. Exact Code Path for async_mode=true + console_window enabled

`_spawn_process` (`async_shell.py:449-569`):
1. Line 472/487: main process `configure_windows_utf8(cmd, False)` → flags =
   `CREATE_NEW_PROCESS_GROUP` (no new console window). Piped stdio (489-502).
2. Line 521-538: **viewer "duplicate"**: `cmd.exe /c <chcp 65001> & <original command>`
   spawned with `CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP` and **no piped
   stdio** (inherits a fresh console window).

This is the crux: **the viewer is NOT an attachment / mirror of the tracked
process.** It is an *independent re-execution* of the same command. It does not
share the main process's stdout/stderr or life cycle.

## 4. Root Causes: Why the Window Still Flickers/Closes

### A. The viewer is a duplicate execution (design flaw) — HIGH confidence
The viewer runs `cmd.exe /c` with a **copy** of the command (line 524 uses
`original_command`). It is a fully separate process from the tracked `main`.
Consequences:
- The window's lifetime = the Viewer's **own** copy's lifetime.
- Its on-screen output is the viewer copy's (may not match tracked output /
  side effects).
- It is never attached to the tracked process; "hello world" is double-run
  for any long command.

### B. Window closes the moment the (viewer's) command ends — Confirmed mechanism
The viewer is launched with `cmd.exe /c ...` (no `cmd /k`, no `pause`, no
keep-alive). When that shell finishes, the console window closes. Because the
main and viewer are started together from the same command, they finish at
**about the same time**. So on normal completion the "keep it open" intent of the
fix is defeated — the viewer naturally ends when the command ends, at which
point the window closes (right around the final heartbeat/completion).

### C. Double-execution / fast-fail -> instant flash — HIGH confidence for the "immediate" symptom
When the command is not valid in the viewer's context (bash-style command run
under `cmd.exe`, `%`-expansion issues, etc.) the command **fails (rc=1)
instantly**. Existing evidence (console.log, various line versions):
- `... ... "for i in {1..30}; do echo Step; sleep 2; done"` → `Early completion detected rc=1` immediately.
- `cmd /c for /L %i ... && ...` → `rc=1` immediately.
- In these cases: main completes at launch (rc=1), the viewer's duplicate also
  fails immediately → **window flashes and closes in <100ms**. The agent then
  receives the early completion output — perceived as "closes as soon as the
  first output/heartbeat arrives."

Thus the *most reproducible* "flicker + immediate close" case is a command that
fails fast (very common for wrong-shell syntax), where both copies (viewer + main)
exit instantly.

### D. Detach-without-keepalive does not fix the long-running case
For a genuinely long OK command (e.g. `Start-Sleep 30`), the viewer window does
stay open for the command's duration (seen at 02:28:12 run). The problem is:
- that window duplicates the command (double side effects),
- closing is driven by the viewer's own completion, not the tracked process,
- `detach` on normal completion only stops force-kill; it does not keep the
  window open any longer, since the viewer and main share the same command
  lifetime. The only way detach truly helps is if the viewer outlives the main
  (e.g. main killed early while the viewer continues). Detach doesn't keep it.

### E. No keep-alive "holding window" for inspection
There's no mechanism to hold the window after the viewer's command ends (like
`cmd /k`/pause or a `pause`/`timeout` after the command). So even successful
commands close the console at its natural end — no lasting inspection.

## 5. Discrepancy Between Intended and Actual Behavior

| Intent | Actual |
|---|---|
| "Viewer finishes naturally and keeps the window open on normal completion" | Viewer finishes when ITS OWN copy finishes, which races with the main copy → usually closes at/near main completion. |
| viewer "shows the command output for inspection" | It re-**executes** the command (double execution); it doesn't mirror the tracked process windows. |
| Fix stops main window | ✅ main create_new_console=False works (no main popup). |
| Fix stops viewer zero-kill on main normal-completion | ✅ detach works — but only relevant if viewer runs *longer* than main, which for a mirrored copy basically never stabilizes. |

## 6. Recommendations (no code changes made)

1. **Make the viewer actually mirror the main process** rather than duplicate:
   - Preferred: Use the **main** process with `CREATE_NEW_CONSOLE` and have the
     *headless* viewer be a fake one, OR
   - Attach the real process via a console/shared handle (Windows
     `ATTACH_PARENT_PROCESS` / shared conhost) — complex, but the only correct
     way to "show the running tracked process".
2. Simpler fix*: Make the main process keep a persistent console window by
   launching with `create_new_console=True` and piped internals to conhost;
   remove the duplicate viewer entirely. The original comment at 515-518 noted
   CREATE_NEW_CONSOLE + piped stdio doesn't produce a visible conhost window
   — confirm that claim; if true, this needs a real console attach mechanism.
3. **If duplicate-execution is kept**: at least fix the "instant flash" for
   fast-fail by adding a `cmd /k`-style hold (window stays open on error and
   shows it passed) so the user sees a useful message rather than an empty flash.
4. **Validate the double-execution**: for any shell command with side effects the
   current "viewer" runs the command twice — a correctness/security hazard.

## 7. Confidence Levels
- **Confirmed:** fix present and running; viewer is an independent re-execution; window is `cmd /c` (no keep-alive); fast-fail commands flash.
- **High:** the "immediate close on first output" is the early-completion fast-fail path colliding with the viewer closing.
- **Moderate:** the long-running case window closes at command end; keeping window truly open requires a redesign (attach or keep-alive).

## Open questions
- Does `CREATE_NEW_CONSOLE + piped` actually produce no visible window (verify the 515 comment)? If yes, the whole viewer approach must be replaced.
- Exact repro command Maine used most recently (to confirm fast-fail vs long).
- Whether the user wants window to persist after command completes (inspect outward).