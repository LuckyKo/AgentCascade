import logging
import os
import re
import time
from typing import Optional, Union

from agent_cascade.async_shell import _elapsed_for_task
from agent_cascade.operation_manager.shell import MAX_SHELL_TIMEOUT, ShellMixin
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.settings import (ASYNC_SHELL_DEFAULT_TIMEOUT, AUTO_ASYNC_TIMEOUT_THRESHOLD,
                                    DEFAULT_AUTO_ASYNC_HEARTBEAT, SAFE_MODE_AUTO_RAM_FLOOR_MB,
                                    SAFE_MODE_AUTO_VRAM_FLOOR_MB, WAIT_CMD_DEFAULT_TIMEOUT, WAIT_CMD_MAX_TIMEOUT,
                                    WAIT_CMD_POLL_INTERVAL)
from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.tools.base import BaseTool, register_tool

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
            return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
                    f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s).")

        # Sleep briefly before next poll (use smaller interval near timeout)
        sleep_time = min(poll_interval, remaining)
        _time.sleep(sleep_time)

        # Check task state
        with task._lock:
            is_completed = task.completed
            rc = task.return_code

        if is_completed:
            task_elapsed = _elapsed_for_task(task)
            return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Process completed "
                    f"(exit code {rc}, elapsed {task_elapsed:.0f}s).")

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
        # BUG_0024: reject unknown keys loudly (e.g. the legacy 'async_mode' after the
        # execution_mode rename) instead of silently running with defaults. BaseTool
        # converts the rejection into a friendly "unknown parameter(s) ..." ValueError.
        'additionalProperties': False,
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
                'enum': ['auto', 'sync', 'async', 'safe', None],
                'default': 'auto',
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

    def _is_restricted(self, agent_name: str) -> bool:
        """True if the calling agent instance is system-invoked (restricted shell).

        Delegates to ShellMixin._agent_is_restricted — pass self so it can resolve
        self.agent_pool via the same logic as the sync path in shell.py.
        """
        # ShellMixin._agent_is_restricted expects (self, agent_name) where self has .agent_pool
        return ShellMixin._agent_is_restricted(self, agent_name)

    @staticmethod
    def _truncate_shell_message(text: str, agent_name: str, agent_pool) -> str:
        """Truncate __wait output using mid-truncation with spillover.

        The __wait path reads raw lines directly from the task (not pre-truncated
        like heartbeats), so it needs its own truncation pass. Uses the same
        shell_char_limit as other async shell output paths for consistency.
        """
        if not text:
            return ''
        try:
            llm_cfg = getattr(agent_pool, 'llm_cfg', {}) if agent_pool else {}
            char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
            base_dir = agent_pool.operation_manager.base_dir if agent_pool and hasattr(agent_pool,
                                                                                       'operation_manager') else None
            if base_dir and char_limit > 0:
                return truncate_with_spillover(
                    text,
                    char_limit,
                    instance_name=agent_name,
                    tool_name='shell_cmd_async',
                    base_dir=base_dir,
                    operation_mode='mid',
                )
        except Exception as e:
            logger.debug(f"[shell_cmd] _truncate_shell_message failed for {agent_name}: {e}")
        return text

    def dms_bound(self, tool_args, instance):
        """Holder-DMS bound for shell_cmd: the hard ceiling MAX_SHELL_TIMEOUT.

        The sync path blocks up to this bound (operation_manager/shell.py proc.wait). The boundary
        carries it with the marker so the holder-side DMS shields a legitimately-bounded sync shell
        for exactly its real ceiling and no longer (t292 — replaces the old scattered marker write).
        Async shell_cmd returns from _call_tool immediately (the holder parks in SLEEPING, excluded by
        state), so this bound only ever applies to the genuinely-blocking sync path.
        """
        return float(MAX_SHELL_TIMEOUT)

    def call(self, params: Union[str, dict], **kwargs) -> str:
        from agent_cascade.utils.utils import json_loads

        # Normalize to a dict up front so the alias coercion below works on BOTH input
        # shapes. The production path hands us a fresh dict (ToolDispatcher deep-copies
        # args), but direct callers may pass a JSON string or their own dict — never mutate
        # a caller's dict in place. Unparseable/non-dict input becomes {} so that
        # _verify_json_format_args reports the missing 'command' as a normal tool error.
        if isinstance(params, str):
            try:
                params = json_loads(params)
            except Exception:
                params = None
            if not isinstance(params, dict):
                params = {}

        # BUG_0024 (defence in depth): the schema rejects unknown keys (e.g. the legacy
        # 'async_mode' after the execution_mode rename). The common case is coerced with a
        # nudge so the caller succeeds on the first retry; anything else unknown errors loudly
        # instead of silently running sync. Returned as an ERROR string so the model sees it
        # as a normal tool result rather than a crash line.
        try:
            if 'async_mode' in params and 'execution_mode' not in params:
                v = params['async_mode']
                # The legacy async_mode was a boolean flag; reject non-bool values loudly
                # instead of silently coercing "false" → True via bool().
                if not isinstance(v, bool):
                    return f"ERROR: 'async_mode' must be a boolean (true/false), got: {v!r}"
                params = dict(params)
                del params['async_mode']
                params['execution_mode'] = 'async' if v else 'sync'
                logger.info("shell_cmd: coerced legacy alias 'async_mode' -> 'execution_mode'=%s",
                            params['execution_mode'])
            params = self._verify_json_format_args(params)
        except ValueError as e:
            return f"ERROR: {e}"
        command = params['command']
        justification = params.get('justification')
        cwd = params.get('cwd', '.')
        timeout = params.get('timeout')  # None means use default (30s sync / 3600s async)

        # ── Parse execution parameters ──────────────────────────────
        # execution_mode is a tri-state parameter ('auto'/'sync'/'async'). 'auto' is the default and
        # is also what omission/null maps to (jsonschema default does not inject, so absence arrives as None):
        #   auto/None/absent → async iff timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD
        #   sync             → force blocking, never auto-async (even for long timeouts)
        #   async            → force background regardless of timeout
        mode = params.get('execution_mode') or 'auto'  # None/absent → 'auto'
        is_auto = (mode == 'auto')
        run_safe = (mode == 'safe')
        run_async = (mode == 'async') or (is_auto and timeout is not None and timeout > AUTO_ASYNC_TIMEOUT_THRESHOLD)
        heartbeat_interval = float(params.get('heartbeat_interval', -1))
        tool_id = params.get('tool_id')

        # Auto-async mode: only when execution_mode is auto/omitted/null AND timeout exceeds threshold
        if run_async and is_auto:
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

        # Resolve the runtime agent instance name early so the __help branch below can
        # render the correct restricted/normal banner. Nothing else here depends on it.
        agent_name = self._get_agent_name(kwargs)

        # ── __help: informational, works for ALL agents (incl. restricted). ──
        # Checked before the justification-required check and before control-command routing
        # so it needs no `justification` and is unambiguous even if a tool_id is also passed.
        if command.strip().lower() == '__help':
            return ShellMixin._format_help(restricted=self._is_restricted(agent_name))

        # Validate justification is required for non-control commands.
        # Note: Control commands are detected here and routed before auto-async logic applies.
        # Even if a control command has timeout > 60, it won't trigger auto-async mode.
        # When tool_id is provided, route to _handle_control_command which handles both
        # __control commands (__kill, __status, etc.) and stdin input (any other text).
        has_tool_id = tool_id is not None

        if not has_tool_id and not justification:
            raise ValueError("'justification' is required for shell_cmd unless using control commands with tool_id")

        # ── Handle control commands + stdin input for existing async shells (takes priority) ──
        if has_tool_id:
            return self._handle_control_command(
                agent_name=agent_name,
                tool_id=tool_id,
                command=command,
                heartbeat_interval=heartbeat_interval,
            )

        # ── AUTO RAM floor: force safe when free system RAM is critically low. ──
        # 'auto' only (explicit sync/async/safe win). Takes precedence over auto-async — a command
        # about to OOM the box must not run unmonitored in the background. Capability gate mirrors
        # _execute_safe's GATE A; probe targets the HELD endpoint; best-effort (see state_ops.ram_vram_pressure).
        if is_auto and not run_safe:
            from agent_cascade.state_ops import (resolve_held_endpoint, is_autoloader_endpoint,
                                                 is_state_save_capable, ram_vram_pressure)
            from agent_cascade.operation_manager import _queue_tool_warning
            pool = self.agent_pool
            instance = self._get_instance(agent_name) if pool else None
            last_ep = getattr(instance, '_last_endpoint_config', None) if instance else None
            if instance is not None and is_state_save_capable(last_ep):
                held = resolve_held_endpoint(pool, instance)
                if held and is_autoloader_endpoint(held['api_base']):
                    settings = getattr(pool, 'settings', None)
                    ram_floor = getattr(settings, 'safe_mode_auto_ram_floor_mb', SAFE_MODE_AUTO_RAM_FLOOR_MB) if settings else SAFE_MODE_AUTO_RAM_FLOOR_MB
                    vram_floor = getattr(settings, 'safe_mode_auto_vram_floor_mb', SAFE_MODE_AUTO_VRAM_FLOOR_MB) if settings else SAFE_MODE_AUTO_VRAM_FLOOR_MB
                    press = ram_vram_pressure(held['api_base'], ram_floor, vram_floor)
                    if press['force_safe']:
                        run_safe = True
                        run_async = False
                        logger.info('Auto safe-mode: free RAM %s MB < floor %s MB',
                                    press['ram_free_mb'], ram_floor)
                        _queue_tool_warning(
                            pool, agent_name,
                            f"[shell_cmd] Running in SAFE mode: free system RAM {press['ram_free_mb']} MB "
                            f"< floor {ram_floor} MB — local model unloaded to avoid OOM; command may be slower.")
                    elif press['low_vram_gpus']:
                        _queue_tool_warning(
                            pool, agent_name,
                            f"[shell_cmd] low free VRAM on GPU(s) {', '.join(map(str, press['low_vram_gpus']))}: "
                            f"consider execution_mode='safe' if you run any command that may use significant VRAM.")

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
        # Mirror the async-branch guard: a control command without tool_id must not
        # fall through to real shell execution (e.g. `__kill` would run as a literal).
        if command in ShellMixin._CONTROL_COMMANDS or command.startswith(ShellMixin._CONTROL_HEARTBEAT_PREFIX):
            return f"[shell_cmd] Control command '{command}' requires a tool_id. Launch a shell first, then use the returned tool_id."
        if run_safe:
            return self._execute_safe(
                agent_name=agent_name,
                command=command,
                justification=justification,
                cwd=cwd,
                timeout=timeout,
            )
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
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0 or
                                    timeout > MAX_SHELL_TIMEOUT):
            return f"ERROR: Invalid timeout value: {timeout}. Must be a positive integer between 1 and {MAX_SHELL_TIMEOUT}."
        return None

    # ────────────────────────────────────────────────────────────────
    # BUG_0057: `| head` / `| tail` pipe stages are STRIPPED (not denied) on all platforms.
    # AgentCascade already truncates shell output with spillover, so the trailing filter is
    # redundant — but stripping silently changes which bytes survive (last-N → first-N+spill),
    # so a note is emitted to keep the model honest about what it saw.
    _HEAD_TAIL_STRIP_NOTE = ("[note] stripped '| head'/'| tail' pipe stage(s); "
                              'output truncated with spillover instead.')

    @staticmethod
    def _strip_note(was_stripped: bool) -> str:
        """Return the strip note (with trailing newline) if pipes were stripped, else ''."""
        return ShellCmd._HEAD_TAIL_STRIP_NOTE + '\n' if was_stripped else ''

    @staticmethod
    def _strip_head_tail_pipes(command: str) -> tuple[str, bool]:
        """Strip post-``|`` ``head``/``tail`` stages from a shell command.

        Returns ``(stripped_command, was_stripped)``. Only post-first pipe stages are
        inspected (case-insensitive, skipping leading ``-flag`` tokens, so ``head -n 5``,
        ``tail --lines=10`` and ``HEAD`` all match). ``head``/``tail`` as the FIRST stage or
        as a git arg (e.g. ``git show-ref --head``) is NOT stripped. The leading
        ``cd <path> &&`` / ``;`` prefix is handled via :meth:`ShellMixin._strip_cd_prefix`
        for consistency with the safe classifier, and re-attached to the result so the
        executed command still changes directory first.

        Surviving stages are rejoined with ``' | '`` (no naive ``str.replace``). If stripping
        would leave no runnable stage (e.g. a malformed leading-pipe command like ``| head``),
        the original command is returned unchanged with ``was_stripped=False`` so normal
        classification/approval handles it.

        BUG_0060: only UNQUOTED ``|`` are treated as pipeline separators. A ``|`` inside a
        single- or double-quoted argument (e.g. ``python -c "...'| head'..."``) is string data,
        not a stage boundary, and is left untouched so the command is not corrupted.
        """
        if not command:
            return command, False
        cmd = ShellMixin._strip_cd_prefix(command.strip())
        cd_prefix = ''
        if cmd != command.strip():
            sep_match = re.search(r'\s*(&&|;)\s*', command.strip())
            if sep_match:
                prefix = command.strip()[:sep_match.start()].strip()
                if prefix.lower().startswith('cd'):
                    cd_prefix = f"{prefix} && "
        # BUG_0060: split only on UNQUOTED '|'. A naive cmd.split('|') breaks commands like
        # `python -c "...'| head'..."` where '|' appears inside a quoted string literal — the
        # rejoin below then splits the string in half (SyntaxError). Pipes inside single or
        # double quotes are argument data, not shell pipeline separators.
        stages = []
        cur = ''
        quote = None
        for ch in cmd:
            if quote:
                cur += ch
                if ch == quote:
                    quote = None
            elif ch in ('"', "'"):
                quote = ch
                cur += ch
            elif ch == '|':
                stages.append(cur)
                cur = ''
            else:
                cur += ch
        stages.append(cur)
        surviving = [stages[0]]
        was_stripped = False
        for stage in stages[1:]:
            tokens = stage.strip().split()
            first_cmd = None
            for tok in tokens:
                if tok.startswith('-'):
                    continue
                first_cmd = tok.lower()
                break
            if first_cmd in ('head', 'tail'):
                was_stripped = True
            else:
                surviving.append(stage)
        if not was_stripped:
            return command, False
        stripped_body = ' | '.join(s.strip() for s in surviving).strip()
        if not stripped_body:
            # Nothing left to run — leave the original command for normal handling.
            return command, False
        return f"{cd_prefix}{stripped_body}", was_stripped

    # ────────────────────────────────────────────────────────────────
    def _launch_async(
        self,
        agent_name: str,
        command: str,
        justification: str,
        cwd: str,
        timeout: int,
        heartbeat_interval: float,
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
        # ── Strip `| head` / `| tail` pipe stages (BUG_0057): the base command runs,
        #    output is truncated with spillover instead. No early return — the (possibly
        #    rewritten) command flows through to classification and execution. ──
        command, was_stripped = ShellCmd._strip_head_tail_pipes(command)

        # ── Resolve cwd using the same resolver as file tools ────────
        try:
            from agent_cascade.utils.tool_path_resolver import resolve_tool_path
            resolved_cwd = resolve_tool_path(cwd, mode='rw', agent_pool=self.agent_pool)
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

        # Default timeout for async mode is much longer (ASYNC_SHELL_DEFAULT_TIMEOUT, 1 hour).
        # Computed before the approval gate so tool_args can expose it to the security guard.
        effective_timeout = timeout if timeout else ASYNC_SHELL_DEFAULT_TIMEOUT

        # ── Approval gate: mirror sync mode logic (§4.4) ───────────────
        # `strict` is the unchanged read-only bool and is the ONLY thing the
        # restricted-agent gate may consult (D5). The widened decision comes from
        # the shared entry point. Async gets the approval widening but NOT
        # ownership attribution — the command has not run yet here; ownership is
        # Phase 2 (mark on completion in the tracker), documented in shell.py.
        strict = ShellMixin._is_safe_readonly_shell_command(command)

        # ── Restricted (system-invoked) agents: hard-reject non-read-only commands. ──
        if not strict and self._is_restricted(agent_name):
            return ('REJECTED: Command not allowed for system agents. '
                    'Only read-only filesystem/git commands are permitted. '
                    'Use __help to see allowed commands.')

        # Widened decision. A test stub whose operation_manager is a MagicMock cannot
        # produce a real (bool, list) decision; fall back to the strict bool so such
        # stubs keep their prior behaviour. Real pools get the new-file allowance.
        # BUG_0081: AgentPool.operation_manager is legitimately None when the pool was
        # constructed without one (pool/core.py injects it, default None) — treat a
        # missing advisor as "no widened evaluation" instead of crashing the async launch.
        _op_mgr = getattr(self.agent_pool, 'operation_manager', None) if self.agent_pool else None
        if _op_mgr is not None:
            try:
                _decision = _op_mgr.evaluate_shell_command_safety(
                    command, cwd, agent_name, resolved_cwd=resolved_cwd)
            except AttributeError:
                # Test stub (or partial pool) whose operation_manager lacks the widened
                # decision API — fall back to the strict bool so such stubs keep their
                # prior behaviour.
                _decision = (strict, [])
        else:
            _decision = (strict, [])
        if isinstance(_decision, tuple) and len(_decision) == 2:
            is_safe, _new_file_targets = _decision
        else:
            is_safe, _new_file_targets = strict, []

        if not is_safe:
            description = (f"⚠️ **SECURITY WARNING**: This is a host shell command running in async (background) mode. "
                           f"It can potentially bypass folder restrictions!\n\n"
                           f"**CWD**: {resolved_cwd}\n"
                           f"**Execute Shell Command**:\n```bash\n{command}\n```\n"
                           f"**Justification**: {justification}")

            approved, reason = self.agent_pool.operation_manager.request_user_approval(
                agent_name=agent_name,
                tool_name='shell_cmd',
                tool_args={
                    'command': command,
                    'justification': justification,
                    'cwd': cwd,
                    'execution_mode': 'async',
                    'timeout': effective_timeout,
                },
                description=description,
            )

            if not approved:
                return f"REJECTED: {reason}"
            justification_text = reason
        else:
            justification_text = ''

        tracker = self._get_tracker()
        if tracker is None:
            return '[shell_cmd] Async shell not available (tracker not initialized).'

        # Respect user toggle for console window popup
        console_window = True
        if self.agent_pool and hasattr(self.agent_pool, '_enable_async_shell_console_window'):
            console_window = bool(self.agent_pool._enable_async_shell_console_window)

        # Opt-out override (e.g. test harnesses): force no console window regardless of pool state.
        # Does NOT change production defaults — only takes effect when this env var is set truthy.
        if os.getenv('AGENT_CASCADE_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW', '').strip() not in ('', '0', 'false', 'False'):
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
            status = 'success' if (rc == 0) else f"exit code {rc}"
            approval_line = 'AUTO-APPROVED\n' if is_safe else 'APPROVED\n'
            if not is_safe and justification_text:
                approval_line += f"Security Justification: {justification_text}\n"
            strip_note = ShellCmd._strip_note(was_stripped)
            result = (f"⟨shell_cmd completed⟩ Tool ID: {tool_id} | PID: {pid}\n"
                      f"{approval_line}"
                      f"{strip_note}"
                      f"Completed in {elapsed:.1f} s ({status}).\n")
            # Append early output if available (truncate if large)
            if early_output:
                output_text = '\n'.join(early_output)
                try:
                    base_dir = self.agent_pool.operation_manager.base_dir if self.agent_pool and hasattr(
                        self.agent_pool, 'operation_manager') else None
                    if base_dir:
                        char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
                        output_text = truncate_with_spillover(
                            output_text,
                            char_limit,
                            instance_name=agent_name,
                            tool_name='shell_cmd',
                            base_dir=base_dir,
                            operation_mode='mid',
                        )
                except Exception as e:
                    logger.debug(
                        f"[shell_cmd] truncate_with_spillover failed in early completion for {agent_name}: {e}")
                result += f"\nOutput:\n{output_text}"
            return result

        # Case 2 & 3: Still running — return launched message, with early output appended if available
        console_line = 'A console window has been opened for inspection (Windows).\n' if console_window else ''
        approval_line = 'AUTO-APPROVED\n' if is_safe else 'APPROVED\n'
        if not is_safe and justification_text:
            approval_line += f"Security Justification: {justification_text}\n"
        strip_note = ShellCmd._strip_note(was_stripped)
        launched_msg = (
            f"⟨shell_cmd launched⟩ Tool ID: {tool_id} | PID: {pid}\n"
            f"{approval_line}"
            f"{strip_note}"
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
            f"  - any other text → send as stdin input to the running command")

        # Append early output if available (Case 2, truncate if large)
        if early_output:
            output_text = '\n'.join(early_output)
            try:
                base_dir = self.agent_pool.operation_manager.base_dir if self.agent_pool and hasattr(
                    self.agent_pool, 'operation_manager') else None
                if base_dir:
                    char_limit = llm_cfg.get('shell_char_limit', 2048) if isinstance(llm_cfg, dict) else 2048
                    output_text = truncate_with_spillover(
                        output_text,
                        char_limit,
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
        self,
        agent_name: str,
        tool_id: int,
        command: str,
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
            return '[shell_cmd] Async shell not available (tracker not initialized).'

        # Parse special command prefixes
        if command == '__kill':
            return tracker.kill_task(agent_name, tool_id) or 'No action taken.'
        elif command == '__status':
            return tracker.get_status(agent_name, tool_id) or 'No status available.'
        elif command.startswith('__heartbeat='):
            try:
                new_interval = float(command.split('=')[1])
                return tracker.update_heartbeat(agent_name, tool_id, new_interval) or 'No action taken.'
            except (ValueError, IndexError):
                return f"[shell_cmd] Invalid heartbeat value in command: {command}"
        elif command == '__ctrl_c':
            return tracker.send_ctrl_c(agent_name, tool_id) or 'No action taken.'
        elif command == '__wait':
            # If there's no known task, fail fast.
            task = tracker._get_task(agent_name, tool_id)

            if task is None:
                # BUG_0061: the live task may already have been reaped after completion.
                # Fall back to the bounded completed-run record so a late __wait returns
                # the buffered output instead of dropping it. Only when the id is genuinely
                # unknown (no live task AND no completed record) do we report not-found —
                # and we keep the "No running shell found" substring for that terminal case.
                run = tracker._get_completed_run(agent_name, tool_id)
                if run is not None:
                    rc = run.get('return_code')
                    elapsed = run.get('elapsed', 0.0) or 0.0
                    output = run.get('output')
                    body = (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Process already completed "
                            f"(exit code {rc}, elapsed {elapsed:.0f}s).")
                    if output:
                        body += f"\n\nOutput:\n{output}"
                    return ShellCmd._truncate_shell_message(body, agent_name, self.agent_pool)
                # RC4 softening: a follow-up __wait can race task cleanup right before the
                # completion USER message lands; keep the "No running shell found" substring.
                return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No running shell found "
                        f"(may have just completed — watch for the completion message).")

            # Read initial state under lock to avoid race with tracking thread.
            # All AsyncShellTask shared state must be accessed under task._lock
            # (see async_shell.py line 245 for locking discipline documentation).
            with task._lock:
                is_completed = task.completed
                rc = task.return_code

            if is_completed:
                elapsed = _elapsed_for_task(task)
                return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - Process already completed "
                        f"(exit code {rc}, elapsed {elapsed:.0f}s).")

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
                        logger.debug('[shell_cmd] _is_our_shell_msg: failed to str() message: %s', e)
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
                    return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - No new output "
                            f"(timeout after {timeout:.0f}s, elapsed {task_elapsed:.0f}s).")
                if _is_our_shell_msg(msg):
                    # Front message was THIS tool's shell msg → already consumed by the primitive.
                    return str(msg)
                # Front message is something else (user/system/other-tool) → it was only peeked,
                # still queued. Return default wake-up; normal drain delivers it in sequence.
                return (f"⟨shell_cmd wait⟩ Tool ID: {tool_id} - "
                        f"Woken by queued message (not this shell). Check your message queue.")

            # Fallback: pool lacks a real wait_for_message (e.g. MagicMock in unit tests).
            return _polling_wait(task, tool_id, agent_name, timeout, self.agent_pool)
        else:
            # Send as stdin input to the running process
            return tracker.send_input(agent_name, tool_id, command) or f"Input sent [Tool ID: {tool_id}]."

    # ────────────────────────────────────────────────────────────────
    def _execute_sync(
        self,
        agent_name: str,
        command: str,
        justification: str,
        cwd: str,
        timeout: int,
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
        # ── Strip `| head` / `| tail` pipe stages (BUG_0057): the base command runs,
        #    output is truncated with spillover instead. The rewritten command flows to
        #    execute_shell_command so the classifier sees the same string that executes. ──
        command, was_stripped = ShellCmd._strip_head_tail_pipes(command)

        # Get the truncation limit from agent/tool options
        char_limit = 2048
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('shell_char_limit', char_limit)
        elif self.cfg.get('shell_char_limit'):
            char_limit = self.cfg.get('shell_char_limit')

        result = self.agent_pool.operation_manager.execute_shell_command(
            command=command,
            justification=justification,
            agent_name=agent_name,
            cwd=cwd,
            char_limit=int(char_limit),
            timeout=timeout,
        )
        note = ShellCmd._strip_note(was_stripped)
        return f"{note}{result}"

    # ────────────────────────────────────────────────────────────────
    # safe execution_mode: free local model RAM/VRAM around a blocking command.
    # ────────────────────────────────────────────────────────────────
    def _get_instance(self, agent_name: str) -> Optional['AgentInstance']:
        """Resolve the caller AgentInstance from the pool, or None if absent.

        ``_get_agent_name`` can return 'unknown', so the None check in the caller is
        required, not decorative.
        """
        try:
            return self.agent_pool.get_instance(agent_name)
        except Exception:
            return None

    def _execute_safe(
        self,
        agent_name: str,
        command: str,
        justification: str,
        cwd: str,
        timeout: int,
    ) -> str:
        """Blocking execution that frees local model RAM/VRAM around the command.

        Order (per the safe-mode plan): save KV state BEFORE the security guard, unload
        the local model immediately before Popen, restore after. Degrades to plain sync
        when the caller's endpoint isn't state-save-capable.

        KV save/restore is FORCED ON for the command window regardless of the global
        "State KV Save/Restore" toggle (a pure system-agent checkpoint setting, off by
        default) — that is the whole point of safe mode: unload the model, run the
        command, restore KV with no reprocessing. The per-instance ``_safe_mode_kv``
        override (set/cleared here) is ORed into the engine's save/restore gates via
        ``state_ops.kv_save_effectively_enabled``. The model-release (unload) is ungated.

        A restore is only attempted when a fresh snapshot exists: if this call's own save
        failed/timed out and no Security advisor owns the label, the post-command restore is
        skipped (loading an arbitrarily old snapshot would silently replace a warm cache with
        a stale one); RAM safety still wins, so the unload is not rolled back. The advisor
        path keeps its stable-label restore — the advisor saved under the same label and the
        state FILE survives.
        """
        instance = self._get_instance(agent_name)
        pool = self.agent_pool

        # ── GATE A: caller-endpoint capability. No fixed ports anywhere. ──
        ep = getattr(instance, '_last_endpoint_config', None) if instance else None
        from agent_cascade.state_ops import is_state_save_capable
        capable = bool(is_state_save_capable(ep))

        # ── Advisor suppression: one boolean gating ONLY the save. ──
        # When the Security advisor will run it owns the KV save/restore on the SAME
        # label, so safe mode must not double-save. Read pool.auto_security with the
        # same default-True-when-missing semantics as the auto_security read in
        # approval.py so the tool and the guard can't disagree. The decision comes
        # from the SHARED instance entry point (same as the sync/async gates) —
        # calling it on pool.operation_manager means `self` is that operation_manager,
        # so we pass NO explicit self argument.
        advisor_will_run = bool(getattr(pool, 'auto_security', True)) and \
            not pool.operation_manager.evaluate_shell_command_safety(command, cwd, agent_name)[0]

        # Safe mode forces KV save/restore ON for this window regardless of the checkpoint
        # toggle (see docstring + state_ops.kv_save_effectively_enabled). Set before
        # execute_shell_command (which triggers the approval/security window) and cleared in
        # the finally below.
        if instance is not None:
            with instance._state_lock:
                instance._safe_mode_kv = True

        saved = freed = False
        restored = False
        if capable and not advisor_will_run:
            from agent_cascade.state_ops import save_instance_state
            # Lock around the save to match ExecutionEngine.save_before_slot_yield: makes the
            # read of _last_endpoint_config + write of _state_label atomic against a concurrent
            # advisor (removes a torn-state window). Safe because the instance's own _state_lock
            # is an RLock and save_instance_state re-acquires it.
            with instance._state_lock:
                saved = bool(save_instance_state(instance))  # BEFORE the guard

        def _release_model():
            # First statement inside the try in ShellMixin.execute_shell_command → its
            # exceptions hit that method's except, and this method's own finally still
            # restores.
            nonlocal freed
            freed = True  # set BEFORE the POST (media_gen V2/F1 discipline)
            from agent_cascade.state_ops import unload_all_models
            if not unload_all_models(ep['api_base']):
                logger.warning('[shell_cmd] model not unloaded before command')

        try:
            result = pool.operation_manager.execute_shell_command(
                command=command,
                justification=justification,
                agent_name=agent_name,
                cwd=cwd,
                timeout=timeout,
                before_execute=(_release_model if capable else None),
            )
        finally:
            try:
                if freed and (saved or advisor_will_run):
                    # Model was released → restore KV to the endpoint we actually hold
                    # (never a stale _last_endpoint_config). Restore by the STABLE label
                    # (instance_name), not the consume-once _state_label: a system agent
                    # (Security/Compressor) may have consumed _state_label inside the
                    # approval window before this unload, but the state FILE it wrote
                    # survives the load, so re-issuing state/load for the stable label
                    # re-warms the KV. Never raises.
                    from agent_cascade.state_ops import restore_held_slot_state
                    restored = bool(restore_held_slot_state(pool, instance, agent_name,
                                                            label=instance.instance_name))
                elif freed:
                    # The tool's own save FAILED or timed out, and no advisor owns the
                    # label — there is nothing fresh to restore. Loading would apply an
                    # arbitrarily old snapshot and report success against a stale snapshot. The unload still
                    # happened: RAM was the trigger, and memory safety wins over KV
                    # preservation. The KV is cold from here on; the next turn reprocesses.
                    logger.warning('[shell_cmd] safe mode: state save failed — skipping the '
                                   'post-command KV restore (no fresh snapshot to load)')
                elif saved:
                    # We saved but never unloaded (e.g. rejected): clear the label we set so
                    # it can't re-fire an expensive load; the KV is still warm.
                    from agent_cascade.state_ops import clear_orphaned_state_label
                    clear_orphaned_state_label(instance, agent_name)
            finally:
                # Always release the safe-mode KV override, even if the restore raised.
                if instance is not None:
                    with instance._state_lock:
                        instance._safe_mode_kv = False

        # ── Notification: gated on `freed` alone, but HONEST about what happened. ──
        # Never emitted on REJECTED/ERROR/degraded results, since freed is only ever
        # set inside before_execute, which is unreachable on the rejection path. The KV
        # half of the note is driven by what actually ran: the restore is skipped when the
        # save failed and may itself fail, so claiming "KV state restored" unconditionally
        # made the tool output contradict the autoloader log in both incident reports.
        note = ''
        if freed:
            if restored:
                note = ('[safe] Local model(s) unloaded before the command; KV state restored after.\n')
            elif saved or advisor_will_run:
                # The restore ran but did not succeed (or was skipped because the held
                # endpoint moved) — the KV is cold and the next turn reprocesses.
                note = ('[safe] Local model(s) unloaded before the command; KV state could NOT be '
                        'restored (the conversation will be reprocessed).\n')
            else:
                note = ('[safe] Local model(s) unloaded before the command; KV state save failed, '
                        'no restore attempted (the conversation will be reprocessed).\n')
        return note + result
