"""Shell execution — safety check and command execution mixin."""

import os
import re
import signal
import subprocess
import threading
import time
from typing import List, Optional, Tuple

# ─── Named constants (avoid magic numbers) ──────────────────────────────
PIPE_READ_SIZE = 4096                   # Bytes per read call on stdout/stderr pipes
DRAIN_THREAD_JOIN_TIMEOUT = 3           # Seconds to wait for drain threads after process ends
WINDOWS_UTF8_CODE_PAGE = '65001'        # Windows code page for UTF-8 output
TASKKILL_RETRY_DELAY = 0.5              # Seconds between taskkill passes on timeout
MAX_PROCESS_TREE_DEPTH = 10             # Max recursion depth when killing process descendants

# Platform flag (evaluated once at import time)
ON_WINDOWS = os.name == 'nt'

# Cached Windows environment dict (set PYTHONIOENCODING for child Python processes)
if ON_WINDOWS:
    _WIN_ENV = os.environ.copy()
    _WIN_ENV['PYTHONIOENCODING'] = 'utf-8'
else:
    _WIN_ENV = None  # noqa: constant used only in method below


# ─── Module-level helper (used inside ShellMixin) ──────────────────────

def _run_taskkill(pid: int, timeout: int = 10):
    """Run taskkill to forcibly terminate a Windows process by PID.

    Args:
        pid: Process ID to kill.
        timeout: Seconds before the taskkill call itself times out.

    Returns:
        subprocess.CompletedProcess on success.

    Raises:
        Exception if taskkill fails or times out.
    """
    return subprocess.run(
        ['taskkill', '/F', '/PID', str(pid)],
        capture_output=True, timeout=timeout,
        encoding='utf-8', errors='replace',
    )


# ─── Mixin: Shell methods for OperationManager ─────────────────────────

