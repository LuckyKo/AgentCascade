"""BUG_0205 (part a): surface the loop-detector termination reason in call_agent feedback.

Covers:
- The loop detector records a machine-readable ``inst._terminated_reason`` before it
  terminates an instance (both Tier-1 and Tier-2 write sites).
- ``child_runner._termination_reason`` reports that reason (``loop-stuck: ...``) and
  still falls back to the honest default for a bare ``MagicMock`` instance (mock-parity
  guard — MagicMock auto-creates truthy attributes).
- ``run_child_core`` surfaces the reason through the raised ``ChildAgentFailedError``
  and it survives ``format_crash``'s 160-char truncation (the parent-facing seam).

Self-contained — no LLM or API server required.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.child_runner import ChildAgentFailedError, _termination_reason, run_child_core
from agent_cascade.error_reporting import format_crash


# ──────────────────────────────────────────────
# Fixtures — mirror tests/test_loop_detection.py::TestMaxAutoRollbacksEnforcement
# ──────────────────────────────────────────────


def _make_fake_instance(name='test_agent', rollback_count=0):
    """Plain-class fake AgentInstance (MagicMock would poison getattr gates)."""

    class FakeInstance:

        def __init__(self, instance_name, rollback_count):
            self.instance_name = instance_name
            if rollback_count >= 0:
                self._loop_rollback_count = rollback_count

    return FakeInstance(name, rollback_count)


def _make_fake_pool(max_auto_rollbacks=3, auto_rollback_on_loop=True):
    pool = MagicMock()
    pool.settings = MagicMock()
    pool.settings.max_auto_rollbacks = max_auto_rollbacks
    pool.settings.auto_rollback_on_loop = auto_rollback_on_loop
    return pool


def _make_engine(pool):
    from agent_cascade.execution_engine import ExecutionEngine

    engine = MagicMock(spec=ExecutionEngine)
    engine.pool = pool
    engine.compression_handler = MagicMock()
    engine.compression_handler.handle_rollback_command.return_value = False
    engine.compression_handler.handle_compress_command.return_value = False
    # Bind the REAL _pre_llm_checks so we exercise actual wiring.
    engine._pre_llm_checks = ExecutionEngine._pre_llm_checks.__get__(engine, ExecutionEngine)
    engine._consume_turn = ExecutionEngine._consume_turn.__get__(engine, ExecutionEngine)
    engine._check_stop_conditions = MagicMock(return_value=False)
    engine._inject_async_messages = MagicMock(return_value=False)
    engine._check_and_trigger_compression = MagicMock(return_value=False)
    engine._telemetry = MagicMock(return_value=None)
    engine._append_and_log = MagicMock()
    engine._inline_rollback_and_hint = MagicMock()
    return engine


def _repeating_msgs():
    from agent_cascade.llm.schema import ASSISTANT, USER, Message

    def _m(role, content):
        return Message(role=role, content=content, reasoning_content=None, function_call=None)

    return [_m(USER, 'q'), _m(ASSISTANT, 'a'),
            _m(USER, 'q'), _m(ASSISTANT, 'a'),
            _m(USER, 'q'), _m(ASSISTANT, 'a')]


# ══════════════════════════════════════════════
# 1. _pre_llm_checks records the reason on exhaustion
# ══════════════════════════════════════════════


def test_pre_llm_checks_records_terminated_reason_on_exhaustion():
    """Forcing _pre_llm_checks past max_auto_rollbacks sets inst._terminated_reason."""
    pool = _make_fake_pool(max_auto_rollbacks=2, auto_rollback_on_loop=True)
    engine = _make_engine(pool)
    inst = _make_fake_instance('test_agent')
    msgs = _repeating_msgs()

    # Three detections: rollbacks 1, 2 (within limit) then 3 (> 2) → terminate.
    for _ in range(3):
        turns = [50]
        with patch('agent_cascade.engine.llm_call._detect_exact_loop', return_value=('repeat', 2)):
            engine._pre_llm_checks(inst, msgs, [], [], turns)

    pool.terminate_instance.assert_called_once()
    # The reason must be recorded before the terminate call, and carry the rollback count + pattern.
    assert hasattr(inst, '_terminated_reason'), 'BUG_0205: reason must be recorded on exhaustion'
    reason = inst._terminated_reason
    assert isinstance(reason, str) and reason
    assert '3 rollbacks' in reason, f'rollback count missing from reason: {reason!r}'
    assert 'max_auto_rollbacks=2' in reason, f'limit missing from reason: {reason!r}'
    assert 'repeat' in reason, f'pattern missing from reason: {reason!r}'


# ══════════════════════════════════════════════
# 2. _termination_reason reports the loop reason (with mock-parity guard)
# ══════════════════════════════════════════════


def test_termination_reason_reports_loop_reason():
    """With _terminated_reason set on a real instance, the loop reason wins over the fallback."""
    pool = MagicMock()
    inst = _make_fake_instance('child')
    inst._terminated_reason = 'exhausted loop-recovery budget (4 rollbacks, max_auto_rollbacks=3); pattern: repeat'

    got = _termination_reason(pool, 'child', inst, was_stopped=False, result='stale prose')
    assert got.startswith('loop-stuck:'), f'expected loop-stuck reason, got: {got!r}'
    assert 'exhausted loop-recovery budget' in got


def test_termination_reason_falls_back_for_mock_instance():
    """A bare MagicMock auto-creates a truthy _terminated_reason (a MagicMock, not a str),
    so the strict isinstance guard must fall through to the honest default (mock-parity)."""
    pool = MagicMock()
    mock_inst = MagicMock()
    # Simulate: no explicit _terminated_reason set → auto-created MagicMock attribute.
    # (MagicMock auto-creates on access, so getattr returns a MagicMock, not a str.)

    got = _termination_reason(pool, 'child', mock_inst, was_stopped=False, result='stale prose')
    # Must NOT be the loop-stuck branch — the fallback is returned.
    assert got.startswith('terminated with no explicit reason recorded'), f'got: {got!r}'


def test_termination_reason_none_inst_falls_back():
    """inst is None → no reason can be read → fallback."""
    pool = MagicMock()
    got = _termination_reason(pool, 'child', None, was_stopped=False, result='stale prose')
    assert got.startswith('terminated with no explicit reason recorded'), f'got: {got!r}'


# ══════════════════════════════════════════════
# 3. e2e-ish: run_child_core surfaces the reason through the raised error
# ══════════════════════════════════════════════


def test_run_child_core_surfaces_loop_reason_through_format_crash():
    """A loop-terminated child raises ChildAgentFailedError whose Reason: line carries
    the loop-stuck cause, and that cause survives format_crash's 160-char cut."""
    from agent_cascade.agent_instance import AgentInstance, AgentState

    inst = AgentInstance(
        instance_name='child', agent_class='coder', conversation=[],
        created_at=0.0, last_activity=0.0, latest_marker_index=-1,
    )
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = 'exhausted loop-recovery budget (4 rollbacks, max_auto_rollbacks=3); pattern: repeat'

    pool = MagicMock()
    pool.stopped = False
    pool._halted_instances = set()
    pool._compression_halted = set()
    pool.terminated_instances = {'child'}
    pool.get_instance.return_value = inst
    pool.instance_loggers = None

    conv = [{'role': 'assistant', 'content': 'stale prose'}]
    engine = MagicMock()
    engine._create_and_run_agent.return_value = (inst, conv)

    with patch('agent_cascade.child_runner.extract_instance_output', return_value='stale prose'):
        with pytest.raises(ChildAgentFailedError) as exc_info:
            run_child_core(engine, pool, 'coder', 'child', {}, 'Main', 1)

    msg = str(exc_info.value)
    assert 'Reason: loop-stuck:' in msg, f'loop-stuck reason missing from raw message: {msg!r}'
    # Parent-facing seam: the cause must survive format_crash's 160-char truncation.
    rendered = format_crash(exc_info.value)
    assert 'loop-stuck:' in rendered, f'loop-stuck reason lost to format_crash truncation: {rendered!r}'
    assert len(rendered) <= 160 + len('ChildAgentFailedError: ') + 1, f'rendered too long: {len(rendered)}'
