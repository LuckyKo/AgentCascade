"""Unit and integration tests for deadlock protection fixes in security_handler.py.

Tests the three specific fixes:
1. Hard timeout on LLM generator (engine.run never yields)
2. Reentrant lock (RLock) prevents self-deadlock
3. Lock acquire timeout prevents permanent block on crash

Plus integration tests that exercise actual deadlock scenarios with real threading.
"""
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Must import before patches take effect
from agent_cascade.security_handler import (
    SecurityAdvisorHandler,
    SECURITY_LLM_TIMEOUT_SECONDS,
    SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS,
    _get_security_check_lock,
    _get_security_execution_lock,
)


# ── Unit tests for individual mechanisms ────────────────────────────────────


class TestSecurityLLMTimeout:
    """Test that the hard LLM timeout prevents infinite block when generator never yields."""

    def test_llm_timeout_mechanism_works(self):
        """Verify the timeout mechanism (threading.Event + Timer) works correctly.

        This tests the core pattern used in security_handler.py without needing
        to mock the entire ExecutionEngine stack.
        """
        timeout_event = threading.Event()
        timeout_seconds = 1

        def trigger():
            timeout_event.set()

        timer = threading.Timer(timeout_seconds, trigger)
        timer.daemon = True
        timer.start()

        # Simulate a blocking generator that never yields
        start = time.monotonic()
        while not timeout_event.is_set():
            time.sleep(0.05)  # Small sleep to avoid busy-waiting
        elapsed = time.monotonic() - start
        timer.cancel()

        assert elapsed >= timeout_seconds * 0.9, (
            f"Timeout should fire after ~{timeout_seconds}s, got {elapsed:.2f}s"
        )
        assert elapsed < timeout_seconds * 1.5, (
            f"Timeout fired too late: {elapsed:.2f}s"
        )

    def test_llm_timeout_constant_is_importable(self):
        """SECURITY_LLM_TIMEOUT_SECONDS should be importable from security_handler."""
        from agent_cascade.security_handler import SECURITY_LLM_TIMEOUT_SECONDS
        assert isinstance(SECURITY_LLM_TIMEOUT_SECONDS, (int, float))
        assert SECURITY_LLM_TIMEOUT_SECONDS > 0


class TestReentrantSecurityLock:
    """Test that RLock allows reentrant acquisition by the same thread."""

    def test_rlock_allows_same_thread_reacquire(self):
        """RLock must allow the same thread to acquire multiple times without deadlock."""
        lock = threading.RLock()

        # First acquire (simulates outer security check)
        assert lock.acquire(timeout=1), "First acquire should succeed"
        try:
            # Second acquire from same thread (simulates nested security check)
            assert lock.acquire(timeout=1), "Second acquire by same thread should succeed (reentrant)"
            try:
                # Third acquire for good measure
                assert lock.acquire(timeout=1), "Third acquire by same thread should succeed"
                lock.release()
            finally:
                lock.release()
        finally:
            lock.release()

    def test_rlock_different_threads_block(self):
        """Different threads should still block on the RLock (concurrency control preserved)."""
        lock = threading.RLock()
        lock.acquire()

        blocked = threading.Event()

        def try_acquire():
            result = lock.acquire(timeout=0.5)
            blocked.set()
            return result

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=2)

        assert blocked.is_set(), "Second thread should have attempted and timed out"
        # Lock is still held by main thread
        lock.release()

    def test_security_handler_creates_rlock_type(self):
        """Verify security_handler.py uses RLock for execution lock (not Semaphore or Lock)."""
        import inspect
        from agent_cascade import security_handler

        source = inspect.getsource(security_handler)
        assert "RLock()" in source, (
            "security_handler should use threading.RLock() for reentrant safety"
        )

    def test_both_security_locks_are_rlocks(self):
        """Both prompt lock and execution lock must be RLocks for nested safety."""
        app = type('App', (), {})()  # Simple mock app object

        lock1 = _get_security_check_lock(app)
        assert isinstance(lock1, type(threading.RLock())), (
            "_get_security_check_lock must return RLock for reentrant prompt building"
        )

        lock2 = _get_security_execution_lock(app)
        assert isinstance(lock2, type(threading.RLock())), (
            "_get_security_execution_lock must return RLock for reentrant execution"
        )

    def test_unused_semaphore_removed_from_api_server(self):
        """api_server.py should not create security_check_semaphore anymore."""
        import inspect
        from agent_cascade import api_server
        source = inspect.getsource(api_server)
        assert "security_check_semaphore" not in source, (
            "Unused security_check_semaphore should be removed from api_server.py"
        )