class ShellMixin:
    """Shell execution methods. Expects self to have __init__-set attributes."""

    # ------------------------------------------------------------------
    @staticmethod
    def _is_safe_readonly_shell_command(command: str) -> bool:
        """Check if a shell command is purely read-only (directory listing/search)."""
        cmd = command.strip()
        if not cmd:
            return False

        # Command chaining with && ; ||
        if '&&' in cmd or ';' in cmd or '||' in cmd:
            return False

        # Subshell execution: $(...) or backticks
        if '$(' in cmd or '`' in cmd:
            return False

        # Redirections that write to files (but not 2>/dev/null or 2>&1)
        redirect_match = re.search(r'>[^>]', cmd)
        if redirect_match:
            redir_target = redirect_match.group(0)
            if '/dev/null' not in redir_target and 'NUL' not in redir_target.upper() and '&' not in redir_target:
                return False

        # Background processes: & that's not part of && (already checked above)
        if re.search(r'(?<!&)&(?!&)', cmd):
            return False

        pipeline = cmd.split('|')

        SAFE_PIPE_COMMANDS = {
            'grep', 'egrep', 'fgrep', 'head', 'tail', 'sort', 'uniq',
            'wc', 'cat', 'more', 'less', 'awk', 'sed', 'cut', 'tr',
            'tee', 'xargs', 'comm', 'diff', 'nl', 'rev', 'fold'
        }

        SAFE_PRIMARY_COMMANDS = {
            'find', 'dir', 'ls', 'tree', 'directory',
            'vfd', 'where', 'whereis', 'locate', 'which', 'type',
            'pwd', 'stat', 'file', 'du', 'df',
        }

        first_cmd_part = pipeline[0].strip()

        words = first_cmd_part.split()
        if not words:
            return False

        primary_cmd = words[0].lower()

        # Check for "cmd /c" or "powershell -c" wrappers
        if primary_cmd in ('cmd', 'command.com'):
            return False
        if primary_cmd in ('powershell', 'pwsh'):
            return False

        if primary_cmd not in SAFE_PRIMARY_COMMANDS:
            return False

        # For "find" commands, check that they don't use -exec with dangerous actions
        if primary_cmd == 'find':
            if '-exec' in cmd or '-ok' in cmd:
                return False

        for i, stage in enumerate(pipeline[1:], 1):
            stage_words = stage.strip().split()
            if not stage_words:
                continue
            stage_cmd = stage_words[0].lower()
            if stage_cmd not in SAFE_PIPE_COMMANDS:
                return False

        return True

    # ------------------------------------------------------------------
    def _configure_windows_utf8(self, command: str) -> Tuple[str, int]:
        """Prepend chcp 65001 to force CMD into UTF-8 mode on Windows.

        Returns:
            (modified_command, creationflags) tuple ready for subprocess.Popen.
        """
        return (f'chcp {WINDOWS_UTF8_CODE_PAGE} > nul 2>&1 & {command}',
                subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    def _terminate_process_tree_windows(self, pid: int):
        """Kill a Windows process and all its descendants.

        Strategy:
          1. taskkill /F /T (first pass — kills process + immediate children)
          2. Brief sleep then second taskkill /F /T for good measure
          3. WMIC sweep to find deeper descendants, kill them individually
        """
        from agent_cascade.log import logger

        # First pass: taskkill with tree flag
        try:
            result = _run_taskkill(pid)
            if result.returncode != 0:
                logger.warning(f"taskkill returned code {result.returncode} for PID {pid}")
        except Exception as e:
            logger.warning(f"taskkill failed for PID {pid}: {e}")

        # Second pass: retry taskkill after brief delay
        time.sleep(TASKKILL_RETRY_DELAY)
        try:
            result = _run_taskkill(pid)
            if result.returncode != 0:
                logger.debug(f"Second-pass taskkill returned code {result.returncode} for PID {pid}")
        except Exception as e:
            logger.debug(f"Second-pass taskkill failed (non-critical): {e}")

        # WMIC sweep for deeper descendants
        try:
            def _get_child_pids(parent_pid):
                """Query child PIDs of a given parent via WMIC."""
                res = subprocess.run(
                    ['wmic', 'process', 'where',
                     f'ParentProcessId={parent_pid}',
                     'get', 'ProcessId'],
                    capture_output=True, text=True, timeout=5,
                    encoding='utf-8', errors='replace',
                )
                pids = []
                for line in res.stdout.strip().split('\n'):
                    line = line.strip()
                    if line.isdigit():
                        pids.append(int(line))
                return pids

            descendants: set = set()
            to_check = [pid]
            depth = 0

            # Recurse until no new children are found, bounded by max depth guard
            while to_check and depth < MAX_PROCESS_TREE_DEPTH:
                next_level = []
                for check_pid in to_check:
                    children = _get_child_pids(check_pid)
                    for cpid in children:
                        if cpid not in descendants and cpid != pid:
                            descendants.add(cpid)
                            next_level.append(cpid)
                to_check = next_level
                depth += 1

            # Kill discovered descendants individually (best effort)
            for dpid in descendants:
                try:
                    _run_taskkill(dpid, timeout=5)
                except Exception as e:
                    logger.debug(f"WMIC child kill failed (non-critical): {e}")
        except Exception as e:
            logger.warning(f"WMIC descendant sweep failed: {e}")

    # ------------------------------------------------------------------
    def execute_shell_command(
        self, command: str, justification: str, agent_name: str,
        cwd: str = ".", char_limit: int = 2000, timeout: Optional[int] = None,
    ) -> str:
        """Execute a shell command — auto-approved for safe read-only commands."""
        try:
            resolved_cwd = self._resolve_path(cwd, mode="rw")
        except Exception as e:
            return f"ERROR: Invalid working directory: {str(e)}"

        if len(command) > char_limit:
            return f"ERROR: Command exceeds maximum length of {char_limit} characters."

        DEFAULT_SHELL_TIMEOUT = 30
        MAX_SHELL_TIMEOUT = 3600
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0 or timeout > MAX_SHELL_TIMEOUT):
            return f"ERROR: Invalid timeout value: {timeout}. Must be a positive integer between 1 and {MAX_SHELL_TIMEOUT}."
        effective_timeout = timeout if timeout is not None else DEFAULT_SHELL_TIMEOUT

        is_safe = self._is_safe_readonly_shell_command(command)

        if is_safe:
            approved = True
            reason = "Auto-approved: safe read-only filesystem operation"
            justification_text = reason
        else:
            description = (
                f"⚠️ **SECURITY WARNING**: This is a host shell command. It can potentially bypass folder restrictions!\n\n"
                f"**CWD**: {resolved_cwd}\n"
                f"**Execute Shell Command**:\n```bash\n{command}\n```\n**Justification**: {justification}"
            )

            approved, reason = self.request_user_approval(
                agent_name=agent_name,
                tool_name='shell_cmd',
                tool_args={'command': command, 'justification': justification, 'cwd': cwd},
                description=description,
            )

            if not approved:
                return f"REJECTED BY USER: {reason}"
            justification_text = reason

        try:
            from agent_cascade.log import logger
            from agent_cascade.tool_utils import MAX_SPILL_SIZE, generate_spillover_filename

            # ── Platform-specific setup ──────────────────────────────
            if ON_WINDOWS:
                command, creationflags = self._configure_windows_utf8(command)
                env = _WIN_ENV  # pre-cached dict with PYTHONIOENCODING set
            else:
                creationflags = 0
                env = None

            proc = subprocess.Popen(
                command,
                cwd=str(resolved_cwd),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=creationflags,
                start_new_session=True,
                env=env,
            )

            # Use threaded pipe reading to prevent output loss on timeout/hang.
            stdout_chunks: List[str] = []
            stderr_chunks: List[str] = []
            drain_errors: List[Exception] = []

            def _drain_pipe(pipe, chunks: List[str], errors: List[Exception]) -> None:
                """Continuously drain a pipe into a list until EOF or error."""
                try:
                    while True:
                        chunk = pipe.read(PIPE_READ_SIZE)
                        if not chunk:
                            break  # EOF
                        chunks.append(chunk)
                except Exception as e:
                    errors.append(e)

            t_out = threading.Thread(target=_drain_pipe, args=(proc.stdout, stdout_chunks, drain_errors), daemon=True, name='shell_stdout_reader')
            t_err = threading.Thread(target=_drain_pipe, args=(proc.stderr, stderr_chunks, drain_errors), daemon=True, name='shell_stderr_reader')
            t_out.start()
            t_err.start()

            # Wait for process to finish within the timeout window
            try:
                proc.wait(timeout=effective_timeout)
                result_ok = True
            except subprocess.TimeoutExpired:
                result_ok = False
                if ON_WINDOWS:
                    self._terminate_process_tree_windows(proc.pid)
                else:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception as e:
                        logger.debug(f"Unix process group kill failed (falling back to proc.kill): {e}")
                        try:
                            proc.kill()
                        except Exception as e:
                            logger.debug(f"Process kill fallback failed (non-critical): {e}")

            # Wait for reader threads to drain remaining pipe buffers.
            t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
            t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

            if t_out.is_alive() or t_err.is_alive():
                time.sleep(0.1)  # Brief grace period for thread cleanup

            if drain_errors:
                logger.warning(
                    f"Pipe drain errors on PID {proc.pid}: "
                    + "; ".join(str(e) for e in drain_errors)
                )

            stdout: str = ''.join(stdout_chunks)
            stderr: str = ''.join(stderr_chunks)

            if result_ok:
                output = ""
                if stdout:
                    output += f"STDOUT:\n{stdout}\n"
                if stderr:
                    output += f"STDERR:\n{stderr}\n"

                if proc.returncode == 0:
                    status = "Command completed successfully."
                else:
                    status = f"Command exited with return code {proc.returncode}."

                if not output.strip():
                    output = "No output produced."

                final_output = output
                if char_limit != -1 and len(output) > char_limit:
                    log_dir = self.base_dir / 'logs' / 'spillover'
                    log_dir.mkdir(parents=True, exist_ok=True)

                    if len(output) > MAX_SPILL_SIZE:
                        output_copy = output[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"
                    else:
                        output_copy = output

                    spill_filename = generate_spillover_filename(agent_name, 'shell', log_dir)
                    spill_path = log_dir / spill_filename

                    try:
                        spill_path.write_text(output_copy, encoding='utf-8')
                        try:
                            rel_spill = str(spill_path.relative_to(self.base_dir))
                        except ValueError:
                            rel_spill = str(spill_path)
                    except Exception as e:
                        rel_spill = f"ERROR SAVING SPILL: {e}"

                    final_output = output[:char_limit] + f"\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. Full output saved to: {rel_spill}]"
                    status += " [TRUNCATED]"

                if is_safe:
                    final_msg = f"AUTO-APPROVED: {status}\n"
                else:
                    final_msg = f"APPROVED: {status}\n"
                    if justification_text:
                        final_msg += f"Security Justification: {justification_text}\n"
                return final_msg + f"\n{final_output}"

            # ── Timeout path ────────────────────────────────────────
            output = ""
            if stdout:
                output += f"STDOUT (partial):\n{stdout}\n"
            if stderr:
                output += f"STDERR (partial):\n{stderr}\n"

            timeout_msg = (
                f"ERROR: Command timed out after {effective_timeout} seconds. "
                f"All child processes have been forcibly terminated. "
                f"Command was: `{command[:200]}`. "
                f"If the process is expected to take a long time, consider using a background command "
                f"(e.g. using '&' on linux or 'Start-Job' on windows) or optimizing the task."
            )
            if output.strip():
                return f"{timeout_msg}\n\n{output}"
            return timeout_msg

        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"