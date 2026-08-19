"""Tests for thread-safe AsyncToolRegistry executor resize (Bug 3 fix).

Covers the new ``resize_executor`` behavior and settings-driven pool sizing:
- resize swaps executor; new size correct; old pool drains (NOT cancelled)
- clamping to >=1; same-size resize is a cheap no-op (no thread leak)
- register() after resize submits to the NEW executor
- initial pool size honors pool.settings.max_workers; falls back to AGENT_MAX_WORKERS
- reset path (new registry with pool=self) preserves configured size
- threaded race: concurrent register + resize loses nothing, leaks no threads
- no dangling parent: queued children still complete after a mid-flight resize
- integration through a real AgentPool-shaped object

No LLM or network connections required.
"""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent_cascade.async_tools import AsyncToolRegistry
from agent_cascade.settings import AGENT_MAX_WORKERS


# ============================================================================
# Fixtures and helpers
# ============================================================================

def _wait_pending_false(registry, instance_name, timeout=10.0):
    """Poll has_pending until False (or timeout). Returns True if it cleared."""
    deadline = time.monotonic() + timeout
    while registry.has_pending(instance_name) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not registry.has_pending(instance_name)


def _count_async_tool_threads():
    """Count live threads named like the async_tool executor prefix."""
    return sum(1 for t in threading.enumerate() if t.name.startswith("async_tool"))


@pytest.fixture
def mock_pool():
    """A minimal AgentPool-shaped object (no settings attribute)."""
    pool = MagicMock()
    pool.enqueue_message = MagicMock()
    # Ensure the MagicMock does not accidentally expose a 'settings' attr that
    # would drive sizing — set it to None so getattr falls back to AGENT_MAX_WORKERS.
    pool.settings = None
    return pool


@pytest.fixture
def registry(mock_pool):
    return AsyncToolRegistry(pool=mock_pool)


def _make_pool_with_settings(max_workers):
    """Build a real AgentPool-shaped object exposing .settings.max_workers."""
    settings = SimpleNamespace(max_workers=max_workers)

    class _Pool:
        def __init__(self):
            self.settings = settings
            self.messages = []

        def enqueue_message(self, instance_name, msg):
            self.messages.append((instance_name, msg))

        def is_instance_terminated(self, name):
            return False

    return _Pool()


# ============================================================================
# 1. resize swaps executor; new size correct; old drains (not cancelled)
# ============================================================================

class TestResizeSwapsExecutor:
    def test_resize_swaps_and_sets_size(self, registry):
        old_exec = registry._executor
        assert old_exec._max_workers == AGENT_MAX_WORKERS

        result = registry.resize_executor(8)
        assert result is True
        new_exec = registry._executor
        assert new_exec is not old_exec
        assert new_exec._max_workers == 8

    def test_old_pool_drains_not_cancelled(self, registry):
        """A task queued on the OLD executor still completes after a resize.

        This proves we did NOT use cancel_futures=True (which would drop it).
        """
        started = threading.Event()
        release = threading.Event()

        def gated_tool():
            # Occupy an old-pool worker so we can force a queued state on the old pool.
            started.set()
            release.wait(timeout=10)
            return "gated"

        # 3 workers by default; occupy all of them then queue one more on OLD pool.
        for i in range(AGENT_MAX_WORKERS):
            registry.register(f"w{i}", gated_tool, function_id=f"c{i}")
        started.wait(timeout=5)
        queued_entry = registry.register("queued", gated_tool, function_id="cq")

        # Now resize to a larger pool. The queued task is on the OLD pool.
        assert registry.resize_executor(AGENT_MAX_WORKERS + 2) is True

        # Release the workers; the previously-queued task must still run to completion.
        release.set()
        deadline = time.monotonic() + 10
        while not queued_entry.completed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert queued_entry.completed is True, "Queued task on old pool was lost (cancel_futures?)"
        assert queued_entry.result == "gated"


