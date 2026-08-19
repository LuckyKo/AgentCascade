"""Thin facade for the async_shell package (Phase 3c pure-move refactor).

This module used to contain all background-shell execution logic. It has been split into
``agent_cascade.async_shell_pkg`` (constants / windows / task / tracker). Production importers
are unchanged: they import from ``agent_cascade.async_shell`` and receive the SAME objects now
re-exported from the sub-package.

``KILL_WAIT_TIMEOUT`` is defined in ``async_shell_pkg.constants`` and read by the tracker via
module-attribute access, so tests can patch it at ``..._pkg.constants.KILL_WAIT_TIMEOUT``.
"""

from agent_cascade.async_shell_pkg.tracker import AsyncShellTracker  # noqa: F401
from agent_cascade.async_shell_pkg.task import AsyncShellTask, _elapsed_for_task  # noqa: F401
from agent_cascade.async_shell_pkg.windows import ON_WINDOWS, _WIN_ENV, _send_windows_ctrl_c  # noqa: F401
from agent_cascade.async_shell_pkg import constants, windows, task  # noqa: F401

# Re-export public timing constants for backward compatibility.
from agent_cascade.async_shell_pkg.constants import (  # noqa: F401
    KILL_WAIT_TIMEOUT,
    PROCESS_KILL_SETTLE_DELAY,
    DRAIN_THREAD_FLUSH_DELAY,
    LAUNCH_POLL_INTERVAL,
    VIEWER_EXIT_WAIT_TIMEOUT,
)

__all__ = [
    "AsyncShellTracker",
    "AsyncShellTask",
    "_elapsed_for_task",
    "ON_WINDOWS",
    "_WIN_ENV",
    "_send_windows_ctrl_c",
    "KILL_WAIT_TIMEOUT",
    "PROCESS_KILL_SETTLE_DELAY",
    "DRAIN_THREAD_FLUSH_DELAY",
    "LAUNCH_POLL_INTERVAL",
    "VIEWER_EXIT_WAIT_TIMEOUT",
]