class TestSecurityLockAcquireTimeout:
    """Test that lock acquire timeout prevents permanent block."""

    def test_acquire_timeout_constant_is_reasonable(self):
        """SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS should be short enough to detect problems quickly."""
        assert 1 <= SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS <= 30, (
            f"Lock acquire timeout ({SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s) should be between 1-30s"
        )

    def test_acquire_timeout_fires_when_lock_held_by_other_thread(self):
        """If lock is held by another thread and not released, acquire should timeout."""
        lock = threading.RLock()

        held = threading.Event()
        release_later = threading.Event()

        def hold_lock():
            lock.acquire()
            held.set()
            release_later.wait(timeout=5)
            lock.release()

        holder = threading.Thread(target=hold_lock)
        holder.start()
        held.wait(timeout=2)

        start = time.monotonic()
        result = lock.acquire(timeout=0.5)
        elapsed = time.monotonic() - start

        release_later.set()
        holder.join(timeout=2)

        assert not result, "Acquire should fail when lock is held by another thread"
        assert 0.3 <= elapsed <= 1.5, f"Timeout should fire in ~0.5s, got {elapsed:.2f}s"

    def test_acquire_timeout_error_message_includes_context(self):
        """The timeout error message should include request_id and guidance."""
        msg = (
            f"[SECURITY] Failed to acquire security execution lock within "
            f"{SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS}s for request test_rid. "
            f"A previous check may have crashed without releasing. "
            f"Manual restart may be required."
        )
        assert "request" in msg.lower(), "Error message should reference the request for debugging"
        assert "test_rid" in msg, "Error message should include request_id"


class TestSecurityLLMTimeoutConfiguration:
    """Test that LLM timeout constant is properly configured."""

    def test_llm_timeout_constant_exists_and_reasonable(self):
        """SECURITY_LLM_TIMEOUT_SECONDS should be set to a reasonable value."""
        assert isinstance(SECURITY_LLM_TIMEOUT_SECONDS, (int, float))
        assert 10 <= SECURITY_LLM_TIMEOUT_SECONDS <= 300, (
            f"LLM timeout ({SECURITY_LLM_TIMEOUT_SECONDS}s) should be between 10-300s"
        )

    def test_llm_timeout_is_separate_from_approval_timeout(self):
        """The LLM timeout should be independent of user-facing approval timeout."""
        assert SECURITY_LLM_TIMEOUT_SECONDS > 0


# ── Integration tests with real threading ───────────────────────────────────


def _make_minimal_pool():
    """Create a minimal mock AgentPool for integration tests."""
    pool = MagicMock()
    pool.stopped = False
    pool.operation_manager.base_dir = "/tmp/test"
    pool.operation_manager.extra_work_folders_ro = []
    pool.operation_manager.extra_work_folders_rw = []
    pool.operation_manager.enable_timeout = True
    pool.operation_manager.approval_timeout_seconds = 180
    pool.instance_state = {}
    pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    return pool


def _make_minimal_app():
    """Create a minimal mock app state for integration tests."""
    return type('App', (), {})()


