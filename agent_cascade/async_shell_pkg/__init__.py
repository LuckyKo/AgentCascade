"""async_shell package — pure-move split of async_shell.py (Phase 3c).

Dependency DAG (bottom-up): constants → windows/task → tracker.
"""

from agent_cascade.async_shell_pkg import constants  # noqa: F401  (re-export)
from agent_cascade.async_shell_pkg import task  # noqa: F401  (re-export)
from agent_cascade.async_shell_pkg import tracker  # noqa: F401  (re-export)
from agent_cascade.async_shell_pkg import windows  # noqa: F401  (re-export)

__all__ = [
    'AsyncShellTracker',
    'AsyncShellTask',
    '_elapsed_for_task',
    'ON_WINDOWS',
    'KILL_WAIT_TIMEOUT',
]
