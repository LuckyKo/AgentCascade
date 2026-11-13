"""Tests for call_cli_agent auto-setup + introspection (Pass A).

Covers the additive surface that Phase-0's test_call_cli_agent.py does not touch:
  * config handlers for the 4 new pool keys (type-coerced, non-conforming dropped);
  * registry invariants for the 3 dict-valued keys (seeded + typed) + pool-key membership;
  * secrets_loader.set_secret (writer, atomic, cache-refresh, value never logged);
  * runner docker -e key injection + :ro extra-path mounts (and default-off argv byte-safety);
  * image auto-build (build-once + memoize, fail-open);
  * cli_agent_status shape (antigravity key=None, quota='unknown');
  * system_info._render_cli_agents wiring;
  * schema drift guard (required=[], status/model present, enum still matches registry);
  * ws handle_set_cli_agent_key (writes for API-key harnesses, no-ops for antigravity/unknown).

No Docker, no real CLI binary, no production secrets.json is ever touched.
"""

import asyncio
import json
import os
import sys

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

_REPO = os.environ.get('AGENTCASCADE_REPO', r'N:\work\WD\AgentCascade')
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import agent_cascade.engine.cli_agent_runner as runner  # noqa: E402
import agent_cascade.settings_registry as registry  # noqa: E402
from agent_cascade.config_handlers import (  # noqa: E402
    _handle_cli_agent_enabled, _handle_cli_agent_images, _handle_cli_agent_models,
    _handle_cli_agent_paths_ro)
from agent_cascade.prompts import dna  # noqa: E402
from agent_cascade.tools.custom.system_info import SystemInfo  # noqa: E402
from agent_cascade.ws_handlers import WsMessageHandler  # noqa: E402

_DICT_KEYS = ('cli_agent_enabled', 'cli_agent_images', 'cli_agent_models')


# ── config handlers ──────────────────────────────────────────────────────────


def _settings_pool():
    return NS(settings=NS(cli_agent_enabled={}, cli_agent_images={},
                          cli_agent_models={}, cli_agent_paths_ro=False))


class TestConfigHandlers:
    def test_enabled_drops_nonbool(self):
        pool = _settings_pool()
        _handle_cli_agent_enabled({'cli_agent_enabled': {'claude_code': True, 'bad': 'x'}}, pool, [])
        assert pool.settings.cli_agent_enabled == {'claude_code': True}

    def test_images_drops_nonstr(self):
        pool = _settings_pool()
        _handle_cli_agent_images({'cli_agent_images': {'opencode': 't:1', 'x': 5}}, pool, [])
        assert pool.settings.cli_agent_images == {'opencode': 't:1'}

    def test_models_full_map(self):
        pool = _settings_pool()
        _handle_cli_agent_models({'cli_agent_models': {'antigravity': 'm-x', 'y': None}}, pool, [])
        assert pool.settings.cli_agent_models == {'antigravity': 'm-x'}

    def test_paths_ro_bool(self):
        pool = _settings_pool()
        _handle_cli_agent_paths_ro({'cli_agent_paths_ro': True}, pool, [])
        assert pool.settings.cli_agent_paths_ro is True


# ── registry invariants ──────────────────────────────────────────────────────


class TestRegistryInvariants:
    def test_dict_keys_seeded_and_typed(self):
        for k in _DICT_KEYS:
            assert k in registry._SEED_DEFAULTS and registry._SEED_DEFAULTS[k] == {}, k
            assert registry._PY_TYPES[k] is dict, k

    def test_all_four_in_pool_keys(self):
        for k in (*_DICT_KEYS, 'cli_agent_paths_ro'):
            assert k in registry._POOL_KEYS, k


# ── secrets writer ───────────────────────────────────────────────────────────


