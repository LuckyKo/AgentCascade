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

import os
import sys
from typing import NoReturn


def restart_server_process() -> NoReturn:
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
    """
    os.execl(sys.executable, sys.executable, *sys.argv)
    os._exit(1)
