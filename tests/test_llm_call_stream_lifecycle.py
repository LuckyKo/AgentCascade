"""Phase 8 — streaming-lifecycle behavior (P8-1 watchdog abort + P8-2 keep-partial guard).

Each test drives the REAL ExecutionEngine streaming loop (via
tests.phase8_stream_helpers) because only the real loop can exercise the watchdog
abort, the silent retry path, and the keep-partial re-yield. Hermetic: no network,
no LLM, <30s.

See the plan: N:\\work\\WD\\AgentCascade\\plans\\phase8_behavior_fixes_plan.md
P8-0 was DROPPED (the stream_end_recorded flag is redundant — the TelemetryCollector
``llm_call`` pop-guard already collapses any double-end to one event).
"""

from unittest.mock import patch

from agent_cascade.telemetry import TelemetryCollector
from tests.phase8_stream_helpers import (
    FakeClock,
    chunk_gen,
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
# Pre-fix: the bare self._abort_llm_stream(...) calls at the silence and total-duration
# watchdog sites in _stream_llm_attempt are generator expressions evaluated and DISCARDED — they
# never run, so the generator is not closed, no 'end' is recorded by the abort,
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


def _run_watchdog_abort(collector, *, silence_limit, total_limit):
    """Drive a stream that stalls on the 2nd chunk; return (results, execute)."""
    engine, instance = make_stream_engine(collector)
    # Bound the retry budget deterministically and set ONLY the targeted watchdog.
    engine.pool.settings.retry_max_attempts = 3
    engine.pool.settings.stream_max_silence_seconds = silence_limit
    engine.pool.settings.stream_max_total_seconds = total_limit

    clock = FakeClock(start=1000.0)
    # Two chunks: the first stays inside every limit (so last_output gets set),
    # the second advances the clock past the targeted watchdog.
    gen = stalling_gen(['chunk-0', 'chunk-1'], clock, advance=5.0)
    results, execute = drive_stream(
        engine, instance, gen, clock=clock, deadline_seconds=10000)
    return results, execute


def _assert_watchdog_abort(collector, results, execute):
    """Shared discriminators for both watchdog-abort tests. The only real
    difference between them is which limit the fixture lowers; that stays in the
    fixture call, not here."""
    # (a) silent retry fired: the attempt was retried, not treated as success.
    assert execute.call_count >= 2, (
        f"watchdog must abort+retry, but _execute_llm_call was called "
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


def test_watchdog_silence_trips_abort_and_retries(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    # Silence watchdog: lower only the silence limit.
    results, execute = _run_watchdog_abort(collector, silence_limit=1.0, total_limit=1e9)
    _assert_watchdog_abort(collector, results, execute)


def test_watchdog_total_duration_trips_abort_and_retries(tmp_path):
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    # Total-duration watchdog: lower only the total limit (trips after a chunk
    # set last_output, exercising the last_output-set path).
    results, execute = _run_watchdog_abort(collector, silence_limit=1e9, total_limit=1.0)
    _assert_watchdog_abort(collector, results, execute)


# ──────────────────────────────────────────────────────────────────────────────
# P8-2 — REGRESSION GUARD (NOT a discriminator): a mid-stream stop must commit
# the partial (keep-partial re-yield) with exactly one 'end', no [RETRYING],
# and no empty-response error. This PASSES on current code — it pins the
# keep-partial decision so a future "suppress the re-yield" change would break it.
# Covers both _is_terminal_stop checks in _stream_llm_attempt (first-chunk and after a UI update).
# ──────────────────────────────────────────────────────────────────────────────


def _assert_keep_partial_stop(collector, results):
    contents = extract_assistant_contents(results)
    assert 'partial-response' in contents, (
        f'keep-partial: the partial must be committed on a mid-stream stop; got: {contents}')
    assert len(end_events(collector)) == 1, (
        f'a stopped attempt must record exactly one "end", got {len(end_events(collector))}')
    assert retrying_messages(results) == [], 'a mid-stream stop is not a retry'
    assert extract_system_errors(results) == [], (
        'a mid-stream stop must not surface an empty-response error')


def test_stop_after_first_chunk(tmp_path):
    """GUARD: stop at the first-chunk _is_terminal_stop check in _stream_llm_attempt."""
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)
    results, _execute = drive_stream(
        engine, instance, chunk_gen('partial-response'), terminal_stop=True)
    _assert_keep_partial_stop(collector, results)


def test_stop_after_ui_update(tmp_path):
    """GUARD: stop at the after-UI-update _is_terminal_stop check in _stream_llm_attempt.

    Forces should_update True (STREAM_UPDATE_MAX_CHUNKS=1) so the UI-sync block
    runs, then _is_terminal_stop flips True only at the second check.
    """
    collector = TelemetryCollector(log_dir=str(tmp_path), instance_id='unit_test')
    engine, instance = make_stream_engine(collector)
    results, _execute = drive_stream(
        engine, instance, chunk_gen('partial-response'),
        extra_patches=(
            patch.object(engine, '_is_terminal_stop', side_effect=[False, True]),
            patch('agent_cascade.engine.llm_call.STREAM_UPDATE_MAX_CHUNKS', 1),
        ))
    _assert_keep_partial_stop(collector, results)
