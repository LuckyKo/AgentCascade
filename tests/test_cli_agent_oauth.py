"""Tests for the interactive OAuth login manager + its WebSocket surface (BUG_0307).

Everything Docker/CLI-facing is faked: ``subprocess.Popen`` yields a scripted stdout stream and
records the code written to stdin; ``webbrowser.open`` and ``docker kill`` are recorded. No
container is ever spawned and no real browser is opened.
"""

import asyncio
import os
import sys
import threading
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = os.environ.get('AGENTCASCADE_REPO', r'N:\work\WD\AgentCascade')
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent_cascade.engine import cli_agent_oauth as oauth_mod  # noqa: E402
from agent_cascade.engine import cli_agent_runner as runner  # noqa: E402
import agent_cascade.engine.cli_agent_oauth as mod  # noqa: E402
from agent_cascade.ws_handlers import WsMessageHandler  # noqa: E402

_AUTH_URL = 'https://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=abc'
_URL_LINE = f'  {_AUTH_URL}'


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeStdin:
    """Records the pasted code; releasing the reader simulates the CLI consuming stdin."""

    def __init__(self, proc):
        self.proc = proc
        self.data = ''

    def write(self, s):
        self.data += s
        self.proc.code_seen = True
        self.proc.release_output.set()

    def flush(self):
        return None


class FakeProc:
    """Minimal ``subprocess.Popen`` stand-in with a scripted stdout stream.

    The reader thread iterates ``lines`` then blocks on ``release_output`` — so a session stays
    ``waiting`` until the test (or a stdin write) ends it. ``wait`` then resolves the exit code
    from whether a code was submitted.
    """

    def __init__(self, lines, *, rc_with_code=0, rc_no_code=1, auto_release=False):
        self.lines = list(lines)
        self.rc_with_code = rc_with_code
        self.rc_no_code = rc_no_code
        self.code_seen = False
        self.returncode = None
        self.release_output = threading.Event()
        self.stdin = FakeStdin(self)
        self.stdout = self._stream()
        self.killed = False
        if auto_release:
            self.release_output.set()

    def _stream(self):
        for line in self.lines:
            yield line
        self.release_output.wait(5)

    def wait(self, timeout=None):
        self.returncode = self.rc_with_code if self.code_seen else self.rc_no_code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.release_output.set()


def _patch_popen(monkeypatch, lines, **kw):
    created = []

    def factory(cmd, **kwargs):
        proc = FakeProc(lines, **kw)
        proc.cmd = list(cmd)
        proc.kwargs = kwargs
        created.append(proc)
        _ALL_PROCS.append(proc)
        return proc

    monkeypatch.setattr(mod.subprocess, 'Popen', factory)
    return created


_ALL_PROCS: list = []


@pytest.fixture(autouse=True)
def _release_fake_procs():
    """Unblock every scripted reader thread so no test leaks a waiting daemon."""
    yield
    for proc in _ALL_PROCS:
        proc.release_output.set()
    _ALL_PROCS.clear()


def _patch_stack(monkeypatch):
    """Isolate the manager: record browser opens and docker-kill calls, clear sessions."""
    opened, killed = [], []
    monkeypatch.setattr(mod.webbrowser, 'open', lambda url: opened.append(url))
    monkeypatch.setattr(mod.subprocess, 'run',
                        lambda cmd, **kw: killed.append(list(cmd)) or MagicMock(returncode=0))
    monkeypatch.setattr(mod, '_SESSIONS', {})
    monkeypatch.setattr(runner, '_AGY_AUTH_CACHE', {})
    return opened, killed


def _manager(**kw):
    return oauth_mod.CliAgentOAuthManager(open_browser=True, **kw)


# ── manager ──────────────────────────────────────────────────────────────────


