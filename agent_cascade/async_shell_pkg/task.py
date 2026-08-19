"""AsyncShellTask data model + elapsed-time helper (moved verbatim from async_shell.py).

Phase 3c pure-move refactor.
"""

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from agent_cascade.settings import ASYNC_SHELL_DEFAULT_TIMEOUT

def _elapsed_for_task(task: Optional['AsyncShellTask']) -> float:
    """Return seconds elapsed since task creation, read under the task lock.

    Returns 0.0 when there is no task or no start_time. Safe to call from any
    thread; this helper acquires ``task._lock`` internally.

    WARNING: Do NOT call this while already holding ``task._lock`` — the lock is
    non-reentrant, so a second acquire from the same thread will deadlock. If you
    are already inside a ``with task._lock:`` block, compute elapsed inline as
    ``time.time() - task.start_time`` instead (see _send_heartbeat / get_status).

    Used by every shell_cmd reply that has a live or completed task record so
    elapsed time is reported consistently.
    """
    if task is None:
        return 0.0
    with task._lock:
        return time.time() - task.start_time


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
        completed_at_launch: Whether the command finished during launch so tracking thread skips all messages
        launch_check_done: Event signaled when launch() has finished its early completion check
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
    heartbeat_count: int = 0           # Number of heartbeats sent for this task
    console_window: bool = False       # Pop console window (TODO #21)
    completed_at_launch: bool = False  # If True, tracking thread skips heartbeats/output/completion

    # Lock for thread-safe access to mutable fields during heartbeat reads
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Event to synchronize launch()'s early check with tracking thread's completed_at_launch read
    launch_check_done: threading.Event = field(default_factory=threading.Event, repr=False)

    # Flag set under _lock when task is killed externally; prevents further heartbeats.
    killed: bool = False

    # Set by kill_task before calling _kill_process_tree to prevent double-kill with tracking thread.
    kill_in_progress: bool = False

    # Secondary "viewer" process for visible console window on Windows (fire-and-forget)
    viewer_process: Optional[subprocess.Popen] = None
