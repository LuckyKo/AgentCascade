"""Tests for the shared server-restart helper and the port-race bind retry.

Covers plan §6 (server-restart-dedup-fix-plan.md):
- A. ``agent_cascade.server_restart.restart_server_process`` branch selection & safety.
- B. ``start_api_server._bind_socket_with_retry`` hermetic retry behavior.
- C. CHECKPOINT 1: uvicorn ``server.run(sockets=[pre_bound_sock])`` starts cleanly
  alongside a custom (no-op) signal handler — no double-bind, no error.

Run with: python -m pytest tests/test_server_restart.py -v
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure top-level imports work (mirror tests/test_api_endpoints.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))


def _free_port() -> int:
    """Ask the OS for a free port (close it immediately; small race window is fine here)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# A. Shared restart helper — branch selection & safety
# ---------------------------------------------------------------------------

def test_restart_posix_uses_execl(monkeypatch):
    """os.name='posix': re-execs in place via os.execl; Popen is NOT used."""
    from agent_cascade import server_restart

    monkeypatch.setattr(server_restart.os, 'name', 'posix')
    with patch.object(server_restart.os, 'execl') as mock_execl, \
         patch.object(server_restart.subprocess, 'Popen') as mock_popen, \
         patch.object(server_restart.os, '_exit'):  # patched: execl "returns" without killing the test
        server_restart.restart_server_process()

    mock_execl.assert_called_once_with(sys.executable, sys.executable, *sys.argv)
    mock_popen.assert_not_called()


def test_restart_windows_spawns_detached_then_exits(monkeypatch):
    """os.name='nt': spawns a detached Popen with the same argv and explicit std
    handles (stdin=DEVNULL, stdout->log file, stderr=STDOUT), then os._exit(0)."""
    from agent_cascade import server_restart

    monkeypatch.setattr(server_restart.os, 'name', 'nt')
    with patch.object(server_restart.subprocess, 'Popen') as mock_popen, \
         patch.object(server_restart.os, '_exit') as mock_exit:
        server_restart.restart_server_process()

    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    assert args[0] == [sys.executable, *sys.argv]
    flags = kwargs['creationflags']
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NO_WINDOW
    assert kwargs['close_fds'] is True
    # Explicit std handles: a detached no-window child must NOT inherit the
    # parent's (invalid) console std handles.
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['stderr'] == subprocess.STDOUT
    stdout = kwargs['stdout']
    assert stdout != subprocess.DEVNULL
    assert hasattr(stdout, 'write'), 'stdout must be an open file object'
    # The log path is anchored to the project root (not cwd) — matches LOG_FILE.
    assert Path(stdout.name).resolve() == server_restart.LOG_FILE.resolve()
    mock_exit.assert_called_once_with(0)


def test_restart_windows_stdout_falls_back_to_devnull_when_log_unopenable(monkeypatch):
    """os.name='nt' + log file open raising OSError: stdout falls back to DEVNULL,
    spawn still proceeds and os._exit(0) is still called.

    The real logs/ dir always exists (path is anchored to the project root), so we
    force the failure deterministically by making builtins.open raise OSError."""
    from agent_cascade import server_restart

    monkeypatch.setattr(server_restart.os, 'name', 'nt')

    def _raise(*a, **k):
        raise OSError('simulated: cannot open log file')

    with patch.object(server_restart.subprocess, 'Popen') as mock_popen, \
         patch.object(server_restart.os, '_exit') as mock_exit, \
         patch('builtins.open', side_effect=_raise):
        server_restart.restart_server_process()

    mock_popen.assert_called_once()
    args, kwargs = mock_popen.call_args
    assert args[0] == [sys.executable, *sys.argv]
    assert kwargs['stdin'] == subprocess.DEVNULL
    assert kwargs['stdout'] == subprocess.DEVNULL  # fallback
    assert kwargs['stderr'] == subprocess.STDOUT
    flags = kwargs['creationflags']
    assert flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_NO_WINDOW
    mock_exit.assert_called_once_with(0)


def test_restart_windows_no_exit_if_spawn_fails(monkeypatch):
    """os.name='nt' + Popen raising: os._exit must NOT be called (server stays alive)
    AND the opened log file handle must be closed (no fd leak on the failure path)."""
    from agent_cascade import server_restart

    monkeypatch.setattr(server_restart.os, 'name', 'nt')
    with patch.object(server_restart.subprocess, 'Popen', side_effect=OSError('spawn failed')), \
         patch.object(server_restart.os, '_exit') as mock_exit, \
         patch('builtins.open') as mock_open:
        mock_fh = mock_open.return_value  # the "opened" log file object
        with pytest.raises(OSError, match='spawn failed'):
            server_restart.restart_server_process()

    mock_exit.assert_not_called()
    mock_fh.close.assert_called_once_with()  # handle released despite the spawn failure


# ---------------------------------------------------------------------------
# B. Port-race retry — _bind_socket_with_retry (hermetic, small budget)
# ---------------------------------------------------------------------------

def _eaddrinuse_error():
    """Build an EADDRINUSE OSError matching the platform's real errno (98 POSIX / 10048 Windows)."""
    return OSError(10048 if os.name == 'nt' else 98, '[Errno] Address already in use')


