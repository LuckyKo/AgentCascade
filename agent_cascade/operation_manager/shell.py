"""Shell execution — safety check and command execution mixin."""

import os
import re
import signal
import subprocess
import threading
import time
from typing import List, Optional, Tuple

# Project imports (used by ShellMixin.execute_shell_command)
from agent_cascade.log import logger
from agent_cascade.shell_utils import (
    DRAIN_THREAD_JOIN_TIMEOUT,
    drain_pipe_chunks,
    configure_windows_utf8,
)
from agent_cascade.tool_utils import truncate_with_spillover

# ─── Named constants (avoid magic numbers) ──────────────────────────────
TASKKILL_RETRY_DELAY = 0.5              # Seconds between taskkill passes on timeout
MAX_PROCESS_TREE_DEPTH = 10             # Max recursion depth when killing process descendants
DEFAULT_SHELL_TIMEOUT = 30              # Default seconds before shell command times out
MAX_SHELL_TIMEOUT = 3600                # Maximum allowed shell command timeout (1 hour)

# Platform flag (evaluated once at import time)
ON_WINDOWS = os.name == 'nt'

# Cached Windows environment dict (set PYTHONIOENCODING for child Python processes)
if ON_WINDOWS:
    _WIN_ENV = os.environ.copy()
    _WIN_ENV['PYTHONIOENCODING'] = 'utf-8'
else:
    _WIN_ENV = None  # noqa: constant used only in method below


# ─── Module-level helper (used inside ShellMixin) ──────────────────────

