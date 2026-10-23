"""Termination paths of the LLM retry loop must close the telemetry call.

BUG_0128: when an attempt was aborted by ``AgentTerminatedError`` (or the
router's "has been terminated" runtime abort), ``_execute_llm_call_with_retry``
``break``-ed without recording the telemetry ``end``. The stale entry was then
silently overwritten by the next ``record_llm_call_start``, so the aborted
attempt's latency never reached the session/config/class totals.

This drives the real retry loop with a mocked ``_execute_llm_call`` and a real
``TelemetryCollector``.
"""

from unittest.mock import MagicMock, patch

from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.llm.schema import ASSISTANT, Message
from agent_cascade.telemetry import TelemetryCollector


class _Settings:
    retry_max_attempts = 5
    retry_base_delay = 0.1
    retry_max_delay = 1.0
    loop_min_chars = 4000
    loop_max_chars = 40960
    loop_char_run_enabled = True
    loop_char_run_limit = 129
    loop_max_chars_enabled = True
    loop_two_phase_enabled = False
    loop_suspicion_threshold = 7
    loop_confirm_required = 3
    loop_cooldown_feeds = 50


def _make_engine(collector):
    pool = MagicMock()
    pool.telemetry = collector
    pool.stopped = False
    pool.is_instance_terminated.return_value = False
    pool._run_generation = 1
    pool.settings = _Settings()

    instance = MagicMock()
    instance.instance_name = 'test-agent'
    lock = MagicMock()
    lock.__enter__ = MagicMock()
    lock.__exit__ = MagicMock()
    instance._compression_lock = lock
    instance._streaming_responses = []

    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    return engine, instance


def _template():
    t = MagicMock()
    t.llm_cfg = {'model': 'test'}
    t.function_map = {}
    t.llm.generate_cfg = {}
    t.llm.model = 'test-model'
    return t


def _drive(engine, instance, exc):
    def mock_execute(*args, **kwargs):
        raise exc
        yield  # pragma: no cover — makes the function a generator like the real one

    with patch.object(engine, '_execute_llm_call', side_effect=mock_execute):
        return list(engine._execute_llm_call_with_retry(
            instance, [Message(role='user', content='hi')], _template(), []))


def test_agent_terminated_error_records_one_llm_call_end(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = _make_engine(collector)

    _drive(engine, instance, AgentTerminatedError('stopped'))

    events = [e for e in list(collector.events) if e.get('type') == 'llm_call']
    assert len(events) == 1
    assert events[0]['instance'] == 'test-agent'
    assert collector.get_session_summary()['total_llm_calls'] == 1


def test_termination_abort_runtime_error_records_one_llm_call_end(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = _make_engine(collector)

    _drive(engine, instance, RuntimeError('agent has been terminated'))

    events = [e for e in list(collector.events) if e.get('type') == 'llm_call']
    assert len(events) == 1
    assert collector.get_session_summary()['total_llm_calls'] == 1


# ──────────────────────────────────────────────────────────────────────────────
# P8-3 — a clean termination must NOT surface the generic empty-response error.
#
# A termination branch in _handle_llm_retry_failure returns True (break the retry
# loop) but never sets state.error_already_yielded. _finalize_llm_result then sees
# last_output is None and emits "[SYSTEM ERROR: Empty LLM response]" (llm_call.py:549)
# even though the instance terminated cleanly (no error). Setting
# error_already_yielded=True in both termination branches suppresses it.
# ──────────────────────────────────────────────────────────────────────────────


def _system_errors(results):
    return [m.content for m in results
            if isinstance(m, Message) and isinstance(m.content, str)
            and m.content.startswith('[SYSTEM ERROR')]


def _drive_after_partial(engine, instance, exc, content='partial-response'):
    """Generator that yields one chunk (sets last_output) then raises ``exc``."""
    def mock_execute(*args, **kwargs):
        yield [Message(role=ASSISTANT, content=content)]
        raise exc
        yield  # pragma: no cover — keeps it a generator like the real one

    with patch.object(engine, '_execute_llm_call', side_effect=mock_execute):
        return list(engine._execute_llm_call_with_retry(
            instance, [Message(role='user', content='hi')], _template(), []))


def test_agent_terminated_before_output_emits_no_empty_response_error(tmp_path):
    """Termination BEFORE any output: no [SYSTEM ERROR: Empty LLM response],
    no [RETRYING], exactly one telemetry 'end'."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = _make_engine(collector)

    results = _drive(engine, instance, AgentTerminatedError('stopped'))

    assert _system_errors(results) == [], (
        f'clean termination must not emit an empty-response error; got: {_system_errors(results)}')
    assert not any(isinstance(m, Message) and isinstance(m.content, str)
                   and m.content.startswith('[RETRYING]') for m in results), \
        'a clean termination is not a retry'
    events = [e for e in list(collector.events) if e.get('type') == 'llm_call']
    assert len(events) == 1, 'a terminated attempt must record exactly one end'


def test_termination_after_partial_commits_partial(tmp_path):
    """Termination AFTER a partial chunk: the partial IS committed (keep-partial),
    exactly one 'end', no empty-response error."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = _make_engine(collector)

    results = _drive_after_partial(engine, instance, AgentTerminatedError('stopped'))

    contents = [m.content for m in results if isinstance(m, Message)]
    assert 'partial-response' in contents, (
        f'keep-partial: a partial present at termination must be committed; got: {contents}')
    assert _system_errors(results) == [], (
        'a clean termination after a partial must not emit an empty-response error')
    events = [e for e in list(collector.events) if e.get('type') == 'llm_call']
    assert len(events) == 1, 'a terminated attempt must record exactly one end'
