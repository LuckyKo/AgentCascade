"""
Async Shell Module — Background shell command execution with heartbeat support.

Provides the AsyncShellTracker that manages background shell processes:
- Launches commands in background, returns immediately with tool_id + PID
- Drains stdout/stderr in dedicated threads (reuses shared pipe drain pattern)
- Sends periodic heartbeats via agent message queue
- Injects final result into agent message queue on completion
- Handles timeout by killing process tree
- Pops console window on Windows for user inspection (TODO #21)

Components:
- AsyncShellTask: Dataclass tracking individual shell executions
- AsyncShellTracker: Singleton per AgentPool managing all background shells
"""

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from agent_cascade.log import logger
from agent_cascade.settings import (
    MAX_ASYNC_SHELL_PER_AGENT,
    ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS,
    ASYNC_SHELL_DEFAULT_TIMEOUT,
)
from agent_cascade.shell_utils import (
    DRAIN_THREAD_JOIN_TIMEOUT,
    drain_pipe_lines,
    configure_windows_utf8,
)

# ─── Named constants ──────────────────────────────────────────────
HEARTBEAT_CHECK_INTERVAL = 0.5          # How often the tracker thread checks for heartbeats (seconds)
HEARTBEAT_TRUNCATE_FIRST_LINES = 5      # Lines kept at start when truncating heartbeat output
HEARTBEAT_TRUNCATE_LAST_LINES = 10       # Lines kept at end when truncating heartbeat output
EARLY_OUTPUT_CHECK_TIMEOUT = 0.5        # Max seconds to wait for early output/completion after launch

ON_WINDOWS = os.name == 'nt'

# Pre-cached Windows environment dict with PYTHONIOENCODING set for child Python processes
if ON_WINDOWS:
    _WIN_ENV = os.environ.copy()
    _WIN_ENV['PYTHONIOENCODING'] = 'utf-8'
else:
    _WIN_ENV = None


