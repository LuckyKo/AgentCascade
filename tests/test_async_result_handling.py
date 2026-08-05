"""Async result handling tests for AsyncToolRegistry with unified message queue.

Tests cover:
- AsyncToolRegistry integration with enqueue_message (single-queue migration)
- has_pending behavior during and after tool execution
- clear_pending removes entries correctly
- Race condition between has_pending and result availability (via enqueue_message)

No LLM or network connections required.
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from agent_cascade.async_tools import AsyncToolRegistry


# ============================================================================
# Fixtures and helpers
# ============================================================================

@pytest.fixture
def mock_pool():
    """Create a mock pool with enqueue_message for single-queue testing."""
    pool = MagicMock()
    pool.enqueue_message = MagicMock()
    return pool


@pytest.fixture
def registry(mock_pool):
    return AsyncToolRegistry(pool=mock_pool)


# ============================================================================
# AsyncToolRegistry integration with unified message queue
# ============================================================================

class TestAsyncToolRegistryIntegration:
    """Test AsyncToolRegistry properly integrates with enqueue_message (single queue)."""

    def test_completed_tool_result_enqueued(self, registry, mock_pool):
        """Completed tool results are enqueued via pool.enqueue_message."""
        def quick_tool():
            return "tool_output"

        registry.register("worker1", quick_tool, function_id="call_123")

        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)

        mock_pool.enqueue_message.assert_called_once()
        agent_name, msg = mock_pool.enqueue_message.call_args[0]
        assert agent_name == "worker1"
        assert "tool_output" in msg

    def test_tool_error_enqueued(self, registry, mock_pool):
        """Tool errors are enqueued as formatted error messages."""
        def failing_tool():
            raise RuntimeError("Something broke")

        registry.register("worker1", failing_tool)

        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)

        mock_pool.enqueue_message.assert_called_once()
        agent_name, msg = mock_pool.enqueue_message.call_args[0]
        assert agent_name == "worker1"
        assert "Error" in msg or "broke" in msg

    def test_has_pending_true_while_running(self, registry):
        """has_pending returns True while tool is executing."""
        completed_event = threading.Event()

        def slow_tool():
            time.sleep(0.2)
            completed_event.set()
            return "done"

        registry.register("worker1", slow_tool)

        assert registry.has_pending("worker1") is True

        # Wait for tool to complete via event instead of blind sleep.
        completed_event.wait(timeout=5.0)
        assert registry.has_pending("worker1") is False

    def test_has_pending_false_after_completion(self, registry):
        """has_pending returns False after all tools complete."""
        def quick_tool():
            return "done"

        registry.register("worker1", quick_tool)

        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)

        assert registry.has_pending("worker1") is False

    def test_clear_pending_removes_entries(self, registry):
        """clear_pending removes all pending entries for an instance."""
        finish_event = threading.Event()

        def slow_tool():
            # Wait indefinitely; will not complete if pending is cleared
            finish_event.wait(timeout=30)
            return "done"

        registry.register("worker1", slow_tool)
        assert registry.has_pending("worker1") is True

        cancelled = registry.clear_pending("worker1")
        assert cancelled >= 0
        assert registry.has_pending("worker1") is False


# ============================================================================
# Race condition: has_pending vs result availability (single queue)
# ============================================================================

class TestHasPendingRaceCondition:
    """Test the race condition fix between has_pending and result enqueue."""

    def test_no_race_between_completed_and_enqueue(self, mock_pool):
        """When has_pending returns False, results are guaranteed to be enqueued."""
        registry = AsyncToolRegistry(pool=mock_pool)

        def quick_tool():
            return "result"

        registry.register("worker1", quick_tool)

        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)

        # Now: has_pending should be False AND result should have been enqueued
        pending = registry.has_pending("worker1")
        enqueue_calls = mock_pool.enqueue_message.call_count

        if not pending:
            assert enqueue_calls >= 1, \
                "Race condition: has_pending=False but no message enqueued"


# ============================================================================
# Nested agent calls via AsyncToolRegistry
# ============================================================================

class TestNestedAgentCallsViaAsyncToolRegistry:
    """Test nested agent calls produce results in the parent's queue."""

    def test_nested_call_result_enqueued_to_parent(self, mock_pool):
        """Simulated nested call result is enqueued to parent instance."""
        registry = AsyncToolRegistry(pool=mock_pool)

        def nested_agent_tool():
            return "[Agent researcher1 Completed]: Nested result data"

        registry.register("parent_worker", nested_agent_tool, function_id="nested_1")

        # Poll until completion.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("parent_worker") and time.monotonic() < deadline:
            time.sleep(0.05)

        mock_pool.enqueue_message.assert_called_once()
        agent_name, msg = mock_pool.enqueue_message.call_args[0]
        assert agent_name == "parent_worker"
        assert "Nested result data" in msg


# ============================================================================
# Multiple concurrent async tools
# ============================================================================

class TestMultipleConcurrentAsyncTools:
    """Test multiple concurrent async tools for the same instance."""

    def test_multiple_tools_all_results_enqueued(self, mock_pool):
        """All results from multiple concurrent tools are enqueued."""
        registry = AsyncToolRegistry(pool=mock_pool)

        def tool1():
            return "result1"

        def tool2():
            return "result2"

        registry.register("worker", tool1)
        registry.register("worker", tool2)

        # Poll until both complete.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker") and time.monotonic() < deadline:
            time.sleep(0.05)

        assert mock_pool.enqueue_message.call_count == 2
        msgs = [call[0][1] for call in mock_pool.enqueue_message.call_args_list]
        assert any("result1" in m for m in msgs)
        assert any("result2" in m for m in msgs)


# ============================================================================
# Single-queue regression: async results wake sleeping agents
# ============================================================================

class TestSingleQueueRegression:
    """Regression tests ensuring async results flow through unified message queue."""

    def test_async_result_is_plain_string_for_message_queue(self, mock_pool):
        """AsyncToolRegistry enqueues plain strings (not tuples) for message queue."""
        registry = AsyncToolRegistry(pool=mock_pool)

        def quick_tool():
            return "test_output"

        registry.register("worker1", quick_tool)

        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)

        mock_pool.enqueue_message.assert_called_once()
        agent_name, msg = mock_pool.enqueue_message.call_args[0]
        assert isinstance(msg, str), "Message must be a plain string for unified queue"
        assert agent_name == "worker1"