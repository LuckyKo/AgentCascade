import logging
import os
import re
import time
from agent_cascade.async_shell import _elapsed_for_task
from agent_cascade.operation_manager.shell import ShellMixin
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.settings import (
    AUTO_ASYNC_TIMEOUT_THRESHOLD,
    DEFAULT_AUTO_ASYNC_HEARTBEAT,
    ASYNC_SHELL_DEFAULT_TIMEOUT,
    WAIT_CMD_MAX_TIMEOUT,
    WAIT_CMD_DEFAULT_TIMEOUT,
    WAIT_CMD_POLL_INTERVAL,
)

logger = logging.getLogger(__name__)


def _has_real_wait_for_message(pool) -> bool:
    """True only when pool is a real MessageQueueMixin with a working wait_for_message.

    Guards the queue-driven ``__wait`` path: unit tests wire a bare ``MagicMock`` as
    ``agent_pool``, for which ``hasattr(pool, 'wait_for_message')`` is True and calling it
    would return a truthy MagicMock (breaking the timeout/cap tests). The isinstance check
    forces those mock pools down the polling fallback path.
    """
    # Lazy import: importing agent_cascade.pool at module top closes a circular
    # agents→tools→pool→agents cycle (pool/core.py imports agent_cascade.agents),
    # breaking any entry point that imports `agent_cascade.agents` first
    # (e.g. tests/agents/*). Only needed at call time here.
    from agent_cascade.pool.message_queue import MessageQueueMixin
    return isinstance(pool, MessageQueueMixin) and callable(getattr(pool, 'wait_for_message', None))


_TOOL_ID_RE_CACHE = {}


def _tool_id_re(tool_id):
    """Compiled regex matching 'Tool ID: <exact id>' followed by a non-digit boundary.

    Anchors BOTH sides of the number so a short id never matches a longer one that merely
    starts with it (e.g. tool_id=1 must NOT match "Tool ID: 12"). The leading 'Tool ID: '
    prefix anchors the start of the number; the (?=\\D|$) lookahead asserts the character
    after the full id is a non-digit or end-of-string. This handles both real formats:
      - heartbeat (tracker.py): "...Tool ID: {id} | ..."  → space after id
      - completion (tracker.py): "⟨shell_cmd completed⟩ Tool ID: {id}\\n..." → newline after id
    A trailing-space anchor would match heartbeats but break completions, so the non-digit
    boundary is used instead.
    """
    pat = _TOOL_ID_RE_CACHE.get(tool_id)
    if pat is None:
        pat = re.compile(r'Tool ID: ' + str(int(tool_id)) + r'(?=\D|$)')
        _TOOL_ID_RE_CACHE[tool_id] = pat
    return pat


def _polling_wait(task, tool_id: int, agent_name: str, timeout: float, pool) -> str:
    """Busy-poll fallback for ``__wait`` when the pool lacks a real wait_for_message.

    This is the ORIGINAL polling loop (moved verbatim into a helper so it is not duplicated
    inline in the queue-driven path). It sleeps WAIT_CMD_POLL_INTERVAL and returns on
    completion or ANY new stdout/stderr line, or the timeout string after `timeout` seconds.
    """
    import time as _time
    start_time = _time.time()
    poll_interval = WAIT_CMD_POLL_INTERVAL

    with task._lock:
        last_stdout_len = len(task.stdout_lines)
        last_stderr_len = len(task.stderr_lines)

    while True:
        # Check for timeout
        elapsed = _time.time() - start_time
        remaining = timeout - elapsed
        if remaining <= 0:
            task_elapsed = _elapsed_for_task(task)
            return (
                f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
                f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s)."
            )

        # Sleep briefly before next poll (use smaller interval near timeout)
        sleep_time = min(poll_interval, remaining)
        _time.sleep(sleep_time)

        # Check task state
        with task._lock:
            is_completed = task.completed
            rc = task.return_code

        if is_completed:
            task_elapsed = _elapsed_for_task(task)
            return (
                f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Process completed "
                f"(exit code {rc}, elapsed {task_elapsed:.0f}s)."
            )

        with task._lock:
            # Collect new output since last check
            new_stdout = list(task.stdout_lines[last_stdout_len:])
            new_stderr = list(task.stderr_lines[last_stderr_len:])
            last_stdout_len = len(task.stdout_lines)
            last_stderr_len = len(task.stderr_lines)

        if new_stdout or new_stderr:
            # Format output similar to heartbeat style
            lines = new_stdout + new_stderr
            output_text = '\n'.join(line.rstrip('\r\n') for line in lines)
            truncated = ShellCmd._truncate_shell_message(output_text, agent_name, pool)
            task_elapsed = _elapsed_for_task(task)
            return f"⟨shell_cmd wait⟩ Tool ID: {tool_id} (elapsed {task_elapsed:.0f}s)\n{truncated}"


