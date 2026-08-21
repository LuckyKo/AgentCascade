"""
Unit tests for BUG-7 fix — failed-compression backoff gate + honest return values.

Spec: reports/fix_plans/BUG-7_compression_failure_backoff.md

Covers:
- Exception path: streak recorded, returns False (was: return True).
- Backoff gate: immediate retry short-circuits BEFORE halt_all_instances.
- Gate expiry: proceeds to halt+compress once the backoff window passes.
- Soft-failure path (result.success=False): streak + False (was: implicit None).
- Success resets the failure streak.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.llm.schema import Message


def make_instance(name="A"):
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = "coder"
    inst.parent_instance = None
    inst._force_compress_count = 1
    inst._force_compress_fail_streak = 0
    inst._last_force_compress_fail_at = 0.0
    inst._allocated_max_input_tokens = 0   # real int: avoids MagicMock > int in feedback formatting
    lock = threading.RLock()
    inst._compression_lock = lock
    return inst


def make_handler():
    pool = MagicMock()
    pool.instances = []          # no Compressor_ instances to exempt
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
        messages, llm_messages = [Message(role="user", content="x")], []

        with patch("agent_cascade.compression.core.compress_context",
                   side_effect=RuntimeError("boom")):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is False
        assert inst._force_compress_fail_streak == 1
        assert inst._last_force_compress_fail_at > 0.0
        pool.halt_all_instances.assert_called_once()   # attempt #1 did halt normally
        pool.resume_all_instances.assert_called_once()

    def test_gate_short_circuits_before_halt_on_immediate_retry(self):
        """Second call right after a failure must skip halt_all_instances entirely."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        messages, llm_messages = [Message(role="user", content="x")], []

        with patch("agent_cascade.compression.core.compress_context",
                   side_effect=RuntimeError("boom")):
            first = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert first is False
        assert pool.halt_all_instances.call_count == 1

        with patch("agent_cascade.engine.compression_exec.logger"), \
             patch.object(engine, "_count_history_tokens", return_value=90_000), \
             patch.object(engine, "_get_max_tokens", return_value=100_000), \
             patch("agent_cascade.compression.core.compress_context") as compress_mock:
            second = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert second is False
        compress_mock.assert_not_called()
        pool.halt_all_instances.assert_called_once(), \
            "gate must fire before halt_all_instances — no pool-wide freeze while backing off"
        assert inst._force_compress_fail_streak == 1  # unchanged by the skip

    def test_gate_expires_and_retries(self):
        """Backoff window elapsed → gate passes, halt+compress proceed."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        inst._force_compress_fail_streak = 1
        inst._last_force_compress_fail_at = time.monotonic() - 61.0  # 60s window passed
        messages, llm_messages = [Message(role="user", content="x")], []
        ok = MagicMock(success=True, tokens_before=100, tokens_after=10,
                       summary_text="s", messages_discarded=5)
        engine._telemetry.return_value = None

        with patch("agent_cascade.compression.core.compress_context", return_value=ok), \
             patch.object(engine, "_rebuild_working_set"), \
             patch.object(handler, "_sync_logger_after_compression"), \
             patch.object(handler, "_inject_compression_notification"):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is True
        pool.halt_all_instances.assert_called_once()

    def test_soft_failure_records_streak_and_returns_false(self):
        """result.success=False → previously fell off returning None; now False + streak."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        messages, llm_messages = [Message(role="user", content="x")], []
        bad = MagicMock(success=False, error="compressor LLM unavailable")

        with patch("agent_cascade.compression.core.compress_context", return_value=bad), \
             patch.object(handler, "_format_compression_failure",
                          return_value="[COMPRESSION] failed"), \
             patch.object(handler, "_inject_compression_notification"):
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
        messages, llm_messages = [Message(role="user", content="x")], []
        ok = MagicMock(success=True, tokens_before=100, tokens_after=10,
                       summary_text="s", messages_discarded=5)
        engine._telemetry.return_value = None

        with patch("agent_cascade.compression.core.compress_context", return_value=ok), \
             patch.object(engine, "_rebuild_working_set"), \
             patch.object(handler, "_sync_logger_after_compression"), \
             patch.object(handler, "_inject_compression_notification"):
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is True
        assert inst._force_compress_fail_streak == 0

    def test_backoff_warning_injected_on_skip(self):
        """While gated, the model still sees a compression warning (pressure signal)."""
        handler, pool, engine = make_handler()
        inst = make_instance()
        inst._force_compress_fail_streak = 1
        inst._last_force_compress_fail_at = time.monotonic() - 5.0
        messages, llm_messages = [Message(role="user", content="x")], []

        with patch.object(engine, "_count_history_tokens", return_value=90_000) as cnt, \
             patch.object(engine, "_get_max_tokens", return_value=100_000), \
             patch.object(engine, "_inject_compression_warning") as warn:
            result = handler.execute_force_compression(inst, messages, llm_messages, 96.0)

        assert result is False
        warn.assert_called_once()
        cnt.assert_called_once()