@dataclass
class AsyncShellTask:
    """Tracks a background shell command execution.

    Attributes:
        tool_id: Simple counter ID (1, 2, 3...) assigned per agent
        agent_name: Which agent owns this task
        command: The shell command string
        pid: Process PID (0 until process starts)
        process: Popen handle for the running subprocess
        stdout_lines: Accumulated stdout output lines
        stderr_lines: Accumulated stderr output lines
        heartbeat_interval: Seconds between heartbeat updates (-1 = only on completion)
        timeout: Max seconds before process is killed
        start_time: When this task was created (epoch float)
        completed: Whether the command has finished
        return_code: Exit code of the process (None until complete)
        last_heartbeat_sent_pos: Index into combined output for tracking what was sent
        console_window: Pop a console window on Windows for user inspection
        completion_reported: Whether completion was already reported from launch() for fast commands
        launch_complete: Event signaled when launch() finishes its early result check (synchronizes with _track_task)
    """
    tool_id: int
    agent_name: str
    command: str
    pid: int = 0
    process: Optional[subprocess.Popen] = None
    stdout_lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)
    heartbeat_interval: float = -1.0
    timeout: int = ASYNC_SHELL_DEFAULT_TIMEOUT
    start_time: float = field(default_factory=time.time)
    completed: bool = False
    return_code: Optional[int] = None
    last_heartbeat_sent_pos: int = 0   # Index into combined output lines
    console_window: bool = True        # Pop console window (TODO #21)
    completion_reported: bool = False  # Track if completion was reported early from launch()

    # Lock for thread-safe access to mutable fields during heartbeat reads
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Event to synchronize launch() with _track_task — ensures early result check completes
    # before tracking thread proceeds, preventing race on completion_reported.
    launch_complete: threading.Event = field(default_factory=threading.Event, repr=False)


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

        # Fix #7: Validate heartbeat interval
        if heartbeat_interval < -1:
            logger.debug(
                f"[AsyncShell] Invalid heartbeat_interval={heartbeat_interval} for "
                f"{agent_name}, clamping to -1 (completion only)"
            )
            heartbeat_interval = -1

        tool_id = self._next_id(agent_name)

        task = AsyncShellTask(
            tool_id=tool_id,
            agent_name=agent_name,
            command=command,
            heartbeat_interval=heartbeat_interval,
            timeout=timeout,
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
        # Wrap in try/finally to guarantee launch_complete is signaled even if an exception
        # occurs — without this, _track_task would deadlock waiting on the event.
        try:
            early_output, completed_early, return_code = self._get_launch_result(
                agent_name, tool_id, timeout=EARLY_OUTPUT_CHECK_TIMEOUT
            )
        finally:
            task.launch_complete.set()

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
            If completion was detected early, sets task.completion_reported=True so
            the tracking thread doesn't send a duplicate completion message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            logger.warning(
                f"[AsyncShell] Task not found during launch result check: "
                f"{agent_name} tool_id={tool_id}"
            )
            return None, False, None

        start_wait = time.time()
        while time.time() - start_wait < timeout:
            with task._lock:
                # Wait until process has actually started
                if task.process is None:
                    time.sleep(0.05)
                    continue

                pid = task.pid
                combined = list(task.stdout_lines) + list(task.stderr_lines)
                # Filter empty lines, mirroring _format_output_text's logic (returns list here, not joined string)
                early_output = [l for l in combined if l.strip()] if combined else None
                completed_early = task.completed
                return_code = task.return_code

                # If already completed, mark as reported atomically under the same lock.
                # This prevents _track_task from sending a duplicate completion message.
                if completed_early:
                    task.completion_reported = True

                # Update heartbeat position so subsequent heartbeats don't resend
                # lines already shown in the launch message.
                if early_output is not None:
                    task.last_heartbeat_sent_pos = len(combined)

            # Got process info, don't wait longer regardless of state
            if completed_early:
                logger.debug(
                    f"[AsyncShell] Early completion detected for {agent_name} "
                    f"tool_id={tool_id}, PID={pid}, rc={return_code}"
                )
            elif early_output:
                logger.debug(
                    f"[AsyncShell] Early output captured for {agent_name} "
                    f"tool_id={tool_id}, PID={pid}, lines={len(early_output)}"
                )

            return early_output, completed_early, return_code

        # Timeout elapsed without process starting — fall through to normal launched behavior
        return None, False, None

    # ────────────────────────────────────────────────────────────────
    @staticmethod
    def _format_output_text(lines: List[str]) -> str:
        """Filter empty lines and join into output text.

        Used by both _send_heartbeat and _send_remaining_output to avoid duplication.
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

        proc = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
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

        # Set PID on the task so status queries can report it
        with task._lock:
            task.pid = proc.pid
            task.process = proc

        logger.debug(
            f"[AsyncShell] Launched tool_id={tool_id} for {agent_name}, "
            f"PID={proc.pid}, cmd='{original_command[:80]}'"
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

        while proc.poll() is None:
            elapsed = time.time() - task.start_time
            if elapsed > task.timeout:
                timed_out = True
                self._kill_process_tree(proc, agent_name, tool_id)
                # Wait briefly for drain threads to flush after kill
                t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
                t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
                break

            # Send heartbeat if interval configured and enough time passed
            if task.heartbeat_interval > 0:
                since_last_hb = time.time() - last_heartbeat_time
                if since_last_hb >= task.heartbeat_interval:
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
        # Fix #3: Ensure cleanup even on exception — register task for tracking
        task = self._get_task(agent_name, tool_id)
        if task is None:
            logger.debug(f"[AsyncShell] Task not found at start of _track_task: {agent_name} tool_id={tool_id}")
            return

        t_out, t_err = None, None  # Track drain threads for join in finally
        timed_out = False          # Fix: ensure defined if exception before _poll_loop

        try:
            original_command = command

            # ── Spawn process and pipe drain threads ────────────────
            self._spawn_process(agent_name, tool_id, command, cwd)
            proc = task.process
            t_out, t_err, stdout_lock, stderr_lock = (
                task._drain_t_out, task._drain_t_err,
                task._stdout_lock, task._stderr_lock,
            )

            # Wait for launch() to finish its early result check. This ensures that
            # completion_reported is set (if applicable) before we proceed to poll
            # and potentially send a completion message, preventing duplicate messages.
            task.launch_complete.wait()

            # ── Poll loop: wait for process with heartbeat checks ───
            timed_out = self._poll_loop(agent_name, tool_id, proc, task, t_out, t_err)

            # Wait for reader threads to drain remaining buffers
            t_out.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)
            t_err.join(timeout=DRAIN_THREAD_JOIN_TIMEOUT)

            # ── Capture return code ────────────────────────────────
            self._wait_for_completion(proc, task)

            # ── Send any remaining output as final heartbeat ─────────
            self._send_remaining_output(agent_name, tool_id, timed_out)

            # ── Send final result BEFORE marking completed ─────────────
            # Order matters: send_completion_message puts into _async_results.
            # If we mark completed first, has_pending() returns False and a
            # sleeping agent might transition to COMPLETING before seeing the
            # completion message. By sending first, the agent either sees the
            # message in _async_results (wakes up) or has_pending() still
            # returns True (keeps sleeping, will see message next iteration).
            # Skip if already reported early from launch() for fast-completing commands.
            with task._lock:
                already_reported = task.completion_reported
            if not already_reported:
                self._send_completion_message(agent_name, tool_id, original_command, timed_out)

            with task._lock:
                task.completed = True

        except Exception as e:
            logger.warning(
                f"[AsyncShell] Track error for {agent_name} tool_id={tool_id}: {e}"
            )
            self._send_completion_message(
                agent_name, tool_id, original_command,
                timed_out=False, error=str(e),
            )
            with task._lock:
                task.completed = True
                task.return_code = -1

        finally:
            # Fix #2 & #3: Join drain threads before removing from _tasks
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
    def _kill_process_tree(self, proc: subprocess.Popen, agent_name: str, tool_id: int):
        """Kill the process and its descendants. Reuses ShellMixin logic."""
        pid = proc.pid
        if ON_WINDOWS:
            try:
                # First pass: taskkill with tree flag
                subprocess.run(
                    ['taskkill', '/F', '/T', '/PID', str(pid)],
                    capture_output=True, timeout=10, text=True,
                )
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"[AsyncShell] taskkill for PID {pid}: {e}")
        else:
            try:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except OSError:
                try:
                    proc.kill()
                except Exception as e:
                    logger.warning(f"[AsyncShell] kill fallback for PID {pid}: {e}")

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
        truncates to ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS, and enqueues as a user message.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return

        # Fix #1: Read output + update position atomically under the same lock
        with task._lock:
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            new_lines = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)

        if not new_lines:
            # Still running with no new output — send minimal heartbeat so sleeping
            # agents wake up and know the process hasn't died.
            logger.debug("[async_shell] heartbeat(no output) agent=%s tool_id=%s",
                         agent_name, tool_id)
            msg = f"⟨shell_cmd heartbeat⟩ Tool ID: {tool_id} | No new output (still running)"
            pool = self._pool
            if pool and hasattr(pool, '_async_results'):
                pool._async_results.put(agent_name, msg, function_id=f"heartbeat_{tool_id}")
            else:
                self._enqueue(agent_name, msg)
            return

        logger.debug("[async_shell] heartbeat with output agent=%s tool_id=%s lines=%d",
                     agent_name, tool_id, len(new_lines))
        output_text = self._format_output_text(new_lines)
        if not output_text:
            return

        # Truncate to heartbeat char limit (keep first/last N lines with ellipsis)
        max_chars = ASYNC_SHELL_HEARTBEAT_TRUNCATE_CHARS
        if len(output_text) > max_chars:
            lines = output_text.split('\n')
            keep_first = min(HEARTBEAT_TRUNCATE_FIRST_LINES, len(lines))
            keep_last = min(HEARTBEAT_TRUNCATE_LAST_LINES, len(lines))
            skipped = len(lines) - keep_first - keep_last
            truncated_lines = lines[:keep_first]
            if skipped > 0:
                truncated_lines.append(f"... ({skipped} lines omitted ...)")
            truncated_lines.extend(lines[-keep_last:])
            output_text = '\n'.join(truncated_lines)

        # Count lines for info header
        line_count = len(output_text.split('\n'))

        msg = (
            f"⟨shell_cmd heartbeat⟩ Tool ID: {tool_id} | "
            f"{line_count} line{'s' if line_count != 1 else ''} since last tick\n"
            f"{output_text}"
        )
        # Put heartbeat into async result buffer so it wakes sleeping agents
        pool = self._pool
        if pool and hasattr(pool, '_async_results'):
            pool._async_results.put(agent_name, msg, function_id=f"heartbeat_{tool_id}")
        else:
            # Fallback to normal enqueue if pool not available
            self._enqueue(agent_name, msg)

    # ────────────────────────────────────────────────────────────────
    def _send_remaining_output(self, agent_name: str, tool_id: int, timed_out: bool):
        """Send any output not yet sent as heartbeats."""
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return

        # Fix #1: Read output + update position atomically under the same lock
        with task._lock:
            combined = list(task.stdout_lines) + list(task.stderr_lines)
            remaining = combined[task.last_heartbeat_sent_pos:]
            task.last_heartbeat_sent_pos = len(combined)

        if not remaining:
            return

        output_text = self._format_output_text(remaining)
        line_count = len(output_text.split('\n'))

        if timed_out:
            msg = (
                f"⟨shell_cmd final output⟩ Tool ID: {tool_id} | "
                f"{line_count} remaining line{'s' if line_count != 1 else ''}\n"
                f"{output_text}"
            )
        else:
            msg = (
                f"⟨shell_cmd final output⟩ Tool ID: {tool_id} | "
                f"{line_count} line{'s' if line_count != 1 else ''}\n"
                f"{output_text}"
            )

        # Put remaining output into async result buffer so it wakes sleeping agents
        pool = self._pool
        if pool and hasattr(pool, '_async_results'):
            pool._async_results.put(agent_name, msg, function_id=f"shell_remaining_{tool_id}")
        else:
            # Fallback to normal enqueue if pool not available
            self._enqueue(agent_name, msg)

    # ────────────────────────────────────────────────────────────────
    def _send_completion_message(
        self, agent_name: str, tool_id: int, command: str,
        timed_out: bool = False, error: Optional[str] = None,
    ):
        """Send the final completion message to the agent."""
        task = self._get_task(agent_name, tool_id)

        if timed_out and not error:
            msg = (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Timed out after {task.timeout if task else '?'} seconds. "
                f"All child processes terminated.\n"
                f"Command: `{command[:200]}`\n"
            )
        elif error:
            msg = (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Error: {error}\n"
                f"Command: `{command[:200]}`\n"
            )
        else:
            rc = task.return_code if task else 0
            status = "success" if (rc == 0) else f"exit code {rc}"
            msg = (
                f"⟨shell_cmd completed⟩ Tool ID: {tool_id}\n"
                f"Completed ({status}).\n"
                f"Command: `{command[:200]}`\n"
            )

        # Put completion into async result buffer so it wakes sleeping agents
        pool = self._pool
        if pool and hasattr(pool, '_async_results'):
            pool._async_results.put(agent_name, msg, function_id=f"shell_complete_{tool_id}")
        else:
            # Fallback to normal enqueue if pool not available
            self._enqueue(agent_name, msg)

    # ────────────────────────────────────────────────────────────────
    def _enqueue(self, agent_name: str, text: str):
        """Inject a message into the agent's queue via the pool."""
        if self._pool and hasattr(self._pool, 'enqueue_message'):
            try:
                self._pool.enqueue_message(agent_name, text)
            except Exception as e:
                logger.debug(f"[AsyncShell] Enqueue failed for {agent_name}: {e}")

    # ────────────────────────────────────────────────────────────────
    def send_input(self, agent_name: str, tool_id: int, input_text: str) -> Optional[str]:
        """Send stdin input to a running shell process.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier
            input_text: Text to write to the process's stdin

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return f"No running shell found for agent '{agent_name}' with tool_id {tool_id}."

        try:
            with task._lock:
                proc = task.process
            if proc and proc.stdin:
                proc.stdin.write(input_text + '\n')
                proc.stdin.flush()
                return f"Input sent to shell [Tool ID: {tool_id}, PID: {task.pid}]."
            else:
                return f"Shell stdin not available for tool_id {tool_id} (PID: {task.pid})."
        except Exception as e:
            return f"Failed to send input to tool_id {tool_id}: {e}"

    # ────────────────────────────────────────────────────────────────
    def kill_task(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Kill a running shell task.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier

        Returns:
            Confirmation string or error message.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return f"No running shell found for agent '{agent_name}' with tool_id {tool_id}."

        try:
            with task._lock:
                proc = task.process
                pid = task.pid
            if proc and proc.poll() is None:
                self._kill_process_tree(proc, agent_name, tool_id)
                return f"Shell killed [Tool ID: {tool_id}, PID: {pid}]."
            else:
                with task._lock:
                    rc = task.return_code
                return f"Shell already finished [Tool ID: {tool_id}], return code: {rc or 0}."
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
            return f"No running shell found for agent '{agent_name}' with tool_id {tool_id}."

        try:
            with task._lock:
                proc = task.process
                pid = task.pid
            if ON_WINDOWS:
                # Send CTRL_C_EVENT to the process group on Windows
                if proc:
                    proc.send_signal(signal.CTRL_C_EVENT)  # type: ignore[attr-defined]
            else:
                import signal as sig
                if proc:
                    os.killpg(os.getpgid(proc.pid), sig.SIGINT)
            return f"Ctrl+C sent to shell [Tool ID: {tool_id}, PID: {pid}]."
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
            return f"No running shell found for agent '{agent_name}' with tool_id {tool_id}."

        with task._lock:
            old = task.heartbeat_interval
            task.heartbeat_interval = new_interval
        return f"Heartbeat interval updated from {old}s to {new_interval}s [Tool ID: {tool_id}]."

    # ────────────────────────────────────────────────────────────────
    def get_status(self, agent_name: str, tool_id: int) -> Optional[str]:
        """Get the current status of a running shell task.

        Args:
            agent_name: Owner agent name
            tool_id: Task identifier

        Returns:
            Status string with process info and recent output lines.
        """
        task = self._get_task(agent_name, tool_id)
        if task is None:
            return f"No running shell found for agent '{agent_name}' with tool_id {tool_id}."

        with task._lock:
            pid = task.pid
            completed = task.completed
            return_code = task.return_code
            heartbeat = task.heartbeat_interval
            elapsed = time.time() - task.start_time

        # Get last 20 lines of output for context
        combined = self._get_combined_output(task)
        recent_lines = [l for l in combined[-20:] if l.strip()]
        output_preview = '\n'.join(recent_lines[:15]) if recent_lines else "(no output yet)"

        status_label = "completed" if completed else f"running ({elapsed:.0f}s elapsed)"
        msg = (
            f"⟨shell_cmd status⟩ Tool ID: {tool_id}\n"
            f"Status: {status_label}\n"
            f"PID: {pid}\n"
            f"Return code: {return_code if completed else 'N/A'}\n"
            f"Heartbeat interval: {heartbeat}s\n"
            f"Command: `{task.command[:200]}`\n"
            f"Recent output:\n{output_preview}"
        )
        return msg

    # ────────────────────────────────────────────────────────────────
    def kill_all(self, agent_name: str) -> int:
        """Kill all async shell tasks for a specific agent.

        Called during agent dismissal to clean up background processes.
        Waits briefly for tracking threads to finish after killing each process.

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
                if proc and proc.poll() is None:
                    self._kill_process_tree(proc, agent_name, tool_id)
                    # Wait briefly so drain threads flush remaining output
                    time.sleep(0.2)
                    count += 1
            except Exception as e:
                logger.debug(
                    f"[AsyncShell] Kill-all error for {agent_name} tool_id={tool_id}: {e}"
                )

        with self._lock:
            if agent_name in self._tasks:
                # Mark all remaining tasks as completed to prevent stale heartbeats
                for task in self._tasks[agent_name].values():
                    with task._lock:
                        task.completed = True
                del self._tasks[agent_name]

        return count