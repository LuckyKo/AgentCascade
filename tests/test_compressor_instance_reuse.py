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

    def test_warm_hit_sets_restricted_shell_pinned(self):
        # Plan §3b caveat, ACCEPT-AND-PIN (build review F-2): the reuse reset sets
        # inst.restricted_shell = True unconditionally (engine/system_agent.py), so a warm
        # Compressor inherits this Security-only flag. It is INERT for the tool-less Compressor
        # (it never builds a shell/tool registry), so the plan's recommendation is to accept it;
        # pinned here so any future change to that line is a deliberate, test-visible decision.
        warm = _make_warm_compressor()
        assert warm.restricted_shell is False  # fresh instances start unrestricted
        engine = _make_engine(_make_pool(warm))
        got = _acquire(engine, task='T')
        assert got is not None and got[1] is True           # warm hit
        assert warm.restricted_shell is True


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


# ── 5. Reviewer warm wiring (Phase 2) ────────────────────────────────────────


def _call_reviewer(pool, *, enabled, warm_result=None, exec_side_effect=None, exec_return=('[PASS]', '')):
    with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', enabled), \
            patch.object(agent_invoker, '_ensure_compressor_loaded'), \
            patch('agent_cascade.execution_engine.ExecutionEngine'), \
            patch.object(agent_invoker, '_configure_reviewer_instance') as cfg, \
            patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                         side_effect=exec_side_effect, return_value=exec_return if exec_side_effect is None else None) as x, \
            patch.object(compressor_reuse, 'acquire_compressor_agent',
                         return_value=warm_result) as acq, \
            patch.object(compressor_reuse, 'release_claim') as rel:
        try:
            result = agent_invoker.invoke_compaction_reviewer(pool, 'IN', 'OUT', 'Maine')
        except RuntimeError as e:
            result = e
    return result, acq, cfg, x, rel


class TestInvokerReviewerWiring:
    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_flag_on_warm_hit_is_toolless_single_shot(self):
        pool = _invoker_pool()
        warm = _make_warm_compressor(name='CompressorReviewer_warm')
        warm.max_turns = 99                       # stale budget from a previous config
        warm._generate_cfg_override = {'disabled_tools': []}  # stale: tools enabled
        result, acq, cfg, x, rel = _call_reviewer(
            pool, enabled=True, warm_result=(warm, True, 'CompressorReviewer_warm'))
        assert result == (True, [])               # [PASS] parsed normally
        assert acq.call_args.args[2] == 'CompressorReviewer_warm'   # distinct warm name
        cfg.assert_called_once()                   # tool-less config re-applied after reset
        assert x.call_args.args[2] is warm
        assert x.call_args.args[3] == 'CompressorReviewer_warm'
        assert warm.max_turns == agent_invoker.COMPACTION_REVIEWER_MAX_TURNS  # stays 1

    def test_flag_off_spawns_fresh_reviewer_per_launch(self):
        # Kill-switch OFF (the default): real helper runs, warm path never attempted, fresh
        # per-launch reviewer name — identical to today.
        pool = _invoker_pool()
        with patch.object(agent_invoker, 'COMPRESSOR_REUSE_ENABLED', False), \
                patch.object(agent_invoker, '_ensure_compressor_loaded'), \
                patch('agent_cascade.execution_engine.ExecutionEngine') as EE, \
                patch.object(agent_invoker, '_configure_reviewer_instance'), \
                patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                             return_value=('[PASS]', '')) as x:
            result = agent_invoker.invoke_compaction_reviewer(pool, 'IN', 'OUT', 'Maine')
        assert result == (True, [])
        EE.return_value._acquire_reusable_system_agent.assert_not_called()
        fresh = EE.return_value._create_system_agent.call_args.kwargs['instance_name']
        assert fresh != 'CompressorReviewer_warm' and fresh.startswith('CompressionReviewer_')
        assert x.call_args.args[3] == fresh
        assert compressor_reuse.claim_holder('CompressorReviewer_warm') is None

    def test_failopen_contract_preserved_and_claim_released(self):
        # Infra failure still raises RuntimeError (so the caller's gate fail-opens) and the
        # warm claim is still released in finally.
        pool = _invoker_pool()
        result, acq, cfg, x, rel = _call_reviewer(
            pool, enabled=True, warm_result=(_make_warm_compressor(name='CompressorReviewer_warm'),
                                             True, 'CompressorReviewer_warm'),
            exec_side_effect=RuntimeError('reviewer infra down'))
        assert isinstance(result, RuntimeError) and 'Compaction review failed' in str(result)
        rel.assert_called_once()

    def test_summarizer_claim_held_does_not_block_reviewer(self):
        # Distinct claims are the point: compressor warm held + reviewer warm free → the
        # reviewer wiring uses the reviewer name regardless (registry-level independence is
        # pinned in TestClaimRegistry; this pins the invoker passes the reviewer name).
        compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, 'r_other')
        try:
            pool = _invoker_pool()
            result, acq, cfg, x, rel = _call_reviewer(
                pool, enabled=True, warm_result=(object(), True, 'CompressorReviewer_warm'))
            assert acq.call_args.args[2] == 'CompressorReviewer_warm'
            assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'r_other'  # untouched
        finally:
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r_other')


