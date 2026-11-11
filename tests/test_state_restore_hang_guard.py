"""BUG_0295: a never-returning state restore must not pin the orchestrator turn.

The incident (2026-11-11): the autoloader COMPLETED Maine's KV restore server-side in
~26s (llama-autoloader console.log: "slot restore | slot 0 | 101309 tokens | 2.92 GiB"),
but the response flush back to AC never surfaced — no ``Restored state``, no
``State restore failed``, no ``ReadTimeout`` WARNING for 375.8s until the server was
manually restarted. ``state_ops.restore_state`` blocks the orchestrator's turn thread on
a synchronous ``httpx.post`` whose ``timeout`` is PER-OPERATION, not a total-request
ceiling, so a stalled/half-open round-trip never trips ``httpx.ReadTimeout`` and the turn
is pinned forever (RC-1).

What these tests pin (the fix under test — a bounded wall-clock deadline at the single
restore chokepoint):
  - the EXECUTOR deadline (not httpx's per-operation timeout) unblocks the caller within
    ``STATE_HTTP_TIMEOUT + margin`` and returns False (cold-start fallback), proving the
    core guard;
  - a never-returning POST is abandoned in ~that budget, never at an OS/socket timeout;
  - the actionable ``[STATE_RESTORE_TIMEOUT]`` line reaches the log;
  - through the real caller (``restore_instance_state``) a timed-out restore still clears
    ``_state_label`` (no re-fire) so the turn survives;
  - a successful restore logs no ``[STATE_RESTORE_TIMEOUT]`` (negative control — the guard
    must not become noise on the hot reacquire path).

Capturing style mirrors ``tests/test_state_ops_refusal_reason_logging.py``: state_ops logs
through the late-bound ``_AppLoggerProxy``, so assertions attach a handler to the live sink
(``agent_cascade.log.logger``). Tests that intentionally block the POST swap in an isolated
daemon executor so the production module-level pool is never saturated and no wedged worker
outlives the test.
"""

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import agent_cascade.log as log_mod
import agent_cascade.state_ops as state_ops

# Fast, injectable timeouts for the guard so the deadline math is exercised in <1s.
_TIMEOUT = 0.2
_MARGIN = 0.1
_DEADLINE = _TIMEOUT + _MARGIN  # what future.result() waits before abandoning


class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def sink():
    """Records routed through the app log sink (the object the proxy delegates to)."""
    target = log_mod.logger
    handler = _ListHandler()
    prev_level = target.level
    target.addHandler(handler)
    target.setLevel(logging.DEBUG)
    try:
        yield handler.records
    finally:
        target.setLevel(prev_level)
        target.removeHandler(handler)


@pytest.fixture
def fast_guard(monkeypatch):
    """Shrink the injectable restore budget so the deadline fires fast in tests."""
    monkeypatch.setattr(state_ops, 'STATE_HTTP_TIMEOUT', _TIMEOUT)
    monkeypatch.setattr(state_ops, '_RESTORE_TIMEOUT_MARGIN_S', _MARGIN)


@pytest.fixture
def isolated_executor(monkeypatch):
    """Swap in a fresh daemon executor so blocking tests never saturate (or leak onto)
    the production module-level ``_RESTORE_EXECUTOR``."""
    monkeypatch.setattr(
        state_ops, '_RESTORE_EXECUTOR',
        state_ops._DaemonThreadPoolExecutor(max_workers=1, thread_name_prefix='test-restore'),
    )


class _Resp200:
    """Minimal response surface state_ops reads."""
    status_code = 200
    text = '{"tokens": 1}'


def _warnings(records):
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


def _instance(label='Maine'):
    return SimpleNamespace(
        instance_name='Maine',
        _state_lock=threading.Lock(),
        _state_label=label,
        _last_endpoint_config={'api_base': 'http://127.0.0.1:1/v1', 'model': 'model-x'},
    )