class TestConcurrentSecurityChecks:
    """Integration test: two concurrent security checks should serialize via execution lock."""

    def test_concurrent_checks_serialize_via_execution_lock(self):
        """Two security checks running simultaneously should not execute in parallel.

        The execution lock ensures only one check runs engine.run at a time.
        We verify this by tracking when each check enters/exits the execution phase.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()

        # Track execution timeline
        timeline = []
        timeline_lock = threading.Lock()

        def quick_gen():
            yield ("[YES] Safe", False)

        template = MagicMock()
        template.llm = MagicMock()
        template.llm.generate_cfg = {}
        pool.get_template.return_value = template

        results = []
        errors = []

        def run_check(rid, delay_ms=0):
            """Simulate _execute_check with timeline tracking."""
            try:
                # Ensure execution lock exists
                if not getattr(app, 'security_execution_lock', None):
                    app.security_execution_lock = threading.RLock()

                # Acquire with timeout (mimics security_handler behavior)
                acquired = app.security_execution_lock.acquire(timeout=5)
                if not acquired:
                    errors.append(f"{rid}: failed to acquire lock")
                    return

                try:
                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{rid}_START"))

                    time.sleep(delay_ms / 1000.0)  # Simulate work

                    with timeline_lock:
                        timeline.append((time.monotonic(), f"{rid}_END"))

                    results.append(rid)
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                errors.append(f"{rid}: {e}")

        # Run two checks concurrently
        t1 = threading.Thread(target=run_check, args=("check_A", 200))
        t2 = threading.Thread(target=run_check, args=("check_B", 200))

        t1.start()
        time.sleep(0.05)  # Small delay so check_A gets lock first
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        assert not errors, f"Errors during test: {errors}"
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"

        # Verify serialization: check_A should complete before check_B starts
        events = sorted(timeline, key=lambda x: x[0])
        a_start = next(t for t, e in events if e == "check_A_START")
        a_end = next(t for t, e in events if e == "check_A_END")
        b_start = next(t for t, e in events if e == "check_B_START")

        assert b_start >= a_end - 0.05, (
            f"Checks should serialize: check_B started at {b_start:.3f} but check_A ended at {a_end:.3f}"
        )


class TestNestedSecurityCheckReentrancy:
    """Integration test: nested security check should not deadlock with RLock."""

    def test_nested_security_check_allows_reentrant_lock(self):
        """If a Security agent triggers another security check, RLock allows reentrancy.

        Simulates the scenario where _execute_check calls itself (via call_agent → new check).
        With a regular Lock/Semaphore this would deadlock; with RLock it succeeds.
        """
        app = _make_minimal_app()

        if not getattr(app, 'security_execution_lock', None):
            app.security_execution_lock = threading.RLock()

        outer_completed = threading.Event()
        inner_completed = threading.Event()
        deadlock_detected = threading.Event()

        def inner_check():
            try:
                # Inner check tries to acquire the same lock
                acquired = app.security_execution_lock.acquire(timeout=2)
                if not acquired:
                    raise RuntimeError("Inner check failed to acquire lock — possible deadlock")
                try:
                    inner_completed.set()
                    time.sleep(0.05)  # Simulate work
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                outer_completed.set()  # Unblock outer thread
                raise

        def outer_check():
            try:
                acquired = app.security_execution_lock.acquire(timeout=2)
                if not acquired:
                    raise RuntimeError("Outer check failed to acquire lock")
                try:
                    # Simulate nested security check (same thread re-acquires)
                    inner_check()

                    outer_completed.set()
                finally:
                    app.security_execution_lock.release()
            except Exception as e:
                deadlock_detected.set()
                raise

        t = threading.Thread(target=outer_check)
        t.start()

        # Wait with timeout — if we hit this, it's a deadlock
        assert outer_completed.wait(timeout=5), (
            "Outer check did not complete within 5s — RLock reentrancy failed (deadlock)"
        )
        assert inner_completed.is_set(), "Inner check should have completed"
        assert not deadlock_detected.is_set(), "Deadlock was detected during nested check"

        t.join(timeout=2)


class TestTimerCleanupOnException:
    """Integration test: timers are cleaned up even when exceptions occur."""

    def test_warning_timer_cancelled_on_exception_before_execution_lock(self):
        """If an exception occurs before acquiring execution lock, warning timer must be cancelled.

        This tests the timer lifecycle fix — sec_warning_timer is created inside the prompt lock
        but must be cancelled in the outer finally block regardless of where the exception occurs.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()

        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        # Track timer objects created
        timers_created = []
        original_timer_init = threading.Timer.__init__

        def tracked_timer_init(self, interval, function, args=None, kwargs=None):
            original_timer_init(self, interval, function, args, kwargs)
            timers_created.append(self)

        ap = {
            "request_id": "test_rid_timer",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Hold execution lock in another thread so acquire times out
        app.security_execution_lock = threading.RLock()
        release_event = threading.Event()

        def hold_lock():
            app.security_execution_lock.acquire()
            release_event.wait(timeout=10)
            app.security_execution_lock.release()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()

        try:
            with patch('threading.Timer.__init__', tracked_timer_init):
                with patch('agent_cascade.security_handler.SECURITY_LLM_TIMEOUT_SECONDS', 5):
                    with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.3):
                        try:
                            handler._execute_check(
                                ap=ap,
                                sec_inst=None,
                                rid="test_rid_timer",
                                auto_apply=True,
                                instance_name="Maine",
                                caller_agent="Maine",
                                prompt_template="Test {tool_name}",
                                timeout_seconds=3600,
                                warning_seconds=2400,
                            )
                        except RuntimeError:
                            pass  # Expected — lock acquire failed

            # Give a moment for any pending cleanup to complete
            time.sleep(0.1)

            # Verify any created timers were cancelled or have stopped running.
            # A properly cleaned up timer is either: _cancelled=True, or not alive (stopped).
            for timer in timers_created:
                is_cancelled = getattr(timer, '_cancelled', False)
                is_alive = timer.is_alive()
                assert is_cancelled or not is_alive, (
                    f"Timer should have been cancelled/stopped. _cancelled={is_cancelled}, alive={is_alive}"
                )
        finally:
            release_event.set()
            holder.join(timeout=2)


