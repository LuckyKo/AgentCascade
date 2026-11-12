"""Regression guard for BUG_0296: the pytest config dir must never be the real one,
must be private per test, and must never reach the live ``config/api_endpoints.json``.

``APIRouter`` resolves its config file from ``AGENT_CASCADE_TEST_CONFIG_DIR`` (that env var
overrides the ``config_dir=`` argument) and ``_load()`` merges a persisted ``default_llm_cfg``
over the cfg passed to ``__init__``. Three distinct hazards follow:

1. **Module-scoped fixtures run before function-scoped ones.** A module-scoped app/pool built
   while the env var is unset binds its router to the REAL workspace config dir - real LLM
   traffic and real config writes. The first test covers that.
2. **A session-wide directory makes persisted state process-global.** A router built in one
   test silently inherits the default config another test saved (the generalized BUG_0292
   leak). The order-dependent pair covers that; it is order-dependent BY DESIGN.
3. **An accidental live path must be inert.** Even if a future refactor resolves the live
   file, a test process must not read it (that sends real LLM traffic) or write it. The
   instance-scope and guard tests cover that.
"""

import json
import os
from pathlib import Path

import pytest

from agent_cascade.api_router import APIRouter
from agent_cascade.api_router_pkg import endpoints as endpoints_mod
from agent_cascade.api_router_pkg.endpoints import (api_endpoints_config_path,
                                                    ensure_api_endpoints_config,
                                                    migrate_legacy_api_endpoints_config)


def _real_config_dirs():
    """The config directories a test must never bind to (project root and the live workspace)."""
    from agent_cascade.settings import DEFAULT_WORKSPACE

    project_root = Path(__file__).resolve().parent.parent
    return {
        (project_root / 'config').resolve(),
        (Path(DEFAULT_WORKSPACE) / 'config').resolve(),
    }


@pytest.fixture(scope='module')
def module_scoped_router():
    """Build a router at MODULE scope - i.e. before any function-scoped autouse fixture."""
    return APIRouter(default_llm_cfg={'model': 'module-scope-guard'})


def test_module_scoped_router_never_binds_to_a_real_config_dir(module_scoped_router):
    """The session baseline must be in place even when no per-test dir exists yet (RED pre-fix)."""
    cfg_dir = Path(module_scoped_router._config_path).parent.resolve()
    assert cfg_dir not in _real_config_dirs(), (
        f'APIRouter() built at module scope bound to the REAL config dir {cfg_dir}; '
        'AGENT_CASCADE_TEST_CONFIG_DIR must be set for the whole session, not just per test'
    )


def test_config_file_name_is_instance_scoped():
    """A test process carries an instance ID, so it can never open the live single-instance file."""
    from agent_cascade.instance_id import get_instance_id

    instance_id = get_instance_id()
    assert instance_id, 'conftest must set AGENT_CASCADE_INSTANCE_ID for test runs'

    router = APIRouter(default_llm_cfg={'model': 'scope'})
    assert router._config_path.name == f'api_endpoints_{instance_id}.json', (
        f'test router is not instance-scoped: {router._config_path.name}'
    )
    assert router._config_path.parent == Path(os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'])
    assert router._config_path.name != 'api_endpoints.json'


def test_save_refuses_the_live_config_under_pytest(monkeypatch, tmp_path):
    """A save aimed at the live config is skipped instead of overwriting it."""
    fake_live_dir = tmp_path / 'live_config'
    monkeypatch.setattr(endpoints_mod, 'project_config_dir', lambda: fake_live_dir)
    fake_live = fake_live_dir / 'api_endpoints.json'

    router = APIRouter(default_llm_cfg={'model': 'written-by-a-test'})
    router._config_path = fake_live
    router._save()

    assert not fake_live.exists(), 'a test process wrote the LIVE api_endpoints.json path'


def test_load_refuses_the_live_config_under_pytest(monkeypatch, tmp_path):
    """Loading the live config is skipped, so a test keeps the config it was constructed with."""
    fake_live_dir = tmp_path / 'live_config'
    fake_live_dir.mkdir(parents=True)
    fake_live = fake_live_dir / 'api_endpoints.json'
    fake_live.write_text(json.dumps({'endpoints': [],
                                     'default_llm_cfg': {'model': 'live-real-model'}}),
                         encoding='utf-8')
    monkeypatch.setattr(endpoints_mod, 'project_config_dir', lambda: fake_live_dir)

    router = APIRouter(default_llm_cfg={'model': 'own-default'})
    router._config_path = fake_live
    router._load()

    assert router.default_llm_cfg == {'model': 'own-default'}, (
        f'test process read the LIVE config: {router.default_llm_cfg!r}'
    )


def test_ensure_api_endpoints_config_refuses_the_live_path(monkeypatch, tmp_path):
    """The startup initializer is inert for the live path when running under pytest.

    The guard covers the one dangerous case: a test process with NO instance ID (so the
    instance-scoped name collapses back to ``api_endpoints.json``).
    """
    fake_live_dir = tmp_path / 'live_config'
    monkeypatch.setattr(endpoints_mod, 'project_config_dir', lambda: fake_live_dir)
    monkeypatch.delenv('AGENT_CASCADE_INSTANCE_ID', raising=False)

    assert ensure_api_endpoints_config(str(fake_live_dir)) is False
    assert not (fake_live_dir / 'api_endpoints.json').exists()


def test_legacy_config_is_copied_for_named_instances(monkeypatch, tmp_path):
    """A real named instance upgrades from api_endpoints.json to api_endpoints_<id>.json once."""
    legacy = tmp_path / 'api_endpoints.json'
    legacy.write_text(json.dumps({'endpoints': [], 'agent_priorities': {}}), encoding='utf-8')

    monkeypatch.setattr(endpoints_mod, 'is_test_process', lambda: False)
    monkeypatch.setenv('AGENT_CASCADE_INSTANCE_ID', 'prod')

    assert migrate_legacy_api_endpoints_config(tmp_path) is True
    scoped = tmp_path / 'api_endpoints_prod.json'
    assert scoped.exists()
    assert scoped.read_text(encoding='utf-8') == legacy.read_text(encoding='utf-8')
    assert legacy.exists(), 'the legacy file must be left in place'
    assert migrate_legacy_api_endpoints_config(tmp_path) is False, 'migration must be idempotent'


def test_router_persists_a_usable_default():
    """Save a distinctive, USABLE default into whatever directory the test points at."""
    router = APIRouter(default_llm_cfg={
        'model': 'leak-sentinel',
        'model_server': 'http://127.0.0.1:9/v1',
        'api_key': 'EMPTY',
        'max_input_tokens': 8192,
    })
    router._save()

    cfg_dir = Path(os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'])
    assert api_endpoints_config_path(cfg_dir).exists(), (
        'the router did not persist into AGENT_CASCADE_TEST_CONFIG_DIR'
    )


def test_fresh_router_does_not_inherit_the_previous_test_default():
    """A new router keeps its own default instead of merging the previous test's (RED pre-fix)."""
    router = APIRouter(default_llm_cfg={'model': 'own-default'})
    assert router.default_llm_cfg == {'model': 'own-default'}, (
        f"router inherited persisted config from another test: {router.default_llm_cfg!r}"
    )