def test_set_secret_writes_and_preserves(tmp_path, monkeypatch):
    import config.secrets_loader as sl
    # Redirect the write target to tmp + start from a clean in-memory cache so _load_secrets
    # is served from cache (never reads production config/secrets.json).
    monkeypatch.setattr(sl, '__file__', str(tmp_path / 'secrets_loader.py'))
    monkeypatch.setattr(sl, '_SECRETS_LOADED', True, raising=False)
    monkeypatch.setattr(sl, '_SECRETS_CACHE', {}, raising=False)

    sl.set_secret('cli_claude_code_key', 'k1')
    assert sl.get_secret('cli_claude_code_key') == 'k1'
    # Second write must preserve the first (single-writer merge, atomic replace).
    sl.set_secret('cli_opencode_key', 'k2')
    data = json.loads((tmp_path / 'secrets.json').read_text(encoding='utf-8'))
    assert data == {'cli_claude_code_key': 'k1', 'cli_opencode_key': 'k2'}


# ── runner docker cmd: -e keys + :ro mounts ──────────────────────────────────


class TestDockerCmd:
    def test_env_and_ro_injected(self):
        cmd = runner._build_docker_cmd(
            container_name='c', workspace='.', harness_cmd=['claude'], image='cli_agent:all',
            env_vars={'ANTHROPIC_API_KEY': 'k'},
            extra_ro_mounts=[{'host': '/h', 'container': '/extra_ro_0'}])
        assert '-e' in cmd and 'ANTHROPIC_API_KEY=k' in cmd
        assert any(x == '/h:/extra_ro_0:ro' for x in cmd)
        # -e and the :ro mount appear BEFORE the image + harness argv.
        assert cmd.index('-e') < cmd.index('cli_agent:all')
        assert cmd[-1] == 'claude'

    def test_default_off_argv_is_byte_identical(self):
        base = runner._build_docker_cmd(container_name='c', workspace='.',
                                        harness_cmd=['claude', '-p'], image='i')
        assert '-e' not in base
        assert not any(x.endswith(':ro') for x in base)
        # image immediately precedes the harness argv (no dangling -v/-e).
        assert base[base.index('i') + 1:] == ['claude', '-p']


# ── image auto-build ─────────────────────────────────────────────────────────


def test_ensure_image_builds_once_and_memoizes(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, '_BUILD_OK', set())
    monkeypatch.setattr(runner, '_image_present', lambda tag: False)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return MagicMock(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    monkeypatch.setattr(runner, '_DOCKERFILE', tmp_path / 'Dockerfile.cli_agent')
    monkeypatch.setattr(runner, '_REPO_ROOT', str(tmp_path))

    runner._ensure_image('cli_agent:all')
    assert any('build' in c and 'cli_agent:all' in c for c in calls)
    n = len(calls)
    runner._ensure_image('cli_agent:all')  # memoized -> no new subprocess
    assert len(calls) == n


def test_ensure_image_fail_open_on_build_error(monkeypatch):
    """A failed build must not raise and must NOT be memoized (so it can retry)."""
    monkeypatch.setattr(runner, '_BUILD_OK', set())
    monkeypatch.setattr(runner, '_image_present', lambda tag: False)
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError('no docker')))
    runner._ensure_image('t:x')  # must not raise
    assert 't:x' not in runner._BUILD_OK


# ── status ───────────────────────────────────────────────────────────────────


def test_cli_agent_status_shape(monkeypatch):
    import config.secrets_loader as sl
    monkeypatch.setattr(sl, 'get_secret',
                        lambda name: 'stored' if name == 'cli_claude_code_key' else None)
    monkeypatch.setattr(runner, '_image_present_cached', lambda tag: True)
    pool = NS(settings=NS(cli_agent_enabled={'claude_code': True}, cli_agent_images={},
                          cli_agent_models={'opencode': 'm-x'}, cli_agent_paths_ro=False))
    st = runner.cli_agent_status(pool)
    assert set(st.keys()) == {'claude_code', 'opencode', 'antigravity'}
    assert st['claude_code'] == {'enabled': True, 'key_configured': True,
                                 'image_present': True, 'quota_available': 'unknown',
                                 'model': None}
    # antigravity has no API-key auth -> key_configured must be None (invariant #3).
    assert st['antigravity']['key_configured'] is None
    assert st['opencode']['model'] == 'm-x'