# ============================================================================
# 2. clamping + same-size no-op
# ============================================================================

class TestClampAndNoop:
    def test_clamps_below_one(self, registry):
        assert registry.resize_executor(0) is True
        assert registry._executor._max_workers == 1
        assert registry.resize_executor(-5) is True
        assert registry._executor._max_workers == 1

    def test_accepts_float(self, registry):
        assert registry.resize_executor(4.9) is True
        assert registry._executor._max_workers == 4

    def test_non_numeric_input_returns_false_and_keeps_pool(self, registry):
        """resize_executor must return False (never raise) on non-numeric input."""
        before = registry._executor
        # None / list are not int()-coercible -> False, pool unchanged.
        assert registry.resize_executor(None) is False
        assert registry.resize_executor([3]) is False
        assert registry._executor is before

    def test_same_size_is_noop_and_leaks_no_threads(self, registry):
        # Resize to the same size must not grow the pool: worker count stays bounded.
        assert registry.resize_executor(AGENT_MAX_WORKERS) is True
        assert registry._executor._max_workers == AGENT_MAX_WORKERS
        # Let workers spin up, then confirm we never exceed the configured count.
        deadline = time.monotonic() + 5
        while _count_async_tool_threads() < AGENT_MAX_WORKERS and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _count_async_tool_threads() <= AGENT_MAX_WORKERS, "same-size resize leaked threads"


# ============================================================================
# 3. register() after resize submits to the NEW executor
# ============================================================================

class TestRegisterAfterResize:
    def test_register_uses_new_executor(self, registry):
        """After a resize, register() must submit to the NEW executor, not the old one."""
        old_exec = registry._executor

        # Spy on the OLD pool's submit: if register ever used it after resize, this fires.
        old_submits = []
        real_old_submit = old_exec.submit

        def spy_old_submit(*a, **k):
            old_submits.append((a, k))
            return real_old_submit(*a, **k)

        old_exec.submit = spy_old_submit

        assert registry.resize_executor(6) is True
        new_exec = registry._executor
        assert new_exec is not old_exec

        entry = registry.register("worker1", lambda: "hi", function_id="c1")

        # The old pool must NOT have been used for the new registration.
        assert old_submits == [], "register() submitted to the OLD executor after resize"
        # And the returned future is a real, pending/completable Future on the new pool.
        assert entry.future is not None
        deadline = time.monotonic() + 5
        while not entry.future.done() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entry.future.done() is True


# ============================================================================
# 4. initial pool size honors settings; falls back to AGENT_MAX_WORKERS
# ============================================================================

class TestInitialSizing:
    def test_honors_pool_settings(self):
        pool = _make_pool_with_settings(7)
        reg = AsyncToolRegistry(pool=pool)
        assert reg._executor._max_workers == 7

    def test_fallback_when_no_pool(self):
        reg = AsyncToolRegistry(pool=None)
        assert reg._executor._max_workers == AGENT_MAX_WORKERS

    def test_fallback_when_settings_missing(self, mock_pool):
        # mock_pool.settings is None -> getattr falls back to AGENT_MAX_WORKERS
        reg = AsyncToolRegistry(pool=mock_pool)
        assert reg._executor._max_workers == AGENT_MAX_WORKERS


# ============================================================================
# 5. reset preserves configured size (lifecycle.py:516 path: pool=self)
# ============================================================================

class TestResetPreservesSize:
    def test_reset_recreates_with_configured_size(self):
        # Simulate the lifecycle reset: shutdown then recreate with pool=self.
        pool = _make_pool_with_settings(5)
        first = AsyncToolRegistry(pool=pool)
        assert first._executor._max_workers == 5
        first.shutdown()

        # Recreate exactly as lifecycle.py does: AsyncToolRegistry(pool=self).
        second = AsyncToolRegistry(pool=pool)
        assert second._executor._max_workers == 5, "reset did not preserve configured size"


# ============================================================================
# 6. threaded race: hammer register + resize concurrently
# ============================================================================

