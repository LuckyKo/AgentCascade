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
  ``CREATE_NO_WINDOW`` prevents a console pop-up. The child's std handles are
  set explicitly (stdin=DEVNULL, stdout/stderr -> ``logs/server_restart.log``
  in append mode) because a detached no-window child that inherits the
  parent's console std handles has undefined I/O and dies or hangs silently
  before binding the port. Redirecting to a file makes bootstrap failures
  diagnosable instead of silent.

The "Server is restarting…" broadcast/notice is intentionally NOT part of this
helper: the two callers use different broadcast mechanisms (WS ``broadcast_fn``
vs REST module-level ``broadcast``) and both send their notice BEFORE calling
here. The helper is pure sync code with no async/app dependencies.
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# Anchored to the project root (same convention as log.py), NOT cwd — so the
# bootstrap log lands in <project>/logs/ regardless of where the server was
# launched from. The child still inherits the working dir via Popen(cwd=...).
LOG_FILE = Path(__file__).resolve().parent.parent / 'logs' / 'server_restart.log'


def restart_server_process() -> NoReturn:
    """Re-launch the AC server process with the same interpreter + argv + environment.

    - POSIX:   ``os.execl(sys.executable, sys.executable, *sys.argv)`` (in-place re-exec)
    - Windows: detached ``subprocess.Popen([sys.executable, *sys.argv])``, then ``os._exit(0)``.
      The child's stdin is DEVNULL and stdout/stderr are redirected to
      ``logs/server_restart.log`` (append) so bootstrap failures are captured
      in a file instead of being silently lost.

    Does NOT return on success (replaces or exits the process). Only exits AFTER a
    successful spawn/re-exec — if the spawn raises, the exception propagates and the
    running server is left alive. Reads ``os.name`` at call time (not import time) so
    tests can monkeypatch the branch selection.
    """
    if os.name == 'nt':
        # Spawn a detached child running the same interpreter + argv, then exit.
        # The parent exits immediately after spawn; the child re-binds the port on
        # its own (start_api_server.py retries EADDRINUSE during startup).
        #
        # std handles MUST be set explicitly: with no stdin/stdout/stderr given,
        # Popen inherits the parent's console std handles. Under
        # CREATE_NO_WINDOW | DETACHED_PROCESS those inherited handles are
        # invalid/undefined for the child -> it dies or hangs before binding the
        # port (silent, no log line). Redirecting stdout/stderr to a log file
        # also makes bootstrap failures diagnosable instead of silent.
        log_file = None
        try:
            try:
                log_file = open(LOG_FILE, 'a', encoding='utf-8')
            except OSError:
                log_file = None  # e.g. read-only logs dir — fall back to DEVNULL below
            subprocess.Popen(
                [sys.executable, *sys.argv],
                stdin=subprocess.DEVNULL,
                stdout=log_file if log_file is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
                close_fds=True,
                cwd=os.getcwd(),
            )
        finally:
            # Close our reference to the log file. On the failure path (Popen raised)
            # this prevents an fd leak until process exit. On success it's also safe:
            # Popen has already dup'ed the handle into the child before returning, so
            # closing the parent's copy does not affect the child; os._exit(0) follows.
            if log_file is not None:
                log_file.close()
        os._exit(0)
    else:
        # In-place re-exec. os.execl only returns on failure (in which case it has
        # already raised), but belt-and-braces exit if we ever get here anyway.
        os.execl(sys.executable, sys.executable, *sys.argv)
        os._exit(1)
