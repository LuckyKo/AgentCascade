"""
Unit tests for BUG-7 fix — failed-compression backoff gate + honest return values.

Spec: reports/fix_plans/BUG-7_compression_failure_backoff.md

Covers (gate timing and method invocation, NOT scoping):
- Exception path: streak recorded, returns False (was: return True).
- Backoff gate: immediate retry short-circuits BEFORE compress_context runs.
- Gate expiry: proceeds to compress once the backoff window passes.
- Soft-failure path (result.success=False): streak + False (was: implicit None).
- Success resets the failure streak.

Note: t151b removed the pool-scoped halt (_build_compression_halt_scope) — forced compression
no longer halts any sibling; the Compressor is a pure FIFO citizen bounded by QUEUE_WAIT_TIMEOUT.
These tests therefore verify only that the BUG-7 backoff gate short-circuits BEFORE compress_context
on an immediate retry (the gate sits ahead of the acquire). Scoping / no-halt behavior is covered by
tests/test_t151_forced_compression_fifo.py and tests/test_t151b_compressor_fifo_fairness.py.
"""

import threading
import time
from unittest.mock import MagicMock, patch

from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.llm.schema import Message


def make_instance(name='A'):
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'coder'
    inst.parent_instance = None
    inst._force_compress_count = 1
    inst._force_compress_fail_streak = 0
    inst._last_force_compress_fail_at = 0.0
    inst._allocated_max_input_tokens = 0  # real int: avoids MagicMock > int in feedback formatting
    lock = threading.RLock()
    inst._compression_lock = lock
    return inst


def make_handler():
    pool = MagicMock()
    pool.instances = []  # no Compressor_ instances to exempt
    pool.get_conversation.return_value = []  # real list: safe to iterate/bool-test
    handler = CompressionHandler(pool)
    engine = MagicMock()
    handler.set_engine(engine)
    return handler, pool, engine


class TestBug7BackoffGate:

    def test_exception_records_streak_and_returns_false(self):
        """compress_context raising → streak=1, timestamp set, return False."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        messages, llm_messages = [Message(role='user', content='x')], []

        with patch('agent_cascade.compression.core.compress_context', side_effect=RuntimeError('boom')):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is False
        assert inst._force_compress_fail_streak == 1
        assert inst._last_force_compress_fail_at > 0.0
        # t151b: forced compression no longer halts any sibling (Compressor is a FIFO citizen);
        # the finally block still calls resume_all_instances() defensively (idempotent now).
        pool.resume_all_instances.assert_called_once()

    def test_gate_short_circuits_before_halt_on_immediate_retry(self):
        """Second call right after a failure must skip halt entirely."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        messages, llm_messages = [Message(role='user', content='x')], []

        with patch('agent_cascade.compression.core.compress_context', side_effect=RuntimeError('boom')):
            first = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert first is False

        # t151b: the gate short-circuits BEFORE compress_context runs (there is no longer a halt to
        # skip — the Compressor simply acquires its slot through the FIFO). Assert compress_context
        # itself was not reached on the immediate retry.
        with patch('agent_cascade.engine.compression_exec.logger'), \
             patch.object(engine, '_count_history_tokens', return_value=90_000), \
             patch.object(engine, '_get_max_tokens', return_value=100_000), \
             patch('agent_cascade.compression.core.compress_context') as compress_mock:
            second = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert second is False
        compress_mock.assert_not_called()
        assert inst._force_compress_fail_streak == 1  # unchanged by the skip

    def test_gate_expires_and_retries(self):
        """Backoff window elapsed → gate passes, halt+compress proceed."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        inst._force_compress_fail_streak = 1
        inst._last_force_compress_fail_at = time.monotonic() - 61.0  # 60s window passed
        messages, llm_messages = [Message(role='user', content='x')], []
        ok = MagicMock(success=True, tokens_before=100, tokens_after=10, summary_text='s', messages_discarded=5)
        engine._telemetry.return_value = None

        with patch('agent_cascade.compression.core.compress_context', return_value=ok), \
             patch.object(engine, '_rebuild_working_set'), \
             patch.object(handler, '_sync_logger_after_compression'), \
             patch.object(handler, '_inject_compression_notification'):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is True

    def test_soft_failure_records_streak_and_returns_false(self):
        """result.success=False → previously fell off returning None; now False + streak."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        messages, llm_messages = [Message(role='user', content='x')], []
        bad = MagicMock(success=False, error='compressor LLM unavailable')

        with patch('agent_cascade.compression.core.compress_context', return_value=bad), \
             patch.object(handler, '_format_compression_failure',
                          return_value='[COMPRESSION] failed'), \
             patch.object(handler, '_inject_compression_notification'):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is False
        assert inst._force_compress_fail_streak == 1
        assert inst._last_force_compress_fail_at > 0.0

    def test_success_resets_streak(self):
        """A successful forced compression zeroes the failure streak."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        inst._force_compress_fail_streak = 3
        inst._last_force_compress_fail_at = time.monotonic() - 700.0  # past 600s cap
        messages, llm_messages = [Message(role='user', content='x')], []
        ok = MagicMock(success=True, tokens_before=100, tokens_after=10, summary_text='s', messages_discarded=5)
        engine._telemetry.return_value = None

        with patch('agent_cascade.compression.core.compress_context', return_value=ok), \
             patch.object(engine, '_rebuild_working_set'), \
             patch.object(handler, '_sync_logger_after_compression'), \
             patch.object(handler, '_inject_compression_notification'):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is True
        assert inst._force_compress_fail_streak == 0

    def test_backoff_warning_injected_on_skip(self):
        """While gated, the model still sees a compression warning (pressure signal)."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        inst._force_compress_fail_streak = 1
        inst._last_force_compress_fail_at = time.monotonic() - 5.0
        messages, llm_messages = [Message(role='user', content='x')], []

        with patch.object(engine, '_count_history_tokens', return_value=90_000) as cnt, \
             patch.object(engine, '_get_max_tokens', return_value=100_000), \
             patch.object(engine, '_inject_compression_warning') as warn:
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is False
        warn.assert_called_once()
        cnt.assert_called_once()
