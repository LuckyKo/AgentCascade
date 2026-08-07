# ConPTY Implementation — async_shell.py Diff Summary

**File:** `N:\work\WD\AgentCascade\agent_cascade\async_shell.py`
**Scope:** Windows `console_window=True` async shell execution
**Method:** stdlib-only ConPTY via ctypes (no external binaries / pywin32)

---

## 1. Removed Code

**Tee-based PowerShell wrapper (fully removed, no longer present):**
- `_build_tee_command` — the PowerShell command wrapper that piped output to a log file + visible console window.
- `_tail_log_file` — the helper that polled/tailed the log file to feed heartbeat buffers.

**Legacy "viewer" / log-file machinery reduced to deprecated no-op stubs:**
- `_cleanup_viewer(...)` — now a no-op; docstring marks it "DEPRECATED: No-op. Viewer processes removed in ConPTY refactor."
- `_kill_viewer_process(...)` — now a no-op; same DEPRECATED marker.
- `_cleanup_log_files(...)` — now a no-op; "DEPRECATED: No-op. Log files removed in ConPTY refactor (output captured via pipes)."
- Removed the previous log-file lifecycle (creation, path tracking, cleanup) and the PowerShell `CreateNewConsole` console viewer that re-ran a command through a PowerShell wrapper.

**Note:** No remaining references to `tee`, `_tail_log`, or `_build_tee_command` exist in the file.

---

## 2. Added Code

**Module-level ConPTY constants (ctypes):**
- `PSEUDOCONSOLE_INHERIT_CURSOR 0x1`, `PIPE_ACCESS_INBOUND/OUTBOUND`, `PIPE_TYPE_BYTE`, `PIPE_READMODE_BYTE`, `PIPE_WAIT`, `PIPE_UNLIMITED_INSTANCES`, `INVALID_HANDLE_VALUE`, `DUPLICATE_SAME_ACCESS`.
- `STARTF_USESTDHANDLES 0x00000100`.
- `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 0x00020016`, `HANDLE_FLAG_INHERIT`, `STD_INPUT/OUTPUT/ERROR_HANDLE`.
- `COORD` and `STARTUPINFOEXW` ctypes `Structure` definitions.

**ctypes function prototypes (Kernel32):**
- `CreatePseudoConsole`, `ClosePseudoConsole`, `InitializeProcThreadAttributeList`, `UpdateProcThreadAttribute`, `DeleteProcThreadAttributeList`, `CreatePipe`, `SetHandleInformation`, `DuplicateHandle`, `CreateFileW`, `ReadFile`, `WriteFile`, `CloseHandle`.

**New functions:**
- `_create_inheritable_pipe() -> Tuple[int,int]` — creates an anonymous pipe with inheritable handle(s) via `CreatePipe` + `SetHandleInformation`.
- `_close_handle(h)` — safe `CloseHandle` wrapper.
- `_build_relay_script() -> str` — returns a self-contained Python script for the relay display window (`CREATE_NEW_CONSOLE`). Reads binary stdin (fed by the ConPTY reader thread) and writes it raw to its own console; prints `--- finished ---` on EOF. It never re-executes the user's command.
- `_spawn_conpty_with_relay(command, cwd, task, tool_id)` — orchestrates: create ConPTY I/O pipes, duplicate stdin write handle, `CreatePseudoConsole`, create relay pipe + spawn relay window, spawn user command attached to ConPTY via `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE`, then start the reader thread.
- `_conpty_reader_thread_fn(conpty_out_handle, relay_write_handle)` — background thread reading raw bytes from the ConPTY output pipe via `ReadFile`; writes raw bytes to the relay display pipe and decodes/splits into `task.stdout_lines` for heartbeats. Exits on 0-byte read (ConPTY closed).

**New handle fields / constants:**
- Named timing constants: `CONPTY_READ_BUFFER_SIZE 65536`, `CONPTY_DEFAULT_COLUMNS 120`, `CONPTY_DEFAULT_ROWS 50`, etc.

---

## 3. Modified Functions

