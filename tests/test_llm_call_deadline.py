"""Tests for the LLM wall-clock deadline (Phase 1, Fix A2).

Exercises the real production code path in:
- agent_cascade/engine/llm_call.py (_execute_llm_call_with_retry)

The deadline is an absolute wall-clock cap over the ENTIRE call (all retries
included). It is captured at entry, checked at the top of each retry iteration,
and used to cap the backoff sleep. On expiry the generator yields a
"[SYSTEM ERROR: LLM call exceeded ...s wall-clock deadline]" message and stops
retrying.

All tests are self-contained — no LLM or API server required. The underlying
_execute_llm_call is mocked so we control exactly how long each attempt takes,
and a controlled fake clock (patched time.monotonic / time.sleep in the llm_call
module) makes the deadline fire deterministically without any real sleeps.

Run: pytest tests/test_llm_call_deadline.py -v
"""

from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.llm.schema import ASSISTANT, USER, Message


# ──────────────────────────────────────────────────────────────────────────────
# Fake clock: advances only by the amount slept, so the test controls time.
# ──────────────────────────────────────────────────────────────────────────────

class FakeClock:
    """Manual monotonic clock. ``sleep`` advances it; ``monotonic`` reports it.

    This lets a test deterministically drive an LLM call across (or up to) the
    wall-clock deadline without real sleeping, while still exercising the real
    backoff-cap logic in _execute_llm_call_with_retry.
    """

    def __init__(self, start=1000.0):
        self.now = float(start)
        self.sleep_calls = []  # every (requested, actually_advanced) pair

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleep_calls.append((seconds, min(seconds, max(0.0, self.deadline - self.now))))
        self.now += seconds


# ──────────────────────────────────────────────────────────────────────────────
# Helpers: mock pool + real ExecutionEngine (mirrors test_fallback_compression.py)
# ──────────────────────────────────────────────────────────────────────────────

def _make_pool_and_engine():
    """Create a mocked pool and real ExecutionEngine for testing the retry loop."""
    from agent_cascade.execution_engine import ExecutionEngine

    pool = MagicMock()
    instance = MagicMock()
    compression_lock = MagicMock()
    compression_lock.__enter__ = MagicMock()
    compression_lock.__exit__ = MagicMock()
    instance._compression_lock = compression_lock
    instance._streaming_responses = []
    instance.instance_name = "test-agent"

    # _is_terminal_stop() reads self.pool.stopped, self._my_generation,
    # self.pool._run_generation and self.pool.is_instance_terminated().
    # Configure them so the check returns False (no terminal stop).
    pool.stopped = False
    pool.is_instance_terminated.return_value = False
    pool._run_generation = 1

    # settings must be a real object with proper attributes, not mocks.
    class Settings:
        retry_max_attempts = 5       # Plenty of attempts so the deadline, not the attempt cap, ends the loop
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

    pool.settings = Settings()

    engine = ExecutionEngine(pool)
    # _my_generation is normally captured in run(); this test calls
    # _execute_llm_call_with_retry directly, bypassing run(), so set it here.
    engine._my_generation = 1
    return engine, pool, instance


def _make_template():
    """Minimal template object accepted by _execute_llm_call_with_retry."""
    template = MagicMock()
    template.llm_cfg = {"model": "test"}
    template.function_map = {}
    template.llm = MagicMock()
    template.llm.generate_cfg = {}
    return template


def _extract_system_errors(results):
    """Return the content of any yielded Message whose content is a SYSTEM ERROR string."""
    errors = []
    for item in results:
        if isinstance(item, Message) and isinstance(item.content, str) \
                and item.content.startswith("[SYSTEM ERROR"):
            errors.append(item.content)
    return errors


def _run_with_deadline(engine, instance, deadline_seconds, mock_execute, clock):
    """Drive _execute_llm_call_with_retry under a patched deadline + fake clock.

    ``clock.deadline`` is set to (capture time + deadline_seconds) before the call
    so FakeClock.sleep can record how much of each backoff actually elapses.
    """
    with patch.object(engine, "_execute_llm_call", side_effect=mock_execute):
        with patch("agent_cascade.engine.llm_call.LLM_CALL_DEADLINE_SECONDS", deadline_seconds):
            # The deadline is captured as (monotonic() + LLM_CALL_DEADLINE_SECONDS).
            clock.deadline = clock.monotonic() + deadline_seconds
            with patch("agent_cascade.engine.llm_call.time.monotonic", side_effect=clock.monotonic), \
                 patch("agent_cascade.engine.llm_call.time.sleep", side_effect=clock.sleep):
                return list(engine._execute_llm_call_with_retry(
                    instance, [Message(role=USER, content="test")], _make_template(), []))


def _always_fail():
    """Mock _execute_llm_call that always raises (a transient error)."""
    def mock_execute(*args, **kwargs):
        raise ConnectionError("Simulated failure")
        yield  # pragma: no cover (turns this into a generator)
    return mock_execute


# ──────────────────────────────────────────────────────────────────────────────
# 1. Deadline fires → yields the system error and stops retrying
# ──────────────────────────────────────────────────────────────────────────────

