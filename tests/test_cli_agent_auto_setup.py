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
import datetime
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
    _handle_cli_agent_agy_auth_volume, _handle_cli_agent_enabled, _handle_cli_agent_images,
    _handle_cli_agent_models, _handle_cli_agent_paths_ro)
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

    def test_agy_auth_volume_str_and_off_state(self):
        pool = _settings_pool()
        _handle_cli_agent_agy_auth_volume({'cli_agent_agy_auth_volume': ' vol-x '}, pool, [])
        assert pool.settings.cli_agent_agy_auth_volume == 'vol-x'
        # '' and '0' are the documented OFF values — they must be stored verbatim, not defaulted.
        for off in ('', '0'):
            _handle_cli_agent_agy_auth_volume({'cli_agent_agy_auth_volume': off}, pool, [])
            assert pool.settings.cli_agent_agy_auth_volume == off


# ── registry invariants ──────────────────────────────────────────────────────


class TestRegistryInvariants:
    def test_dict_keys_seeded_and_typed(self):
        for k in _DICT_KEYS:
            assert k in registry._SEED_DEFAULTS and registry._SEED_DEFAULTS[k] == {}, k
            assert registry._PY_TYPES[k] is dict, k

    def test_all_four_in_pool_keys(self):
        for k in (*_DICT_KEYS, 'cli_agent_paths_ro'):
            assert k in registry._POOL_KEYS, k

    def test_agy_auth_volume_registered_str_default(self):
        from agent_cascade.settings import CLI_AGENT_AGY_AUTH_VOLUME
        assert 'cli_agent_agy_auth_volume' in registry._POOL_KEYS
        assert registry._DEFAULTS['cli_agent_agy_auth_volume'] == CLI_AGENT_AGY_AUTH_VOLUME
        assert registry.spec('cli_agent_agy_auth_volume').py_type is str


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

    def test_oauth_volume_mounted_before_image(self):
        cmd = runner._build_docker_cmd(
            container_name='c', workspace='.', harness_cmd=['agy'], image='cli_agent_agy',
            extra_volumes=[{'name': 'cli_agent_agy_auth', 'target': '/root/.gemini'}])
        assert 'cli_agent_agy_auth:/root/.gemini' in cmd
        assert cmd.index('cli_agent_agy_auth:/root/.gemini') < cmd.index('cli_agent_agy')
        assert cmd[-1] == 'agy'


# ── OAuth volume: resolution, tolerant probe, run gate ───────────────────────