# ── 6. §3f warm consolidation session — no-drop re-interrogation ─────────────

from agent_cascade.compressor_reuse import (  # noqa: E402
    WarmCompressorSession, acquire_warm_consolidation)
from agent_cascade.compression.agent_invoker import (  # noqa: E402
    _build_consolidation_prompt, _preserve_items_instruction)
from agent_cascade.compression.core import _validate_and_reconsolidate  # noqa: E402
from agent_cascade.prompts.dna import END_MARKER_INSTRUCTION  # noqa: E402
from agent_cascade.settings import COMPRESSION_REVIEW_MAX_RETRIES  # noqa: E402

_GATE_KW = dict(
    agent_pool=None,  # per-test MagicMock(stopped=False)
    target_agent_name='Target',
    summaries_to_consolidate=['S1', 'S2'],
    summary_spans=[(None, None), (None, None)],
    initial_summary='INITIAL',
)


class TestWarmSessionRefine:
    """(a) No-drop retention through the REAL engine continuation method."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_refine_appends_only_keeps_full_context(self):
        warm = _make_warm_compressor()
        warm.append_message(Message(role=USER, content='ORIG_CONSOLIDATION_TASK'))
        warm.append_message(Message(role=ASSISTANT, content='REJECTED_SUMMARY'))
        assert len(warm.conversation) == 3  # sys + task + rejected answer
        task0, rejected0 = warm.conversation[1], warm.conversation[2]

        pool = _make_pool(warm)
        rid = 'r_sess'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        engine = _make_engine(pool)
        sess = WarmCompressorSession(warm, COMPRESSOR_WARM_NAME, rid, engine, pool, 'Maine')
        # AgentInstance is slotted (instance attrs read-only) → spy at class level.
        with patch.object(agent_invoker, '_execute_compressor_and_extract_summary',
                          return_value=('FIXED_SUMMARY', 'cap')) as x, \
                patch.object(agent_invoker, '_reset_terminated_compressor_for_retry'), \
                patch.object(AgentInstance, 'reset_conversation', autospec=True) as resetm, \
                patch.object(AgentInstance, 'rebuild_conversation', autospec=True) as rebuildm:
            res = sess.refine(['BUG_0330'])
        assert res == ('FIXED_SUMMARY', 'cap')
        # Append-only: original task + rejected summary still present, one new user turn.
        assert len(warm.conversation) == 4
        assert warm.conversation[1] is task0 and warm.conversation[2] is rejected0
        assert warm.conversation[-1].role == USER
        corr = warm.conversation[-1].content
        assert 'BUG_0330' in corr and 'MUST retain them verbatim' in corr
        assert END_MARKER_INSTRUCTION in corr  # output contract preserved (§3f F-3)
        # No reset on the correction path; system prompt frozen; claim kept for the session.
        assert resetm.call_count == 0 and rebuildm.call_count == 0
        assert warm._system_prompt_frozen is True
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == rid
        x.assert_called_once()
        assert x.call_args.args[0] is pool and x.call_args.args[2] is warm
        assert x.call_args.args[3] == COMPRESSOR_WARM_NAME

    def test_refine_refused_after_claim_lost(self):
        # A session whose claim was freed (e.g. stale finally) must not mutate the instance.
        warm = _make_warm_compressor()
        warm.append_message(Message(role=USER, content='TASK'))
        warm.append_message(Message(role=ASSISTANT, content='ANSWER'))
        before = list(warm.conversation)
        rid = 'r_lost'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, rid)  # externally wedged loose
        engine = _make_engine(_make_pool(warm))
        sess = WarmCompressorSession(warm, COMPRESSOR_WARM_NAME, rid, engine, _make_pool(warm), 'Maine')
        assert sess.refine(['x']) is None
        assert warm.conversation == before

    def test_release_is_idempotent(self):
        rid = 'r_rel'
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
        sess = WarmCompressorSession(object(), COMPRESSOR_WARM_NAME, rid, None, None, 'Maine')
        sess.release()
        sess.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None
        assert sess.refine(['x']) is None  # refined after release → refused


class _CountingSession:
    """Explicit-method stub (never MagicMock: auto-attrs would fake the session protocol)."""

    def __init__(self, outputs=None):
        self._outputs = list(outputs or [])
        self.refines = 0

    def refine(self, preserve_items):
        self.refines += 1
        if self._outputs:
            return self._outputs.pop(0)
        return ('ROUND' + str(self.refines), '')


class TestGateWarmSessionLoop:
    """(b) loop bound + best-so-far semantics with a warm session, (c) flag-OFF identity."""

    def _run(self, *, warm_session, review_fn, retries=2, cons_return=('CONSOLIDATED', '')):
        pool = MagicMock(stopped=False)
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', retries), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      return_value=cons_return) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=review_fn) as mr:
            result = _validate_and_reconsolidate(
                **{**_GATE_KW, 'agent_pool': pool, 'warm_session': warm_session})
        return result, mc, mr

    def test_warm_refine_respects_loop_bound_and_beats_fresh_path(self):
        sess = _CountingSession()
        # INITIAL: 3 flagged; warm rounds: 2 then 2 (tie → latest wins the best-so-far slot).
        def review(**kwargs):
            counts = {'INITIAL': 3, 'ROUND1': 2, 'ROUND2': 2}
            return (False, ['i%d' % n for n in range(counts[kwargs['outgoing_summary']])])
        result, mc, mr = self._run(warm_session=sess, review_fn=review)
        assert result == 'ROUND2'                 # best-so-far semantics unchanged (ties → latest)
        assert sess.refines == 2                  # == COMPRESSION_REVIEW_MAX_RETRIES, not exceeded
        assert mc.call_count == 0                 # warm healthy → fresh-respawn never used
        assert mr.call_count == 3                 # INITIAL + both warm rounds reviewed

    def test_bound_matches_production_setting(self):
        sess = _CountingSession()
        result, mc, mr = self._run(warm_session=sess, review_fn=lambda **kw: (False, ['x']),
                                   retries=COMPRESSION_REVIEW_MAX_RETRIES)
        assert sess.refines == COMPRESSION_REVIEW_MAX_RETRIES

    def test_warm_degrade_falls_back_to_fresh_respawn(self):
        # First refine unusable → this AND remaining retries take the byte-identical fresh path.
        class _DeadSession(_CountingSession):
            def refine(self, preserve_items):
                self.refines += 1
                return None
        sess = _DeadSession()
        seen = []

        def cons(**kwargs):
            seen.append(kwargs.get('preserve_items'))
            return ('FRESH_ROUND', '')
        pool = MagicMock(stopped=False)
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 2), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=cons) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=lambda **kw: (False, ['a'])) as mr:
            result = _validate_and_reconsolidate(
                **{**_GATE_KW, 'agent_pool': pool, 'warm_session': sess})
        assert result == 'FRESH_ROUND'            # best-so-far updated by fresh round (tie→latest)
        assert sess.refines == 1                  # degrades permanently after first failure
        assert mc.call_count == 2                 # both retries take the fresh branch
        assert seen == [['a'], ['a']]             # fresh path pins preserve_items as before

    def test_flag_off_acquire_returns_none_and_gate_takes_fresh_branch(self):
        # (c) Kill-switch OFF: session open short-circuits before touching the registry, and the
        # gate behaves exactly like the pre-BUG_0330 code (fresh respawn with preserve_items).
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', False):
            assert acquire_warm_consolidation(MagicMock(), ['S1'], 'Caller') is None
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None  # registry untouched
        seen = []

        def cons(**kwargs):
            seen.append(kwargs.get('preserve_items'))
            return ('FRESH', '')
        pool = MagicMock(stopped=False)
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 1), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=cons) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=lambda **kw: (False, ['z'])):
            result = _validate_and_reconsolidate(**{**_GATE_KW, 'agent_pool': pool})  # no session
        assert result == 'FRESH'
        assert mc.call_count == 1 and seen == [['z']]
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_shared_builders_are_deterministic_and_pinned(self):
        # Single-source prompt builders (§3f): byte-identical repeats + preserve wording in both.
        p1 = _build_consolidation_prompt(['S1', 'S2'], None)
        p2 = _build_consolidation_prompt(['S1', 'S2'], None)
        assert p1 == p2 and 'MUST retain them verbatim' not in p1
        p3 = _build_consolidation_prompt(['S1'], None, ['KEEP_ME'])
        assert p3 != p1 and 'MUST retain them verbatim' in p3 and 'KEEP_ME' in p3
        assert _preserve_items_instruction([]) == '' and _preserve_items_instruction(None) == ''
        assert 'KEEP_ME' in _preserve_items_instruction([' KEEP_ME '])


class TestSessionAcquireForeignClaim:
    """(d) A foreign held claim must not wedge warm consolidation — no-drop never starves."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_foreign_claim_held_returns_none_foreign_kept_instance_untouched(self):
        warm = _make_warm_compressor()
        warm.append_message(Message(role=USER, content='TASK'))
        warm.append_message(Message(role=ASSISTANT, content='ANSWER'))
        before = list(warm.conversation)
        assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, 'foreign_rid')
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', True):
            sess = acquire_warm_consolidation(_make_pool(warm), ['S1', 'S2'], 'Caller',
                                              marker_time_ranges=[(None, None), (None, None)])
        assert sess is None                       # caller degrades to today's fresh path
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) == 'foreign_rid'  # no cross-free
        assert warm.conversation == before        # never mutated by the loser
        # No wedge: once the foreign owner releases, the very next call opens a session.
        compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'foreign_rid')
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', True):
            sess2 = acquire_warm_consolidation(_make_pool(warm), ['S1'], 'Caller')
        assert sess2 is not None and sess2.actual_name == COMPRESSOR_WARM_NAME
        sess2.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_flag_off_never_touches_registry_or_engine(self):
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_ENABLED', False):
            assert acquire_warm_consolidation(MagicMock(), ['S'], 'C') is None
        # (registry untouched == nothing claimable was ever recorded)
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None


