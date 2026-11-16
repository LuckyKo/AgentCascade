"""Tests for Compressor warm instance reuse (BUG_0330).

Mirrors the structure of tests/test_security_instance_reuse.py:
  * Claim registry semantics (try_claim / release_claim / claim_holder) — unit, no LLM.
  * The two-branch acquire helper (shape guard, best-effort fresh-spawn fallback) — unit.
  * Engine-level warm acquire (seed/reuse, eligibility, claim hand-off) — real engine + fake pool.
  * Invoker integration (compression / consolidation / reviewer warm switch) — patched engine.
  * §3f no-drop re-interrogation (correction turn, loop bound, kill-switch, held-claim no-wedge).

No test in this file needs an LLM server: transport is patched at the boundary.
"""
import sys
import os
from unittest.mock import MagicMock

import pytest

# Ensure the project root is importable regardless of how pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent_cascade.compressor_reuse import (  # noqa: E402
    try_claim, release_claim, claim_holder, _clear_all, acquire_compressor_agent)

COMPRESSOR_WARM = 'Compressor_warm'
REVIEWER_WARM = 'CompressorReviewer_warm'
FRESH_NAME = 'Compressor_99'


# ── 1. Claim registry ────────────────────────────────────────────────────────


class TestClaimRegistry:
    def setup_method(self):
        _clear_all()

    def teardown_method(self):
        _clear_all()

    def test_first_claim_succeeds_second_fails(self):
        assert try_claim(COMPRESSOR_WARM, 'r1') is True
        assert try_claim(COMPRESSOR_WARM, 'r2') is False  # already owned
        assert claim_holder(COMPRESSOR_WARM) == 'r1'

    def test_same_rid_reclaim_fails_nonreentrant(self):
        # Non-reentrancy is load-bearing for §3f: a per-turn re-acquire must FAIL so the
        # session never re-enters the reset path mid-loop.
        assert try_claim(COMPRESSOR_WARM, 'r1') is True
        assert try_claim(COMPRESSOR_WARM, 'r1') is False

    def test_release_by_wrong_rid_is_noop(self):
        try_claim(COMPRESSOR_WARM, 'r1')
        release_claim(COMPRESSOR_WARM, 'r2')  # wrong rid → must NOT free it
        assert claim_holder(COMPRESSOR_WARM) == 'r1'
        release_claim(COMPRESSOR_WARM, 'r1')  # correct rid → frees it
        assert claim_holder(COMPRESSOR_WARM) is None

    def test_release_allows_reclaim(self):
        try_claim(COMPRESSOR_WARM, 'r1')
        release_claim(COMPRESSOR_WARM, 'r1')
        assert claim_holder(COMPRESSOR_WARM) is None
        assert try_claim(COMPRESSOR_WARM, 'r2') is True
        assert claim_holder(COMPRESSOR_WARM) == 'r2'

    def test_release_twice_is_idempotent(self):
        try_claim(COMPRESSOR_WARM, 'r1')
        release_claim(COMPRESSOR_WARM, 'r1')
        release_claim(COMPRESSOR_WARM, 'r1')  # second release must not raise
        assert claim_holder(COMPRESSOR_WARM) is None

    def test_empty_name_is_noop_everywhere(self):
        assert try_claim('', 'r1') is False
        release_claim('', 'r1')
        assert claim_holder('') is None

    def test_summarizer_and_reviewer_claims_are_independent(self):
        # Distinct warm names → holding the summarizer claim never blocks a review (plan §3a).
        assert try_claim(COMPRESSOR_WARM, 'r1') is True
        assert try_claim(REVIEWER_WARM, 'r2') is True
        assert claim_holder(REVIEWER_WARM) == 'r2'
        release_claim(COMPRESSOR_WARM, 'r1')
        assert claim_holder(REVIEWER_WARM) == 'r2'


# ── 2. Two-branch acquire helper ─────────────────────────────────────────────


def _fake_engine(warm_result=None, warm_raises=None):
    """Fake engine exposing only what acquire_compressor_agent touches."""
    eng = MagicMock()
    if warm_raises is not None:
        eng._acquire_reusable_system_agent.side_effect = warm_raises
    else:
        eng._acquire_reusable_system_agent.return_value = warm_result
    return eng


