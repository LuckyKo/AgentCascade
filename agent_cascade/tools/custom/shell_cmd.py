import subprocess
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA

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
        'required': ['command', 'justification'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

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
        justification = params.get('justification', 'No justification provided.')
        cwd = params.get('cwd', '.')
        timeout = params.get('timeout')  # None means use default (30s sync / 3600s async)

        # ── Parse new async parameters ──────────────────────────────
        async_mode = bool(params.get('async_mode', False))
        heartbeat_interval = float(params.get('heartbeat_interval', -1))
        tool_id_ref = params.get('tool_id')  # Can be int, string, or None

        # Parse tool_id if provided as a string number
        if tool_id_ref is not None:
            try:
                tool_id_ref = int(tool_id_ref)
            except (ValueError, TypeError):
                pass

        agent_name = kwargs.get('agent_instance_name') or self.agent_name

        # ── Async mode: reference existing task via tool_id ─────────
        if async_mode and tool_id_ref is not None:
            return self._handle_control_command(
                agent_name=agent_name,
                tool_id=tool_id_ref,
                command=command,
                heartbeat_interval=heartbeat_interval,
            )

        # ── Async mode: launch new background shell ────────────────
        if async_mode:
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
            Response string with tool_id and PID.
        """
        tracker = self._get_tracker()
        if tracker is None:
            return "[shell_cmd] Async shell not available (tracker not initialized)."

        # Default timeout for async mode is much longer (1 hour)
        effective_timeout = timeout if timeout else 3600

        try:
            tool_id, pid = tracker.launch(
                agent_name=agent_name,
                command=command,
                heartbeat_interval=heartbeat_interval,
                timeout=effective_timeout,
                cwd=cwd,
            )
        except ValueError as e:
            return f"[shell_cmd] {e}"

        return (
            f"⟨shell_cmd launched⟩ Tool ID: {tool_id}\n"
            f"Command running in background.\n"
            f"Command: `{command[:200]}`\n"
            f"Heartbeat interval: {heartbeat_interval}s\n"
            f"Timeout: {effective_timeout}s\n"
            f"A console window has been opened for inspection (Windows).\n\n"
            f"You can manage this shell by calling shell_cmd with tool_id={tool_id}:\n"
            f"  - __status → check current status and recent output\n"
            f"  - __kill → terminate the process\n"
            f"  - __ctrl_c → send interrupt signal\n"
            f"  - __heartbeat=N → update heartbeat interval (N seconds)\n"
            f"  - any other text → send as stdin input to the running command"
        )

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