def test_start_session_captures_url_and_opens_browser(monkeypatch):
    opened, _ = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE, 'Waiting...'],
                 rc_no_code=1)
    mgr = _manager()
    sid, url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    assert url == _AUTH_URL
    assert opened == [_AUTH_URL]
    st = mgr.session_state(sid)
    assert st['harness'] == 'antigravity'
    assert st['auth_url'] == _AUTH_URL
    assert st['state'] in (oauth_mod.STATE_WAITING, oauth_mod.STATE_EXPIRED)


def test_submit_code_success_writes_stdin_and_invalidates_cache(monkeypatch):
    opened, _ = _patch_stack(monkeypatch)
    created = _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE], rc_with_code=0)
    runner._AGY_AUTH_CACHE[('vol-1', 'cli_agent_agy')] = (0.0, False)
    mgr = _manager()
    sid, _url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')

    assert mgr.submit_code(sid, '  4/0AX4XfWh_code  \n') is True
    assert created[0].stdin.data == '4/0AX4XfWh_code\n'  # stripped + newline
    assert mgr.session_state(sid)['state'] == oauth_mod.STATE_SUCCESS
    assert runner._AGY_AUTH_CACHE == {}  # status re-probes after a login


def test_expired_window_is_retryable(monkeypatch):
    _, _ = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE], rc_no_code=1)
    mgr = _manager()
    sid, _url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    session = mod._SESSIONS[sid]
    session.proc.release_output.set()  # simulate agy's 60 s window closing by itself
    assert session.done_event.wait(5)
    st = mgr.session_state(sid)
    assert st['state'] == oauth_mod.STATE_EXPIRED
    assert st['retryable'] is True
    # A code arriving after the window is refused (not a silent hang).
    assert mgr.submit_code(sid, 'late-code') is False


def test_broken_pipe_reports_expired(monkeypatch):
    _, _ = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE])
    mgr = _manager()
    sid, _url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    session = mod._SESSIONS[sid]

    def boom(_s):
        raise BrokenPipeError('closed')

    session.proc.stdin.write = boom
    assert mgr.submit_code(sid, 'code') is False
    assert mgr.session_state(sid)['state'] == oauth_mod.STATE_EXPIRED


def test_already_authenticated_volume_exits_zero_without_url(monkeypatch):
    opened, _ = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['ok'], rc_no_code=0, auto_release=True)
    mgr = _manager(url_wait=2)
    sid, url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    assert url is None
    assert opened == []
    assert mod._SESSIONS[sid].done_event.wait(5)
    assert mgr.session_state(sid)['state'] == oauth_mod.STATE_SUCCESS


def test_cancel_session_kills_container(monkeypatch):
    _, killed = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE])
    mgr = _manager()
    sid, _url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    mgr.cancel_session(sid)
    assert mgr.session_state(sid)['state'] == oauth_mod.STATE_CANCELLED
    assert any(c[:2] == ['docker', 'kill'] for c in killed)
    assert mod._SESSIONS[sid].proc.killed is True


def test_reaper_drops_sessions_past_ttl(monkeypatch):
    _, killed = _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE])
    mgr = _manager(session_ttl=0)  # everything is immediately past its TTL
    sid, _url = mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    mgr._reap()
    assert sid not in mod._SESSIONS
    assert any(c[:2] == ['docker', 'kill'] for c in killed)


def test_start_session_builds_volume_mount_and_adapter_argv(monkeypatch):
    _, _ = _patch_stack(monkeypatch)
    created = _patch_popen(monkeypatch, ['Authentication required...', _URL_LINE])
    mgr = _manager()
    mgr.start_session(harness='antigravity', volume='vol-1', image='cli_agent_agy')
    cmd = created[0].cmd
    assert 'vol-1:/root/.gemini' in cmd
    assert cmd[cmd.index('cli_agent_agy') + 1] == 'agy'  # adapter argv, not a hardcoded binary
    assert any(str(x).startswith('-p=') for x in cmd)
    assert created[0].kwargs['stdin'] is mod.subprocess.PIPE