class TestAcquireHelper:
    def setup_method(self):
        _clear_all()

    def teardown_method(self):
        _clear_all()

    def test_kill_switch_off_never_attempts_warm(self):
        # reuse_name None (the flag-OFF shape): warm path untouched, fresh spawn exactly today.
        eng = _fake_engine()
        inst, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=None, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        eng._acquire_reusable_system_agent.assert_not_called()
        eng._create_system_agent.assert_called_once_with(
            agent_class='Compressor', instance_name=FRESH_NAME, task='t', caller='Maine')
        assert inst is eng._create_system_agent.return_value
        assert reused is False and name == FRESH_NAME

    def test_warm_hit_returns_tuple_passthrough(self):
        warm_inst = object()
        eng = _fake_engine(warm_result=(warm_inst, True, COMPRESSOR_WARM))
        inst, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert inst is warm_inst and reused is True and name == COMPRESSOR_WARM
        eng._create_system_agent.assert_not_called()

    def test_warm_seed_reports_not_reused(self):
        seed_inst = object()
        eng = _fake_engine(warm_result=(seed_inst, False, COMPRESSOR_WARM))
        inst, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert inst is seed_inst and reused is False and name == COMPRESSOR_WARM
        eng._create_system_agent.assert_not_called()

    def test_empty_returned_name_falls_back_to_warm_name(self):
        eng = _fake_engine(warm_result=(object(), True, ''))
        _, _, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert name == COMPRESSOR_WARM

    def test_warm_miss_falls_back_to_fresh_spawn(self):
        eng = _fake_engine(warm_result=None)  # claim held / ineligible / no instance
        inst, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert reused is False and name == FRESH_NAME
        assert inst is eng._create_system_agent.return_value

    def test_magicmock_engine_never_counts_as_warm_hit(self):
        # Known fixture gotcha: a bare-MagicMock ExecutionEngine returns a truthy auto-spec
        # MagicMock from any method — that is NOT our 3-tuple and must not be treated as reuse.
        eng = MagicMock()  # _acquire_reusable_system_agent.return_value is an auto-MagicMock
        acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        eng._create_system_agent.assert_called_once()
        assert eng._create_system_agent.call_args.kwargs['instance_name'] == FRESH_NAME

    def test_bad_shape_returns_none_instance_falls_back(self):
        eng = _fake_engine(warm_result=(None, True, COMPRESSOR_WARM))
        _, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert reused is False and name == FRESH_NAME
        eng._create_system_agent.assert_called_once()

    def test_acquire_exception_degrades_to_fresh_spawn(self):
        eng = _fake_engine(warm_raises=RuntimeError('engine exploded'))
        _, reused, name = acquire_compressor_agent(
            eng, agent_class='Compressor', reuse_name=COMPRESSOR_WARM, fallback_name=FRESH_NAME,
            task='t', caller='Maine', rid='r1')
        assert reused is False and name == FRESH_NAME
        eng._create_system_agent.assert_called_once()


# ── 3. Engine-level warm acquire (Phase 1) ───────────────────────────────────

import threading  # noqa: E402
import time  # noqa: E402

import agent_cascade.compressor_reuse as compressor_reuse  # noqa: E402
import agent_cascade.security_reuse as security_reuse  # noqa: E402
from agent_cascade.agent_instance import AgentInstance, AgentState  # noqa: E402
from agent_cascade.compression.agent_invoker import COMPRESSOR_WARM_NAME  # noqa: E402
from agent_cascade.llm.schema import Message, SYSTEM, USER, ASSISTANT  # noqa: E402

import agent_cascade.compression.agent_invoker as agent_invoker  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _make_warm_compressor(name=COMPRESSOR_WARM_NAME, dirty_tail=False):
    """A clean, eligible warm Compressor. dirty_tail=True adds a stale [user, assistant] tail
    so tests can assert the reuse reset drops it while keeping conversation[0]."""
    conversation = [Message(role=SYSTEM, content='COMPRESSOR_SYS_PREFIX')]
    if dirty_tail:
        conversation += [Message(role=USER, content='old task'),
                         Message(role=ASSISTANT, content='old summary')]
    inst = AgentInstance(
        instance_name=name,
        agent_class='Compressor',
        conversation=conversation,
        created_at=time.monotonic(),
        last_activity=time.monotonic() - 60.0,  # well past the idle epsilon
        latest_marker_index=-1,
    )
    inst._system_prompt_frozen = True
    return inst


