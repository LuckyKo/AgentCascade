"""Tests for exposing ``COMPRESSION_REVIEW_MAX_RETRIES`` as a UI setting (BUG_0419).

Covers three seams of the feature (clone of tests/test_auto_skill_min_turns_setting.py):

1. The config handler (``config_handlers._handle_compression_review_max_retries``) — clamp to
   0..5, with the critical BUG_0419 decision-2 case: **0 stays 0** (it is a legit "gate off"
   value, NOT clamped up to 1 like a sibling ``max(1, …)`` handler would do).
2. The ``PoolSettings.compression_review_max_retries`` dataclass field — default matches the
   settings constant + persistence round-trip (auto-persists via asdict/from_dict).
3. The live gate at ``compression/core.py::_validate_and_reconsolidate`` — proves that when the
   live ``COMPRESSION_REVIEW_MAX_RETRIES`` is 0 the gate is a byte-identical no-op (no initial
   review LLM call, no re-consolidation, no LOSS telemetry), i.e. 0 == the reviewer gate being
   off (decision 1). A non-zero value must still run the gate (guards the `== 0` short-circuit
   against over-firing).

Related: plans/BUG_0419_compression-review-retries-ui-plan.md.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from agent_cascade.agent_instance import PoolSettings


@contextmanager
def _restored_review_globals():
    """Restore the two consumer module globals so a handler apply() never leaks past a test.

    The handler calls ``apply_compression_review_max_retries``, which mutates
    ``compression.core.COMPRESSION_REVIEW_MAX_RETRIES`` and
    ``compression.agent_invoker.COMPRESSION_REVIEW_MAX_RETRIES`` at module scope. Snapshot both
    and restore on exit so the write stays local to the test.
    """
    import agent_cascade.compression.agent_invoker as ai
    import agent_cascade.compression.core as core

    saved = (core.COMPRESSION_REVIEW_MAX_RETRIES, ai.COMPRESSION_REVIEW_MAX_RETRIES)
    try:
        yield
    finally:
        core.COMPRESSION_REVIEW_MAX_RETRIES, ai.COMPRESSION_REVIEW_MAX_RETRIES = saved


class TestHandlerCompressionReviewMaxRetries:
    """Validation of the handler clamp (0..5) + 0-tolerant decision."""

    def _run_handler(self, value):
        from agent_cascade.config_handlers import CONFIG_HANDLERS

        pool = MagicMock()
        pool.settings = PoolSettings()
        with _restored_review_globals():
            CONFIG_HANDLERS['compression_review_max_retries']({'compression_review_max_retries': value}, pool, [])
        return pool.settings.compression_review_max_retries

    def test_handler_valid_values(self):
        assert self._run_handler(1) == 1
        assert self._run_handler(2) == 2
        assert self._run_handler(5) == 5

    def test_handler_zero_stays_zero_not_one(self):
        # Decision 2: clamp is max(0, …) NOT max(1, …) — 0 is a valid "gate off" value.
        assert self._run_handler(0) == 0

    def test_handler_clamps_negative_to_zero(self):
        assert self._run_handler(-5) == 0

    def test_handler_clamps_high_to_five(self):
        assert self._run_handler(6) == 5
        assert self._run_handler(9999) == 5


class TestPoolSettingsField:
    """The ``compression_review_max_retries`` dataclass field default + persistence round-trip."""

    def test_default_matches_constant(self):
        from agent_cascade.settings import COMPRESSION_REVIEW_MAX_RETRIES

        assert PoolSettings().compression_review_max_retries == COMPRESSION_REVIEW_MAX_RETRIES

    def test_from_dict_round_trip(self):
        ps = PoolSettings(compression_review_max_retries=0)  # 0 is a legit persisted value
        d = ps.to_dict()
        assert d['compression_review_max_retries'] == 0
        restored = PoolSettings.from_dict(d)
        assert restored.compression_review_max_retries == 0

    def test_from_dict_missing_key_defaults_to_constant(self):
        # Old pool_settings.json files lack the key → from_dict fills it with the default.
        from agent_cascade.settings import COMPRESSION_REVIEW_MAX_RETRIES

        d = PoolSettings().to_dict()
        d.pop('compression_review_max_retries', None)
        ps = PoolSettings.from_dict(d)
        assert ps.compression_review_max_retries == COMPRESSION_REVIEW_MAX_RETRIES


class TestLiveGateZeroIsNoOp:
    """Prove the gate is byte-identical when COMPRESSION_REVIEW_MAX_RETRIES == 0 (decision 1)."""

    _KW = dict(
        target_agent_name='Target',
        summaries_to_consolidate=['S1', 'S2'],
        summary_spans=[(None, None), (None, None)],
        initial_summary='INITIAL',
    )

    def _run_gate(self, retries, *, enabled=True):
        from agent_cascade.compression.core import _validate_and_reconsolidate

        pool = MagicMock(stopped=False)
        # Reviewer always returns LOSS: without the 0 short-circuit this would force a retry.
        with patch('agent_cascade.compression.core.COMPRESSION_REVIEWER_ENABLED', enabled), \
                patch('agent_cascade.compression.core.COMPRESSION_REVIEW_MAX_RETRIES', retries), \
                patch('agent_cascade.compression.agent_invoker.invoke_consolidation_agent',
                      return_value=('CONSOLIDATED', '')) as mc, \
                patch('agent_cascade.compression.agent_invoker.invoke_compaction_reviewer',
                      return_value=(False, ['BUG_0419'])) as mr:
            result, _ = _validate_and_reconsolidate(agent_pool=pool, **self._KW)
        return result, mc, mr

    def test_zero_retries_is_byte_identical_noop(self):
        # Enabled gate + retries=0 → early return. No initial review, no re-consolidation.
        result, mc, mr = self._run_gate(0, enabled=True)
        assert result == 'INITIAL'
        assert mr.call_count == 0  # even the FIRST review is skipped
        assert mc.call_count == 0  # no re-consolidation

    def test_nonzero_retries_still_runs_the_gate(self):
        # Sanity guard against the `== 0` check over-firing: a non-zero value still reviews
        # and re-consolidates on a LOSS verdict.
        result, mc, mr = self._run_gate(2, enabled=True)
        assert mr.call_count >= 2  # initial review + at least one retry review
        assert mc.call_count >= 1  # at least one re-consolidation was attempted

    def test_gate_disabled_path_unchanged(self):
        # COMPRESSION_REVIEWER_ENABLED False → identical no-op regardless of retries (regression
        # guard: the extended `or` must not break the existing master-toggle path).
        result, mc, mr = self._run_gate(2, enabled=False)
        assert result == 'INITIAL'
        assert mr.call_count == 0 and mc.call_count == 0