def test_bind_retry_succeeds_after_transient_holder():
    """A holder releases the port mid-retry; the helper returns a bound socket on attempt >1.

    The "holder" is simulated by making bind fail with EADDRINUSE for the first N attempts,
    then succeeding — hermetic (no real second socket needed) and deterministic.
    """
    from start_api_server import _bind_socket_with_retry

    port = _free_port()
    attempts = {'n': 0}

    class _FlakySocket(socket.socket):
        def bind(self, addr):
            attempts['n'] += 1
            if attempts['n'] <= 2:  # first two attempts: port "held"
                raise _eaddrinuse_error()
            return super().bind(addr)

    start = time.monotonic()
    with patch('socket.socket', _FlakySocket):
        sock = _bind_socket_with_retry('127.0.0.1', port, max_attempts=5, delay=0.05)
    elapsed = time.monotonic() - start

    assert sock.getsockname()[1] == port
    assert attempts['n'] == 3  # succeeded on the third attempt
    # Two retry delays (2 x 0.05s) — proves retries actually happened. Loose lower bound:
    # time.sleep can return slightly early on Windows (timer resolution).
    assert elapsed >= 0.09
    sock.close()


def test_bind_retry_gives_up_when_genuinely_held():
    """Holder never releases: a clear RuntimeError is raised after exhausting the budget."""
    from start_api_server import _bind_socket_with_retry

    attempts = {'n': 0}

    class _HeldSocket(socket.socket):
        def bind(self, addr):
            attempts['n'] += 1
            raise _eaddrinuse_error()

    with patch('socket.socket', _HeldSocket):
        with pytest.raises(RuntimeError, match='still in use after 3 attempts'):
            _bind_socket_with_retry('127.0.0.1', 49999, max_attempts=3, delay=0.05)

    assert attempts['n'] == 3  # exactly the budget — no extra attempts


def test_bind_retry_sets_reuseaddr():
    """SO_REUSEADDR is set to 1 on every attempt's socket BEFORE bind."""
    from start_api_server import _bind_socket_with_retry

    calls = []

    class _RecordingSocket(socket.socket):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.setsockopt_calls = []
            calls.append(self)

        def setsockopt(self, level, optname, value):
            self.setsockopt_calls.append((level, optname, value))
            return super().setsockopt(level, optname, value)

    port = _free_port()
    with patch('socket.socket', _RecordingSocket):
        sock = _bind_socket_with_retry('127.0.0.1', port, max_attempts=3, delay=0.05)
    sock.close()

    assert len(calls) == 1  # bound on the first attempt (port was free)
    assert (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1) in calls[0].setsockopt_calls


def test_bind_retry_reraises_non_eaddrinuse_immediately():
    """A non-EADDRINUSE OSError is re-raised on the first attempt (no retry)."""
    from start_api_server import _bind_socket_with_retry

    attempts = {'n': 0}

    class _BadSocket(socket.socket):
        def bind(self, addr):
            attempts['n'] += 1
            raise PermissionError(13, 'permission denied')  # not EADDRINUSE — no retry

    with patch('socket.socket', _BadSocket):
        with pytest.raises(PermissionError, match='permission denied'):
            _bind_socket_with_retry('127.0.0.1', 49998, max_attempts=5, delay=0.05)

    assert attempts['n'] == 1  # re-raised immediately, no retry loop


# ---------------------------------------------------------------------------
# C. CHECKPOINT 1 — pre-bound socket + custom signal handler integration
# ---------------------------------------------------------------------------

def test_uvicorn_serves_prebound_socket_with_custom_signal_handler():
    """CHECKPOINT 1: uvicorn Server.run(sockets=[pre_bound]) starts cleanly with our
    no-op install_signal_handlers (i.e. the setup_signal_handler + pre-bound socket
    combination in start_api_server.py works — no double-bind, no error).

    Runs a real uvicorn server on a thread against a minimal ASGI app; proves an HTTP
    round-trip succeeds and that should_exit cleanly stops it (same flag our signal
    handler sets).
    """
    import httpx
    import uvicorn

    async def app(scope, receive, send):
        if scope['type'] == 'http':
            body = b'{"status": "ok"}'
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-type', b'application/json')]})
            await send({'type': 'http.response.body', 'body': body})

    port = _free_port()
    # Pre-bind exactly like start_api_server.py does (SO_REUSEADDR + listen).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('127.0.0.1', port))
    sock.listen(5)

    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning')
    server = uvicorn.Server(config)
    # Mirror start_api_server.py: custom signal handler already installed; suppress uvicorn's.
    server.install_signal_handlers = lambda: None

    errors = []

    def run_server():
        try:
            server.run(sockets=[sock])
        except BaseException as e:  # noqa: BLE001 - report any escape (incl. SystemExit)
            errors.append(e)

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    base_url = f'http://127.0.0.1:{port}'
    try:
        # Wait for the server to come up (bounded).
        deadline = time.monotonic() + 10
        last_err = None
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(base_url + '/ping', timeout=1)
                assert resp.status_code == 200
                assert resp.json() == {'status': 'ok'}
                break
            except Exception as e:  # noqa: BLE001 - server not up yet
                last_err = e
                time.sleep(0.1)
        else:
            pytest.fail(f'Server did not come up on pre-bound socket: {last_err}; errors={errors}')

        assert not errors, f'server.run(sockets=[sock]) raised: {errors}'

        # Clean stop via the same flag our shared signal handler sets.
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), 'server did not stop on should_exit'
    finally:
        if thread.is_alive():
            server.should_exit = True
            thread.join(timeout=5)
        sock.close()
