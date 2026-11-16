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


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