### `AsyncShellTask` (dataclass)
- Added ConPTY-specific runtime fields (set dynamically via `setattr`, not dataclass fields, to avoid type issues):
  - `_conpty_handle` (pseudoconsole handle)
  - `_relay_proc` (relay display window Popen)
  - `_relay_write_handle` (pipe write handle feeding the relay)
  - `_reader_thread` (ConPTY output reader thread)
  - `_stdin_write_handle` (duplicate of ConPTY input write handle, used by `send_input`)

### `_spawn_process` (trajectory change)
- **Prefers ConPTY mode** when `ON_WINDOWS and task.console_window`:
  - Calls `_spawn_conpty_with_relay()`.
  - Stores ConPTY handles on the task and sets `use_conpty = True`.
  - On ConPTY spawn failure, logs a warning and **falls back to piped mode**.
- **Piped mode retained** as the fallback / non-Windows path:
  - Adds `-NoProfile` for PowerShell commands.
  - Uses `proc.stdin/stdout/stderr` and drain threads (`_drain_t_out`/`_drain_t_err`).

### `_track_task`
- Now detects ConPTY mode via `task._conpty_handle is not None`; skips drain-thread join when ConPTY is in use.
- In `finally`, calls the new `_cleanup_conpty(task, tool_id)` then the no-op `_cleanup_log_files`.

### New `_cleanup_conpty(task, tool_id)`
- Closes relay write handle (signals EOF to relay window), joins reader thread, calls `ClosePseudoConsole`, closes stdin write handle, terminates relay process if still alive.

### `send_input`
- **ConPTY mode:** if `task._stdin_write_handle` is set, encodes input as UTF-8 and sends via `_kernel32.WriteFile` to the pseudoconsole input handle.
- **Piped mode** unchanged: writes to `proc.stdin`.

### `_poll_loop`
- Docstring/semantics updated: for ConPTY mode output is captured by the reader thread directly from the pseudoconsole (no log tailing needed); drain threads are `None` in ConPTY mode.

### `_cleanup_viewer` / `_kill_viewer_process` / `_cleanup_log_files`
- Reduced to deprecated no-op stubs (machinery no longer used).

---

## 4. Key Architectural Changes

1. **Single execution of the user's command.** Previously the window-display path ran the command through a PowerShell/tee + log-file wrapper. Now the user command runs **exactly once**, attached directly to a ConPTY pseudoconsole.

   ```
   stdin ─→ ConPTY(pty_in_read/write) ─→ ConPTY ─→ (pty_out_write/read)
                                                    └─ reader thread
                                                       ├─ task buffers (heartbeats)
                                                       └─ relay pipe → relay window (CREATE_NEW_CONSOLE)
   ```

2. **ConPTY output stream is the single source of truth.** The background reader thread captures output once and fans it out to both the heartbeat task buffers and the visible relay window, eliminating tee files and redundant readers.

3. **Relay display process is non-executing.** The relay window (`_build_relay_script`) only displays bytes it receives on stdin; it never re-runs the command, and exits itself when its input pipe closes (EOF).

4. **stdin via `WriteFile`.** Interactive input is sent to the pseudoconsole input handle (`_stdin_write_handle`) rather than `proc.stdin`.

5. **Graceful degradation.** If ConPTY spawning fails, code logs a warning and falls back to the classic piped mode, preserving behavior on non-Windows or when `console_window=False`.

6. **Resource lifecycle concentrated in `_cleanup_conpty`.** Cleanup closes the relay pipe, joins the reader thread, releases the pseudoconsole and handles in one place, replacing the old log-file/viewer teardown.

---

## Confidence & Notes
- **Confidence: High** based on full read of the current file (2014 lines).
- The removed tee functions (`_build_tee_command`, `_tail_log_file`) are absent from the current file, consistent with the stated change; their prior implementations were not preserved in source so removal is inferred from absence + the new docstrings describing the old approach as replaced.
- No diff against a prior committed version was available in this session — the summary reflects the final implementing state plus the removals you described. If an exact line-level diff against the previous file revision is needed, obtain the prior `async_shell.py` and run a diff.