def _run_taskkill(pid: int, timeout: int = 10) -> subprocess.CompletedProcess:
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
    # Safe git sub-commands (all are read-only by nature)
    _SAFE_GIT_SUBCOMMANDS: set = {
        'diff', 'status', 'log', 'show', 'branch', 'tag', 'remote',
        'rev-parse', 'config', 'merge-base', 'describe', 'ls-files',
        'ls-tree', 'stash', 'shortlog', 'blame', 'name-rev', 'hash-object',
        'cat-file', 'for-each-ref', 'var', 'symbolic-ref',
        'version', 'rev-list', 'reflog', 'worktree',
        'count-objects', 'interpret-trailers',
        'notes', 'pack-refs', 'prune', 'replace', 'rerere',
        'verify-commit', 'verify-tag', 'verify-pack',
        'show-ref',
    }

    # Git flags to skip before detecting the subcommand
    # (some consume the next token: -c, -C, --git-dir, --work-tree)
    _GIT_FLAGS: set = {'--no-pager', '-c', '-p', '--paginate', '-C', '--git-dir', '--work-tree'}

    # Dangerous arguments per subcommand — renaming for clarity
    _DANGEROUS_STASH_ARGS: set = {'drop', 'pop', 'apply', 'clear'}
    _DANGEROUS_BRANCH_ARGS: set = {'-d', '-D', '-m', '-M', '-r', '--delete', '--move', '--set-upstream-to'}
    _DANGEROUS_TAG_ARGS: set = {'-d', '-D', '--delete', '-f', '-F', '-a', '-s'}
    _DANGEROUS_REMOTE_ARGS: set = {'set-url', 'add', 'rm', 'rename', 'set-branches', 'set-head'}
    _DANGEROUS_CONFIG_ARGS: set = {'--set', '--add', '--unset', '--unset-all', '-e', '--list-all'}
    _DANGEROUS_WORKTREE_ARGS: set = {'add', 'remove', 'checkout', 'prune'}

    # Safe pipe/filter commands for pipelines (e.g. git diff | grep 'changed')
    _SAFE_PIPE_COMMANDS: set = {
        'grep', 'egrep', 'fgrep', 'head', 'tail', 'sort', 'uniq',
        'wc', 'cat', 'more', 'less', 'cut', 'tr',
        'comm', 'diff', 'nl', 'rev', 'fold', 'findstr',
    }

    # Safe primary commands (directory listing / file inspection)
    _SAFE_PRIMARY_COMMANDS: set = {
        'find', 'dir', 'ls', 'tree', 'directory',
        'vfd', 'where', 'whereis', 'locate', 'which', 'type',
        'pwd', 'stat', 'file', 'du', 'df',
    }

    @staticmethod
    def _is_safe_readonly_shell_command(command: str) -> bool:
        """Check if a shell command is purely read-only (directory listing/search/git).

        Handles:
          - Basic commands: ls, find, dir, tree, pwd, stat, etc.
          - Git commands: git diff, git status, git log, etc.
          - cd && git pattern: cd <path> && git <command>
          - Pipes: git diff --stat | grep 'changed'
        """
        cmd = command.strip()
        if not cmd:
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

        # ── Strip "cd <path> &&" prefix ──
        stripped_cmd = ShellMixin._strip_cd_prefix(cmd)

        # Check for remaining chaining after stripping cd prefix
        if '&&' in stripped_cmd or ';' in stripped_cmd or '||' in stripped_cmd:
            return False

        # Split into pipeline stages
        pipeline = stripped_cmd.split('|')

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

        # ── Git command handling ──
        if primary_cmd == 'git':
            return ShellMixin._check_git_command(words, pipeline)

        # ── Regular command handling ──
        if primary_cmd not in ShellMixin._SAFE_PRIMARY_COMMANDS:
            return False

        # For "find" commands, check that they don't use -exec/-execdir with dangerous actions
        if primary_cmd == 'find':
            if '-exec' in stripped_cmd.lower() or '-ok' in stripped_cmd.lower():
                return False

        # Validate pipe stages are safe
        if not ShellMixin._validate_pipeline_stages(pipeline, ShellMixin._SAFE_PIPE_COMMANDS):
            return False

        return True

    @staticmethod
    def _strip_cd_prefix(cmd: str) -> str:
        """Strip 'cd <path> &&' or 'cd <path>;' prefix from a command.

        Handles both && and ; chaining after cd. Returns the command
        with the cd prefix removed, or the original cmd if no cd prefix.
        """
        # Two-step parse: find the first && or ;, check prefix starts with cd
        sep_match = re.search(r'\s*(&&|;)\s*', cmd)
        if sep_match:
            prefix = cmd[:sep_match.start()].strip()
            rest = cmd[sep_match.end():].strip()
            if prefix.lower().startswith('cd'):
                return rest
        return cmd

    @staticmethod
    def _validate_pipeline_stages(pipeline: list, safe_commands: set) -> bool:
        """Validate that all pipe stages after the first use safe commands.

        Args:
            pipeline: List of pipeline stages (split by '|').
            safe_commands: Set of allowed pipe/filter command names.

        Returns:
            True if all pipe stages are safe (or there are no extra stages).
        """
        for stage in pipeline[1:]:
            stage_words = stage.strip().split()
            if not stage_words:
                continue
            if stage_words[0].lower() not in safe_commands:
                return False
        return True

    @staticmethod
    def _check_git_command(words: list, pipeline: list) -> bool:
        """Check if a git command is safe (read-only).

        Skips git flags before detecting the subcommand, then validates that
        the subcommand isn't followed by write-modifying arguments
        (e.g. stash drop, branch -d, tag -d).

        Args:
            words: Split words of the first pipeline stage.
            pipeline: All pipeline stages after splitting by '|'.

        Returns:
            True if the git command is read-only.
        """
        # Handle 'git' with no subcommand (shows help, safe)
        if len(words) == 1:
            return True

        # Flags that consume the next token as their value (space-separated)
        _flags_with_values = {'-c', '-C', '--git-dir', '--work-tree'}

        # Skip git flags to find the actual subcommand
        idx = 1  # words[0] is 'git'
        while idx < len(words):
            word = words[idx].lower()
            # Check for exact flag match or =-joined form (e.g. --git-dir=.git)
            flag = word.split('=', 1)[0]
            if flag not in ShellMixin._GIT_FLAGS:
                break
            if flag in _flags_with_values:
                if '=' in words[idx]:
                    idx += 1  # --git-dir=.git is one token
                elif idx + 1 < len(words):
                    idx += 2  # -c color.ui=always is two tokens
                else:
                    idx += 1
            else:
                idx += 1

        if idx >= len(words):
            return True  # git with only flags is safe

        subcommand = words[idx].lower()
        if subcommand not in ShellMixin._SAFE_GIT_SUBCOMMANDS:
            return False

        # Collect remaining arguments after the subcommand
        args = [w.lower() for w in words[idx + 1:]]

        # Validate subcommand-specific arguments
        if subcommand == 'stash' and args and args[0] in ShellMixin._DANGEROUS_STASH_ARGS:
            return False
        if subcommand == 'branch' and args and args[0] in ShellMixin._DANGEROUS_BRANCH_ARGS:
            return False
        if subcommand == 'tag' and args and args[0] in ShellMixin._DANGEROUS_TAG_ARGS:
            return False
        if subcommand == 'remote' and args and args[0] in ShellMixin._DANGEROUS_REMOTE_ARGS:
            return False
        if subcommand == 'config' and args and args[0] in ShellMixin._DANGEROUS_CONFIG_ARGS:
            return False
        if subcommand == 'worktree' and args and args[0] in ShellMixin._DANGEROUS_WORKTREE_ARGS:
            return False

        # Validate pipe stages are safe
        return ShellMixin._validate_pipeline_stages(pipeline, ShellMixin._SAFE_PIPE_COMMANDS)

    # ------------------------------------------------------------------
    @staticmethod
    def _detect_multiline_python(command: str) -> bool:
        """Check if the command uses python -c with newlines inside quotes.

        Returns True if a `python -c "..."` or `python3 -c "..."` pattern is found
        containing literal newline characters within its quoted argument.
        """
        # Match python/python3 -c followed by double-quoted string containing newlines
        m = re.search(r'(?:python3?\s+-c\s*)("([^"]*\n[^"]*)")', command, re.IGNORECASE)
        return bool(m)

    @staticmethod
    def _multiline_python_hint() -> str:
        """Return a system hint for multi-line python -c usage on Windows."""
        return "[SYSTEM: Multi-line python -c detected. On Windows, use semicolons (;) instead of newlines inside quoted strings, e.g., python -c \"import sys; print(sys.version)\"]"

    def _configure_windows_utf8(self, command: str) -> Tuple[str, int]:
        """Prepend chcp 65001 to force CMD into UTF-8 mode on Windows.

        Returns:
            (modified_command, creationflags) tuple ready for subprocess.Popen.
        """
        return configure_windows_utf8(command, create_new_console=False)

    # ------------------------------------------------------------------
    def _terminate_process_tree_windows(self, pid: int):
        """Kill a Windows process and all its descendants.

        Three-stage strategy to handle race conditions where children spawn
        new processes between termination passes:

          1. taskkill /F (first pass — kills the target + immediate children)
          2. Brief sleep then second taskkill /F — catches children that were
             spawned in the window between the first kill and process exit,
             since a dying process can fork new children before it fully terminates.
          3. WMIC sweep — discovers deeper descendants (grandchildren+) that may
             have been created during passes 1–2 and kills them individually.
        """
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
                return f"REJECTED: {reason}"
            justification_text = reason

        try:
            # ── Platform-specific setup ──────────────────────────────
            original_command = command  # keep for multi-line detection hint
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

            t_out = threading.Thread(target=drain_pipe_chunks, args=(proc.stdout, stdout_chunks, drain_errors), daemon=True, name='shell_stdout_reader')
            t_err = threading.Thread(target=drain_pipe_chunks, args=(proc.stderr, stderr_chunks, drain_errors), daemon=True, name='shell_stderr_reader')
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
                    # Detect multi-line python -c and append a hint on Windows
                    if ON_WINDOWS and self._detect_multiline_python(original_command):
                        output = f"No output produced.\n\n{self._multiline_python_hint()}"
                    else:
                        output = "No output produced."

                final_output = truncate_with_spillover(
                    output, char_limit,
                    instance_name=agent_name,
                    tool_name='shell',
                    base_dir=self.base_dir,
                    operation_mode='mid',
                )
                if final_output is not output:
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

            # Append multi-line python hint if applicable
            if ON_WINDOWS and self._detect_multiline_python(original_command):
                timeout_msg += f"\n\n{self._multiline_python_hint()}"

            if output.strip():
                return f"{timeout_msg}\n\n{output}"
            return timeout_msg

        except Exception as e:
            return f"ERROR: Approved but execution failed: {str(e)}"