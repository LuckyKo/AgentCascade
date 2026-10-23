"""Phase 8 — streaming-lifecycle behavior fix (P8-1: stream watchdog abort).

Each test drives the REAL ExecutionEngine streaming loop (via
tests.phase8_stream_helpers) because only the real loop can exercise the watchdog
abort, the silent retry path, and the keep-partial re-yield. Hermetic: no network,
no LLM, <30s.

See the plan: N:\\work\\WD\\AgentCascade\\plans\\phase8_behavior_fixes_plan.md
P8-0 was DROPPED (the stream_end_recorded flag is redundant — the TelemetryCollector
pop-guard at telemetry.py:531-533 already collapses any double-end to one event).
"""

from agent_cascade.telemetry import TelemetryCollector
from tests.phase8_stream_helpers import (
    drive_stream,
    end_events,
    extract_assistant_contents,
    extract_system_errors,
    make_stream_engine,
    retrying_messages,
    stalling_gen,
)


# ──────────────────────────────────────────────────────────────────────────────
# P8-1 — a stream watchdog must actually ABORT the generator (yield from), not
# call _abort_llm_stream() as a bare statement.
#
# Pre-fix: the bare self._abort_llm_stream(...) at llm_call.py:654 (silence) /
# :660 (total-duration) is a generator expression evaluated and DISCARDED — it
# never runs, so the generator is not closed, no 'end' is recorded by the abort,
# state.last_output is left set, and the post-loop path treats the stalled stream
# as a SUCCESS: the partial is committed to history and NO retry happens.
#
# Post-fix: yield from runs the abort (closes the generator, records one 'end',
# clears last_output, bumps retry_count), so the attempt retries silently (no
# [RETRYING]) and on exhaustion the terminal message is the generic
# [SYSTEM ERROR: Empty LLM response].
#
# Clock-isolated via FakeClock: the non-targeted watchdog is left effectively
# infinite so only the intended one trips.
# ──────────────────────────────────────────────────────────────────────────────

_EMPTY_RESPONSE = '[SYSTEM ERROR: Empty LLM response]'


def _run_watchdog_abort(collector, tmp_path, *, silence_limit, total_limit):
    """Drive a stream that stalls on the 2nd chunk; return (results, execute)."""
    engine, instance = make_stream_engine(collector)
    # Bound the retry budget deterministically and set ONLY the targeted watchdog.
    engine.pool.settings.retry_max_attempts = 3
    engine.pool.settings.stream_max_silence_seconds = silence_limit
    engine.pool.settings.stream_max_total_seconds = total_limit

    clock = _make_clock()
    # Two chunks: the first stays inside every limit (so last_output gets set),
    # the second advances the clock past the targeted watchdog.
    gen = stalling_gen(['chunk-0', 'chunk-1'], clock, advance=5.0)
    results, execute = drive_stream(
        engine, instance, gen, clock=clock, deadline_seconds=10000)
    return results, execute, engine


def _make_clock():
    from tests.phase8_stream_helpers import FakeClock
    return FakeClock(start=1000.0)


def test_watchdog_silence_trips_abort_and_retries(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    results, execute, engine = _run_watchdog_abort(
        collector, tmp_path, silence_limit=1.0, total_limit=1e9)

    # (a) silent retry fired: the attempt was retried, not treated as success.
    assert execute.call_count >= 2, (
        f"silence watchdog must abort+retry, but _execute_llm_call was called "
        f"{execute.call_count}x (pre-fix: the bare abort is a no-op → no retry)")
    # (b) the partial is NOT committed (the abort cleared last_output).
    contents = extract_assistant_contents(results)
    assert 'chunk-0' not in contents, (
        f"watchdog abort must not commit the partial; got: {contents}")
    # (c) no [RETRYING] — this is the SILENT watchdog path, not backoff.
    assert retrying_messages(results) == [], 'watchdog retry must be silent'
    # (d) exhaustion surfaces the generic empty-response error.
    assert _EMPTY_RESPONSE in extract_system_errors(results), (
        f"expected {_EMPTY_RESPONSE!r}, got: {extract_system_errors(results)}")
    # guard: exactly one 'end' per attempt (no double-record per attempt).
    assert len(end_events(collector)) == execute.call_count


def test_watchdog_total_duration_trips_abort_and_retries(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    results, execute, engine = _run_watchdog_abort(
        collector, tmp_path, silence_limit=1e9, total_limit=1.0)

    # Same discriminators as the silence test, but the TOTAL-duration watchdog
    # trips (after a chunk set last_output, exercising the last_output-set path).
    assert execute.call_count >= 2, (
        'total-duration watchdog must abort+retry, not treat a stalled stream as success')
    contents = extract_assistant_contents(results)
    assert 'chunk-0' not in contents, 'watchdog abort must not commit the partial'
    assert retrying_messages(results) == [], 'watchdog retry must be silent'
    assert _EMPTY_RESPONSE in extract_system_errors(results), (
        f"expected {_EMPTY_RESPONSE!r}, got: {extract_system_errors(results)}")
    assert len(end_events(collector)) == execute.call_count