def _make_pool(inst=None):
    """Minimal fake pool exposing exactly what _acquire_reusable_system_agent touches.
    Fake pool deliberately has NO .lifecycle attribute (mirrors a real AgentPool)."""
    pool = MagicMock()
    pool.instances = {inst.instance_name: inst} if inst else {}
    pool.message_queues = {}
    exec_obj = MagicMock()
    exec_obj._state_lock = threading.RLock()
    exec_obj.active_stack = []
    pool._execution = exec_obj
    pool.is_instance_halted.return_value = False
    pool.is_instance_terminated.return_value = False
    return pool


def _make_engine(pool):
    from agent_cascade.execution_engine import ExecutionEngine
    engine = ExecutionEngine(pool)
    engine._update_webui_state = MagicMock()
    engine.stream_publisher = MagicMock()
    return engine


def _acquire(engine, name=COMPRESSOR_WARM_NAME, task='summarize', caller='Maine', rid='r1',
             agent_class='Compressor'):
    return engine._acquire_reusable_system_agent(
        agent_class=agent_class, instance_name=name, task=task, caller=caller, rid=rid)


class TestEngineWarmAcquireCompressor:
    def setup_method(self):
        compressor_reuse._clear_all()
        security_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()
        security_reuse._clear_all()

    def test_seed_on_miss_then_reuse(self):
        warm = _make_warm_compressor()
        engine = _make_engine(_make_pool(None))          # empty pool → seed path
        engine._create_system_agent = MagicMock(return_value=warm)
        got = _acquire(engine, task='TASK_A')
        assert got is not None and got[1] is False and got[2] == COMPRESSOR_WARM_NAME
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'r1'  # handed to caller
        compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r1')  # caller's finally
        # Second op: warm instance now exists in the pool under the fixed name.
        engine.pool.instances[COMPRESSOR_WARM_NAME] = warm
        got2 = _acquire(engine, task='TASK_B', rid='r2')
        assert got2 is not None and got2[1] is True
        assert got2[0] is warm and got2[2] == COMPRESSOR_WARM_NAME
        engine._create_system_agent.assert_called_once()  # seeded exactly once

    def test_claims_land_in_compressor_registry_not_security(self):
        engine = _make_engine(_make_pool(_make_warm_compressor()))
        assert _acquire(engine) is not None
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'r1'
        assert security_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_claim_held_returns_none_and_leaves_instance_untouched(self):
        warm = _make_warm_compressor()
        before = list(warm.conversation)
        compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, 'owner_rid')  # concurrent op holds it
        engine = _make_engine(_make_pool(warm))
        assert _acquire(engine, rid='caller_rid') is None  # FIFO fallback → caller spawns fresh
        assert warm.conversation == before               # never mutated by the loser
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'owner_rid'  # not freed

    def test_wrong_class_instance_releases_claim(self):
        warm = _make_warm_compressor()
        warm.agent_class = 'Admin'  # another class owns the fixed name
        engine = _make_engine(_make_pool(warm))
        assert _acquire(engine) is None
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None  # never wedged

    def test_reuse_reset_keeps_conversation0_identity_and_drops_stale_tail(self):
        warm = _make_warm_compressor(dirty_tail=True)
        sys0 = warm.conversation[0]
        warm._state_label = 'stale-state-label'  # must be cleared so warm KV cache is kept
        warm.max_turns = 99
        warm._generate_cfg_override = {'stale': True}
        engine = _make_engine(_make_pool(warm))
        got = _acquire(engine, task='NEW_TASK')
        assert got is not None
        assert warm.conversation[0] is sys0                 # prefix-cache win: same object
        assert warm.conversation[-1].role == USER
        assert 'NEW_TASK' in warm.conversation[-1].content
        assert len(warm.conversation) == 2                  # stale tail dropped
        assert warm._state_label is None                    # warm-KV [STATE_RESTORE_SKIP] branch
        assert warm.max_turns is None and warm._generate_cfg_override is None  # caller re-applies


# ── 4. Invoker wiring (Phase 1) ──────────────────────────────────────────────


def _invoker_pool():
    pool = MagicMock()
    pool.stopped = False
    pool.instance_state = {}
    pool._execution._state_lock = threading.RLock()
    return pool