# ── 7. Re-mint on COMPRESSOR_REUSE_MAX_REUSES (Phase 3) ──────────────────────

from agent_cascade.settings import COMPRESSOR_REUSE_MAX_REUSES  # noqa: E402


class TestRemintReuses:
    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def test_default_is_off(self):
        assert COMPRESSOR_REUSE_MAX_REUSES == 0

    def test_max_reuses_zero_never_retires(self):
        warm = _make_warm_compressor()
        engine = _make_engine(_make_pool(warm))
        pool = engine.pool
        pool.instances[COMPRESSOR_WARM_NAME] = warm
        for i in range(4):
            got = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', COMPRESSOR_WARM_NAME, 'Compressor_x', 't', 'Maine', f'r{i}')
            assert got[1] is True
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, f'r{i}')
        assert COMPRESSOR_WARM_NAME in pool.instances  # no retirement at 0

    def test_re_mints_after_n_reuses(self):
        warm = _make_warm_compressor()
        engine = _make_engine(_make_pool(None))   # empty pool → first call seeds
        pool = engine.pool
        engine._create_system_agent = MagicMock(return_value=warm)
        with patch.object(compressor_reuse, 'COMPRESSOR_REUSE_MAX_REUSES', 2):
            # Seed (not counted) + simulate the registration the real _create_system_agent does.
            got = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', COMPRESSOR_WARM_NAME, 'Compressor_x', 't', 'Maine', 'r0')
            assert (got[0], got[1], got[2]) == (warm, False, COMPRESSOR_WARM_NAME)
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r0')
            pool.instances[COMPRESSOR_WARM_NAME] = warm
            # Reuse 1 < 2: kept.
            got = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', COMPRESSOR_WARM_NAME, 'Compressor_x', 't', 'Maine', 'r1')
            assert got[1] is True
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r1')
            assert COMPRESSOR_WARM_NAME in pool.instances
            # Reuse 2 == 2: retire the pool slot.
            got = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', COMPRESSOR_WARM_NAME, 'Compressor_x', 't', 'Maine', 'r2')
            assert got[1] is True
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r2')
            assert COMPRESSOR_WARM_NAME not in pool.instances
            # Next op re-seeds a brand-new warm instance under the same fixed name.
            fresh = _make_warm_compressor()
            engine._create_system_agent = MagicMock(return_value=fresh)
            got = compressor_reuse.acquire_compressor_agent(
                engine, 'Compressor', COMPRESSOR_WARM_NAME, 'Compressor_x', 't', 'Maine', 'r3')
            assert (got[0], got[1], got[2]) == (fresh, False, COMPRESSOR_WARM_NAME)
            compressor_reuse.release_claim(COMPRESSOR_WARM_NAME, 'r3')