# ── system_info render ───────────────────────────────────────────────────────


def test_render_cli_agents(monkeypatch):
    monkeypatch.setattr(runner, 'cli_agent_status', lambda pool: {
        'claude_code': {'enabled': True, 'key_configured': False, 'image_present': None,
                        'quota_available': 'unknown', 'model': 'sonnet'}})
    out = SystemInfo(agent_pool=MagicMock())._render_cli_agents()
    assert 'claude_code' in out and 'enabled=True' in out and 'model=sonnet' in out


def test_render_cli_agents_degrades_to_empty(monkeypatch):
    """A status failure must never raise — degrade to '' like every other render helper."""
    monkeypatch.setattr(runner, 'cli_agent_status',
                        lambda pool: (_ for _ in ()).throw(RuntimeError('boom')))
    assert SystemInfo(agent_pool=MagicMock())._render_cli_agents() == ''


# ── schema drift guard ───────────────────────────────────────────────────────


def test_schema_relaxed_and_status_model_present():
    from agent_cascade.engine.cli_agent_adapters import registered_harnesses
    s = dna.TOOL_METADATA['call_cli_agent']
    assert s['required'] == []  # a status-only call is valid
    assert 'status' in s['parameters'] and 'model' in s['parameters']
    # enum must not drift from the adapter registry (unchanged by this feature).
    assert set(s['parameters']['harness']['enum']) == set(registered_harnesses())


# ── ws handle_set_cli_agent_key ──────────────────────────────────────────────


def _ws_obj():
    obj = WsMessageHandler.__new__(WsMessageHandler)
    obj.agent_pool = None
    obj._broadcast = AsyncMock()
    return obj


class TestSetCliAgentKeyHandler:
    def test_writes_for_api_key_harness(self, monkeypatch):
        import config.secrets_loader as sl
        wrote = MagicMock()
        monkeypatch.setattr(sl, 'set_secret', wrote)
        asyncio.run(WsMessageHandler.handle_set_cli_agent_key(
            _ws_obj(), {'harness': 'claude_code', 'key': 'abc'}))
        wrote.assert_called_once_with('cli_claude_code_key', 'abc')

    def test_noop_for_antigravity(self, monkeypatch):
        import config.secrets_loader as sl
        wrote = MagicMock()
        monkeypatch.setattr(sl, 'set_secret', wrote)
        asyncio.run(WsMessageHandler.handle_set_cli_agent_key(
            _ws_obj(), {'harness': 'antigravity', 'key': 'abc'}))
        wrote.assert_not_called()

    def test_noop_for_unknown_harness(self, monkeypatch):
        import config.secrets_loader as sl
        wrote = MagicMock()
        monkeypatch.setattr(sl, 'set_secret', wrote)
        asyncio.run(WsMessageHandler.handle_set_cli_agent_key(
            _ws_obj(), {'harness': 'nope', 'key': 'abc'}))
        wrote.assert_not_called()

# ── enable gate: disabled harness never spawns (invariant #4) ───────────────────

class TestEnableGate:
    def test_disabled_harness_returns_126_and_never_spawns(self, monkeypatch, tmp_path):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
        spawn = MagicMock()
        monkeypatch.setattr(runner.subprocess, 'run', spawn)
        pool = NS(settings=NS(cli_agent_enabled={}, max_cli_agent_children=2))
        res = runner.run_cli_agent(
            adapter=get_adapter('claude_code'), harness='claude_code', task='t',
            workspace=str(tmp_path), permission_posture='acceptEdits', timeout=30,
            pool=pool, caller_name='c')
        assert res.exit_code == 126
        spawn.assert_not_called()  # gate fires BEFORE semaphore/subprocess


