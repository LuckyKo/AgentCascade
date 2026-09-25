"""Shared server-restart helper (single copy of the re-exec/relaunch logic).

Both restart entry points — the WebSocket ``restart_server`` handler
(``ws_handlers.handle_restart_server``) and the REST ``POST /api/restart``
endpoint (``api_server.api_restart``) — call :func:`restart_server_process`.
Keeping the spawn/re-exec here means the two paths cannot drift apart.

Platform behavior:
- POSIX:   in-place re-exec via ``os.execl(sys.executable, sys.executable, *sys.argv)``
  (preserves PID, console/stdin, and environment).
- Windows: detached child via ``subprocess.Popen([sys.executable, *sys.argv],
  CREATE_NO_WINDOW | DETACHED_PROCESS)`` followed by ``os._exit(0)``.
  ``DETACHED_PROCESS`` guarantees the child survives the parent's exit;
  ``CREATE_NO_WINDOW`` prevents a console pop-up.

The "Server is restarting…" broadcast/notice is intentionally NOT part of this
helper: the two callers use different broadcast mechanisms (WS ``broadcast_fn``
vs REST module-level ``broadcast``) and both send their notice BEFORE calling
here. The helper is pure sync code with no async/app dependencies.
"""

import os
import subprocess
import sys
from typing import NoReturn


def restart_server_process() -> NoReturn:
    """Re-launch the AC server process with the same interpreter + argv + environment.

    - POSIX:   ``os.execl(sys.executable, sys.executable, *sys.argv)`` (in-place re-exec)
    - Windows: detached ``subprocess.Popen([sys.executable, *sys.argv])``, then ``os._exit(0)``

    Does NOT return on success (replaces or exits the process). Only exits AFTER a
    successful spawn/re-exec — if the spawn raises, the exception propagates and the
    running server is left alive. Reads ``os.name`` at call time (not import time) so
    tests can monkeypatch the branch selection.
    """
    if os.name == 'nt':
        # Spawn a detached child running the same interpreter + argv, then exit.
        # The parent exits immediately after spawn; the child re-binds the port on
        # its own (start_api_server.py retries EADDRINUSE during startup).
        subprocess.Popen(
            [sys.executable, *sys.argv],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
            cwd=os.getcwd(),
        )
        os._exit(0)
    else:
        # In-place re-exec. os.execl only returns on failure (in which case it has
        # already raised), but belt-and-braces exit if we ever get here anyway.
        os.execl(sys.executable, sys.executable, *sys.argv)
        os._exit(1)
