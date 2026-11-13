"""BUG_0310 — boot smoke: the changed boot path still builds a serving-ready pool.

This is a pool-only boot (no uvicorn, no LLM call): it exercises exactly the boot path my
BUG_0310 edits touch — ``AgentPool.__init__``'s boot-timing marks, the skill discover phase,
and the now-BACKGROUNDED memory-hint rescan (R2) — and asserts concrete boot invariants
rather than merely "no crash":
  * all 12 agent templates registered (the serving pool is populated);
  * the skill registry is non-empty (skill_discover phase ran).

The R2 deferral (rescan runs off the boot thread, index populates on a non-main daemon
thread within a bounded window) is fully pinned by
tests/memory_hint/test_bug0310_background_rescan.py; it is intentionally not duplicated
here.

The MCP enabled-filter value assertions live in the dedicated, kept-green
test_mcp_enabled_filter.py (which drives the REAL init_config_async filter); duplicating a
real MCP connect here would add flakiness without adding coverage.
"""

from pathlib import Path

import pytest

from agent_cascade.agent_pool import AgentPool


PROJECT_ROOT = Path(__file__).parent.parent.absolute()


@pytest.fixture(scope='module')
def booted_pool():
    """A real AgentPool built offline (model_server points nowhere; nothing calls it)."""
    llm_cfg = {
        'model': 'mock-model',
        'model_server': 'http://127.0.0.1:1/v1',  # never contacted by pool init
        'api_key': 'EMPTY',
        'model_type': 'qwenvl_oai',
        'max_input_tokens': 8192,
        'memory_hint_enabled': True,  # exercise the R2 backgrounded-rescan path
    }
    pool = AgentPool(llm_cfg, agents_dir=str(PROJECT_ROOT / 'agents'))
    yield pool
    shutdown = getattr(pool, 'shutdown', None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:  # noqa: BLE001 — teardown best-effort
            pass


def test_all_agent_templates_registered(booted_pool):
    assert len(booted_pool.templates) == 12, (
        f'expected 12 agent templates at boot, got {len(booted_pool.templates)}')


def test_skill_registry_populated(booted_pool):
    names = booted_pool.skill_manager.get_skill_names()
    assert len(names) > 0, 'skill registry empty after boot — skill_discover did not run'