class TestThreadedRace:
    def test_concurrent_register_and_resize_lose_nothing(self):
        pool = _make_pool_with_settings(3)
        reg = AsyncToolRegistry(pool=pool)
        N = 40
        stop = threading.Event()
        errors = []

        def resizer():
            size = 2
            while not stop.is_set():
                try:
                    reg.resize_executor(size)
                except Exception as e:  # pragma: no cover - defensive
                    errors.append(e)
                size = size % 4 + 1  # cycle 1..4

        def registrar(i):
            for k in range(N // 4):
                try:
                    reg.register(f"agent{i}", lambda i=i, k=k: f"ok-{i}-{k}",
                                 function_id=f"f{i}_{k}")
                except Exception as e:  # pragma: no cover - defensive
                    errors.append(e)

        threads = [threading.Thread(target=resizer)]
        for i in range(4):
            threads.append(threading.Thread(target=registrar, args=(i,)))
        for t in threads:
            t.start()

        # Run resizer for a bounded window.
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"exceptions during race: {errors[:3]}"

        # Every registered entry must reach completed (none lost/hung).
        deadline = time.monotonic() + 15
        while any(reg.has_pending(n) for n in [f"agent{i}" for i in range(4)]) \
                and time.monotonic() < deadline:
            time.sleep(0.02)
        for i in range(4):
            assert not reg.has_pending(f"agent{i}"), f"agent{i} has pending entries after race"

        # No thread leak: all async_tool threads should drain down to zero.
        deadline = time.monotonic() + 10
        while _count_async_tool_threads() > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _count_async_tool_threads() == 0, "async_tool threads leaked after race"


# ============================================================================
# 7. no dangling parent after resize (queued children still complete)
# ============================================================================

class TestNoDanglingParentAfterResize:
    def test_all_queued_children_complete_after_resize(self):
        pool = _make_pool_with_settings(2)
        reg = AsyncToolRegistry(pool=pool)
        N = 6
        done = []
        lock = threading.Lock()

        def child(i):
            time.sleep(0.05)
            with lock:
                done.append(i)
            return f"child-{i}"

        # Submit all children; only 2 run at once, rest queue on the old pool.
        entries = [reg.register("parent", lambda i=i: child(i), function_id=f"c{i}") for i in range(N)]
        time.sleep(0.1)  # let a couple start and the rest queue

        # Resize while some are still queued on the OLD pool.
        assert reg.resize_executor(4) is True

        deadline = time.monotonic() + 15
        while not all(e.completed for e in entries) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(e.completed for e in entries), "some children did not complete after resize"
        assert sorted(done) == list(range(N)), f"lost children: {sorted(done)}"


# ============================================================================
# 8. integration with a real AgentPool-shaped object
# ============================================================================

class TestIntegrationWithRealPoolShapedObject:
    def test_register_resize_completion(self):
        pool = _make_pool_with_settings(3)
        reg = AsyncToolRegistry(pool=pool)
        assert reg._executor._max_workers == 3

        # Register a couple of tools.
        e1 = reg.register("worker1", lambda: "one", function_id="c1")
        e2 = reg.register("worker1", lambda: "two", function_id="c2")

        # Live-resize (as the UI slider would).
        assert reg.resize_executor(5) is True
        assert reg._executor._max_workers == 5

        # Register after resize; everything must complete and enqueue.
        e3 = reg.register("worker1", lambda: "three", function_id="c3")

        deadline = time.monotonic() + 10
        while not all(e.completed for e in (e1, e2, e3)) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert all(e.completed for e in (e1, e2, e3))
        assert e1.result == "one" and e2.result == "two" and e3.result == "three"

        # Results enqueued to the pool's message queue.
        assert len(pool.messages) == 3
        payloads = [m for _, m in pool.messages]
        assert any("one" in p for p in payloads)
        assert any("two" in p for p in payloads)
        assert any("three" in p for p in payloads)
