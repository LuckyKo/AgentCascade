"""Termination paths of the LLM retry loop must close the telemetry call.

BUG_0128: when an attempt was aborted by ``AgentTerminatedError`` (or the
router's "has been terminated" runtime abort), ``_execute_llm_call_with_retry``
``break``-ed without recording the telemetry ``end``. The stale entry was then
silently overwritten by the next ``record_llm_call_start``, so the aborted
attempt's latency never reached the session/config/class totals.

This drives the real retry loop with a mocked ``_execute_llm_call`` and a real
``TelemetryCollector``.
"""

from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm.schema import Message
from agent_cascade.telemetry import TelemetryCollector
from tests.phase8_stream_helpers import (
    drive_stream,
    end_events,
    extract_system_errors,
    make_stream_engine,
    raise_after_gen,
    retrying_messages,
)


def test_agent_terminated_error_records_one_llm_call_end(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)

    drive_stream(engine, instance, raise_after_gen([], AgentTerminatedError('stopped')))

    events = end_events(collector)
    assert len(events) == 1
    assert events[0]['instance'] == 'test-agent'
    assert collector.get_session_summary()['total_llm_calls'] == 1


def test_termination_abort_runtime_error_records_one_llm_call_end(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)

    drive_stream(engine, instance, raise_after_gen([], RuntimeError('agent has been terminated')))

    events = end_events(collector)
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


def test_agent_terminated_before_output_emits_no_empty_response_error(tmp_path):
    """Termination BEFORE any output: no [SYSTEM ERROR: Empty LLM response],
    no [RETRYING], exactly one telemetry 'end'."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)

    results, _execute = drive_stream(engine, instance, raise_after_gen([], AgentTerminatedError('stopped')))

    assert extract_system_errors(results) == [], (
        f'clean termination must not emit an empty-response error; got: {extract_system_errors(results)}')
    assert not retrying_messages(results), 'a clean termination is not a retry'
    events = end_events(collector)
    assert len(events) == 1, 'a terminated attempt must record exactly one end'


def test_termination_after_partial_commits_partial(tmp_path):
    """Termination AFTER a partial chunk: the partial IS committed (keep-partial),
    exactly one 'end', no empty-response error."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)

    results, _execute = drive_stream(
        engine, instance, raise_after_gen(['partial-response'], AgentTerminatedError('stopped')))

    contents = [m.content for m in results if isinstance(m, Message)]
    assert 'partial-response' in contents, (
        f'keep-partial: a partial present at termination must be committed; got: {contents}')
    assert extract_system_errors(results) == [], (
        'a clean termination after a partial must not emit an empty-response error')
    events = end_events(collector)
    assert len(events) == 1, 'a terminated attempt must record exactly one end'


def test_termination_abort_runtime_error_no_empty_response_message(tmp_path):
    """Router-termination (RuntimeError matching 'has been terminated') BEFORE any output:
    no [SYSTEM ERROR: Empty LLM response], no [RETRYING], exactly one telemetry 'end'.

    Mirrors test_agent_terminated_before_output_emits_no_empty_response_error but exercises
    the _is_termination_abort branch (llm_call.py:914) — the second branch the P8-3 fix
    sets state.error_already_yielded on.
    """
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)

    results, _execute = drive_stream(
        engine, instance,
        raise_after_gen([], RuntimeError("Instance 'test-agent' has been terminated")))

    assert extract_system_errors(results) == [], (
        f'router-termination must not emit an empty-response error; got: {extract_system_errors(results)}')
    assert not retrying_messages(results), 'a clean termination is not a retry'
    events = end_events(collector)
    assert len(events) == 1, 'a terminated attempt must record exactly one end'
