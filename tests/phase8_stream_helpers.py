"""Shared hermetic helpers for the Phase 8 streaming-lifecycle regression tests.

The only harness that can drive the real streaming loop and count real ``end``
events is a real ``ExecutionEngine`` + real ``TelemetryCollector`` (the MagicMock
stub in ``test_llm_call_retry_stop.py`` cannot). This module provides that harness,
a ``FakeClock`` (copied from ``test_llm_call_deadline.py``), partial-yielding
generator factories, and a ``drive_stream(...)`` driver.

Hermetic: no network, no LLM, <30s. Import as ``from tests.phase8_stream_helpers
import ...`` (the tests/conftest.py puts the project root on sys.path, so ``tests``
is an importable namespace package — same pattern as ``tests.slot_test_helpers``).
"""

import contextlib
from unittest.mock import MagicMock, patch

from agent_cascade.execution_engine import ExecutionEngine
from agent_cascade.llm.schema import ASSISTANT, Message


# ──────────────────────────────────────────────────────────────────────────────
# Fake clock: advances only by the amount slept / advanced, so the test controls
# time. Copied verbatim from tests/test_llm_call_deadline.py.
# ──────────────────────────────────────────────────────────────────────────────


class FakeClock:
    """Manual monotonic clock. ``sleep`` advances it; ``monotonic`` reports it.

    Lets a test deterministically drive a stream across a watchdog without real
    sleeping, while still exercising the real watchdog arithmetic in
    _stream_llm_attempt.
    """

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.deadline = float('inf')
        self.sleep_calls = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleep_calls.append((seconds, min(seconds, max(0.0, self.deadline - self.now))))
        self.now += seconds


# ──────────────────────────────────────────────────────────────────────────────
# Pool / engine / template doubles (mirrors test_llm_call_telemetry_termination).
# ──────────────────────────────────────────────────────────────────────────────


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
    # Engine watchdog limits default to effectively-infinite so a test that targets
    # one watchdog never accidentally trips the other (clock isolation, review
    # Finding 6). A P8-1 test lowers exactly the one it means to trip.
    stream_max_silence_seconds = 1e9
    stream_max_total_seconds = 1e9


def make_stream_engine(collector):
    """Real ExecutionEngine + real TelemetryCollector + MagicMock pool/instance.

    Configured so the streaming loop runs but no terminal stop fires unless a test
    asks for one. Mirrors _make_engine in test_llm_call_telemetry_termination.py.
    """
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


def make_template():
    t = MagicMock()
    t.llm_cfg = {'model': 'test'}
    t.function_map = {}
    t.llm.generate_cfg = {}
    t.llm.model = 'test-model'
    return t


# ──────────────────────────────────────────────────────────────────────────────
# Generator factories. Every chunk is a ``list[Message]`` so state.last_output is
# a non-empty list and finalize's re-yield branch (keep-partial) is reached — a
# bare string/None would take the empty path instead.
# ──────────────────────────────────────────────────────────────────────────────


def _chunk(content):
    return [Message(role=ASSISTANT, content=content)]


def chunk_gen(*contents):
    """Yield one ``list[Message]`` per content (no clock, no raise)."""

    def gen(*args, **kwargs):
        for c in contents:
            yield _chunk(c)

    return gen


def stalling_gen(contents, clock, advance):
    """Yield one chunk per content; before each chunk AFTER the first, advance
    ``clock.now`` by ``advance``.

    The engine reads ``time.monotonic()`` at the top of the for-loop (line 643)
    right after ``next(gen)`` returns, so advancing before yielding makes the
    watchdog see the elapsed gap on the targeted iteration while the first chunk
    stays within every limit.
    """

    def gen(*args, **kwargs):
        for i, c in enumerate(contents):
            if i > 0:
                clock.now += advance
            yield _chunk(c)

    return gen


def raise_after_gen(contents, exc):
    """Yield one chunk per content, then raise ``exc`` (termination-after-partial)."""

    def gen(*args, **kwargs):
        for c in contents:
            yield _chunk(c)
        raise exc
        yield  # pragma: no cover — keeps this a generator like the real one

    return gen


# ──────────────────────────────────────────────────────────────────────────────
# Inspection helpers.
# ──────────────────────────────────────────────────────────────────────────────


def llm_call_events(collector):
    # Only record_llm_call_end emits a 'llm_call' event (record_llm_call_start just
    # opens a timer), so this list is exactly the sequence of 'end' recordings.
    return [e for e in list(collector.events) if e.get('type') == 'llm_call']


def end_events(collector):
    return llm_call_events(collector)


def _str_content_startswith(item, prefix):
    """True if item is a Message whose str content starts with prefix."""
    return (isinstance(item, Message) and isinstance(item.content, str)
            and item.content.startswith(prefix))


def extract_system_errors(results):
    """Content of any yielded Message whose content is a [SYSTEM ERROR ...] string."""
    errors = []
    for item in results:
        if _str_content_startswith(item, '[SYSTEM ERROR'):
            errors.append(item.content)
    return errors


def extract_assistant_contents(results):
    """All assistant-message contents present in the yielded output.

    Covers both the finalize re-yield (bare Message) and any list-wrapped chunk,
    so a keep-partial assertion can check whether the partial content is committed.
    """
    contents = []
    for item in results:
        if isinstance(item, list):
            for m in item:
                if isinstance(m, Message):
                    contents.append(m.content)
        elif isinstance(item, Message):
            contents.append(item.content)
    return contents


def retrying_messages(results):
    """[RETRYING] UI signals (response_pipeline builds content starting with
    '[RETRYING]'). Absence proves the silent retry path (watchdog abort) was taken
    rather than the backoff/[RETRYING] path (_handle_llm_retry_failure)."""
    out = []
    for item in results:
        if _str_content_startswith(item, '[RETRYING]'):
            out.append(item.content)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Driver.
# ──────────────────────────────────────────────────────────────────────────────


def drive_stream(engine, instance, gen_factory, *,
                 terminal_stop=None, clock=None, deadline_seconds=None,
                 extra_patches=()):
    """Patch _execute_llm_call (and optionally _is_terminal_stop + the clock) and
    drive _execute_llm_call_with_retry to completion.

    Returns ``(results, execute_mock)`` — ``execute_mock.call_count`` is how many
    attempts were made. ``terminal_stop`` is a bool (return_value); for a per-
    call sequence a test passes a ``patch.object(engine, '_is_terminal_stop',
    side_effect=[...])`` via ``extra_patches`` instead.
    """
    with contextlib.ExitStack() as stack:
        execute = stack.enter_context(
            patch.object(engine, '_execute_llm_call', side_effect=gen_factory))
        if terminal_stop is not None:
            stack.enter_context(
                patch.object(engine, '_is_terminal_stop', return_value=terminal_stop))
        if clock is not None:
            stack.enter_context(
                patch('agent_cascade.engine.llm_call.time.monotonic',
                      side_effect=clock.monotonic))
            stack.enter_context(
                patch('agent_cascade.engine.llm_call.time.sleep',
                      side_effect=clock.sleep))
        if deadline_seconds is not None:
            stack.enter_context(
                patch('agent_cascade.engine.llm_call.LLM_CALL_DEADLINE_SECONDS',
                      deadline_seconds))
        for p in extra_patches:
            stack.enter_context(p)

        results = list(engine._execute_llm_call_with_retry(
            instance, [Message(role='user', content='hi')], make_template(), []))
        return results, execute