class TestOAuthVolume:
    def _pool(self, volume='vol-1', enabled=True, **extra):
        return NS(settings=NS(cli_agent_enabled={'antigravity': enabled},
                              cli_agent_images={}, cli_agent_models={},
                              cli_agent_paths_ro=False, max_cli_agent_children=2,
                              cli_agent_agy_auth_volume=volume, **extra))

    def test_resolve_only_for_oauth_adapters(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        pool = self._pool()
        assert runner._resolve_auth_volume(pool, get_adapter('antigravity')) == 'vol-1'
        for h in ('claude_code', 'opencode'):
            assert runner._resolve_auth_volume(pool, get_adapter(h)) is None

    def test_resolve_off_values_and_mock_settings(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('antigravity')
        for off in ('', '0', '   '):
            assert runner._resolve_auth_volume(self._pool(volume=off), ad) is None
        # A MagicMock settings double must not leak a fake volume name into docker argv.
        assert runner._resolve_auth_volume(NS(settings=MagicMock()), ad) is not None

    def test_probe_tolerant_candidates_and_cache_invalidation(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            return MagicMock(stdout='', stderr='', returncode=1)

        monkeypatch.setattr(runner, '_AGY_AUTH_CACHE', {})
        monkeypatch.setattr(runner.subprocess, 'run', fake_run)
        assert runner._agy_auth_volume_populated('v', 'img') is False
        # candidate paths + the name-grep fallback are all in the probe script
        script = calls[0][calls[0].index('-c') + 1]
        for candidate in runner._AGY_AUTH_CREDENTIAL_CANDIDATES:
            assert candidate in script
        assert 'grep' in script
        # cached -> no second docker call; invalidation forces a re-probe
        runner._agy_auth_volume_populated('v', 'img')
        assert len(calls) == 1
        runner._invalidate_agy_auth_cache('v')
        runner._agy_auth_volume_populated('v', 'img')
        assert len(calls) == 2

    def test_probe_docker_unavailable_is_indeterminate(self, monkeypatch):
        monkeypatch.setattr(runner, '_AGY_AUTH_CACHE', {})
        monkeypatch.setattr(runner.subprocess, 'run',
                            lambda cmd, **kw: (_ for _ in ()).throw(OSError('no docker')))
        assert runner._agy_auth_volume_populated('v', 'img') is None

    def test_gate_blocks_unauthenticated_volume(self, monkeypatch, tmp_path):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
        monkeypatch.setattr(runner, '_agy_auth_volume_populated', lambda vol, img: False)
        prepared = runner.prepare_cli_agent_run(
            adapter=get_adapter('antigravity'), task='t', workspace=str(tmp_path),
            permission_posture='acceptEdits', pool=self._pool(), caller_name='c')
        assert prepared.early_result is not None
        assert prepared.early_result.exit_code == 126
        assert 'vol-1' in prepared.early_result.stderr
        assert 'Authenticate' in prepared.early_result.stderr
        assert prepared.docker_cmd is None  # nothing spawned

    def test_gate_indeterminate_fails_open_and_mounts_volume(self, monkeypatch, tmp_path):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
        monkeypatch.setattr(runner, '_agy_auth_volume_populated', lambda vol, img: None)
        monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: 'cli_agent_agy')
        monkeypatch.setattr(runner, '_binary_in_image', lambda image, binary: True)
        prepared = runner.prepare_cli_agent_run(
            adapter=get_adapter('antigravity'), task='t', workspace=str(tmp_path),
            permission_posture='acceptEdits', pool=self._pool(), caller_name='c')
        assert prepared.early_result is None
        assert 'vol-1:/root/.gemini' in prepared.docker_cmd

    def test_no_volume_mount_for_key_harness(self, monkeypatch, tmp_path):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
        monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: 'cli_agent:all')
        monkeypatch.setattr(runner, '_binary_in_image', lambda image, binary: True)
        pool = NS(settings=NS(cli_agent_enabled={'claude_code': True}, cli_agent_images={},
                              cli_agent_models={}, cli_agent_paths_ro=False,
                              max_cli_agent_children=2, cli_agent_agy_auth_volume='vol-1'))
        prepared = runner.prepare_cli_agent_run(
            adapter=get_adapter('claude_code'), task='t', workspace=str(tmp_path),
            permission_posture='acceptEdits', pool=pool, caller_name='c')
        assert prepared.early_result is None
        assert not any('vol-1' in str(x) for x in prepared.docker_cmd)


# ── image auto-build ─────────────────────────────────────────────────────────

def test_ensure_image_builds_once_and_memoizes(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, '_BUILD_OK', {})
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
    monkeypatch.setattr(runner, '_BUILD_OK', {})
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
    monkeypatch.setattr(runner, '_agy_auth_volume_populated', lambda vol, image: True)
    pool = NS(settings=NS(cli_agent_enabled={'claude_code': True}, cli_agent_images={},
                          cli_agent_models={'opencode': 'm-x'}, cli_agent_paths_ro=False,
                          cli_agent_agy_auth_volume='vol-1',
                          cli_agent_rate_limit_seconds=0.0))
    st = runner.cli_agent_status(pool)
    assert set(st.keys()) == {'claude_code', 'opencode', 'antigravity'}
    assert st['claude_code'] == {'enabled': True, 'auth_type': 'api_key', 'key_configured': True,
                                 'image_present': True, 'quota_available': 'unknown',
                                 'model': None, 'rate_limit_seconds': 0.0,
                                 'next_run_in_seconds': 0.0}
    # antigravity has no API-key auth -> key_configured must be None (invariant #3).
    assert st['antigravity']['key_configured'] is None
    assert st['antigravity']['auth_type'] == 'oauth'
    assert st['antigravity']['authenticated'] is True
    # key-based harnesses carry no `authenticated` field at all.
    assert 'authenticated' not in st['opencode']
    assert st['opencode']['model'] == 'm-x'


def test_cli_agent_status_oauth_volume_off_is_indeterminate(monkeypatch):
    """Volume disabled ('' / '0') -> authenticated is None; no docker probe is run."""
    import config.secrets_loader as sl
    monkeypatch.setattr(sl, 'get_secret', lambda name: None)
    monkeypatch.setattr(runner, '_image_present_cached', lambda tag: True)
    probe = MagicMock()
    monkeypatch.setattr(runner, '_agy_auth_volume_populated', probe)
    for off in ('', '0'):
        pool = NS(settings=NS(cli_agent_enabled={}, cli_agent_images={}, cli_agent_models={},
                              cli_agent_paths_ro=False, cli_agent_agy_auth_volume=off))
        assert runner.cli_agent_status(pool)['antigravity']['authenticated'] is None
    probe.assert_not_called()


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

    def test_antigravity_emits_model(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('antigravity')
        # agy 1.5.8 documents --model; a requested model is emitted (no model -> byte-identical).
        assert ad.model_flag == '--model'
        base = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits')
        with_model = ad.build_launch_cmd(task='t', workspace='/w', posture='acceptEdits', model='gemini-x')
        i = with_model.index('--model')
        assert with_model[i + 1] == 'gemini-x'
        assert '--model' not in base
        assert with_model[-1] == '-p=t'  # prompt stays last (agy's -p is greedy)

    def test_antigravity_accept_edits_skips_permissions(self):
        """No TTY in the container: acceptEdits must auto-approve tools as well as edits."""
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('antigravity')
        for posture in ('acceptEdits', 'bypass', 'unknown-posture'):
            cmd = ad.build_launch_cmd(task='t', workspace='/w', posture=posture)
            assert '--dangerously-skip-permissions' in cmd, posture

    def test_antigravity_prompt_attached_via_equals_and_last(self):
        from agent_cascade.engine.cli_agent_adapters import get_adapter
        ad = get_adapter('antigravity')
        cmd = ad.build_launch_cmd(task='do the thing', workspace='/w', posture='acceptEdits')
        # agy's -p is greedy: the prompt MUST be attached via '=' and be the final token, with
        # --mode placed before it (a bare '-p <task>' would swallow --mode as the prompt value).
        assert cmd[0] == 'agy'
        assert cmd[-1] == '-p=do the thing'
        assert '-p' not in cmd[:-1]  # no bare -p flag anywhere in the argv
        assert cmd.index('--mode') < cmd.index('-p=do the thing')
        assert 'do the thing' not in cmd[:-1]  # prompt only appears in the -p= token


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
        monkeypatch.setattr(runner, '_binary_in_image', lambda image, binary: True)  # skip fail-fast probe
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


# ── dedicated antigravity image + periodic refresh + fail-fast ───────────────────


def test_cli_agent_default_image_routes_antigravity():
    from agent_cascade.settings import (CLI_AGENT_ANTIGRAVITY_IMAGE, CLI_AGENT_DEFAULT_IMAGE,
                                         cli_agent_default_image)
    assert cli_agent_default_image('antigravity') == 'cli_agent_agy' == CLI_AGENT_ANTIGRAVITY_IMAGE
    for h in ('claude_code', 'opencode'):
        assert cli_agent_default_image(h) == 'cli_agent:all' == CLI_AGENT_DEFAULT_IMAGE


def test_cli_agent_image_max_age_days_default_is_7():
    from agent_cascade.settings import CLI_AGENT_IMAGE_MAX_AGE_DAYS
    # Falsifiable: fails if the documented default is changed (env unset in the test env).
    assert CLI_AGENT_IMAGE_MAX_AGE_DAYS == 7


def test_dockerfile_agy_exists_and_has_installer():
    df = os.path.join(_REPO, 'Dockerfile.cli_agent_agy')
    assert os.path.isfile(df), df
    with open(df, encoding='utf-8') as fh:
        text = fh.read()
    assert 'node:20-slim' in text
    assert 'antigravity.google/cli/install.sh' in text  # official non-npm installer
    assert 'ENV PATH' in text and '/root/.local/bin' in text  # installer drops agy there


def _recorder(returncode=0):
    """(calls, fake_run): appends every subprocess.run argv and returns a MagicMock rc."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return MagicMock(stdout='', stderr='', returncode=returncode)
    return calls, fake_run


def test_ensure_image_picks_harness_dockerfile(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, '_BUILD_OK', {})
    monkeypatch.setattr(runner, '_image_present', lambda tag: False)  # force build path
    monkeypatch.setattr(runner, '_DOCKERFILE', tmp_path / 'Dockerfile.cli_agent')
    monkeypatch.setattr(runner, '_REPO_ROOT', str(tmp_path))
    monkeypatch.setattr(runner, '_HARNESS_DOCKERFILE',
                        {'antigravity': tmp_path / 'Dockerfile.cli_agent_agy'})
    calls, fake_run = _recorder()
    monkeypatch.setattr(runner.subprocess, 'run', fake_run)

    runner._ensure_image('cli_agent_agy', harness='antigravity')
    agy_build = calls[-1]
    assert agy_build[agy_build.index('-f') + 1].endswith('Dockerfile.cli_agent_agy')

    runner._ensure_image('cli_agent:all', harness='claude_code')  # shared fallback
    shared_build = calls[-1]
    assert shared_build[shared_build.index('-f') + 1].endswith('Dockerfile.cli_agent')


def test_ensure_image_rebuilds_when_stale_skips_when_fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, '_image_present', lambda tag: True)  # present, decide by age
    monkeypatch.setattr(runner, '_DOCKERFILE', tmp_path / 'Dockerfile.cli_agent')
    monkeypatch.setattr(runner, '_REPO_ROOT', str(tmp_path))
    calls, fake_run = _recorder()
    monkeypatch.setattr(runner.subprocess, 'run', fake_run)

    # older than max_age -> rebuild, then memoize the refreshed image.
    monkeypatch.setattr(runner, '_BUILD_OK', {})
    monkeypatch.setattr(runner, '_image_age_days', lambda tag: 10.0)
    runner._ensure_image('t:stale', max_age_days=7)
    assert any('build' in c and 't:stale' in c for c in calls)
    assert 't:stale' in runner._BUILD_OK

    # fresh -> memoize without any build.
    monkeypatch.setattr(runner, '_BUILD_OK', {})
    monkeypatch.setattr(runner, '_image_age_days', lambda tag: 1.0)
    n = len(calls)
    runner._ensure_image('t:fresh', max_age_days=7)
    assert len(calls) == n
    assert 't:fresh' in runner._BUILD_OK

    # indeterminate age -> treat as not stale, never force a rebuild.
    monkeypatch.setattr(runner, '_BUILD_OK', {})
    monkeypatch.setattr(runner, '_image_age_days', lambda tag: None)
    n = len(calls)
    runner._ensure_image('t:unknown', max_age_days=7)
    assert len(calls) == n
    assert 't:unknown' in runner._BUILD_OK


def test_ensure_image_memo_expires_and_rechecks_staleness(monkeypatch, tmp_path):
    """Cadence memo (finding #1): a fresh image is memoized (no docker work) until its refresh
    window elapses, then the age is re-evaluated and a now-stale image is rebuilt — within ONE
    process, i.e. periodic refresh does not require a restart."""
    monkeypatch.setattr(runner, '_image_present', lambda tag: True)
    monkeypatch.setattr(runner, '_DOCKERFILE', tmp_path / 'Dockerfile.cli_agent')
    monkeypatch.setattr(runner, '_REPO_ROOT', str(tmp_path))
    calls, fake_run = _recorder()
    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    monkeypatch.setattr(runner, '_BUILD_OK', {})

    # fresh (age 1 < 7) -> memoize, zero docker work.
    ages = {'v': 1.0}
    monkeypatch.setattr(runner, '_image_age_days', lambda tag: ages['v'])
    runner._ensure_image('t:r', max_age_days=7)
    assert len(calls) == 0 and 't:r' in runner._BUILD_OK
    runner._ensure_image('t:r', max_age_days=7)  # still inside the memo window -> no re-inspect
    assert len(calls) == 0

    # simulate the window elapsing (age now stale AND the memo's validity-until in the past).
    ages['v'] = 99.0
    runner._BUILD_OK['t:r'] = 0.0
    runner._ensure_image('t:r', max_age_days=7)
    assert any('build' in c and 't:r' in c for c in calls)  # re-evaluated -> refreshed


def test_image_age_days_parses_and_handles_error(monkeypatch):
    # ~5 days ago, aware ISO timestamp -> age ~5.0
    created = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.timedelta(days=5)).isoformat()
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: MagicMock(returncode=0, stdout=created))
    age = runner._image_age_days('t:x')
    assert age is not None and 4.9 < age < 5.1

    # docker writes nanosecond precision + Z suffix -> still parses to a positive age.
    monkeypatch.setattr(runner.subprocess, 'run', lambda cmd, **kw: MagicMock(
        returncode=0, stdout='2024-01-01T00:00:00.123456789Z'))
    assert runner._image_age_days('t:y') is not None

    # non-zero inspect exit -> None (indeterminate).
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: MagicMock(returncode=1, stdout=''))
    assert runner._image_age_days('t:z') is None

    # docker unavailable -> None.
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError('no docker')))
    assert runner._image_age_days('t:z') is None


def test_binary_in_image_true_false_none(monkeypatch):
    monkeypatch.setattr(runner, '_BINARY_IN_IMAGE_CACHE', {})
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: MagicMock(returncode=0, stdout='/usr/bin/agy'))
    assert runner._binary_in_image('img', 'agy') is True

    monkeypatch.setattr(runner, '_BINARY_IN_IMAGE_CACHE', {})
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: MagicMock(returncode=1, stdout=''))
    assert runner._binary_in_image('img', 'agy') is False

    monkeypatch.setattr(runner, '_BINARY_IN_IMAGE_CACHE', {})
    monkeypatch.setattr(runner.subprocess, 'run',
                        lambda cmd, **kw: (_ for _ in ()).throw(OSError('no docker')))
    assert runner._binary_in_image('img', 'agy') is None


def test_binary_in_image_ttl_cached(monkeypatch):
    monkeypatch.setattr(runner, '_BINARY_IN_IMAGE_CACHE', {})
    calls, fake_run = _recorder()
    monkeypatch.setattr(runner.subprocess, 'run', fake_run)
    runner._binary_in_image('img', 'agy')
    runner._binary_in_image('img', 'agy')  # cached -> no second docker run
    assert len(calls) == 1


def _ready_pool():
    return NS(settings=NS(cli_agent_enabled={'claude_code': True}, cli_agent_images={},
                          cli_agent_models={}, cli_agent_paths_ro=False,
                          max_cli_agent_children=2))


@pytest.mark.parametrize('probe_result,should_raise', [(False, True), (True, False), (None, False)])
def test_prepare_cli_agent_run_fail_fast_on_missing_binary(monkeypatch, tmp_path,
                                                           probe_result, should_raise):
    from agent_cascade.engine.cli_agent_adapters import HarnessNotFoundError, get_adapter
    monkeypatch.setattr(runner, '_LOGS_DIR', tmp_path)
    monkeypatch.setattr(runner, '_cli_agent_image', lambda h, p: 'img:tag')
    monkeypatch.setattr(runner, '_binary_in_image', lambda image, binary: probe_result)
    spawn = MagicMock()
    monkeypatch.setattr(runner.subprocess, 'run', spawn)
    kwargs = dict(adapter=get_adapter('claude_code'), harness='claude_code', task='t',
                  workspace=str(tmp_path), permission_posture='acceptEdits',
                  pool=_ready_pool(), caller_name='c')
    if should_raise:
        with pytest.raises(HarnessNotFoundError, match='not present in image'):
            runner.prepare_cli_agent_run(**kwargs)
        spawn.assert_not_called()  # rejects before any spawn
    else:
        prepared = runner.prepare_cli_agent_run(**kwargs)
        assert prepared.early_result is None
        assert prepared.docker_cmd is not None  # normal spawn path reached