def _call_compression(pool, *, enabled, warm_result=None):
    """One invoke_compression_agent run with transport + config plumbing mocked."""
    with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', enabled), \
            patch.object(agent_invoker, '_ensure_compressor_loaded'), \
            patch('agent_cascade.execution_engine.ExecutionEngine') as EE, \
            patch.object(agent_invoker, '_configure_compressor_instance') as cfg, \
            patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                         return_value=('SUMMARY', '')) as x, \
            patch.object(compressor_reuse, 'acquire_compressor_agent',
                         return_value=warm_result) as acq, \
            patch.object(compressor_reuse, 'release_claim') as rel:
        result = agent_invoker.invoke_compression_agent(pool, [MagicMock()], caller_name='Maine')
    return result, EE, acq, cfg, x, rel


class TestInvokerCompressionWiring:
    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_flag_off_never_touches_warm_path(self):
        # Kill-switch OFF (the default): no warm attempt at all; fresh name per op exactly today.
        pool = _invoker_pool()
        with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', False), \
                patch.object(agent_invoker, '_ensure_compressor_loaded'), \
                patch('agent_cascade.execution_engine.ExecutionEngine') as EE, \
                patch.object(agent_invoker, '_configure_compressor_instance'), \
                patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                             return_value=('SUMMARY', '')) as x:
            agent_invoker.invoke_compression_agent(pool, [MagicMock()], caller_name='Maine')
        EE.return_value._acquire_reusable_system_agent.assert_not_called()
        fresh_name = EE.return_value._create_system_agent.call_args.kwargs['instance_name']
        assert fresh_name.startswith('Compressor_') and fresh_name != COMPRESSOR_WARM_NAME
        assert x.call_args.args[3] == fresh_name          # state key = fresh name
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_flag_on_warm_hit_reapplies_config_and_state_key(self):
        pool = _invoker_pool()
        warm = _make_warm_compressor()
        warm.max_turns = None               # exactly the post-reset state the engine leaves
        warm._generate_cfg_override = None
        result, EE, acq, cfg, x, rel = _call_compression(
            pool, enabled=True, warm_result=(warm, True, COMPRESSOR_WARM_NAME))
        assert result == ('SUMMARY', '')
        assert acq.call_args.args[2] == COMPRESSOR_WARM_NAME   # warm name passed to the helper
        cfg.assert_called_once()                                # config re-applied after reset
        assert x.call_args.args[2] is warm                      # the WARM instance ran
        assert x.call_args.args[3] == COMPRESSOR_WARM_NAME      # state key re-pointed to warm
        assert warm.max_turns == agent_invoker.COMPRESSOR_AGENT_MAX_TURNS
        rel.assert_called_once_with(COMPRESSOR_WARM_NAME, acq.call_args.args[6])

    def test_warm_claim_released_in_finally_on_hard_failure(self):
        pool = _invoker_pool()
        with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', True), \
                patch.object(agent_invoker, '_ensure_compressor_loaded'), \
                patch('agent_cascade.execution_engine.ExecutionEngine'), \
                patch.object(agent_invoker, '_configure_compressor_instance'), \
                patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                             side_effect=RuntimeError('infra exploded')) as x, \
                patch.object(compressor_reuse, 'acquire_compressor_agent',
                             return_value=(_make_warm_compressor(), True, COMPRESSOR_WARM_NAME)), \
                patch.object(compressor_reuse, 'release_claim') as rel:
            with pytest.raises(RuntimeError):
                agent_invoker.invoke_compression_agent(pool, [MagicMock()], caller_name='Maine')
        rel.assert_called_once()   # finally released even though the run raised

    def test_consolidation_warm_hit_reapplies_config(self):
        pool = _invoker_pool()
        warm = _make_warm_compressor()
        warm.max_turns = None
        warm._generate_cfg_override = None
        with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', True), \
                patch.object(agent_invoker, '_ensure_compressor_loaded'), \
                patch('agent_cascade.execution_engine.ExecutionEngine'), \
                patch.object(agent_invoker, '_configure_compressor_instance') as cfg, \
                patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                             return_value=('CONSOLIDATED', '')) as x, \
                patch.object(compressor_reuse, 'acquire_compressor_agent',
                             return_value=(warm, True, COMPRESSOR_WARM_NAME)), \
                patch.object(compressor_reuse, 'release_claim') as rel:
            summary, _ = agent_invoker.invoke_consolidation_agent(
                pool, ['S1', 'S2'], caller_name='Maine')
        assert summary == 'CONSOLIDATED'
        cfg.assert_called_once()
        assert x.call_args.args[2] is warm and x.call_args.args[3] == COMPRESSOR_WARM_NAME
        assert warm.max_turns == agent_invoker.COMPRESSOR_AGENT_MAX_TURNS
        rel.assert_called_once()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
