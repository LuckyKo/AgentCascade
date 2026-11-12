"""Regression guard for BUG_0296: the pytest config dir must never be the real one,
and must be private per test.

``APIRouter`` resolves its config file from ``AGENT_CASCADE_TEST_CONFIG_DIR`` (that env var
overrides the ``config_dir=`` argument) and ``_load()`` merges a persisted ``default_llm_cfg``
over the cfg passed to ``__init__``. Two distinct hazards follow:

1. **Module-scoped fixtures run before function-scoped ones.** A module-scoped app/pool built
   while the env var is unset binds its router to the REAL workspace config dir - real LLM
   traffic and real config writes. The first test covers that.
2. **A session-wide directory makes persisted state process-global.** A router built in one
   test silently inherits the default config another test saved (the generalized BUG_0292
   leak). The last two tests cover that; they are order-dependent BY DESIGN.
"""

import os
from pathlib import Path

import pytest

from agent_cascade.api_router import APIRouter


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
    assert (cfg_dir / 'api_endpoints.json').exists(), (
        'the router did not persist into AGENT_CASCADE_TEST_CONFIG_DIR'
    )


def test_fresh_router_does_not_inherit_the_previous_test_default():
    """A new router keeps its own default instead of merging the previous test's (RED pre-fix)."""
    router = APIRouter(default_llm_cfg={'model': 'own-default'})
    assert router.default_llm_cfg == {'model': 'own-default'}, (
        f"router inherited persisted config from another test: {router.default_llm_cfg!r}"
    )
