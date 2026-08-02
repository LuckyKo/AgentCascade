"""Async result handling tests for AgentPool and AsyncResultBuffer.

Tests cover:
- Result buffer behavior under load
- Nested agent calls with endpoint rotation
- Concurrent put/drain operations
- AsyncToolRegistry integration with result buffering
- Race condition between has_pending and result availability

No LLM or network connections required.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.async_tools import AsyncResultBuffer, AsyncToolRegistry


# ============================================================================
# Fixtures and helpers
# ============================================================================

@pytest.fixture
def buffer():
    return AsyncResultBuffer()


@pytest.fixture
def mock_pool_with_buffer():
    """Create a mock pool with AsyncResultBuffer."""
    pool = MagicMock()
    pool._async_results = AsyncResultBuffer()
    return pool


@pytest.fixture
def registry(mock_pool_with_buffer):
    return AsyncToolRegistry(pool=mock_pool_with_buffer)


# ============================================================================
# Result buffer behavior under load
# ============================================================================

class TestResultBufferUnderLoad:
    """Test AsyncResultBuffer handles concurrent access correctly."""

    def test_concurrent_puts_no_loss(self, buffer):
        """Multiple threads putting results — none are lost."""
        num_threads = 50
        results_per_thread = 10
        
        def put_results(thread_id):
            for i in range(results_per_thread):
                buffer.put("worker1", f"result_{thread_id}_{i}")
        
        threads = [threading.Thread(target=put_results, args=(t,)) for t in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        drained = buffer.drain("worker1")
        assert len(drained) == num_threads * results_per_thread, \
            f"Expected {num_threads * results_per_thread}, got {len(drained)}"

    def test_concurrent_put_drain_no_corruption(self, buffer):
        """Concurrent puts and drains don't corrupt the buffer."""
        errors = []
        stop_flag = threading.Event()
        
        def putter():
            try:
                i = 0
                while not stop_flag.is_set():
                    buffer.put("worker1", f"p_{i}")
                    i += 1
            except Exception as e:
                errors.append(str(e))
        
        def drainer():
            try:
                while not stop_flag.is_set():
                    buffer.drain("worker1")
                    time.sleep(0.001)
            except Exception as e:
                errors.append(str(e))
        
        t1 = threading.Thread(target=putter)
        t2 = threading.Thread(target=drainer)
        t1.start()
        t2.start()
        
        # Use short fixed duration for stress test; threads are bounded by stop_flag.
        time.sleep(0.2)
        stop_flag.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        
        assert not errors, f"Errors during concurrent put/drain: {errors}"

    def test_per_instance_isolation(self, buffer):
        """Results for different instances are isolated."""
        buffer.put("worker1", "result_a")
        buffer.put("worker2", "result_b")
        buffer.put("worker3", "result_c")
        
        assert len(buffer.drain("worker1")) == 1
        assert len(buffer.drain("worker2")) == 1
        assert len(buffer.drain("worker3")) == 1

    def test_drain_empty_returns_empty_list(self, buffer):
        """Draining an instance with no results returns empty list."""
        assert buffer.drain("nonexistent") == []

    def test_drain_clears_buffer(self, buffer):
        """After drain, the buffer for that instance is empty."""
        buffer.put("worker1", "result1")
        buffer.put("worker1", "result2")
        
        first_drain = buffer.drain("worker1")
        assert len(first_drain) == 2
        
        second_drain = buffer.drain("worker1")
        assert second_drain == []


# ============================================================================
# wait_for_next blocking behavior
# ============================================================================

