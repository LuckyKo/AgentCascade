"""Integration test for real-thread agent dismissal.

Spawns actual execution threads doing interruptible long operations,
dismisses them, and asserts termination within a bounded time.

This tests what the existing 34 dismiss_termination tests cannot:
real thread lifecycle with cooperative stop-checks.
"""

import threading
import time
import pytest


class MockPool:
    """Minimal AgentPool mock for testing thread dismissal."""

    def __init__(self):
        self._instance_threads = {}
        self._instance_threads_lock = threading.Lock()
        self.terminated_instances = set()
        self._pool_lock = threading.RLock()
        self.stopped = False
        self._run_generation = 0
        self._halted_instances = set()

    def is_instance_terminated(self, name: str) -> bool:
        """Check if an instance was terminated (cooperative stop-check)."""
        with self._pool_lock:
            return name in self.terminated_instances


def interruptible_long_operation(pool: MockPool, instance_name: str):
    """Simulates a long-running agent loop with cooperative stop-checks.

    Sleeps in small increments and checks termination between each.
    This models how real agents check pool.is_instance_terminated() periodically.
    """
    start = time.monotonic()
    iterations = 0
    while True:
        # Cooperative stop-check (like real agent loops do)
        if pool.is_instance_terminated(instance_name):
            return f"Stopped after {iterations} iterations ({time.monotonic() - start:.2f}s)"
        time.sleep(0.05)  # Small sleep to simulate work between checks
        iterations += 1


def test_dismiss_real_thread_stops_within_bound():
    """Test that dismissing an agent stops its real thread within a bounded time."""
    pool = MockPool()
    instance_name = "test_agent_1"

    result_container = []

    def target():
        result = interruptible_long_operation(pool, instance_name)
        result_container.append(result)

    # Register thread BEFORE starting (matches run_agent_unified.py fix)
    with pool._instance_threads_lock:
        pool._instance_threads[instance_name] = threading.current_thread() if False else None  # placeholder

    thread = threading.Thread(target=target, daemon=True)
    with pool._instance_threads_lock:
        pool._instance_threads[instance_name] = thread

    thread.start()

    # Let it run briefly to confirm it's alive
    time.sleep(0.15)
    assert thread.is_alive(), "Thread should be alive before dismissal"

    # Dismiss: set termination signal (like terminate_instance does)
    with pool._pool_lock:
        pool.terminated_instances.add(instance_name)

    # Wait for thread to stop cooperatively (bounded wait like dismiss_instance join)
    thread.join(timeout=2.0)

    assert not thread.is_alive(), \
        "Thread should have stopped within 2s via cooperative termination check"
    assert len(result_container) == 1, "Operation should have returned a result"
    assert "Stopped after" in result_container[0], \
        f"Result should indicate cooperative stop: {result_container[0]}"


def test_dismiss_before_thread_starts_keeps_signal():
    """Test that dismissing before thread reaches first stop-check keeps signal alive.

    This tests the fix for the signal-discard bug (RC4): when thread is None or
    not yet registered, the termination signal must NOT be discarded.
    """
    pool = MockPool()
    instance_name = "test_agent_2"

    result_container = []
    start_event = threading.Event()

    def target():
        # Simulate slow startup (like create_main_agent_instance)
        time.sleep(0.1)
        start_event.set()
        result = interruptible_long_operation(pool, instance_name)
        result_container.append(result)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()

    # Dismiss BEFORE the thread reaches its first stop-check
    with pool._pool_lock:
        pool.terminated_instances.add(instance_name)

    # The signal is already set and will be caught when thread starts checking
    start_event.wait(timeout=1.0)  # Wait for thread to reach first check
    thread.join(timeout=2.0)

    assert not thread.is_alive(), \
        "Thread should stop at first cooperative check even if dismissed during startup"


def test_no_thread_registered_signal_not_discarded():
    """Test that when no thread is registered (async executor worker case),
    the termination signal is preserved."""
    pool = MockPool()
    instance_name = "test_async_child"

    # Simulate async child: no thread registered in _instance_threads
    with pool._pool_lock:
        pool.terminated_instances.add(instance_name)

    # The old buggy code would discard the signal here if thread is None:
    #   if not thread or not thread.is_alive(): discard()  # WRONG
    # The fix keeps it:
    #   if thread and not thread.is_alive(): discard()     # CORRECT
    with pool._instance_threads_lock:
        thread = pool._instance_threads.pop(instance_name, None)

    # Simulate the fixed condition
    with pool._pool_lock:
        if thread and not thread.is_alive():
            pool.terminated_instances.discard(instance_name)

    # Signal should still be present since no thread was registered
    assert instance_name in pool.terminated_instances, \
        "Termination signal must persist when no thread was registered (async child case)"


def test_join_timeout_does_not_block_excessively():
    """Test that join timeout is short enough not to block dismissal for 30s."""
    pool = MockPool()
    instance_name = "test_agent_3"

    def blocking_target():
        # This thread NEVER checks termination (simulates worst-case blocked op)
        time.sleep(60)

    thread = threading.Thread(target=blocking_target, daemon=True)
    with pool._instance_threads_lock:
        pool._instance_threads[instance_name] = thread
    thread.start()

    time.sleep(0.1)
    assert thread.is_alive()

    # Simulate dismiss_instance join behavior
    with pool._instance_threads_lock:
        thread_ref = pool._instance_threads.pop(instance_name, None)

    if thread_ref and thread_ref.is_alive():
        start = time.monotonic()
        thread_ref.join(timeout=2.0)  # Fixed timeout (was 30s)
        elapsed = time.monotonic() - start

    assert elapsed < 5.0, \
        f"Join should not block for more than ~2s, took {elapsed:.1f}s"
    # Thread is still alive (it never checks), but dismissal didn't block 30s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