@register_tool('shell_cmd', allow_overwrite=True)
class ShellCmd(BaseTool):
    """Execute a shell command (auto-approved for safe read-only commands like find/dir/ls, requires user approval for everything else)."""

    name = 'shell_cmd'
    description = TOOL_METADATA['shell_cmd']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'command': {
                'type': 'string',
                'description': TOOL_METADATA['shell_cmd']['parameters']['command']
            },
            'justification': {
                'type': 'string',
                'description': TOOL_METADATA['shell_cmd']['parameters']['justification']
            },
            'cwd': {
                'type': 'string',
                'description': TOOL_METADATA['shell_cmd']['parameters']['cwd']
            },
            'timeout': {
                'type': 'integer',
                'minimum': 1,
                'description': TOOL_METADATA['shell_cmd']['parameters']['timeout']
            },
            'execution_mode': {
                'type': ['string', 'null'],
                'enum': ['sync', 'async', None],
                'description': TOOL_METADATA['shell_cmd']['parameters']['execution_mode']
            },
            'heartbeat_interval': {
                'type': 'integer',
                'minimum': -1,
                'default': -1,
                'description': TOOL_METADATA['shell_cmd']['parameters']['heartbeat_interval']
            },
            'tool_id': {
                'type': ['integer', 'string'],
                'description': TOOL_METADATA['shell_cmd']['parameters']['tool_id']
            }
        },
        'required': ['command'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    @staticmethod
    def _truncate_shell_message(text: str, agent_name: str, agent_pool) -> str:
        """Truncate __wait output using mid-truncation with spillover.

        The __wait path reads raw lines directly from the task (not pre-truncated
        like heartbeats), so it needs its own truncation pass. Uses the same
        shell_char_limit as other async shell output paths for consistency.
        """
        if not text:
            return ""
        try:
            llm_cfg = getattr(agent_pool, 'llm_cfg', {}) if agent_pool else {}
            char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
            base_dir = agent_pool.operation_manager.base_dir if agent_pool and hasattr(agent_pool, 'operation_manager') else None
            if base_dir and char_limit > 0:
                return truncate_with_spillover(
                    text, char_limit,
                    instance_name=agent_name,
                    tool_name='shell_cmd_async',
                    base_dir=base_dir,
                    operation_mode='mid',
                )
        except Exception as e:
            logger.debug(f"[shell_cmd] _truncate_shell_message failed for {agent_name}: {e}")
        return text

    def call(self, params: str, **kwargs) -> str:
        from agent_cascade.utils.utils import json_loads
        import json

        try:
            if isinstance(params, str):
                p = json_loads(params)
                params = json.dumps(p)
        except Exception:
            pass

        params = self._verify_json_format_args(params)
        command = params['command']
        justification = params.get('justification')
        cwd = params.get('cwd', '.')
        timeout = params.get('timeout')  # None means use default (30s sync / 3600s async)

        # ── Parse execution parameters ──────────────────────────────
        # execution_mode is a 2-value enum ('sync'/'async') with auto via ABSENCE/null
        # (jsonschema default does not inject, so omission arrives as None):
        #   None/absent/null → auto: async iff timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD
        #   'sync'           → force blocking, never auto-async (even for long timeouts)
        #   'async'          → force background regardless of timeout
        mode = params.get('execution_mode')
        run_async = (mode == 'async') or (mode is None and timeout is not None and timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD)
        heartbeat_interval = float(params.get('heartbeat_interval', -1))
        tool_id = params.get('tool_id')

        # Auto-async mode: only when execution_mode omitted and timeout exceeds threshold
        if run_async and mode is None:
            logger.info(f"Auto-async mode triggered for shell_cmd: timeout={timeout}s")
            # Default heartbeat for auto-async unless explicitly set
            if 'heartbeat_interval' not in params:
                heartbeat_interval = DEFAULT_AUTO_ASYNC_HEARTBEAT

        # Convert tool_id to int if provided as a string number
        if tool_id is not None:
            try:
                tool_id = int(tool_id)
            except (ValueError, TypeError):
                raise ValueError(f"tool_id must be a numeric value, got: {tool_id!r}")

        # Validate justification is required for non-control commands.
        # Note: Control commands are detected here and routed before auto-async logic applies.
        # Even if a control command has timeout > 60, it won't trigger auto-async mode.
        # When tool_id is provided, route to _handle_control_command which handles both
        # __control commands (__kill, __status, etc.) and stdin input (any other text).
        has_tool_id = tool_id is not None

        if not has_tool_id and not justification:
            raise ValueError("'justification' is required for shell_cmd unless using control commands with tool_id")

        agent_name = self._get_agent_name(kwargs)

        # ── Handle control commands + stdin input for existing async shells (takes priority) ──
        if has_tool_id:
            return self._handle_control_command(
                agent_name=agent_name,
                tool_id=tool_id,
                command=command,
                heartbeat_interval=heartbeat_interval,
            )

        # ── Async mode: launch new background shell ────────────────
        if run_async:
            # Validate control commands aren't used without tool_id
            if command in ShellMixin._CONTROL_COMMANDS or command.startswith(ShellMixin._CONTROL_HEARTBEAT_PREFIX):
                return f"[shell_cmd] Control command '{command}' requires a tool_id. Launch a shell first, then use the returned tool_id."
            return self._launch_async(
                agent_name=agent_name,
                command=command,
                justification=justification,
                cwd=cwd,
                timeout=timeout,
                heartbeat_interval=heartbeat_interval,
            )

        # ── Sync mode (default): blocking execution ────────────────
        return self._execute_sync(
            agent_name=agent_name,
            command=command,
            justification=justification,
            cwd=cwd,
            timeout=timeout,
        )

    # ────────────────────────────────────────────────────────────────
    def _get_tracker(self):
        """Get the AsyncShellTracker from the agent pool."""
        if self.agent_pool and hasattr(self.agent_pool, '_async_shell_tracker'):
            return self.agent_pool._async_shell_tracker
        return None

    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_command_input(command: str, timeout: int) -> str | None:
        """Validate command length and timeout before execution.

        Args:
            command: Shell command string to validate.
            timeout: Timeout value or None (uses default).

        Returns:
            Error message string if validation fails, None otherwise.
        """
        from agent_cascade.operation_manager.shell import MAX_SHELL_TIMEOUT

        # Command length check uses the standard char_limit; caller should pass effective limit.
        # This method focuses on timeout validation only — command length is checked inline
        # where char_limit is available (it depends on agent_pool config).
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0 or timeout > MAX_SHELL_TIMEOUT):
            return f"ERROR: Invalid timeout value: {timeout}. Must be a positive integer between 1 and {MAX_SHELL_TIMEOUT}."
        return None

    # ────────────────────────────────────────────────────────────────
    def _launch_async(
        self, agent_name: str, command: str, justification: str,
        cwd: str, timeout: int, heartbeat_interval: float,
    ) -> str:
        """Launch a shell command in the background and return immediately.

        Args:
            agent_name: Owner agent name
            command: Shell command string
            justification: Why this command is needed
            cwd: Working directory
            timeout: Timeout override (None = default 3600s for async)
            heartbeat_interval: Seconds between heartbeats (-1 = only on completion)

        Returns:
            Response string with tool_id and PID, or completion result if command finished quickly.
        """
        # ── Resolve cwd using the same resolver as file tools ────────
        try:
            from agent_cascade.utils.tool_path_resolver import resolve_tool_path
            resolved_cwd = resolve_tool_path(cwd, mode="rw", agent_pool=self.agent_pool)
        except Exception as e:
            return f"ERROR: Invalid working directory: {str(e)}"

        # ── Input validation: mirror sync mode checks (before approval) ──
        char_limit = 2048
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('shell_char_limit', char_limit)
        elif self.cfg.get('shell_char_limit'):
            char_limit = self.cfg.get('shell_char_limit')

        if len(command) > char_limit:
            return f"ERROR: Command exceeds maximum length of {char_limit} characters."

        timeout_error = ShellCmd._validate_command_input(command, timeout)
        if timeout_error is not None:
            return timeout_error

        # ── Approval gate: mirror sync mode logic ────────────────────
        is_safe = ShellMixin._is_safe_readonly_shell_command(command)

        if not is_safe:
            description = (
                f"⚠️ **SECURITY WARNING**: This is a host shell command running in async (background) mode. "
                f"It can potentially bypass folder restrictions!\n\n"
                f"**CWD**: {resolved_cwd}\n"
                f"**Execute Shell Command**:\n```bash\n{command}\n```\n"
                f"**Justification**: {justification}"
            )

            approved, reason = self.agent_pool.operation_manager.request_user_approval(
                agent_name=agent_name,
                tool_name='shell_cmd',
                tool_args={'command': command, 'justification': justification, 'cwd': cwd, 'execution_mode': 'async'},
                description=description,
            )

            if not approved:
                return f"REJECTED: {reason}"
            justification_text = reason
        else:
            justification_text = ""

        tracker = self._get_tracker()
        if tracker is None:
            return "[shell_cmd] Async shell not available (tracker not initialized)."

        # Default timeout for async mode is much longer (ASYNC_SHELL_DEFAULT_TIMEOUT, 1 hour)
        effective_timeout = timeout if timeout else ASYNC_SHELL_DEFAULT_TIMEOUT

        # Respect user toggle for console window popup
        console_window = True
        if self.agent_pool and hasattr(self.agent_pool, '_enable_async_shell_console_window'):
            console_window = bool(self.agent_pool._enable_async_shell_console_window)

        # Opt-out override (e.g. test harnesses): force no console window regardless of pool state.
        # Does NOT change production defaults — only takes effect when this env var is set truthy.
        if os.getenv("QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW", "").strip() not in ("", "0", "false", "False"):
            console_window = False

        start_time = time.time()
        try:
            tool_id, pid, early_output, completed_early, return_code = tracker.launch(
                agent_name=agent_name,
                command=command,
                heartbeat_interval=heartbeat_interval,
                timeout=effective_timeout,
                cwd=resolved_cwd,
                console_window=console_window,  # Respect user toggle
            )
        except ValueError as e:
            return f"[shell_cmd] {e}"

        # Case 1: Command completed very quickly — return completion result directly
        if completed_early:
            elapsed = time.time() - start_time
            rc = return_code if return_code is not None else 0
            status = "success" if (rc == 0) else f"exit code {rc}"
            approval_line = "AUTO-APPROVED\n" if is_safe else "APPROVED\n"
            if not is_safe and justification_text:
                approval_line += f"Security Justification: {justification_text}\n"
            result = (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id} | PID: {pid}\n"
                f"{approval_line}"
                f"Completed in {elapsed:.1f} s ({status}).\n"
            )
            # Append early output if available (truncate if large)
            if early_output:
                output_text = '\n'.join(early_output)
                try:
                    base_dir = self.agent_pool.operation_manager.base_dir if self.agent_pool and hasattr(self.agent_pool, 'operation_manager') else None
                    if base_dir:
                        char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
                        output_text = truncate_with_spillover(
                            output_text, char_limit,
                            instance_name=agent_name,
                            tool_name='shell_cmd',
                            base_dir=base_dir,
                            operation_mode='mid',
                        )
                except Exception as e:
                    logger.debug(f"[shell_cmd] truncate_with_spillover failed in early completion for {agent_name}: {e}")
                result += f"\nOutput:\n{output_text}"
            return result

        # Case 2 & 3: Still running — return launched message, with early output appended if available
        console_line = "A console window has been opened for inspection (Windows).\n" if console_window else ""
        approval_line = "AUTO-APPROVED\n" if is_safe else "APPROVED\n"
        if not is_safe and justification_text:
            approval_line += f"Security Justification: {justification_text}\n"
        launched_msg = (
            f"⟨shell_cmd launched⟩ Tool ID: {tool_id} | PID: {pid}\n"
            f"{approval_line}"
            f"Command running in background.\n"
            f"Command: `{command[:200]}`\n"
            f"Heartbeat interval: {heartbeat_interval}s\n"
            f"Timeout: {effective_timeout}s\n"
            f"{console_line}"
            f"You can manage this shell by calling shell_cmd with tool_id={tool_id}:\n"
            f"  - __status → check current status and recent output\n"
            f"  - __kill → terminate the process\n"
            f"  - __ctrl_c → send interrupt signal\n"
            f"  - __wait → wait until next heartbeat (similar to simply stoping as you will be woken up by the heartbeat response)\n"
            f"  - __heartbeat=N → update heartbeat interval (N seconds)\n"
            f"  - any other text → send as stdin input to the running command"
        )

        # Append early output if available (Case 2, truncate if large)
        if early_output:
            output_text = '\n'.join(early_output)
            try:
                base_dir = self.agent_pool.operation_manager.base_dir if self.agent_pool and hasattr(self.agent_pool, 'operation_manager') else None
                if base_dir:
                    char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
                    output_text = truncate_with_spillover(
                        output_text, char_limit,
                        instance_name=agent_name,
                        tool_name='shell_cmd',
                        base_dir=base_dir,
                        operation_mode='mid',
                    )
            except Exception as e:
                logger.debug(f"[shell_cmd] truncate_with_spillover failed in launched msg for {agent_name}: {e}")
            launched_msg += f"\n\nInitial output:\n{output_text}"

        return launched_msg

    # ────────────────────────────────────────────────────────────────
    def _handle_control_command(
        self, agent_name: str, tool_id: int, command: str,
        heartbeat_interval: float,
    ) -> str:
        """Handle control commands for an existing async shell task.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier to reference
            command: Control command text (__kill, __status, etc.) or stdin input
            heartbeat_interval: Override heartbeat interval if applicable

        Returns:
            Response string with the result of the control operation.
        """
        tracker = self._get_tracker()
        if tracker is None:
            return "[shell_cmd] Async shell not available (tracker not initialized)."

        # Parse special command prefixes
        if command == '__kill':
            return tracker.kill_task(agent_name, tool_id) or "No action taken."
        elif command == '__status':
            return tracker.get_status(agent_name, tool_id) or "No status available."
        elif command.startswith('__heartbeat='):
            try:
                new_interval = float(command.split('=')[1])
                return tracker.update_heartbeat(agent_name, tool_id, new_interval) or "No action taken."
            except (ValueError, IndexError):
                return f"[shell_cmd] Invalid heartbeat value in command: {command}"
        elif command == '__ctrl_c':
            return tracker.send_ctrl_c(agent_name, tool_id) or "No action taken."
        elif command == '__wait':
            # If there's no known task, fail fast.
            task = tracker._get_task(agent_name, tool_id)

            if task is None:
                # RC4 softening: a follow-up __wait can race task cleanup right before the
                # completion USER message lands; keep the "No running shell found" substring.
                return (
                    f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No running shell found "
                    f"(may have just completed — watch for the completion message)."
                )

            # Read initial state under lock to avoid race with tracking thread.
            # All AsyncShellTask shared state must be accessed under task._lock
            # (see async_shell.py line 245 for locking discipline documentation).
            with task._lock:
                is_completed = task.completed
                rc = task.return_code

            if is_completed:
                elapsed = _elapsed_for_task(task)
                return (
                    f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Process already completed "
                    f"(exit code {rc}, elapsed {elapsed:.0f}s)."
                )

            with task._lock:
                # Determine wait timeout based on the task's heartbeat_interval.
                # When heartbeats are configured, wait up to that interval (capped at WAIT_CMD_MAX_TIMEOUT).
                # When no heartbeats (-1), use WAIT_CMD_DEFAULT_TIMEOUT so __wait still pauses meaningfully.
                if task.heartbeat_interval > 0:
                    timeout = min(task.heartbeat_interval, WAIT_CMD_MAX_TIMEOUT)
                else:
                    timeout = WAIT_CMD_DEFAULT_TIMEOUT

            # Queue-driven wait (v2 wake-up contract): block until ANY message is queued for
            # this agent, then inspect the FRONT of the queue. If it is THIS tool_id's shell
            # heartbeat/completion → consume it and return verbatim (the tracker already
            # advanced task.last_heartbeat_sent_pos, so no duplication — RC3 preserved).
            # Otherwise → return a default wake-up string and leave the queue untouched; the
            # normal drain (engine/core.py:2320) delivers all remaining messages in sequence.
            # We do NOT hold task._lock across the queue wait — all task locks above are released first.
            pool = self.agent_pool

            def _is_our_shell_msg(m):
                if isinstance(m, str):
                    text = m
                else:
                    try:
                        text = str(m)
                    except Exception as e:
                        # Non-string message whose __str__ failed — treat as "not ours".
                        logger.debug("[shell_cmd] _is_our_shell_msg: failed to str() message: %s", e)
                        return False
                if not text.startswith('⟨shell_cmd'):
                    return False
                # Boundary-anchored match so tool_id=1 does NOT consume tool_id 12's message.
                return bool(_tool_id_re(tool_id).search(text))

            if _has_real_wait_for_message(pool):
                msg = pool.wait_for_message(agent_name, timeout, consume_predicate=_is_our_shell_msg)
                if msg is None:
                    # Genuine timeout / terminated — empty queue. Existing string (unchanged).
                    task_elapsed = _elapsed_for_task(task)
                    return (
                        f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
                        f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s)."
                    )
                if _is_our_shell_msg(msg):
                    # Front message was THIS tool's shell msg → already consumed by the primitive.
                    return str(msg)
                # Front message is something else (user/system/other-tool) → it was only peeked,
                # still queued. Return default wake-up; normal drain delivers it in sequence.
                return (
                    f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - "
                    f"Woken by queued message (not this shell). Check your message queue."
                )

            # Fallback: pool lacks a real wait_for_message (e.g. MagicMock in unit tests).
            return _polling_wait(task, tool_id, agent_name, timeout, self.agent_pool)
        else:
            # Send as stdin input to the running process
            return tracker.send_input(agent_name, tool_id, command) or f"Input sent [Tool ID: {tool_id}]."

    # ────────────────────────────────────────────────────────────────
    def _execute_sync(
        self, agent_name: str, command: str, justification: str,
        cwd: str, timeout: int,
    ) -> str:
        """Execute a shell command synchronously (blocking).

        Args:
            agent_name: Owner agent name
            command: Shell command string
            justification: Why this command is needed
            cwd: Working directory
            timeout: Timeout override (None = default 30s)

        Returns:
            Command output or error message.
        """
        # Get the truncation limit from agent/tool options
        char_limit = 2048
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('shell_char_limit', char_limit)
        elif self.cfg.get('shell_char_limit'):
            char_limit = self.cfg.get('shell_char_limit')

        return self.agent_pool.operation_manager.execute_shell_command(
            command=command,
            justification=justification,
            agent_name=agent_name,
            cwd=cwd,
            char_limit=int(char_limit),
            timeout=timeout,
        )