def test_start_session_rejects_harness_without_volume_target(monkeypatch):
    _patch_stack(monkeypatch)
    _patch_popen(monkeypatch, [])
    mgr = _manager()
    with pytest.raises(ValueError):
        mgr.start_session(harness='claude_code', volume='v', image='img')


# ── WebSocket handlers ───────────────────────────────────────────────────────


class FakeManager:
    def __init__(self, url=_AUTH_URL, state='waiting', submit_ok=True):
        self.url = url
        self.state = state
        self.submit_ok = submit_ok
        self.calls = []

    def start_session(self, *, harness, volume, image):
        self.calls.append(('start', harness, volume, image))
        return 'sid-1', self.url

    def session_state(self, session_id):
        return {'session_id': session_id, 'harness': 'antigravity', 'state': self.state,
                'auth_url': self.url, 'error': None,
                'retryable': self.state == oauth_mod.STATE_EXPIRED}

    def submit_code(self, session_id, code):
        self.calls.append(('submit', session_id, code))
        if self.submit_ok:
            self.state = oauth_mod.STATE_SUCCESS
        return self.submit_ok

    def cancel_session(self, session_id):
        self.calls.append(('cancel', session_id))


def _ws(**settings):
    obj = WsMessageHandler.__new__(WsMessageHandler)
    cfg = dict(cli_agent_enabled={'antigravity': True}, cli_agent_images={},
               cli_agent_models={}, cli_agent_paths_ro=False, max_cli_agent_children=2,
               cli_agent_agy_auth_volume='vol-1')
    cfg.update(settings)
    obj.agent_pool = NS(settings=NS(**cfg))
    obj.broadcast_fn = AsyncMock()
    obj._broadcast = AsyncMock()
    return obj


def _use_manager(monkeypatch, mgr):
    monkeypatch.setattr(oauth_mod, 'get_oauth_manager', lambda: mgr)
    monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: 'cli_agent_agy')


def _events(ws):
    return [c.args[0] for c in ws.broadcast_fn.await_args_list]


def test_ws_start_emits_url(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_start(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_url'
    assert ev['url'] == _AUTH_URL and ev['session_id'] == 'sid-1'
    assert mgr.calls == [('start', 'antigravity', 'vol-1', 'cli_agent_agy')]


def test_ws_start_already_authenticated(monkeypatch):
    mgr = FakeManager(url=None, state=oauth_mod.STATE_SUCCESS)
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_start(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_success' and ev['already_authenticated'] is True


def test_ws_start_rejects_api_key_harness(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_start(ws, {'harness': 'claude_code'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_failed' and 'API key' in ev['reason']
    assert mgr.calls == []  # never spawns a login container


def test_ws_start_without_volume_fails_actionably(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws(cli_agent_agy_auth_volume='0')
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_start(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_failed'
    assert 'volume' in ev['reason'].lower()
    assert mgr.calls == []


def test_ws_submit_success_broadcasts(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_submit(
        ws, {'session_id': 'sid-1', 'code': 'abc'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_success' and ev['harness'] == 'antigravity'
    assert mgr.calls == [('submit', 'sid-1', 'abc')]
    ws._broadcast.assert_awaited()  # status snapshot re-renders


def test_ws_submit_expired_is_retryable(monkeypatch):
    mgr = FakeManager(state=oauth_mod.STATE_EXPIRED, submit_ok=False)
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_submit(
        ws, {'session_id': 'sid-1', 'code': 'abc'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_failed' and ev['retryable'] is True


def test_ws_cancel_emits_cancelled(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_cancel(ws, {'session_id': 'sid-1'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_cancelled'
    assert mgr.calls == [('cancel', 'sid-1')]


def test_ws_dispatch_registers_oauth_types():
    ws = _ws()
    table = ws._dispatch_table
    for msg_type in ('cli_agent_oauth_start', 'cli_agent_oauth_submit', 'cli_agent_oauth_cancel'):
        assert msg_type in table
