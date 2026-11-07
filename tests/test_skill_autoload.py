"""BUG_0263 — skill auto-load on repeat hint + "Most-missed hinted skills" telemetry.

Covers:
  A1  threshold-cross fires auto-load (real inject_skill path: enqueue + record)
  A2  below-threshold no-fire
  A3  already-loaded no-fire (idempotent)
  A4  disabled config no-fire
  A5  inject_skill failure is swallowed (best-effort, never breaks the hint path)
  T2  telemetry section present / sorted most-missed-first / omitted when empty
  C1  config parse + defaults (handlers + registry SSOT)

No network, no real LLM. Stubs bind the REAL ``AgentPool.inject_skill`` so the load path
exercised here is the same one ``load_skill`` and the memory-hint auto-loader share.
"""

from types import SimpleNamespace

import pytest

from agent_cascade.config_handlers import CONFIG_HANDLERS
from agent_cascade.memory_hint.manager import MemoryHintManager
from agent_cascade.pool.core import AgentPool
from agent_cascade.settings_registry import SETTINGS_REGISTRY, validate_registry
from agent_cascade.tools.custom.system_info import SystemInfo


# ── shared fixture helpers ──────────────────────────────────────────────────

def _make_inject_pool(inst, enqueued, *, fail=False):
    """Stub pool exposing exactly what AgentPool.inject_skill touches, with the REAL method
    bound (not auto-mocked). If ``fail`` is set, inject_skill itself raises (A5)."""
    if fail:
        def _raise(agent_name, skill_name):
            raise RuntimeError('boom: inject_skill blew up')
        return SimpleNamespace(
            skill_manager=SimpleNamespace(
                load_full_instructions=lambda n: 'BODY',
                is_skill_disabled=lambda n: False,
            ),
            get_instance=lambda name: inst,
            enqueue_message=lambda name, content: enqueued.append((name, content)),
            telemetry=None,
            inject_skill=_raise,
        )
    pool = SimpleNamespace(
        skill_manager=SimpleNamespace(
            load_full_instructions=lambda n: 'BODY',
            is_skill_disabled=lambda n: False,
        ),
        get_instance=lambda name: inst,
        enqueue_message=lambda name, content: enqueued.append((name, content)),
        telemetry=None,
    )
    pool.inject_skill = lambda agent_name, skill_name: AgentPool.inject_skill(pool, agent_name, skill_name)
    return pool


def _mgr_for(pool):
    """A MemoryHintManager whose _pool is the stub pool with the real inject_skill. The MagicMock
    is only consumed by __init__; _maybe_autoload_skills uses self._pool, which we override."""
    mgr = MemoryHintManager(SimpleNamespace(llm_cfg={}))
    mgr._pool = pool
    return mgr


def _inst():
    return SimpleNamespace(_loaded_skill_names=None, agent_class='coder')


ON = {'skill_autoload_enabled': True, 'skill_autoload_threshold': 3}


# ── A1-A5: auto-load side effect ────────────────────────────────────────────

def test_a1_threshold_cross_fires_and_records():
    inst = _inst()
    enq = []
    mgr = _mgr_for(_make_inject_pool(inst, enq))
    mgr._maybe_autoload_skills(inst, 'Main', ['skill-a'], {'skill-a': 3}, set(), ON)
    assert len(enq) == 1, enq
    agent, content = enq[0]
    assert agent == 'Main'
    assert 'Apply the above guidelines' in content and 'skill-a' in content
    assert inst._loaded_skill_names == ['skill-a']


def test_a2_below_threshold_no_fire():
    inst = _inst()
    enq = []
    mgr = _mgr_for(_make_inject_pool(inst, enq))
    mgr._maybe_autoload_skills(inst, 'Main', ['skill-a'], {'skill-a': 2}, set(), ON)
    assert enq == []
    assert inst._loaded_skill_names is None


def test_a3_already_loaded_no_fire():
    inst = _inst()
    inst._loaded_skill_names = ['skill-a']
    enq = []
    mgr = _mgr_for(_make_inject_pool(inst, enq))
    # loaded snapshot carries the (lowercased) name -> skip before calling inject_skill.
    mgr._maybe_autoload_skills(inst, 'Main', ['skill-a'], {'skill-a': 5}, {'skill-a'}, ON)
    assert enq == []
    assert inst._loaded_skill_names == ['skill-a']  # untouched


def test_a4_disabled_config_no_fire():
    inst = _inst()
    enq = []
    mgr = _mgr_for(_make_inject_pool(inst, enq))
    off = {'skill_autoload_enabled': False, 'skill_autoload_threshold': 3}
    mgr._maybe_autoload_skills(inst, 'Main', ['skill-a'], {'skill-a': 5}, set(), off)
    assert enq == []
    assert inst._loaded_skill_names is None