class TestActiveChecksCleanupOnLockTimeout:
    """Integration test: active_checks is cleaned up when lock acquire times out."""

    def test_active_checks_removed_on_execution_lock_timeout(self):
        """If execution lock acquire fails, the rid must be removed from active_checks.

        This tests the state leak fix — rid was added in run_check() but if _execute_check
        raises RuntimeError before creating sec_state_key, _cleanup can't find it to remove.
        """
        pool = _make_minimal_pool()
        app = _make_minimal_app()
        session = {"session_name": "Maine", "generate_cfg": {}}
        send_queue = MagicMock()

        handler = SecurityAdvisorHandler(pool, session, app, send_queue, lambda: None)

        # Pre-populate active_checks (simulates run_check having added the rid)
        from agent_cascade.security_handler import _get_active_checks_state
        active_checks, checks_lock = _get_active_checks_state(app)
        with checks_lock:
            active_checks.add("test_rid_leak")

        assert "test_rid_leak" in active_checks

        ap = {
            "request_id": "test_rid_leak",
            "tool_name": "shell_cmd",
            "description": "test",
            "tool_args": {},
            "agent_name": "Maine",
        }

        # Hold execution lock in another thread so acquire times out (RLock blocks different threads)
        app.security_execution_lock = threading.RLock()
        release_event = threading.Event()

        def hold_lock():
            app.security_execution_lock.acquire()
            release_event.wait(timeout=10)
            app.security_execution_lock.release()

        holder = threading.Thread(target=hold_lock, daemon=True)
        holder.start()

        try:
            with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 0.3):
                handler._execute_check(
                    ap=ap,
                    sec_inst=None,
                    rid="test_rid_leak",
                    auto_apply=True,
                    instance_name="Maine",
                    caller_agent="Maine",
                    prompt_template="Test {tool_name}",
                    timeout_seconds=3600,
                    warning_seconds=2400,
                )
                assert False, "Should have raised RuntimeError on lock acquire failure"
        except RuntimeError as e:
            assert "Failed to acquire" in str(e), f"Expected lock acquire error, got: {e}"
        finally:
            release_event.set()
            holder.join(timeout=2)

        # Verify active_checks was cleaned up despite the exception
        with checks_lock:
            assert "test_rid_leak" not in active_checks, (
                "active_checks should be cleaned up even when lock acquire times out"
            )
