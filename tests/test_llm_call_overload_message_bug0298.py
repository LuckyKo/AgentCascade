"""BUG_0298 — the terminal overload message must match the actual error.

The pre-fix guard hardcoded ``[SYSTEM ERROR: LLM endpoint overloaded (HTTP 429)
after N attempts — context cache exhausted]`` for TWO different classes:

- ``ServerBusyError``: every base was breaker-open, so the router sent ZERO
  requests. Nothing was rate limited and no context cache was involved.
- A genuine HTTP 429 from the provider.

That sent the user chasing a context-cache problem during a model-load storm
(2026-11-12 03:57 incident). The surfaced text is now derived from the error:
busy -> "LLM server busy ... no endpoint was reachable"; genuine 429 -> the
rate-limit text.

Driven through the real retry loop via ``tests.phase8_stream_helpers``.
"""

import pytest

from agent_cascade.exceptions import ServerBusyError
from agent_cascade.llm.base import ModelServiceError
from agent_cascade.telemetry import TelemetryCollector
from tests.phase8_stream_helpers import (
    drive_stream,
    extract_system_errors,
    make_stream_engine,
)


def _raises(exc):
    """An attempt that blows up before yielding anything."""

    def gen(*args, **kwargs):
        raise exc
        yield  # pragma: no cover — keeps this a generator like the real one

    return gen


def _drive(tmp_path, exc, *, max_attempts=1):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)
    engine.pool.settings.retry_max_attempts = max_attempts
    results, _execute = drive_stream(engine, instance, _raises(exc), terminal_stop=False)
    return extract_system_errors(results)


def test_server_busy_does_not_claim_http_429_or_context_cache(tmp_path):
    errors = _drive(tmp_path, ServerBusyError('all endpoints busy'))

    assert len(errors) == 1, errors
    msg = errors[0]
    assert 'LLM server busy' in msg
    assert '429' not in msg, msg
    assert 'context cache' not in msg, msg


def test_genuine_429_reports_rate_limit(tmp_path):
    errors = _drive(tmp_path, ModelServiceError(code='429', message='slow down'))

    assert len(errors) == 1, errors
    msg = errors[0]
    assert 'rate-limited (HTTP 429)' in msg
    assert 'context cache' not in msg, msg


def test_non_rate_limit_error_is_not_labelled_429(tmp_path):
    """A :4290-style port mention must not trigger the overload guard."""
    errors = _drive(tmp_path, RuntimeError('connect failed to http://127.0.0.1:4290/v1'),
                    max_attempts=1)

    # Not the overload guard: the generic terminal path (if any) must not claim HTTP 429.
    assert not any('rate-limited (HTTP 429)' in e for e in errors), errors
    assert not any('LLM server busy' in e for e in errors), errors
    # Sanity: the loop still terminates with a single attempt.
    assert len(errors) <= 1


@pytest.mark.parametrize('max_attempts', [1, 2])
def test_overload_message_counts_attempts(tmp_path, max_attempts):
    errors = _drive(tmp_path, ServerBusyError('all endpoints busy'), max_attempts=max_attempts)

    assert len(errors) == 1, errors
    assert f'after {max_attempts} attempt(s)' in errors[0], errors[0]