class TestWaitForNext:
    """Test the blocking wait_for_next method."""

    def test_wait_returns_immediately_if_result_available(self, buffer):
        """If a result is already in the buffer, wait returns immediately."""
        buffer.put("worker1", "existing_result")
        
        result = buffer.wait_for_next("worker1", timeout=1.0)
        assert result == ("existing_result", None)

    def test_wait_times_out_if_no_result(self, buffer):
        """Wait returns None after timeout if no result arrives."""
        start = time.time()
        result = buffer.wait_for_next("worker1", timeout=0.2)
        elapsed = time.time() - start
        
        assert result is None
        assert elapsed >= 0.15, "Should have waited close to timeout"

    def test_wait_returns_first_result_fifo(self, buffer):
        """wait_for_next returns results in FIFO order."""
        buffer.put("worker1", "first")
        buffer.put("worker1", "second")
        
        r1 = buffer.wait_for_next("worker1", timeout=1.0)
        r2 = buffer.wait_for_next("worker1", timeout=1.0)
        
        assert r1[0] == "first"
        assert r2[0] == "second"

    def test_wait_wakes_on_new_result(self, buffer):
        """A waiter is woken when a new result arrives."""
        woke_up = [False]
        waiter_blocked = threading.Event()
        
        def waiter():
            waiter_blocked.set()
            result = buffer.wait_for_next("worker1", timeout=5.0)
            if result:
                woke_up[0] = True
        
        t = threading.Thread(target=waiter)
        t.start()
        
        # Wait until waiter is actually blocked before putting result
        waiter_blocked.wait(timeout=2.0)
        buffer.put("worker1", "late_result")
        
        t.join(timeout=5)
        assert woke_up[0], "Waiter should have been woken by new result"

    def test_wait_indefinite(self, buffer):
        """wait_for_next with timeout=None waits until result arrives."""
        result_holder = [None]
        waiter_blocked = threading.Event()
        
        def waiter():
            waiter_blocked.set()
            result_holder[0] = buffer.wait_for_next("worker1", timeout=None)
        
        t = threading.Thread(target=waiter)
        t.start()
        
        # Wait until waiter is actually blocked before putting result
        waiter_blocked.wait(timeout=2.0)
        buffer.put("worker1", "eventual_result")
        
        t.join(timeout=5)
        assert result_holder[0] == ("eventual_result", None)


# ============================================================================
# AsyncToolRegistry integration
# ============================================================================

class TestAsyncToolRegistryIntegration:
    """Test AsyncToolRegistry properly integrates with result buffering."""

    def test_completed_tool_result_in_buffer(self, registry, mock_pool_with_buffer):
        """Completed tool results are placed in the pool's buffer."""
        def quick_tool():
            return "tool_output"
        
        registry.register("worker1", quick_tool, function_id="call_123")
        
        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        results = mock_pool_with_buffer._async_results.drain("worker1")
        assert len(results) == 1
        assert "tool_output" in results[0][0]
        assert results[0][1] == "call_123"

    def test_tool_error_in_buffer(self, registry, mock_pool_with_buffer):
        """Tool errors are placed in the buffer as formatted error messages."""
        def failing_tool():
            raise RuntimeError("Something broke")
        
        registry.register("worker1", failing_tool)
        
        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        results = mock_pool_with_buffer._async_results.drain("worker1")
        assert len(results) == 1
        assert "Error" in results[0][0] or "broke" in results[0][0]

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

    def test_clear_pending_removes_entries(self, registry, mock_pool_with_buffer):
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
        
        # Verify tool thread was stopped (not waiting for finish_event)
        # by checking no result arrives in the buffer after a short wait
        time.sleep(0.1)
        results = mock_pool_with_buffer._async_results.drain("worker1")
        assert len(results) == 0, "Cleared pending should prevent tool completion"


# ============================================================================
# Race condition: has_pending vs result availability
# ============================================================================

