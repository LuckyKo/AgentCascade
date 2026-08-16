import logging
import os
import time
from agent_cascade.async_shell import _elapsed_for_task
from agent_cascade.operation_manager.shell import ShellMixin
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.settings import AUTO_ASYNC_TIMEOUT_THRESHOLD, DEFAULT_AUTO_ASYNC_HEARTBEAT

logger = logging.getLogger(__name__)

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
            'async_mode': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['shell_cmd']['parameters']['async_mode']
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
        """Return async message content as-is.

        Heartbeat/completion messages are already truncated by async_shell.py before
        being queued. __wait must not re-truncate them; the outer tool-result path
        will apply any necessary truncation once, consistently with other shell_cmd responses.
        """
        return text or ""

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

        # ── Parse new async parameters ──────────────────────────────
        async_mode = bool(params.get('async_mode', False))
        heartbeat_interval = float(params.get('heartbeat_interval', -1))
        tool_id = params.get('tool_id')

        # Auto-async mode: if timeout exceeds threshold and async_mode not explicitly set
        if timeout is not None and timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD and 'async_mode' not in params:
            async_mode = True
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
        if async_mode:
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
                tool_args={'command': command, 'justification': justification, 'cwd': cwd, 'async_mode': True},
                description=description,
            )

            if not approved:
                return f"REJECTED: {reason}"

        tracker = self._get_tracker()
        if tracker is None:
            return "[shell_cmd] Async shell not available (tracker not initialized)."

        # Default timeout for async mode is much longer (1 hour)
        effective_timeout = timeout if timeout else 3600

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
            result = (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
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
        launched_msg = (
            f"⟨shell_cmd launched⟩ Tool ID: {tool_id}\n"
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
                return f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No running shell found."

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
                # When heartbeats are configured, wait up to that interval (capped at 60s).
                # When no heartbeats (-1), use a default 30s so __wait still pauses meaningfully.
                if task.heartbeat_interval > 0:
                    timeout = min(task.heartbeat_interval, 60.0)
                else:
                    timeout = 30.0

            # Poll the task for new output or completion status changes.
            # This directly checks task state rather than relying on message queue,
            # so it works even when no heartbeats are configured.
            import time as _time
            start_time = _time.time()
            poll_interval = 0.5

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
                    truncated = ShellCmd._truncate_shell_message(output_text, agent_name, self.agent_pool)
                    task_elapsed = _elapsed_for_task(task)
                    return f"⟨shell_cmd wait⟩ Tool ID: {tool_id} (elapsed {task_elapsed:.0f}s)\n{truncated}"
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