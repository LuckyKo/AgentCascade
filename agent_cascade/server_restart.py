"""Shared server-restart helper — in-place re-exec on ALL platforms.

Both restart entry points — the WebSocket ``restart_server`` handler
(``ws_handlers.handle_restart_server``) and the REST ``POST /api/restart``
endpoint (``api_server.api_restart``) — call :func:`restart_server_process`.
Keeping the re-exec here means the two paths cannot drift apart.

Platform behavior: in-place re-exec via
``os.execl(sys.executable, sys.executable, *sys.argv)`` on every platform.
- POSIX:   replaces the process image; PID, console/stdin and environment are preserved.
- Windows: spawns a new process image that inherits the current console window
  (no ``CREATE_NO_WINDOW``/``DETACHED_PROCESS``) with the same interpreter,
  argv, env and CWD, then exits the old process. The user's terminal stays the
  running server — no hidden detached child.

The "Server is restarting…" broadcast/notice is intentionally NOT part of this
helper: the two callers use different broadcast mechanisms (WS ``broadcast_fn``
vs REST module-level ``broadcast``) and both send their notice BEFORE calling
here. The helper is pure sync code with no async/app dependencies.
"""

import logging
import os
import sys
from typing import Callable, NoReturn


# Plain stdlib logger on purpose — this module's contract is "no app dependencies"
# (see module docstring). The caller-injected ``before_reexec`` hook is logged here so a
# failed final-save before the re-exec stays visible without importing the app logger.
_logger = logging.getLogger(__name__)


def restart_server_process(before_reexec: Callable[[], None] | None = None) -> NoReturn:
    """Re-launch the AC server in place via ``os.execl`` (same interpreter + argv + env).

    In-place re-exec on all platforms:
    - POSIX:   replaces the process image; PID, console/stdin and environment are preserved.
    - Windows: spawns a new process image that inherits the current console window,
      then exits the old process — the user's terminal stays the running server.

    Does NOT return on success (replaces or exits the process). Only exits AFTER a
    successful re-exec — if ``os.execl`` raises, the exception propagates and the
    running server is left alive. The belt-and-braces ``os._exit(1)`` below is
    unreachable unless ``execl`` returns, which only happens on failure (after it
    has already raised).

    Sets the ``_AC_RESTART_IN_PROGRESS`` env sentinel right before the re-exec so
    the NEW process can tell it came from a restart (env vars survive ``os.execl``;
    the startup hook pops it first thing, so a crash mid-notice can't leak it into
    a later boot).

    ``before_reexec`` (optional): a zero-arg callable run on a best-effort basis
    immediately BEFORE the sentinel + re-exec. Its one use is letting a caller flush
    in-memory state (e.g. a telemetry checkpoint) that would otherwise be lost when
    ``os.execl`` replaces the process image (no SIGTERM/atexit/finally runs there).
    Exceptions are logged and swallowed — a failed hook must never block a requested
    restart. Kept app-free by design: the hook is injected, never imported here.
    """
    if before_reexec is not None:
        try:
            before_reexec()
        except Exception as e:
            _logger.warning('Pre-restart hook failed (non-fatal, restart continues): %s', e)

    os.environ['_AC_RESTART_IN_PROGRESS'] = '1'
    os.execl(sys.executable, sys.executable, *sys.argv)
    os._exit(1)