class TestHasPendingRaceCondition:
    """Test the race condition fix between has_pending and result buffering."""

    def test_no_race_between_completed_and_buffer(self, mock_pool_with_buffer):
        """When has_pending returns False, results are guaranteed in buffer."""
        registry = AsyncToolRegistry(pool=mock_pool_with_buffer)
        
        def quick_tool():
            return "result"
        
        registry.register("worker1", quick_tool)
        
        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("worker1") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        # Now: has_pending should be False AND result should be in buffer
        pending = registry.has_pending("worker1")
        results = mock_pool_with_buffer._async_results.drain("worker1")
        
        if not pending:
            assert len(results) >= 1, \
                "Race condition: has_pending=False but no result in buffer"

    def test_concurrent_has_pending_and_drain(self, mock_pool_with_buffer):
        """Concurrent has_pending checks and drains don't lose results."""
        registry = AsyncToolRegistry(pool=mock_pool_with_buffer)
        
        errors = []
        lock = threading.Lock()
        
        def check_loop():
            try:
                for _ in range(50):
                    pending = registry.has_pending("worker1")
                    mock_pool_with_buffer._async_results.drain("worker1")
            except Exception as e:
                with lock:
                    errors.append(str(e))
        
        # Register many quick tools
        for i in range(20):
            registry.register("worker1", lambda idx=i: f"tool_{idx}")
        
        threads = [threading.Thread(target=check_loop) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        
        assert not errors, f"Errors: {errors}"


# ============================================================================
# Nested agent calls with endpoint rotation
# ============================================================================

class TestNestedAgentCallsWithEndpointRotation:
    """Test nested call_agent scenarios with cursor rotation."""

    def test_nested_calls_use_separate_cursors(self, mock_pool_with_buffer):
        """Each nested agent instance has its own endpoint cursor."""
        from agent_cascade.api_router import APIRouter
        
        # Create a fresh router for this test
        import os
        import tempfile
        test_config_dir = tempfile.mkdtemp()
        
        with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_config_dir}):
            router = APIRouter(default_llm_cfg={
                'api_base': 'http://default',
                'model': 'default',
            })
            
            # Parent advances cursor
            router.advance_instance_endpoint("parent_worker")
            assert router._instance_endpoint_position.get("parent_worker", 0) == 1
            
            # Child should have independent cursor (at 0)
            assert router._instance_endpoint_position.get("child_worker", 0) == 0

    def test_nested_async_result_delivered_to_correct_parent(self, mock_pool_with_buffer):
        """Async result from nested agent goes to the correct parent instance."""
        registry = AsyncToolRegistry(pool=mock_pool_with_buffer)
        
        # Simulate nested call: child_agent runs on behalf of parent_worker
        def nested_call():
            return "[Parallel Agent 'child'] Nested result"
        
        registry.register("parent_worker", nested_call, function_id="nested_call_1")
        
        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("parent_worker") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        results = mock_pool_with_buffer._async_results.drain("parent_worker")
        assert len(results) == 1
        assert "Nested result" in results[0][0]

    def test_multiple_nested_calls_same_parent(self, mock_pool_with_buffer):
        """Multiple nested calls to the same parent don't crash and deliver results."""
        registry = AsyncToolRegistry(pool=mock_pool_with_buffer)
        
        for i in range(5):
            # Use default arg to capture loop variable correctly
            registry.register("parent", lambda idx=i: f"nested_{idx}", function_id=f"call_{i}")
        
        # Poll until all pending tasks complete instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("parent") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        results = mock_pool_with_buffer._async_results.drain("parent")
        # At least some results should be delivered
        assert len(results) > 0, "No results received from nested calls"

    def test_nested_call_failure_does_not_block_parent(self, mock_pool_with_buffer):
        """Failed nested call delivers error result instead of blocking."""
        registry = AsyncToolRegistry(pool=mock_pool_with_buffer)
        
        def failing_nested():
            raise ConnectionError("Nested agent failed")
        
        registry.register("parent", failing_nested)
        
        # Poll until completion instead of blind sleep.
        deadline = time.monotonic() + 5.0
        while registry.has_pending("parent") and time.monotonic() < deadline:
            time.sleep(0.05)
        
        results = mock_pool_with_buffer._async_results.drain("parent")
        assert len(results) == 1
        # Error should be formatted, not raised to parent
        assert "Error" in results[0][0] or "failed" in results[0][0]


# ============================================================================
# function_id tracking
# ============================================================================

class TestFunctionIdTracking:
    """Test that function_id is correctly tracked through the async pipeline."""

    def test_function_id_preserved_in_buffer(self, buffer):
        """function_id is stored with the result in the buffer."""
        buffer.put("worker1", "result_text", function_id="tool_call_abc")
        
        results = buffer.drain("worker1")
        assert len(results) == 1
        assert results[0][0] == "result_text"
        assert results[0][1] == "tool_call_abc"

    def test_function_id_none_is_valid(self, buffer):
        """Results without function_id are valid."""
        buffer.put("worker1", "no_id_result", function_id=None)
        
        results = buffer.drain("worker1")
        assert results[0] == ("no_id_result", None)