def _start_stalling_server():
    """ThreadingHTTPServer whose /state/load begins a chunked 200 then TRICKLES the body.

    Each chunk arrives well within httpx's per-operation read timeout, so
    ``httpx.ReadTimeout`` never fires — but the response body never completes. This is the
    RC-1 signature of BUG_0295: the only thing that can unblock the caller is the executor
    deadline. The server thread and per-connection threads are daemons; the trickle loop
    exits on its own so no wedged handler outlives the test.
    """
    class _StallHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', 0) or 0)
                if length:
                    self.rfile.read(length)
            except Exception:
                pass
            try:
                self.send_response(200)
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                end = time.monotonic() + 1.5  # outlast the executor deadline comfortably
                while time.monotonic() < end:
                    self.wfile.write(b'1\r\nx\r\n')  # one data chunk; never the terminator
                    self.wfile.flush()
                    time.sleep(0.04)  # gap << read timeout, so httpx never times out
            except Exception:
                pass

        def log_message(self, *args):
            pass

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server(('127.0.0.1', 0), _StallHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# ────────────────────────────────────────────────────────────────────────────
# 1. Deterministic repro — real server, response trickled, executor beats httpx
# ────────────────────────────────────────────────────────────────────────────

def test_deterministic_repro_returns_false_within_budget(sink, fast_guard, isolated_executor):
    """A server that completes-but-suppresses the body must not hang the caller.

    The response never finishes, so httpx's read timeout never trips; only the executor
    deadline can return — proving the guard unblocks the caller on a real socket.
    """
    server, port = _start_stalling_server()
    url = f'http://127.0.0.1:{port}/v1'
    try:
        started = time.monotonic()
        result = state_ops.restore_state(url, 'model-x', 'Maine')
        elapsed = time.monotonic() - started

        assert result is False, 'timed-out restore must fall back to cold start'
        # Lower bound: it waited for the deadline (not an immediate error). Upper bound:
        # far below any OS/socket timeout (which would be many seconds).
        assert _DEADLINE * 0.8 <= elapsed < 1.5, f'elapsed {elapsed:.3f}s — expected ≈{_DEADLINE}s'
        msgs = _warnings(sink)
        assert any('[STATE_RESTORE_TIMEOUT]' in m for m in msgs), msgs
        assert any('label=Maine' in m for m in msgs), msgs
    finally:
        server.shutdown()
        server.server_close()


# ────────────────────────────────────────────────────────────────────────────
# 2. Belt-and-suspenders — the EXECUTOR deadline is the only unblocker
# ────────────────────────────────────────────────────────────────────────────

def test_executor_deadline_unblocks_when_post_never_returns(sink, fast_guard, isolated_executor, monkeypatch):
    """Patch httpx.post to block forever: nothing in httpx can unblock the caller, so a
    return within the deadline PROVES it is the executor deadline doing the work."""
    gate = threading.Event()

    def _hang(*args, **kwargs):
        gate.wait()  # only released in teardown, after the caller has already abandoned
        return _Resp200()

    monkeypatch.setattr(state_ops.httpx, 'post', _hang)

    started = time.monotonic()
    try:
        assert state_ops.restore_state('http://127.0.0.1:1/v1', 'model-x', 'Maine') is False
    finally:
        elapsed = time.monotonic() - started
        gate.set()  # release the blocked worker so the daemon thread exits cleanly

    # NOT ~0: the caller waited for the executor deadline, not for httpx (which never
    # returns). NOT many seconds: abandoned at the budget, not at a socket timeout.
    assert _DEADLINE * 0.8 <= elapsed < 1.5, f'elapsed {elapsed:.3f}s — expected ≈{_DEADLINE}s'
    msgs = _warnings(sink)
    assert any('[STATE_RESTORE_TIMEOUT]' in m for m in msgs), msgs
    assert any('label=Maine' in m for m in msgs), msgs


# ────────────────────────────────────────────────────────────────────────────
# 3. Turn-survival regression at the real caller boundary
# ────────────────────────────────────────────────────────────────────────────

def test_turn_survives_timed_out_restore_and_clears_label(sink, fast_guard, isolated_executor, monkeypatch):
    """Drive the chokepoint caller with a timed-out restore: the caller must not hang, the
    pending label must be cleared (no re-fire), and the fallback must be logged."""
    gate = threading.Event()

    def _hang(*args, **kwargs):
        gate.wait()
        return _Resp200()

    monkeypatch.setattr(state_ops.httpx, 'post', _hang)
    inst = _instance('Maine')

    started = time.monotonic()
    try:
        assert state_ops.restore_instance_state(inst) is False
    finally:
        elapsed = time.monotonic() - started
        gate.set()

    assert _DEADLINE * 0.8 <= elapsed < 1.5, f'caller turn pinned for {elapsed:.3f}s'
    # Cleared label ⇒ the next turn cold-starts instead of re-firing the same hung restore.
    assert inst._state_label is None, '_state_label must be cleared on failed restore (no re-fire)'
    msgs = _warnings(sink)
    assert any('[STATE_RESTORE_TIMEOUT]' in m for m in msgs), msgs
    assert any('cleared label' in m for m in msgs), msgs


# ────────────────────────────────────────────────────────────────────────────
# 4. Negative control — the guard must not become noise on the happy path
# ────────────────────────────────────────────────────────────────────────────

def test_successful_restore_logs_no_timeout_warning(sink, isolated_executor, monkeypatch):
    """A normal restore returns True and emits no [STATE_RESTORE_TIMEOUT] (hot reacquire path)."""
    monkeypatch.setattr(state_ops.httpx, 'post', lambda *a, **k: _Resp200())

    assert state_ops.restore_state('http://127.0.0.1:1/v1', 'model-x', 'Maine') is True
    assert not any('[STATE_RESTORE_TIMEOUT]' in m for m in _warnings(sink))


def test_successful_instance_restore_clears_label_without_timeout(sink, isolated_executor, monkeypatch):
    """Full caller path on success: label consumed, state reported restored, no timeout noise."""
    monkeypatch.setattr(state_ops.httpx, 'post', lambda *a, **k: _Resp200())
    inst = _instance('Maine')

    assert state_ops.restore_instance_state(inst) is True
    assert inst._state_label is None, 'successful restore must consume the label'
    assert any('Restored state' in m for m in [r.getMessage() for r in sink]), sink
    assert not any('[STATE_RESTORE_TIMEOUT]' in m for m in _warnings(sink))
