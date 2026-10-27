"""BUG_0157 — an empty LLM response must consume the retry budget.

Pre-fix, ``_execute_llm_call_with_retry`` did::

    _got_output = yield from self._stream_llm_attempt(...)
    if _got_output:
        break

with no increment of ``state.retry_count`` and no sleep on the false branch, so
an endpoint that answered with a clean-but-empty stream (e.g. a mid-stream error
payload the backend dropped) was re-hit back-to-back until the 2400 s wall-clock
deadline. In the inciting incident that produced 265 requests in 15 s.

These tests drive the real retry loop via ``tests.phase8_stream_helpers``.
"""

from agent_cascade.telemetry import TelemetryCollector
from tests.phase8_stream_helpers import (
    FakeClock,
    drive_stream,
    end_events,
    extract_system_errors,
    make_stream_engine,
    retrying_messages,
)


def _empty_gen(*args, **kwargs):
    """An attempt that returns no content and raises nothing."""
    return iter(())


def _drive_empty(tmp_path, *, max_attempts=3, deadline_seconds=10000):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)
    engine.pool.settings.retry_max_attempts = max_attempts
    clock = FakeClock(start=1000.0)
    results, execute = drive_stream(
        engine,
        instance,
        _empty_gen,
        terminal_stop=False,
        clock=clock,
        deadline_seconds=deadline_seconds,
    )
    return collector, clock, results, execute


def test_empty_response_is_bounded_by_retry_max_attempts(tmp_path):
    collector, _clock, results, execute = _drive_empty(tmp_path, max_attempts=3)

    assert execute.call_count == 3, (f'empty responses must stop at retry_max_attempts; '
                                     f'got {execute.call_count} attempts')
    errors = extract_system_errors(results)
    assert len(errors) == 1, f'expected one terminal message, got: {errors}'
    assert 'Empty LLM response after 3 attempt(s)' in errors[0]
    # One 'end' per attempt (the empty attempt records telemetry, then retries).
    assert len(end_events(collector)) == 3


def test_empty_response_backs_off_between_attempts(tmp_path):
    _collector, clock, results, _execute = _drive_empty(tmp_path, max_attempts=3)

    retries = retrying_messages(results)
    assert len(retries) == 2, f'expected a [RETRYING] signal per retry, got: {retries}'
    assert all('Empty response from model' in msg for msg in retries)

    sleeps = [seconds for seconds, _remaining in clock.sleep_calls]
    assert len(sleeps) == 2, f'expected one backoff sleep per retry, got: {sleeps}'
    assert all(seconds > 0 for seconds in sleeps), f'backoff must be non-zero: {sleeps}'


def test_empty_response_respects_wall_clock_deadline(tmp_path):
    """A tiny deadline must abort before the retry budget is burned."""
    _collector, _clock, results, execute = _drive_empty(
        tmp_path, max_attempts=3, deadline_seconds=0.01)

    assert execute.call_count == 1, (f'deadline must stop the loop before a second attempt; '
                                     f'got {execute.call_count}')
    errors = extract_system_errors(results)
    assert len(errors) == 1
    assert 'wall-clock deadline' in errors[0]
