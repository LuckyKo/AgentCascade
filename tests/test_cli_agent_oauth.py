"""Tests for the manual OAuth login command + auth re-check.

agy's OAuth URL is TTY-bound, so the interactive ``start_session`` flow was replaced by a copyable
manual ``docker run -it`` command + a "Re-check auth" recheck. Everything Docker-facing is faked:
the image resolver and the volume probe are patched, so no container is ever spawned.
"""

import asyncio
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

_REPO = os.environ.get('AGENTCASCADE_REPO', r'N:\work\WD\AgentCascade')
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from agent_cascade.engine import cli_agent_oauth as oauth_mod  # noqa: E402
from agent_cascade.engine import cli_agent_runner as runner  # noqa: E402
from agent_cascade.engine.cli_agent_adapters import get_adapter  # noqa: E402
from agent_cascade.ws_handlers import WsMessageHandler  # noqa: E402


# ── manager ──────────────────────────────────────────────────────────────────


def _pool(volume='vol-1'):
    return NS(settings=NS(cli_agent_agy_auth_volume=volume))


def _stub_image(monkeypatch, tag='cli_agent_agy'):
    monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: tag)


def test_build_interactive_cmd_is_just_the_binary():
    # The interactive form is the bare binary — NOT the headless build_launch_cmd argv (which
    # always appends -p). Inherited from InteractiveLoginAdapter.
    assert get_adapter('antigravity').build_interactive_cmd() == ['agy']


def test_build_manual_command_is_the_exact_tty_command(monkeypatch):
    _stub_image(monkeypatch)
    mgr = oauth_mod.CliAgentOAuthManager()
    cmd = mgr.build_manual_command('antigravity', pool=_pool('vol-1'))
    assert cmd == 'docker run -it -v vol-1:/root/.gemini cli_agent_agy agy'
    assert '-it' in cmd
    assert ' -p' not in cmd  # never the headless one-shot flag


def test_build_manual_command_no_volume_raises(monkeypatch):
    _stub_image(monkeypatch)
    mgr = oauth_mod.CliAgentOAuthManager()
    # claude_code is an api-key harness (no oauth volume/target) -> actionable ValueError.
    with pytest.raises(ValueError):
        mgr.build_manual_command('claude_code', pool=_pool())
    # agy with the volume knob OFF is also a ValueError.
    with pytest.raises(ValueError):
        mgr.build_manual_command('antigravity', pool=_pool('0'))


def test_recheck_auth_invalidates_then_returns_populated(monkeypatch):
    _stub_image(monkeypatch)
    runner._AGY_AUTH_CACHE = {('vol-1', 'cli_agent_agy'): (0.0, False)}  # stale cached negative
    monkeypatch.setattr(runner, '_agy_auth_volume_populated',
                        lambda v, img, target='x': True)
    mgr = oauth_mod.CliAgentOAuthManager()
    assert mgr.recheck_auth('antigravity', pool=_pool('vol-1')) is True
    # _invalidate_agy_auth_cache ran before the (patched) probe, so the stale entry is gone.
    assert not [k for k in runner._AGY_AUTH_CACHE if k[0] == 'vol-1']


def test_recheck_auth_indeterminate_is_false(monkeypatch):
    _stub_image(monkeypatch)
    monkeypatch.setattr(runner, '_agy_auth_volume_populated',
                        lambda v, img, target='x': None)  # docker unavailable
    mgr = oauth_mod.CliAgentOAuthManager()
    assert mgr.recheck_auth('antigravity', pool=_pool('vol-1')) is False


def test_recheck_auth_no_volume_returns_false(monkeypatch):
    _stub_image(monkeypatch)
    called = []
    monkeypatch.setattr(runner, '_agy_auth_volume_populated',
                        lambda v, img, target='x': called.append(v) or True)
    mgr = oauth_mod.CliAgentOAuthManager()
    assert mgr.recheck_auth('antigravity', pool=_pool('0')) is False
    assert called == []  # no probe without a volume


def test_removed_oauth_session_settings_are_gone():
    import agent_cascade.settings as s
    for name in ('CLI_AGENT_OAUTH_URL_WAIT_SECONDS', 'CLI_AGENT_OAUTH_SUBMIT_WAIT_SECONDS',
                 'CLI_AGENT_OAUTH_SESSION_TTL_SECONDS', 'CLI_AGENT_OAUTH_WINDOW_SECONDS'):
        assert not hasattr(s, name), name


# ── WebSocket handlers ───────────────────────────────────────────────────────


class FakeManager:
    """Stand-in for CliAgentOAuthManager; records calls and returns canned results."""

    def __init__(self, command='docker run -it -v vol-1:/root/.gemini cli_agent_agy agy',
                 authenticated=True):
        self.command = command
        self.authenticated = authenticated
        self.calls = []

    def build_manual_command(self, harness, *, pool=None):
        self.calls.append(('manual', harness, pool))
        if isinstance(self.command, Exception):
            raise self.command
        return self.command

    def recheck_auth(self, harness, *, pool=None):
        self.calls.append(('recheck', harness, pool))
        if isinstance(self.authenticated, Exception):
            raise self.authenticated
        return self.authenticated


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


def _events(ws):
    return [c.args[0] for c in ws.broadcast_fn.await_args_list]


def test_ws_manual_cmd_emits_command(monkeypatch):
    mgr = FakeManager()
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_manual_cmd(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_manual_cmd'
    assert ev['harness'] == 'antigravity'
    assert ev['command'] == mgr.command
    assert mgr.calls == [('manual', 'antigravity', ws.agent_pool)]


def test_ws_manual_cmd_failure_emits_failed(monkeypatch):
    mgr = FakeManager(command=ValueError('no OAuth credential volume'))
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_manual_cmd(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_failed'
    assert 'volume' in ev['reason']


def test_ws_recheck_emits_status(monkeypatch):
    mgr = FakeManager(authenticated=True)
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_recheck(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_status'
    assert ev['harness'] == 'antigravity'
    assert ev['authenticated'] is True
    assert mgr.calls == [('recheck', 'antigravity', ws.agent_pool)]


def test_ws_recheck_failure_degrades_to_false(monkeypatch):
    mgr = FakeManager(authenticated=RuntimeError('docker unavailable'))
    _use_manager(monkeypatch, mgr)
    ws = _ws()
    asyncio.run(WsMessageHandler.handle_cli_agent_oauth_recheck(ws, {'harness': 'antigravity'}))
    (ev,) = _events(ws)
    assert ev['type'] == 'cli_agent_oauth_status'
    assert ev['authenticated'] is False  # no crash, fail toward not-authenticated


def test_ws_dispatch_registers_oauth_types():
    ws = _ws()
    table = ws._dispatch_table
    assert 'cli_agent_oauth_manual_cmd' in table
    assert 'cli_agent_oauth_recheck' in table
    for gone in ('cli_agent_oauth_start', 'cli_agent_oauth_submit', 'cli_agent_oauth_cancel'):
        assert gone not in table