# ── model plumbing matrix (adapter layer): model x model_flag ───────────────────

class TestModelPlumbing:
    def test_model_flag_appended_when_both_resolve(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('claude_code')
        assert ad.model_flag == '--model'
        with_model = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits', model='sonnet')
        i = with_model.index('--model')
        assert with_model[i + 1] == 'sonnet'

    def test_byte_identical_when_model_none(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('claude_code')
        base = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits')
        none_model = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits', model=None)
        assert none_model == base  # no flag appended when model is None

    def test_antigravity_ignores_model(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('antigravity')
        assert ad.model_flag is None  # invariant #3 (agy flag undocumented)
        base = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits')
        with_model = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits', model='gemini-x')
        assert with_model == base  # model_flag None -> byte-identical
        assert '--model' not in with_model


# ── dispatcher: status short-circuits and never spawns ──────────────────────────

def test_dispatcher_status_short_circuits(monkeypatch):
    from agent_cascade.tool_dispatcher import ToolDispatcher
    monkeypatch.setattr(runner, 'cli_agent_status', lambda pool: {'claude_code': {'enabled': False}})
    spawn = MagicMock()
    monkeypatch.setattr(runner, 'run_cli_agent', spawn)
    disp = ToolDispatcher.__new__(ToolDispatcher)
    disp.pool = NS(settings=MagicMock())
    out = disp.handle_call_cli_agent({'status': True}, MagicMock())
    parsed = json.loads(out)  # must be valid JSON (proves short-circuit, not the error string)
    assert parsed == {'claude_code': {'enabled': False}}
    spawn.assert_not_called()  # status path must not reach the runner


# ── secrets wiring: get_secret -> env_vars -> -e flag (invariant #2) ────────────

class TestDockerCmdNegatives:
    def test_no_key_no_e_and_never_mounts_secrets(self):
        # Pure builder: no env_vars -> no -e; argv must never contain secrets.json.
        cmd = runner._build_docker_cmd(container_name='c', workspace='.',
                                       harness_cmd=['claude'], image='cli_agent:all')
        assert '-e' not in cmd
        assert not any('secrets.json' in str(x) for x in cmd)

    def test_run_cli_agent_wires_secret_to_e_flag(self, monkeypatch, tmp_path):
        import config.secrets_loader as sl
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        monkeypatch.setattr(sl, 'get_secret',
                            lambda name: 'TOPSECRET' if name == 'cli_claude_code_key' else None)
        monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: 'img:tag')  # skip image logic
        monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
        captured = {}

        def fake_run(cmd, **kw):
            captured['cmd'] = list(cmd)
            captured['env'] = kw.get('env')
            return MagicMock(stdout='ok', stderr='', returncode=0)

        monkeypatch.setattr(runner.subprocess, 'run', fake_run)
        pool = NS(settings=NS(cli_agent_enabled={'claude_code': True}, cli_agent_models={},
                              cli_agent_paths_ro=False, max_cli_agent_children=2))
        res = runner.run_cli_agent(
            adapter=get_adapter('claude_code'), harness='claude_code', task='t',
            workspace=str(tmp_path), permission_posture='acceptEdits', timeout=30,
            pool=pool, caller_name='c')
        assert res.exit_code == 0
        cmd = captured['cmd']
        # secret reached the docker -e flag via get_secret -> env_vars -> _build_docker_cmd
        assert '-e' in cmd and 'ANTHROPIC_API_KEY=TOPSECRET' in cmd
        # the secret VALUE is never smuggled through subprocess env= (docker -e only)
        assert not any('TOPSECRET' in str(v) for v in (captured['env'] or {}).values())
        # and never bind-mounted
        assert not any('secrets.json' in str(x) for x in cmd)
