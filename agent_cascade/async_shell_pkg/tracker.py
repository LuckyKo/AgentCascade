"""AsyncShellTracker — background shell process manager (moved verbatim from async_shell.py).

Phase 3c pure-move refactor. Top of the dependency DAG (imports task, windows, constants).

``KILL_WAIT_TIMEOUT`` is read as ``constants.KILL_WAIT_TIMEOUT`` (module-attribute access)
inside ``kill_task`` so that patching ``agent_cascade.async_shell_pkg.constants.
KILL_WAIT_TIMEOUT`` takes effect at call time. All other timing constants are imported
by name (they are never patched by tests).
"""

import os
import signal
import subprocess
import threading
import time
import csv
import io
from typing import Dict, List, Optional, Tuple

from agent_cascade.log import logger
from agent_cascade.tool_utils import truncate_with_spillover
from agent_cascade.settings import (
    MAX_ASYNC_SHELL_PER_AGENT,
    ASYNC_SHELL_DEFAULT_TIMEOUT,
    HEARTBEAT_CHECK_INTERVAL,
    EARLY_OUTPUT_CHECK_TIMEOUT,
)
from agent_cascade.shell_utils import (
    DRAIN_THREAD_JOIN_TIMEOUT,
    drain_pipe_lines,
    configure_windows_utf8,
)
from agent_cascade.async_shell_pkg.task import AsyncShellTask, _elapsed_for_task
from agent_cascade.async_shell_pkg.windows import ON_WINDOWS, _WIN_ENV, _send_windows_ctrl_c
from agent_cascade.async_shell_pkg import constants
# Bare-name timing constants (never patched by tests). KILL_WAIT_TIMEOUT is deliberately
# NOT imported here — it is read via `constants.KILL_WAIT_TIMEOUT` so test patches take effect.
from agent_cascade.async_shell_pkg.constants import (  # noqa: F401
    PROCESS_KILL_SETTLE_DELAY,
    DRAIN_THREAD_FLUSH_DELAY,
    LAUNCH_POLL_INTERVAL,
    VIEWER_EXIT_WAIT_TIMEOUT,
)


def _dead_shell_message(agent_name: str, tool_id: int) -> str:
    """Error message for control calls against a shell whose record was cleaned up.

    The shell has completed or crashed and its task entry is gone. The message must
    stay unambiguously terminal so agents do not misread it as transient and re-poll
    in a loop (see reports/async_shell_polling_bug_status.md). The leading
    "No running shell found" substring is asserted by tests and must be kept.
    """
    return (
        f"No running shell found for agent '{agent_name}' with tool_id {tool_id} — "
        f"TERMINAL: this shell has completed or crashed and its record was cleaned up. "
        f"Do NOT poll again; the result is unobtainable from here (any completion message "
        f"was already queued). Continue without it."
    )