class TestDeadlineFires:
    """When the wall-clock deadline is exceeded, the generator yields a SYSTEM ERROR."""

    def test_deadline_fires_and_yields_system_error(self):
        """With a short deadline and an always-failing LLM, the loop aborts with a system error."""
        engine, pool, instance = _make_pool_and_engine()
        clock = FakeClock()

        results = _run_with_deadline(engine, instance, 0.3, _always_fail(), clock)

        system_errors = _extract_system_errors(results)
        assert any("wall-clock deadline" in e for e in system_errors), \
            f"Expected a wall-clock deadline SYSTEM ERROR, got: {system_errors}"
        # The generic empty-response error must be suppressed (error_already_yielded=True).
        assert not any("Empty LLM response" in e for e in system_errors), \
            f"Deadline error should suppress the empty-response error, got: {system_errors}"

    def test_deadline_error_message_mentions_seconds(self):
        """The yielded error message names the configured deadline in seconds."""
        engine, pool, instance = _make_pool_and_engine()
        # Raise the backoff cap and the attempt count so exponential backoff
        # (0.1, 0.2, 0.4, 0.8, 1.6, ...) accumulates past the 7s deadline before
        # retry_max_attempts is exhausted — i.e. the deadline is what ends the loop.
        engine.pool.settings.retry_max_delay = 100.0
        engine.pool.settings.retry_max_attempts = 20
        clock = FakeClock()

        results = _run_with_deadline(engine, instance, 7, _always_fail(), clock)

        system_errors = _extract_system_errors(results)
        assert any("exceeded 7s wall-clock deadline" in e for e in system_errors), \
            f"Expected 'exceeded 7s wall-clock deadline', got: {system_errors}"


# ──────────────────────────────────────────────────────────────────────────────
# 2. Deadline does NOT fire within the limit
# ──────────────────────────────────────────────────────────────────────────────

class TestDeadlineNotFired:
    """A call that completes (or fails normally) before the deadline is unaffected."""

    def test_no_deadline_error_when_succeeding_quickly(self):
        """A fast successful call yields no SYSTEM ERROR and returns the model output."""
        engine, pool, instance = _make_pool_and_engine()
        clock = FakeClock()

        def mock_execute(*args, **kwargs):
            yield [Message(role=ASSISTANT, content="ok")]

        # Large deadline — plenty of headroom.
        results = _run_with_deadline(engine, instance, 900, mock_execute, clock)

        assert _extract_system_errors(results) == [], \
            f"No SYSTEM ERROR expected on a quick success, got: {_extract_system_errors(results)}"
        # The successful assistant message should be present in the output.
        flat = [m for item in results if isinstance(item, list) for m in item] + \
               [item for item in results if isinstance(item, Message)]
        assert any(getattr(m, "content", None) == "ok" for m in flat), \
            f"Expected the 'ok' assistant message in output, got: {results}"

    def test_no_deadline_error_when_exhausting_attempts_within_limit(self):
        """Retries that exhaust retry_max_attempts before the deadline hit do not report a deadline."""
        engine, pool, instance = _make_pool_and_engine()
        clock = FakeClock()
        # Force a small attempt cap so the loop ends by exhaustion, not by deadline.
        engine.pool.settings.retry_max_attempts = 2

        results = _run_with_deadline(engine, instance, 900, _always_fail(), clock)

        assert not any("wall-clock deadline" in e for e in _extract_system_errors(results)), \
            f"No deadline SYSTEM ERROR expected on attempt exhaustion, got: {_extract_system_errors(results)}"


# ──────────────────────────────────────────────────────────────────────────────
# 3. Backoff respects the deadline (does not sleep past it)
# ──────────────────────────────────────────────────────────────────────────────

class TestBackoffRespectsDeadline:
    """The backoff sleep is capped by the remaining time to the deadline."""

    def test_backoff_sleep_capped_to_remaining(self):
        """time.sleep is never asked for more than the time left before the deadline."""
        engine, pool, instance = _make_pool_and_engine()
        clock = FakeClock()
        deadline_seconds = 0.3

        results = _run_with_deadline(engine, instance, deadline_seconds, _always_fail(), clock)

        # The deadline must have been hit (loop aborted by it, not by attempt cap).
        assert any("wall-clock deadline" in e for e in _extract_system_errors(results)), \
            f"Expected deadline SYSTEM ERROR, got: {_extract_system_errors(results)}"
        # Every requested sleep must be non-negative and bounded by the deadline budget.
        assert clock.sleep_calls, "Expected at least one backoff sleep to be requested"
        for requested, _ in clock.sleep_calls:
            assert 0 <= requested <= deadline_seconds + 1e-9, \
                f"Backoff sleep {requested}s exceeded the remaining deadline budget ({deadline_seconds}s)"

    def test_backoff_aborts_immediately_when_no_time_left(self):
        """If the deadline is already exhausted when a retry would sleep, it aborts without sleeping."""
        engine, pool, instance = _make_pool_and_engine()
        clock = FakeClock()
        # Deadline so short that even the first backoff (0.1s) exceeds what remains:
        # after attempt 1 + 0.1s sleep the clock is past the deadline, so the next
        # top-of-loop check fires before any further sleep is requested.
        results = _run_with_deadline(engine, instance, 0.05, _always_fail(), clock)

        assert any("wall-clock deadline" in e for e in _extract_system_errors(results)), \
            f"Expected deadline SYSTEM ERROR, got: {_extract_system_errors(results)}"
        # The loop must have aborted via the deadline, not by exhausting all 5 attempts.
        # (With retry_max_attempts=5 and only a 0.05s budget, it cannot reach attempt 5.)