# ── 8. Gate-level claim release across all exit paths (build review F-3) ─────

from agent_cascade import settings as _settings  # noqa: E402
from agent_cascade.agent_pool import AgentPool as _AgentPool  # noqa: E402
from agent_cascade.compression import core as _core  # noqa: E402
from agent_cascade.compression.core import _consolidate_markers  # noqa: E402


def _target_inst(name):
    """Real AgentInstance with enough history that the patched marker indices are valid."""
    inst = _make_warm_compressor(name=name)
    for i in range(8):
        inst.append_message(Message(role=USER, content='msg%d' % i))
    return inst


def _real_session(pool, rid='f3_rid'):
    """Real WarmCompressorSession riding REAL claim-registry state (never a bare MagicMock:
    auto-attrs would fake the held/released distinction). engine._continue_... returns False
    so gate refines degrade to the fresh-respawn branch without LLM plumbing."""
    warm = _make_warm_compressor()
    assert compressor_reuse.try_claim(COMPRESSOR_WARM_NAME, rid)
    engine = MagicMock()
    engine._continue_reusable_system_agent_turn.return_value = False
    return compressor_reuse.WarmCompressorSession(
        warm, COMPRESSOR_WARM_NAME, rid, engine, pool, 'Caller')


class TestConsolidateMarkersClaimRelease:
    """F-3: the ONE finally in _consolidate_markers Phase 2 (core.py) must release a live
    warm claim on every exit: gate pass / retry exhaustion / agent_pool.stopped / exception."""

    def setup_method(self):
        compressor_reuse._clear_all()

    def teardown_method(self):
        compressor_reuse._clear_all()

    def _drive(self, *, target, review_fn, cons_exc=None, stopped=False):
        """Run one _consolidate_markers end-to-end with a live held session claim."""
        pool = _invoker_pool()
        pool.stopped = stopped
        pool.get_instance.return_value = _target_inst(target)
        session = _real_session(pool)
        held = []  # observed claim-holder while the gate actually ran
        real_release = compressor_reuse.release_claim  # capture before patching (no recursion)
        def _review(**kw):
            held.append(compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME))
            return review_fn(**kw)
        with patch.object(compressor_reuse, 'acquire_warm_consolidation', return_value=session), \
                patch.object(_settings, 'COMPRESSION_CONSOLIDATION_THRESHOLD', 3), \
                patch.object(_core, 'select_markers_for_consolidation',
                             return_value=([2, 5], 8)), \
                patch.object(_core, 'extract_summary_from_marker',
                             return_value='S1'), \
                patch.object(_core, 'get_marker_time_range',
                             return_value=(0.0, 1.0)), \
                patch.object(_AgentPool, 'find_all_marker_indices', return_value=[2, 5, 8]), \
                patch.object(compressor_reuse, 'release_claim',
                             side_effect=real_release) as rel, \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      side_effect=cons_exc if cons_exc is not None
                      else (lambda **kw: ('CONSOLIDATED', ''))) as mc, \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', True), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', 2), \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      side_effect=_review):
            _consolidate_markers(pool, target)
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None  # THE point: no wedge
        assert compressor_reuse.claim_holder(REVIEWER_WARM) is None
        rel.assert_called_once_with(COMPRESSOR_WARM_NAME, 'f3_rid')  # released exactly once
        return session, held, mc

    def test_pass_path_releases(self):
        session, held, mc = self._drive(
            target='t_pass', review_fn=lambda **kw: (True, []))
        assert held and all(h == 'f3_rid' for h in held)  # claim stayed live during the gate
        session.release()  # idempotent: second release must not raise or cross-free
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_retry_exhaustion_releases(self):
        session, held, mc = self._drive(
            target='t_exhaust', review_fn=lambda **kw: (False, ['x']))
        assert len(held) == 3  # initial + both retries (== MAX_RETRIES bound)
        assert all(h == 'f3_rid' for h in held)
        session.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_pool_stopped_releases(self):
        session, held, mc = self._drive(
            target='t_stopped', review_fn=lambda **kw: (False, ['x']), stopped=True)
        assert len(held) == 1  # initial review only — retry loop bails on pool.stopped
        session.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None

    def test_consolidation_exception_releases(self):
        session, held, mc = self._drive(
            target='t_exc', review_fn=lambda **kw: (True, []),
            cons_exc=RuntimeError('boom'))
        assert held == []          # never reached the gate
        assert mc.call_count == 1  # single Phase-2 attempt, then early return
        session.release()
        assert compressor_reuse.claim_holder(COMPRESSOR_WARM_NAME) is None


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