def test_a5_inject_failure_swallowed():
    """inject_skill raising must not propagate — the hint path keeps going."""
    inst = _inst()
    enq = []
    mgr = _mgr_for(_make_inject_pool(inst, enq, fail=True))
    # Must NOT raise despite the failing inject_skill (best-effort by design).
    mgr._maybe_autoload_skills(inst, 'Main', ['skill-a'], {'skill-a': 3}, set(), ON)
    assert enq == []  # nothing enqueued (inject_skill raised before enqueue)


# ── T2: "Most-missed hinted skills" telemetry ───────────────────────────────

class _Telem:
    """Minimal collector satisfying the 4 getters _render_telemetry_dump uses (all `or {}`/`or []`)."""
    def get_session_summary(self): return {}
    def get_config_comparison(self): return []
    def get_agent_class_summary(self): return []
    def get_skill_usage_summary(self): return []


def _inst_counts(counts, loaded):
    return SimpleNamespace(_skill_hint_counts=counts, _loaded_skill_names=loaded)


def test_t2_section_present_sorted_and_omitted():
    # agg: a hinted=7 loaded=0 (missed 7), c hinted=4 loaded=1 (missed 3), b hinted=1 loaded=0 (missed 1)
    pool = SimpleNamespace(
        telemetry=_Telem(),
        instances={
            'Main': _inst_counts({'a': 5, 'b': 1}, []),
            'W': _inst_counts({'a': 2, 'c': 4}, ['c']),
        },
    )
    out = SystemInfo(agent_pool=pool).call('{"help": "telemetry"}')
    assert 'Most-missed hinted skills:' in out
    assert '  a: hinted=7 loaded=0' in out
    assert '  c: hinted=4 loaded=1' in out
    assert '  b: hinted=1 loaded=0' in out
    # most-missed first: a (7) before c (3) before b (1)
    assert out.index('a: hinted=7') < out.index('c: hinted=4') < out.index('b: hinted=1')

    # omitted when no instance has any hint counts
    empty = SimpleNamespace(telemetry=_Telem(), instances={
        'Main': _inst_counts({}, []),
        'W': _inst_counts({}, None),
    })
    out2 = SystemInfo(agent_pool=empty).call('{"help": "telemetry"}')
    assert 'Most-missed hinted skills:' not in out2


def test_t2_helper_omits_when_no_instances_attr():
    # pool without an `instances` attribute -> default {} -> '' (no raise, no section)
    si = SystemInfo(agent_pool=SimpleNamespace(telemetry=_Telem()))
    assert si._render_missed_skills() == ''


# ── C1: config parse + defaults ─────────────────────────────────────────────

def test_c1_config_defaults_and_parse():
    # registry SSOT carries both keys sourced from llm_cfg, and validate_registry passes
    validate_registry()
    assert 'memory_hint_skill_autoload_threshold' in SETTINGS_REGISTRY
    assert SETTINGS_REGISTRY['memory_hint_skill_autoload_threshold'].value_source == 'llm_cfg'
    assert 'memory_hint_skill_autoload_enabled' in SETTINGS_REGISTRY

    # default when key absent: threshold 3, enabled True
    pool = SimpleNamespace(llm_cfg={})
    CONFIG_HANDLERS['memory_hint_skill_autoload_threshold']({}, pool, [])
    CONFIG_HANDLERS['memory_hint_skill_autoload_enabled']({}, pool, [])
    assert pool.llm_cfg['memory_hint_skill_autoload_threshold'] == 3
    assert pool.llm_cfg['memory_hint_skill_autoload_enabled'] is True

    # parse string value, invalid falls back to default 3, 0 clamped to 1
    pool2 = SimpleNamespace(llm_cfg={})
    CONFIG_HANDLERS['memory_hint_skill_autoload_threshold'](
        {'memory_hint_skill_autoload_threshold': '5'}, pool2, [])
    assert pool2.llm_cfg['memory_hint_skill_autoload_threshold'] == 5
    pool3 = SimpleNamespace(llm_cfg={})
    CONFIG_HANDLERS['memory_hint_skill_autoload_threshold'](
        {'memory_hint_skill_autoload_threshold': 'x'}, pool3, [])
    assert pool3.llm_cfg['memory_hint_skill_autoload_threshold'] == 3
    pool4 = SimpleNamespace(llm_cfg={})
    CONFIG_HANDLERS['memory_hint_skill_autoload_threshold'](
        {'memory_hint_skill_autoload_threshold': 0}, pool4, [])
    assert pool4.llm_cfg['memory_hint_skill_autoload_threshold'] == 1


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-q', '-n', '0']))