class AsyncShellTracker:
    """Manages background shell processes across all agents.

    Singleton per AgentPool — one tracker instance shared by all agents in the pool.
    Each agent has its own counter for tool_ids (simple 1,2,3... numbering).

    Attributes:
        _id_counters: Per-agent counter dict {agent_name: next_id}
        _tasks: Active tasks dict {agent_name: {tool_id: AsyncShellTask}}
        _lock: Lock protecting _id_counters and _tasks mutations
        _pool: Reference to AgentPool for enqueueing messages
    """

    def __init__(self, pool=None):
        """Initialize the async shell tracker.

        Args:
            pool: Optional reference to AgentPool instance for message injection.
        """
        self._id_counters: Dict[str, int] = {}
        self._tasks: Dict[str, Dict[int, AsyncShellTask]] = {}
        self._lock = threading.Lock()
        self._pool = pool

    # ────────────────────────────────────────────────────────────────
    def _next_id(self, agent_name: str) -> int:
        """Get the next tool_id for an agent (thread-safe)."""
        with self._lock:
            current = self._id_counters.get(agent_name, 0)
            current += 1
            self._id_counters[agent_name] = current
            return current

    # ────────────────────────────────────────────────────────────────
    def _get_task(self, agent_name: str, tool_id: int) -> Optional[AsyncShellTask]:
        """Get a task by agent name and tool_id (thread-safe read)."""
        with self._lock:
            return self._tasks.get(agent_name, {}).get(tool_id)

    # ────────────────────────────────────────────────────────────────
    def _active_count(self, agent_name: str) -> int:
        """Count active (non-completed) tasks for an agent."""
        with self._lock:
            return len(self._tasks.get(agent_name, {}))

    # ────────────────────────────────────────────────────────────────
    def _get_shell_char_limit(self) -> int:
        """Get shell output char limit from pool config.

        Reads shell_char_limit from llm_cfg to align with sync mode behavior.
        Default is 2048, matching the sync shell_cmd default.

        Returns:
            Character limit for shell output truncation (positive int).
            Returns -1 if configured that way (no limit).
        """
        if self._pool:
            llm_cfg = getattr(self._pool, 'llm_cfg', {})
            val = llm_cfg.get('shell_char_limit')
            if val is not None and isinstance(val, (int, float)):
                return int(val)
        return 2048  # default matching sync mode

    # ────────────────────────────────────────────────────────────────
    def has_active_tasks(self, agent_name: str) -> bool:
        """Check if there are active (non-completed) async shell tasks for an agent.
        
        Used by AgentPool.has_pending() to determine if an agent should SLEEP
        while waiting for background shell commands to complete.
        
        Thread safety: self._lock protects _tasks dict structure only.
        Individual task state (task.completed) is protected by task._lock.
        We snapshot the task dict reference under self._lock, then iterate
        and read each task's state under its own lock.
        """
        with self._lock:
            agent_tasks = dict(self._tasks.get(agent_name, {}))
        # Read task.completed under task._lock for thread safety
        for task in agent_tasks.values():
            with task._lock:
                if not task.completed:
                    return True
        return False

    # ────────────────────────────────────────────────────────────────
    def launch(
        self,
        agent_name: str,
        command: str,
        heartbeat_interval: float = -1.0,
        timeout: int = ASYNC_SHELL_DEFAULT_TIMEOUT,
        cwd: Optional[str] = None,
        console_window: bool = False,
    ) -> Tuple[int, int, Optional[List[str]], bool, Optional[int]]:
        """Launch a shell command in the background.

        Briefly polls after launch (up to 500ms) for early output or completion.
        A dedicated tracking thread monitors the process for heartbeats and completion.

        Args:
            agent_name: Which agent owns this task
            command: Shell command string to execute
            heartbeat_interval: Seconds between heartbeat updates (-1 = only notify on completion)
            timeout: Max seconds before killing the process tree
            cwd: Working directory (resolved by caller before passing here)
            console_window: If True, pop a visible console window on Windows. Default is False.

        Returns:
            Tuple of (tool_id, pid, early_output, completed_early, return_code):
              - tool_id: Simple counter per agent.
              - pid: 0 until the process actually starts (set asynchronously).
              - early_output: List of non-empty output lines captured during launch wait, or None.
              - completed_early: True if the command finished within the launch wait period.
              - return_code: Exit code if completed_early is True, else None.

        Raises:
            ValueError: If the agent already has MAX_ASYNC_SHELL_PER_AGENT active tasks.
        """
        # Enforce per-agent concurrency limit
        if self._active_count(agent_name) >= MAX_ASYNC_SHELL_PER_AGENT:
            raise ValueError(
                f"Agent '{agent_name}' already has {MAX_ASYNC_SHELL_PER_AGENT} "
                f"async shell commands running. Wait for one to finish or kill it first."
            )

        # Clamp invalid heartbeat intervals to -1 (completion-only mode)
        if heartbeat_interval < -1:
            logger.debug(
                f"[AsyncShell] Invalid heartbeat_interval={heartbeat_interval} for "
                f"{agent_name}, clamping to -1 (completion only)"
            )
            heartbeat_interval = -1

        # Opt-out override (e.g. test harnesses): force no console window regardless of caller state.
        # Does NOT change production defaults — only takes effect when this env var is set truthy.
        if console_window and os.getenv("QWEN_AGENT_DISABLE_ASYNC_SHELL_CONSOLE_WINDOW", "").strip() not in ("", "0", "false", "False"):
            console_window = False

        tool_id = self._next_id(agent_name)

        task = AsyncShellTask(
            tool_id=tool_id,
            agent_name=agent_name,
            command=command,
            heartbeat_interval=heartbeat_interval,
            timeout=timeout,
            console_window=console_window,
        )

        # Register the task before launching so tracking thread can find it
        with self._lock:
            self._tasks.setdefault(agent_name, {})[tool_id] = task

        # Spawn the tracking thread (daemon so it doesn't block process exit)
        tracker_thread = threading.Thread(
            target=self._track_task,
            args=(agent_name, tool_id, command, cwd),
            daemon=True,
            name=f'async_shell_tracker_{agent_name}_{tool_id}',
        )
        tracker_thread.start()

        # Briefly poll for early output or completion to avoid redundant messages.
        # The tracking thread now polls immediately (no blocking wait), so it can detect
        # fast completions and set task.completed before we check here. If we see
        # completed=True, we set completed_at_launch=True so the tracking thread skips
        # sending any messages (heartbeats, output, completion).
        try:
            early_output, completed_early, return_code = self._get_launch_result(
                agent_name, tool_id, timeout=EARLY_OUTPUT_CHECK_TIMEOUT
            )
        finally:
            # Always signal tracking thread that launch check is complete, even on exception.
            # This prevents deadlock if _get_launch_result raises unexpectedly.
            task.launch_check_done.set()

        return tool_id, 0, early_output, completed_early, return_code

    # ────────────────────────────────────────────────────────────────
    def _get_launch_result(
        self, agent_name: str, tool_id: int, timeout: float = EARLY_OUTPUT_CHECK_TIMEOUT
    ) -> Tuple[Optional[List[str]], bool, Optional[int]]:
        """Briefly poll after launch for early output or completion.

        After spawning the tracking thread, waits up to `timeout` seconds checking
        if the process has already produced output or finished. This avoids sending
        a redundant "launched" message when a command completes very quickly.

        Three possible outcomes:
        - Process started and completed within timeout: returns output (if any), True, return_code
        - Process started with output but still running: returns output lines, False, None
        - Timeout elapsed before process started: returns None, False, None (normal launched behavior)

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
            timeout: Maximum seconds to wait (default EARLY_OUTPUT_CHECK_TIMEOUT)

        Returns:
            Tuple of (early_output_lines_or_None, completed_early_bool, return_code_or_None).
            If completion was detected early, sets task.completed_at_launch=True so
            the tracking thread skips all messages.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            logger.warning(
                f"[AsyncShell] Task not found during launch result check: "
                f"{agent_name} tool_id={tool_id}"
            )
            return None, False, None

        start_wait = time.time()
        while True:
            with task._lock:
                # Wait until process has actually started
                if task.process is None:
                    time.sleep(LAUNCH_POLL_INTERVAL)
                    continue

                # Bounded wait: exit the loop when the timeout elapses. The final
                # check below (after the lock) re-reads completion, so a process
                # that finished right at the boundary is still detected here —
                # mirroring _poll_loop's liveness re-check pattern.
                if time.time() - start_wait >= timeout:
                    break

                pid = task.pid
                combined = list(task.stdout_lines) + list(task.stderr_lines)
                # Filter empty lines, mirroring _format_output_text's logic (returns list here, not joined string)
                early_output = [l for l in combined if l.strip()] if combined else None
                completed_early = task.completed
                return_code = task.return_code

                # If already completed, mark it so tracking thread skips all messages.
                # The caller will return the full result inline instead.
                if completed_early:
                    task.completed_at_launch = True

                # Update heartbeat position so subsequent heartbeats don't resend
                # lines already shown in the launch message.
                if early_output is not None:
                    task.last_heartbeat_sent_pos = len(combined)

            # Got process info — if completed, return immediately.
            # If we have output but not completion, keep polling briefly to see if
            # completion is detected (for fast commands like echo). Only return with
            # early_output if timeout is about to expire without completion.
            if completed_early:
                logger.info(
                    f"[AsyncShell] Early completion detected for {agent_name} "
                    f"tool_id={tool_id}, PID={pid}, rc={return_code}"
                )
                return early_output, completed_early, return_code

            # Process started but not yet completed — continue polling briefly.
            # This allows fast commands to be detected as complete even after output arrives.
            time.sleep(LAUNCH_POLL_INTERVAL)

        # Timeout elapsed — do one final check in case process completed right at the boundary.
        with task._lock:
            if task.process is not None:
                combined = list(task.stdout_lines) + list(task.stderr_lines)
                final_output = [l for l in combined if l.strip()] if combined else None

                # Check completion one last time before giving up
                if task.completed:
                    task.completed_at_launch = True
                    return final_output, True, task.return_code

                # Mark output as shown so tracking thread doesn't resend it
                if final_output is not None:
                    task.last_heartbeat_sent_pos = len(combined)

                return final_output, False, None
            return None, False, None

    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _format_output_text(lines: List[str]) -> str:
        """Filter empty lines and join into output text.

        Used by both _send_heartbeat and _get_remaining_output_text to avoid duplication.
        """
        output_lines = [line for line in lines if line.strip()]
        return '\n'.join(output_lines) if output_lines else ''

    # ────────────────────────────────────────────────────────────────
    def _spawn_process(self, agent_name: str, tool_id: int, command: str, cwd: Optional[str]) -> 'AsyncShellTask':
        """Launch the subprocess and configure pipe drain threads.

        Args:
            agent_name: Owner agent name (for logging).
            tool_id: Task identifier (for logging).
            command: Shell command string.
            cwd: Working directory path.

        Returns:
            The AsyncShellTask with process, PID, and drain threads attached.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            logger.debug(f"[AsyncShell] Task not found at spawn: {agent_name} tool_id={tool_id}")
            raise RuntimeError(f"Task {agent_name}/{tool_id} vanished before spawn")

        original_command = command
        creationflags = 0
        env = None

        if ON_WINDOWS:
            # Use shared UTF-8 config with console window popup for async shells
            command, creationflags = configure_windows_utf8(command, create_new_console=task.console_window)
            env = _WIN_ENV

            # Add -NoProfile to PowerShell commands to suppress profile.ps1 loading errors.
            if original_command.strip().lower().startswith('powershell'):
                # Insert -NoProfile after powershell executable name (handles paths like C:\...\powershell.exe)
                import re
                command = re.sub(
                    r'^(powershell(?:\.exe)?\s+)(?!.*-NoProfile)',
                    r'\1-NoProfile ',
                    original_command,
                    count=1,
                    flags=re.IGNORECASE,
                )
                # Re-apply UTF-8 config since we modified the command
                command, creationflags = configure_windows_utf8(command, create_new_console=task.console_window)

        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            shell=True,
            stdin=subprocess.PIPE,      # Enable stdin for interactive input via send_input()
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=creationflags,
            start_new_session=True,
            env=env,
        )

        # Set PID on the task so status queries can report it
        with task._lock:
            task.pid = proc.pid
            task.process = proc

        logger.debug(
            f"[AsyncShell] Launched tool_id={tool_id} for {agent_name}, "
            f"PID={proc.pid}, cmd='{original_command[:80]}'"
        )

        # ── Spawn viewer process for visible console window on Windows ───
        # When console_window=True and stdout/stderr are piped, Windows doesn't show
        # a visible conhost window even with CREATE_NEW_CONSOLE. Workaround: spawn a
        # secondary cmd.exe WITHOUT pipes that inherits console output to display visibly.
        # The viewer is killed in _kill_process_tree only on timeout or external kill.
        # On normal completion, the viewer is left to exit naturally so the console
        # window stays visible until the command truly finishes.
        if ON_WINDOWS and task.console_window:
            try:
                # Reuse configure_windows_utf8 to get consistent chcp prefix
                viewer_cmd, _ = configure_windows_utf8(original_command, create_new_console=True)

                # Use shell=False to avoid quoting issues with special characters in command.
                # The command string is passed as a single argument to cmd.exe /c, preserving its structure.
                viewer = subprocess.Popen(
                    ['cmd.exe', '/c', viewer_cmd],
                    cwd=str(cwd) if cwd else None,
                    creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
                    env=env,
                    # No stdout/stderr args → inherit from parent so it shows in its own window
                )
                with task._lock:
                    task.viewer_process = viewer
                logger.debug(
                    f"[AsyncShell] Viewer process spawned for tool_id={tool_id}, "
                    f"PID={viewer.pid}"
                )
            except Exception as e:
                # Viewer failure affects user-facing behavior (no visible window); log at warning level
                logger.warning(
                    f"[AsyncShell] Failed to spawn viewer for tool_id={tool_id}: {e}"
                )

        # ── Pipe draining threads (use shared drain_pipe_lines) ─────
        stdout_lock = threading.Lock()
        stderr_lock = threading.Lock()

        t_out = threading.Thread(
            target=drain_pipe_lines, args=(proc.stdout, task.stdout_lines, stdout_lock),
            daemon=True, name=f'shell_stdout_{tool_id}',
        )
        t_err = threading.Thread(
            target=drain_pipe_lines, args=(proc.stderr, task.stderr_lines, stderr_lock),
            daemon=True, name=f'shell_stderr_{tool_id}',
        )
        t_out.start()
        t_err.start()

        # Store drain thread refs on task so _track_task can join them later
        with task._lock:
            task._drain_t_out = t_out
            task._drain_t_err = t_err
            task._stdout_lock = stdout_lock
            task._stderr_lock = stderr_lock

        return task

    # ────────────────────────────────────────────────────────────────
    def _poll_loop(self, agent_name: str, tool_id: int, proc: subprocess.Popen, task: 'AsyncShellTask', t_out: threading.Thread, t_err: threading.Thread) -> bool:
        """Main heartbeat/timeout polling loop.

        Args:
            agent_name: Owner agent name.
            tool_id: Task identifier.
            proc: Popen handle for the running subprocess.
            task: AsyncShellTask being tracked.
            t_out: Stdout drain thread.
            t_err: Stderr drain thread.

        Returns:
            True if the process timed out, False otherwise.
        """
        timed_out = False
        last_heartbeat_time = time.time()

        while True:
            # Re-check liveness every iteration (not just at the loop head). A
            # process that exits right after the 0.5s poll sleep must not be
            # allowed to fall through into timeout handling — doing so would
            # kill an already-dead tree, mark the task as timed out, and
            # corrupt the recorded return code (see TestStderrCapture flakiness).
            if proc.poll() is not None:
                break

            # Exit immediately if killed externally (prevents further heartbeats).
            with task._lock:
                if task.killed:
                    break

            elapsed = time.time() - task.start_time
            if elapsed > task.timeout:
                timed_out = True
                # Don't kill here — let _track_task be the single owner of process termination.
                # Just break so it can handle cleanup consistently.
                break

            # Send heartbeat if interval configured and enough time passed.
            # Re-read heartbeat_interval each iteration so __heartbeat=-1 takes effect immediately.
            with task._lock:
                current_hb_interval = task.heartbeat_interval
                is_killed = task.killed  # Read under same lock as heartbeat_interval
            if current_hb_interval > 0 and not is_killed:
                since_last_hb = time.time() - last_heartbeat_time
                if since_last_hb >= current_hb_interval:
                    self._send_heartbeat(agent_name, tool_id)
                    last_heartbeat_time = time.time()

            # Sleep briefly to avoid busy-waiting (poll interval)
            time.sleep(HEARTBEAT_CHECK_INTERVAL)

        return timed_out

    # ────────────────────────────────────────────────────────────────
    def _wait_for_completion(self, proc: subprocess.Popen, task: 'AsyncShellTask') -> None:
        """Wait for process to finish and capture return code.

        Args:
            proc: Popen handle for the running subprocess.
            task: AsyncShellTask being tracked.
        """
        if proc.returncode is not None:
            with task._lock:
                task.return_code = proc.returncode

    @staticmethod
    def _cleanup_viewer(viewer: Optional[subprocess.Popen], tool_id: int) -> None:
        """Wait for viewer to exit naturally, force-kill if it doesn't.

        Called after normal task completion to avoid leaving orphaned viewer processes.
        The viewer_process reference on the task must already be cleared before calling.
        """
        if viewer is None:
            return

        try:
            viewer.wait(timeout=VIEWER_EXIT_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Viewer didn't exit in time — kill it directly to avoid orphaned processes.
            if viewer.poll() is None:
                try:
                    if ON_WINDOWS:
                        subprocess.run(
                            ['taskkill', '/F', '/T', '/PID', str(viewer.pid)],
                            capture_output=True, timeout=5, text=True,
                        )
                    else:
                        viewer.kill()
                    logger.debug(f"[AsyncShell] Killed viewer PID {viewer.pid}")
                except Exception as e:
                    logger.debug(f"[AsyncShell] Failed to kill viewer PID {viewer.pid}: {e}")
        except Exception as e:
            logger.debug(f"[AsyncShell] Viewer wait failed for tool_id={tool_id}: {e}")

    # ────────────────────────────────────────────────────────────────
    def _track_task(self, agent_name: str, tool_id: int, command: str, cwd: Optional[str]):
        """Track a single shell task from launch to completion.

        This runs in its own thread and handles:
        1. Process launch with UTF-8 config on Windows + console window popup
        2. Stdout/stderr draining via background threads
        3. Periodic heartbeat injection into agent's message queue
        4. Final result injection on completion or timeout
        5. Cleanup of the task from _tasks dict

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
            command: Shell command string
            cwd: Working directory path
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            logger.debug(f"[AsyncShell] Task not found at start of _track_task: {agent_name} tool_id={tool_id}")
            return

        t_out, t_err = None, None  # Track drain threads for join in finally
        timed_out = False          # Ensure defined even if exception occurs before _poll_loop

        try:
            original_command = command

            # ── Spawn process and pipe drain threads ────────────────
            self._spawn_process(agent_name, tool_id, command, cwd)

            # Read process and thread refs under lock for thread safety.
            # These are set atomically in _spawn_process under the same lock.
            with task._lock:
                proc = task.process
                t_out, t_err, stdout_lock, stderr_lock = (
                    task._drain_t_out, task._drain_t_err,
                    task._stdout_lock, task._stderr_lock,
                )

            # ── Poll loop: start immediately to detect fast completions ───
            timed_out = self._poll_loop(agent_name, tool_id, proc, task, t_out, t_err)

            # If killed externally or timed out, kill process tree (main + viewer).
            # For normal completion, don't kill the viewer — let it finish naturally
            # so the console window stays visible until the command is actually done.
            with task._lock:
                killed_externally = task.killed
                kill_in_progress = task.kill_in_progress
            if killed_externally and not kill_in_progress:
                # External kill requested but kill_task hasn't started yet (e.g., race).
                # Only kill main process if still alive; _kill_process_tree handles viewer cleanup.
                if proc is not None and proc.poll() is None:
                    self._kill_process_tree(proc, agent_name, tool_id)
                    time.sleep(PROCESS_KILL_SETTLE_DELAY)
                elif task.viewer_process is not None:
                    # Main process already finished but kill was requested — just kill viewer.
                    self._kill_viewer_process(task)
            elif timed_out:
                # Timeout — kill both main process tree and viewer
                self._kill_process_tree(proc, agent_name, tool_id)
                time.sleep(PROCESS_KILL_SETTLE_DELAY)
            else:
                # Normal completion: main process has finished (poll_loop exited).
                # Don't kill the viewer — let it complete on its own so the console
                # window stays visible until the command is truly done. The viewer
                # will exit naturally when its copy of the command finishes.
                with task._lock:
                    viewer = task.viewer_process
                    task.viewer_process = None  # Cleanup reference before waiting

                self._cleanup_viewer(viewer, tool_id)

            # ── Mark completed immediately so _get_launch_result() can detect it ───
            # Set completed and return_code right after poll_loop exits (process finished).
            # This allows launch()'s early check to see completion before we do cleanup.
            # If launch() detects it first, it sets completed_at_launch=True and returns
            # the full result inline; we skip all messages below when that flag is set.
            if killed_externally:
                rc = proc.returncode if proc.returncode is not None else -1  # Killed externally
            else:
                rc = proc.returncode if proc.returncode is not None else (1 if timed_out else 0)
            with task._lock:
                task.completed = True
                task.return_code = rc

            # Wait for reader threads to drain remaining buffers
            t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
            t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

            # ── Wait for launch() to finish its early check ───────────
            # Ensure launch_check_done is set before reading completed_at_launch.
            # This prevents a race where we check the flag before _get_launch_result
            # has had a chance to set it for fast-completing commands.
            task.launch_check_done.wait()

            # ── Check if command completed during launch ───────────
            # If launch() detected completion and returned the full result inline,
            # skip all messages (heartbeats, remaining output, completion).
            with task._lock:
                done_at_launch = task.completed_at_launch

            if done_at_launch:
                logger.debug(
                    f"[AsyncShell] Task completed at launch for {agent_name} "
                    f"tool_id={tool_id}, skipping all messages"
                )
            else:
                # Build a SINGLE merged completion message containing status + elapsed
                # time + any remaining output. Previously this was two separate messages
                # (a "final output" message followed by a "completed" message); merging
                # them avoids the useless duplicate and keeps everything in one reply.
                task = self._get_task(agent_name, tool_id)
                header = self._build_completion_header(tool_id, task, timed_out=timed_out)
                remaining_text = self._get_remaining_output_text(agent_name, tool_id)
                msg = header if not remaining_text else f"{header}\n\nOutput:\n{remaining_text}"

                # Enqueue the merged message BEFORE the finally block removes the task
                # from _tasks. Order matters: _enqueue() puts the message on the queue;
                # if we cleaned up first, has_pending() would return False and a sleeping
                # agent might miss it. A single enqueue before cleanup preserves the
                # wake-up guarantee (has_pending stays True while the message is in flight).
                self._enqueue(agent_name, msg)

        except Exception as e:
            logger.warning(
                f"[AsyncShell] Track error for {agent_name} tool_id={tool_id}: {e}"
            )
            # Clean up processes if they exist to avoid orphans
            with task._lock:
                proc = task.process
            if proc is not None:
                try:
                    self._kill_process_tree(proc, agent_name, tool_id)
                except Exception as kill_err:
                    logger.warning(f"[AsyncShell] Kill on track error failed for {agent_name} tool_id={tool_id}: {kill_err}")
            self._send_completion_message(
                agent_name, tool_id,
                timed_out=False, error=str(e),
            )
            with task._lock:
                task.completed = True
                task.return_code = -1

        finally:
            # Join drain threads before removing from _tasks to prevent data races.
            if t_out is not None and t_out.is_alive():
                t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
            if t_err is not None and t_err.is_alive():
                t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

            # Cleanup from _tasks dict
            with self._lock:
                if agent_name in self._tasks:
                    self._tasks[agent_name].pop(tool_id, None)
                    if not self._tasks[agent_name]:
                        del self._tasks[agent_name]

    # ────────────────────────────────────────────────────────────────
    def _kill_viewer_process(self, task: 'AsyncShellTask') -> None:
        """Atomically retrieve and kill the viewer process under the task's lock.

        Called from _kill_process_tree to clean up the secondary console window.
        Resets task.viewer_process after cleanup to prevent stale references.

        Args:
            task: The AsyncShellTask whose viewer should be killed.
        """
        with task._lock:
            viewer = task.viewer_process
            task.viewer_process = None  # Reset immediately to avoid re-kill race

        if viewer and viewer.poll() is None:
            try:
                if ON_WINDOWS:
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(viewer.pid)],
                        capture_output=True, timeout=5, text=True,
                    )
                else:
                    viewer.kill()
                logger.debug(f"[AsyncShell] Killed viewer PID {viewer.pid}")
            except Exception as e:
                logger.debug(f"[AsyncShell] Failed to kill viewer PID {viewer.pid}: {e}")

    # ────────────────────────────────────────────────────────────────
    def _get_windows_descendant_pids(self, parent_pid: int) -> List[int]:
        """Get all descendant PIDs of a process on Windows using WMI via PowerShell.

        Uses Get-CimInstance Win32_Process to get PID/PPID mapping for all processes,
        then recursively collects descendants. This is more reliable than tasklist which
        doesn't expose PPID in its CSV output.

        Args:
            parent_pid: The root PID whose descendants to find.

        Returns:
            List of descendant PIDs (children, grandchildren, etc.), excluding parent itself.
        """
        try:
            # Query all processes with their PPIDs using PowerShell/CIM
            ps_cmd = (
                'Get-CimInstance Win32_Process | '
                'Select-Object ProcessId, ParentProcessId | '
                'ConvertTo-Csv -NoTypeInformation'
            )
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                logger.debug(f"[AsyncShell] PowerShell CIM query failed (rc={result.returncode}): {result.stderr.strip()}")
                return []
            if not result.stdout.strip():
                return []

            # Build PPID -> [child_pids] mapping from output
            ppid_to_children: Dict[int, List[int]] = {}
            reader = csv.reader(io.StringIO(result.stdout))
            next(reader, None)  # Skip header row
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    pid = int(row[0])
                    ppid = int(row[1])
                    ppid_to_children.setdefault(ppid, []).append(pid)
                except (ValueError, IndexError):
                    continue

            # Recursively collect descendants starting from parent_pid
            # Use visited set to prevent infinite loops from process tree cycles
            descendants: List[int] = []
            visited: set = set()
            to_visit = [parent_pid]
            while to_visit:
                current = to_visit.pop()
                if current in visited:
                    continue
                visited.add(current)
                for child in ppid_to_children.get(current, []):
                    if child != parent_pid and child not in visited:  # Exclude root + prevent duplicates
                        descendants.append(child)
                        to_visit.append(child)

            return descendants
        except Exception as e:
            logger.debug(f"[AsyncShell] Failed to get descendant PIDs for {parent_pid}: {e}")
            return []

    # ────────────────────────────────────────────────────────────────
    def _check_windows_pids_alive(self, pids: List[int]) -> List[int]:
        """Check which of the given PIDs are still alive on Windows.

        Uses a single PowerShell call to query all PIDs at once via Get-CimInstance
        with a Where-Object filter, avoiding one subprocess per PID.

        Args:
            pids: List of PIDs to check.

        Returns:
            Subset of pids that are still running.
        """
        if not pids:
            return []

        alive = []
        try:
            # Build comma-separated list for PowerShell filter
            pid_list = ','.join(str(p) for p in pids)
            ps_cmd = (
                f'Get-CimInstance Win32_Process -Filter "ProcessId IN ({pid_list})" | '
                'Select-Object -ExpandProperty ProcessId'
            )
            result = subprocess.run(
                ['powershell', '-NoProfile', '-Command', ps_cmd],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                target_set = set(pids)
                for line in result.stdout.strip().splitlines():
                    line = line.strip()
                    if line.isdigit():
                        pid = int(line)
                        if pid in target_set:
                            alive.append(pid)
        except Exception as e:
            logger.debug(f"[AsyncShell] Failed to check PIDs {pids}: {e}")

        return alive

    # ────────────────────────────────────────────────────────────────
    def _kill_process_tree(self, proc: subprocess.Popen, agent_name: str, tool_id: int):
        """Kill the main process tree and its associated viewer process (if any).

        Terminates the primary shell subprocess plus all descendants via taskkill/killpg.
        On Windows, captures descendant PIDs before killing, verifies after, and warns
        about any survivors.

        Known survivor scenarios (processes NOT in the kill tree):
        - cmd.exe & operator: spawns sibling processes under same parent, not children of cmd
        - start command: creates detached processes adopted by explorer/System
        - PowerShell Start-Process with separate window: similar detachment behavior

        For these cases, taskkill /T cannot reach them as they are not descendants.
        A warning is logged listing surviving PIDs so users can investigate.

        Also cleans up the secondary "viewer" console window spawned on Windows when
        console_window=True. Uses _kill_viewer_process to avoid race conditions around
        viewer_process access.

        Args:
            proc: Popen handle for the main process.
            agent_name: Owner agent name (for task lookup).
            tool_id: Task identifier (for task lookup and logging).
        """
        pid = proc.pid
        logger.debug(f"[AsyncShell] Killing process tree for {agent_name} tool_id={tool_id}, PID={pid}")

        # Skip if already dead
        if proc.poll() is not None:
            logger.debug(f"[AsyncShell] Process {pid} already finished, skipping kill")
            return

        if ON_WINDOWS:
            # Capture descendant PIDs before killing to verify later
            descendant_pids = self._get_windows_descendant_pids(pid)
            all_target_pids = [pid] + descendant_pids
            if descendant_pids:
                logger.debug(f"[AsyncShell] Captured {len(descendant_pids)} descendant PID(s) for tool_id={tool_id}: {descendant_pids}")

            try:
                # First pass: taskkill with tree flag (main process)
                result = subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    capture_output=True, timeout=10, text=True,
                )
                time.sleep(PROCESS_KILL_SETTLE_DELAY)
                if result.returncode == 0:
                    logger.debug(f"[AsyncShell] Successfully killed PID {pid} (taskkill succeeded)")
                else:
                    logger.warning(f"[AsyncShell] taskkill for PID {pid} failed with rc={result.returncode}: {result.stderr.strip()}")
            except Exception as e:
                logger.warning(f"[AsyncShell] taskkill for PID {pid}: {e}")

            # Verify all captured PIDs are dead; warn about survivors
            if descendant_pids:
                time.sleep(0.3)  # Allow process table to update after kill
                survivors = self._check_windows_pids_alive(all_target_pids)
                if survivors:
                    logger.warning(
                        f"[AsyncShell] __kill: {len(survivors)} process(es) survived tree kill "
                        f"for tool_id={tool_id}: {survivors}. They will be orphaned."
                    )

        else:
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                logger.debug(f"[AsyncShell] Sent SIGKILL to process group {pid}")
            except OSError:
                try:
                    proc.kill()
                    logger.debug(f"[AsyncShell] Fallback kill for PID {pid}")
                except Exception as e:
                    logger.warning(f"[AsyncShell] kill fallback for PID {pid}: {e}")

        # Kill viewer process after main (separate console window)
        task = self._get_task(agent_name, tool_id)
        if task is not None:
            self._kill_viewer_process(task)

    # ────────────────────────────────────────────────────────────────
    def _get_combined_output(self, task: AsyncShellTask) -> List[str]:
        """Get combined stdout+stderr output lines from a task."""
        with task._lock:
            combined = list(task.stdout_lines) + list(task.stderr_lines)
        return combined

    # ────────────────────────────────────────────────────────────────
    def _send_heartbeat(self, agent_name: str, tool_id: int):
        """Send a periodic heartbeat with new output since last heartbeat.

        Reads accumulated stdout/stderr lines from the task that haven't been sent yet,
        truncates to shell_char_limit from llm_cfg (same as sync mode), and enqueues as a user message.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
        """
        # Ensure task is still registered and not killed before sending heartbeat.
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return

        with task._lock:
            if task.killed:
                return
            # Read output + update position atomically under the same lock
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            new_lines = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)
            # Increment heartbeat counter for this task
            task.heartbeat_count += 1
            beat = task.heartbeat_count
            # NOTE: computed inline (already holding task._lock above); must NOT use
            # _elapsed_for_task() here as it re-acquires the non-reentrant lock -> deadlock.
            elapsed = time.time() - task.start_time

        # Re-check killed flag after reading output to avoid sending heartbeat for killed tasks.
        with task._lock:
            if task.killed:
                return

        if not new_lines:
            # Still running with no new output — send minimal heartbeat so sleeping
            # agents wake up and know the process hasn't died.
            logger.debug("[async_shell] heartbeat(no output) agent=%s tool_id=%s beat=%s",
                         agent_name, tool_id, beat)
            msg = f"⟨shell_cmd heartbeat⟩ Beat {beat} ({elapsed:.0f}s), Tool ID: {tool_id} | No new output (still running)"
            self._enqueue(agent_name, msg)
            return

        logger.debug("[async_shell] heartbeat with output agent=%s tool_id=%s lines=%d",
                     agent_name, tool_id, len(new_lines))
        output_text = self._format_output_text(new_lines)
        if not output_text:
            return

        # Count original lines before truncation (so header reflects actual data sent)
        line_count = len(output_text.split('\n'))

        # Truncate to shell_char_limit from config (same as sync mode)
        char_limit = self._get_shell_char_limit()
        if char_limit > 0:
            try:
                base_dir = self._pool.operation_manager.base_dir if self._pool and hasattr(self._pool, 'operation_manager') else None
                if base_dir:
                    output_text = truncate_with_spillover(
                        output_text, char_limit,
                        instance_name=agent_name,
                        tool_name='shell_cmd_async',
                        base_dir=base_dir,
                        operation_mode='mid',
                    )
            except Exception as e:
                logger.debug(f"[AsyncShell] truncate_with_spillover failed in heartbeat for {agent_name}: {e}")

        msg = (
            f"⟨shell_cmd heartbeat⟩ Beat {beat} ({elapsed:.0f}s), Tool ID: {tool_id} | "
            f"{line_count} line{'s' if line_count != 1 else ''} since last tick\n"
            f"{output_text}"
        )
        # Send heartbeat via message queue (wakes sleeping agents)
        self._enqueue(agent_name, msg)

    # ────────────────────────────────────────────────────────────────
    def _get_remaining_output_text(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Return the not-yet-sent output text (truncated), or None if there is none.

        Advances ``task.last_heartbeat_sent_pos`` under ``task._lock`` exactly as the
        old ``_send_remaining_output`` did, so the same output is never re-sent by a
        later heartbeat/status call. The returned text is truncated to
        shell_char_limit (mid-truncation with spillover). Does NOT enqueue anything —
        callers assemble and send the final message themselves.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return None

        # Read output + update position atomically under the same lock
        with task._lock:
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            remaining = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)

        if not remaining:
            return None

        output_text = self._format_output_text(remaining)

        # Truncate large remaining output using mid-truncation with spillover
        char_limit = self._get_shell_char_limit()
        if char_limit > 0:
            try:
                base_dir = self._pool.operation_manager.base_dir if self._pool and hasattr(self._pool, 'operation_manager') else None
                if base_dir:
                    output_text = truncate_with_spillover(
                        output_text, char_limit,
                        instance_name=agent_name,
                        tool_name='shell_cmd_async',
                        base_dir=base_dir,
                        operation_mode='mid',
                    )
            except Exception as e:
                logger.debug(f"[AsyncShell] truncate_with_spillover failed in remaining output for {agent_name}: {e}")

        return output_text or None

    # ────────────────────────────────────────────────────────────────
    def _build_completion_header(
        self, tool_id: int, task: Optional['AsyncShellTask'],
        timed_out: bool = False, error: Optional[str] = None,
    ) -> str:
        """Build the two-line completion header (no output) for a finished shell.

        Pure helper shared by the merged completion message (_track_task), the
        standalone error path (_send_completion_message), and the early-completion
        path in shell_cmd.py so the format cannot drift between call sites.

        Formats:
          - Normal : "⟨shell_cmd completed⟩ Tool ID: {id}\\nCompleted in {elapsed:.1f} s ({status})."
          - Timeout: "⟨shell_cmd completed⟩ Tool ID: {id}\\nTimed out after {timeout}s ({elapsed:.1f}s total). All child processes terminated."
          - Error  : "⟨shell_cmd completed⟩ Tool ID: {id}\\nError: {error} ({elapsed:.1f}s elapsed)."
        """
        elapsed = _elapsed_for_task(task)

        if timed_out and not error:
            timeout_val = task.timeout if task else '?'
            return (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Timed out after {timeout_val}s ({elapsed:.1f}s total). "
                f"All child processes terminated."
            )
        elif error:
            return (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Error: {error} ({elapsed:.1f}s elapsed)."
            )
        else:
            rc = task.return_code if task and task.return_code is not None else 0
            if rc == -1:
                status = "killed externally (via __kill)"
            elif rc == 0:
                status = "success"
            else:
                status = f"exit code {rc}"
            return (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Completed in {elapsed:.1f} s ({status})."
            )

    # ────────────────────────────────────────────────────────────────
    def _send_completion_message(
        self, agent_name: str, tool_id: int,
        timed_out: bool = False, error: Optional[str] = None,
    ):
        """Send the final completion message to the agent.

        Used by the exception path in _track_task (error=...) and any caller that
        wants a standalone completion message without merging output. The normal/
        timeout completion path instead uses the merged single-message build in
        _track_task so the agent sees one message, not two.
        """
        task = self._get_task(agent_name, tool_id)
        header = self._build_completion_header(tool_id, task, timed_out=timed_out, error=error)

        # Send completion via message queue (wakes sleeping agents)
        self._enqueue(agent_name, header)

    # ────────────────────────────────────────────────────────────────
    def _enqueue(self, agent_name: str, text: str):
        """Inject a message into the agent's queue via the pool."""
        if self._pool:
            try:
                self._pool.enqueue_message(agent_name, text)
            except Exception as e:
                logger.debug(f"[AsyncShell] Enqueue failed for {agent_name}: {e}")

    # ────────────────────────────────────────────────────────────────
    def send_input(self, agent_name: str, tool_id: int, input_text: str) -> Optional[str]:
        """Send stdin input to a running shell process.

        Note: All async shells use stdin=PIPE. Some Windows commands (e.g., 'timeout')
        fail when stdin is redirected. Use alternative delay commands like
        'ping -n N 127.0.0.1 >nul' instead.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
            input_text: Text to write to the process's stdin

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return _dead_shell_message(agent_name, tool_id)

        elapsed = _elapsed_for_task(task)
        try:
            with task._lock:
                proc = task.process
            if proc and proc.stdin:
                proc.stdin.write(input_text + '\n')
                proc.stdin.flush()
                return f"Input sent to shell [Tool ID: {tool_id}, PID: {task.pid}] (elapsed {elapsed:.0f}s)."
            else:
                return f"Shell stdin not available for tool_id {tool_id} (PID: {task.pid}) (elapsed {elapsed:.0f}s)."
        except Exception as e:
            return f"Failed to send input to tool_id {tool_id}: {e}"

    # ────────────────────────────────────────────────────────────────
    def kill_task(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Request termination of a running async shell task and wait for confirmation.

        Sets the killed flag so the tracking thread detects it and exits poll_loop.
        Then force-kills the process tree directly (no race with tracking thread).
        Waits until proc.poll() confirms the process is actually dead before returning.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return _dead_shell_message(agent_name, tool_id)

        try:
            with task._lock:
                proc = task.process
                pid = task.pid
                if proc and proc.poll() is None:
                    # Set killed flag under same lock as poll check to prevent TOCTOU.
                    task.killed = True
                    task.kill_in_progress = True
                else:
                    proc = None  # Signal that process already finished

            if proc is not None:
                # Force-kill the process tree directly for immediate termination.
                self._kill_process_tree(proc, agent_name, tool_id)

                # Wait until the process is confirmed dead before returning.
                deadline = time.time() + constants.KILL_WAIT_TIMEOUT
                while proc.poll() is None and time.time() < deadline:
                    time.sleep(0.1)

                with task._lock:
                    task.kill_in_progress = False

                elapsed = _elapsed_for_task(task)
                if proc.poll() is not None:
                    return f"Shell killed [Tool ID: {tool_id}, PID: {pid}] (elapsed {elapsed:.0f}s)."
                else:
                    logger.warning(f"[AsyncShell] kill_task timed out waiting for PID {pid} to die")
                    return f"Shell kill requested but process PID {pid} did not terminate within {constants.KILL_WAIT_TIMEOUT}s (elapsed {elapsed:.0f}s)."
            else:
                with task._lock:
                    rc = task.return_code
                elapsed = _elapsed_for_task(task)
                return f"Shell already finished [Tool ID: {tool_id}], return code: {rc or 0} (elapsed {elapsed:.0f}s)."
        except Exception as e:
            return f"Failed to kill tool_id {tool_id}: {e}"

    # ────────────────────────────────────────────────────────────────
    def send_ctrl_c(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Send Ctrl+C (SIGINT / console event) to a running shell.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return _dead_shell_message(agent_name, tool_id)

        try:
            with task._lock:
                proc = task.process
                pid = task.pid

            if not proc or proc.poll() is not None:
                elapsed = _elapsed_for_task(task)
                return f"Shell already finished [Tool ID: {tool_id}] (elapsed {elapsed:.0f}s)."

            if ON_WINDOWS:
                # proc.send_signal(CTRL_C_EVENT) fails for cmd.exe commands running with
                # CREATE_NEW_CONSOLE. Use GenerateConsoleCtrlEvent via helper subprocess.
                success = _send_windows_ctrl_c(pid)
                if not success:
                    logger.debug(f"[AsyncShell] Ctrl+C helper failed for {agent_name} tool_id={tool_id}")
                    return f"Failed to send Ctrl+C to tool_id {tool_id}: GenerateConsoleCtrlEvent returned failure."
            else:
                import signal as sig
                os.killpg(os.getpgid(proc.pid), sig.SIGINT)

            elapsed = _elapsed_for_task(task)
            return f"Ctrl+C sent to shell [Tool ID: {tool_id}, PID: {pid}] (elapsed {elapsed:.0f}s)."
        except Exception as e:
            return f"Failed to send Ctrl+C to tool_id {tool_id}: {e}"

    # ────────────────────────────────────────────────────────────────
    def update_heartbeat(self, agent_name: str, tool_id: int, new_interval: float) -> Optional[str]:
        """Update the heartbeat interval for a running task.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
            new_interval: New interval in seconds (-1 to disable heartbeats)

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return _dead_shell_message(agent_name, tool_id)

        with task._lock:
            old = task.heartbeat_interval
            task.heartbeat_interval = new_interval
        elapsed = _elapsed_for_task(task)
        return f"Heartbeat interval updated from {old}s to {new_interval}s [Tool ID: {tool_id}] (elapsed {elapsed:.0f}s)."

    # ────────────────────────────────────────────────────────────────
    def get_status(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Get current status and consume all accumulated output (manual heartbeat).

        Returns PID, lifetime, and full accumulated stdout/stderr since last consumption.
        Updates last_heartbeat_sent_pos so subsequent heartbeats don't re-send this output.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier

        Returns:
            Status string with process info and consumed output lines.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return _dead_shell_message(agent_name, tool_id)

        # Read status fields under lock
        with task._lock:
            pid = task.pid
            completed = task.completed
            return_code = task.return_code
            heartbeat = task.heartbeat_interval
            # NOTE: computed inline (already holding task._lock above); must NOT use
            # _elapsed_for_task() here as it re-acquires the non-reentrant lock -> deadlock.
            elapsed = time.time() - task.start_time

            # Consume all output since last consumption (same pattern as _send_heartbeat)
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            consumed_lines = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)

        # Format status header
        if completed:
            rc = return_code if return_code is not None else "?"
            status_label = f"completed (exit code {rc}, {elapsed:.0f}s)"
        else:
            status_label = f"running ({elapsed:.0f}s elapsed)"
        
        msg = (
            f"⟨shell_cmd status⟩ Tool ID: {tool_id}\n"
            f"Status: {status_label}\n"
            f"PID: {pid}\n"
            f"Heartbeat interval: {heartbeat}s\n"
            f"Command: `{task.command[:200]}`\n"
        )

        # Append consumed output if any
        if consumed_lines:
            output_text = self._format_output_text(consumed_lines)

            # Truncate large outputs using mid-truncation with spillover
            char_limit = self._get_shell_char_limit()
            if char_limit > 0:
                try:
                    base_dir = self._pool.operation_manager.base_dir if self._pool and hasattr(self._pool, 'operation_manager') else None
                    if base_dir:
                        output_text = truncate_with_spillover(
                            output_text, char_limit,
                            instance_name=agent_name,
                            tool_name='shell_cmd_async',
                            base_dir=base_dir,
                            operation_mode='mid',
                        )
                except Exception as e:
                    logger.debug(f"[AsyncShell] truncate_with_spillover failed in get_status for {agent_name}: {e}")

            msg += f"\nOutput ({len(consumed_lines)} lines):\n{output_text}"
        else:
            msg += "\nNo new output since last status check."

        return msg

    # ────────────────────────────────────────────────────────────────
    def kill_all(self, agent_name: str) -> int:
        """Kill all async shell tasks for a specific agent (primarily async).

        Called during agent dismissal to clean up background processes.
        Sets killed flags on all active tasks; tracking threads detect these and
        perform the actual process termination. Waits briefly after setting flags
        to allow tracking threads time to propagate kills, but does not block until
        every process has fully terminated (use kill_task() for synchronous waits).

        Args:
            agent_name: Agent whose shells should be killed

        Returns:
            Number of shells terminated.
        """
        with self._lock:
            tasks = dict(self._tasks.get(agent_name, {}))

        count = 0
        for tool_id, task in tasks.items():
            try:
                with task._lock:
                    proc = task.process

                # Set killed flag — tracking thread will detect this and handle
                # the actual process termination via _kill_process_tree.
                if proc and proc.poll() is None:
                    with task._lock:
                        task.killed = True
                    count += 1
            except Exception as e:
                logger.debug(
                    f"[AsyncShell] Kill-all error for {agent_name} tool_id={tool_id}: {e}"
                )

        # Wait briefly so tracking threads can propagate the kill and flush output.
        if count > 0:
            time.sleep(DRAIN_THREAD_FLUSH_DELAY)

        with self._lock:
            if agent_name in self._tasks:
                # Mark all remaining tasks as completed to prevent stale heartbeats.
                # Don't delete entries — let tracking threads' finally blocks handle cleanup.
                for task in self._tasks[agent_name].values():
                    with task._lock:
                        task.completed = True

        return count
