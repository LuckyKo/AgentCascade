"""Tests for concurrency-based SYNC/ASYNC dispatch using real EndpointScheduler + SlotPool.

Verifies that concurrent call_agent dispatch respects real concurrency limits under
actual contention — not mock call counts. Uses the same infrastructure as
test_endpoint_scheduler_stress.py and test_slot_queue.py.

All tests are self-contained — no LLM or API server required.
"""

import threading
import time
from unittest.mock import patch

import pytest

from agent_cascade.api_router import EndpointScheduler


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def scheduler():
    """Fresh EndpointScheduler for each test."""
    return EndpointScheduler()


# ============================================================================
# Test 1: Concurrent dispatch respects concurrency limits under real contention
# ============================================================================

class TestConcurrentDispatchRespectsLimits:
    """Verify that concurrent call_agent dispatch to a limited endpoint never exceeds
    the concurrency limit, using real slot acquisition/release."""

    def test_parallel_endpoint_never_exceeds_limit(self, scheduler):
        """50 agents dispatching to concurrency=3 endpoint — peak active ≤ 3."""
        api_base = "http://test-api"
        concurrency_limit = 3
        num_agents = 50

        peak_active = [0]
        peak_lock = threading.Lock()
        start_event = threading.Event()
        blocked_count = [0]
        blocked_lock = threading.Lock()

        def worker(agent_id):
            release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
            try:
                with peak_lock:
                    current = scheduler.count_active(api_base, concurrency_limit)
                    peak_active[0] = max(peak_active[0], current)

                with blocked_lock:
                    blocked_count[0] += 1

                start_event.wait(timeout=5)
            finally:
                release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()

        # Wait until all agents have acquired and are blocked
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with blocked_lock:
                if blocked_count[0] >= num_agents:
                    break
            time.sleep(0.01)

        start_event.set()
        for t in threads:
            t.join(timeout=30)

        assert peak_active[0] <= concurrency_limit, \
            f"Peak active ({peak_active[0]}) exceeded limit ({concurrency_limit})"

    def test_sequential_endpoint_strictly_serialized(self, scheduler):
        """Concurrency=0 endpoint: only 1 agent active at a time under contention."""
        api_base = "http://sequential-api"
        concurrency_limit = 0
        num_agents = 30

        peak_active = [0]
        peak_lock = threading.Lock()

        def worker(agent_id):
            release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
            try:
                with peak_lock:
                    current = scheduler.count_active(api_base, concurrency_limit)
                    peak_active[0] = max(peak_active[0], current)
                time.sleep(0.02)
            finally:
                release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert peak_active[0] == 1, \
            f"Sequential endpoint allowed {peak_active[0]} concurrent agents (expected 1)"

    def test_no_slot_leaks_under_concurrent_dispatch(self, scheduler):
        """50 rapid acquire/release cycles — no leaked slots."""
        api_base = "http://test-api"
        concurrency_limit = 5
        num_agents = 50
        errors = []

        def worker(agent_id):
            try:
                release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
                time.sleep(0.005)
                release()
            except Exception as e:
                errors.append((agent_id, str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during dispatch: {errors}"

        status = scheduler.get_status()
        active = status.get(api_base, {}).get('active_count', 0)
        assert active == 0, f"Slot leak: {active} still active after all releases"


# ============================================================================
# Test 2: Shared sequential slot across endpoints enforces global serialization
# ============================================================================

class TestSharedSequentialSlot:
    """All concurrency=0 endpoints share one slot — agents on different zero-concurrency
    endpoints are serialized globally."""

    def test_shared_sequential_blocks_cross_endpoint(self, scheduler):
        """Agent A holds seq endpoint 1 → Agent B blocks on seq endpoint 2."""
        api_base_1 = "http://seq-api-1"
        api_base_2 = "http://seq-api-2"

        release1 = scheduler.acquire(api_base_1, 0, "agent_a", "coder")

        acquired = [False]

        def try_acquire():
            import agent_cascade.api_router as ar
            with patch.object(ar, 'ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 1.0):
                try:
                    release2 = scheduler.acquire(api_base_2, 0, "agent_b", "researcher")
                    acquired[0] = True
                    release2()
                except TimeoutError:
                    pass

        t = threading.Thread(target=try_acquire)
        t.start()
        time.sleep(0.1)
        assert not acquired[0], "Second sequential endpoint should block (shared slot)"

        release1()
        t.join(timeout=5)
        assert acquired[0], "Should succeed after first releases"


# ============================================================================
# Test 3: Slot acquisition/release semantics under contention
# ============================================================================

class TestSlotAcquisitionSemantics:
    """Verify actual slot behavior: FIFO ordering, release wakes next waiter."""

    def test_fifo_grant_order_under_contention(self, scheduler):
        """Agents waiting for a limited endpoint are granted in FIFO order."""
        api_base = "http://test-api"
        concurrency_limit = 1

        # Occupy the single slot
        holder_release = scheduler.acquire(api_base, concurrency_limit, "holder", "orchestrator")

        grant_order = []
        lock = threading.Lock()
        acquired_events = {n: threading.Event() for n in ["T1", "T2", "T3"]}

        def waiter(name):
            release = scheduler.acquire(api_base, concurrency_limit, name, "coder")
            with lock:
                grant_order.append(name)
            acquired_events[name].set()
            time.sleep(0.05)  # Hold briefly so grants are sequential
            release()

        threads = []
        for name in ["T1", "T2", "T3"]:
            t = threading.Thread(target=waiter, args=(name,))
            t.start()
            time.sleep(0.05)  # Ensure FIFO enqueue order
            threads.append(t)

        # Release holder — T1 should get it first
        holder_release()

        for t in threads:
            t.join(timeout=10)

        assert grant_order == ["T1", "T2", "T3"], \
            f"FIFO order violated: {grant_order}"

    def test_release_wakes_next_waiter_immediately(self, scheduler):
        """Releasing a slot wakes the next waiter without delay."""
        api_base = "http://test-api"
        concurrency_limit = 1

        holder_release = scheduler.acquire(api_base, concurrency_limit, "holder", "orchestrator")

        b_acquired = threading.Event()

        def waiter_b():
            release = scheduler.acquire(api_base, concurrency_limit, "B", "coder")
            b_acquired.set()
            release()

        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        time.sleep(0.1)  # Let B enqueue
        assert not b_acquired.is_set(), "B should be waiting"

        start = time.monotonic()
        holder_release()
        t_b.join(timeout=5)
        elapsed = time.monotonic() - start

        assert b_acquired.is_set(), "B should have acquired after release"
        assert elapsed < 1.0, f"B took {elapsed:.2f}s to wake (expected near-instant)"


# ============================================================================
# Test 4: Unlimited endpoints bypass scheduling entirely
# ============================================================================

class TestUnlimitedEndpointBypass:
    """Concurrency=-1 endpoints skip scheduling — no blocking, no slot tracking."""

    def test_unlimited_no_blocking(self, scheduler):
        """All agents on unlimited endpoint acquire immediately with no contention."""
        api_base = "http://unlimited-api"
        concurrency_limit = -1

        num_agents = 50
        acquired = []
        lock = threading.Lock()

        def worker(agent_id):
            release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
            with lock:
                acquired.append(agent_id)
            # Unlimited returns None — no release needed
            if release:
                release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(acquired) == num_agents, \
            f"Expected {num_agents} acquires on unlimited endpoint, got {len(acquired)}"

    def test_unlimited_no_slot_tracking(self, scheduler):
        """Unlimited endpoint does not appear in scheduler status."""
        api_base = "http://unlimited-api"
        concurrency_limit = -1

        release = scheduler.acquire(api_base, concurrency_limit, "agent_x", "coder")
        assert release is None, "Unlimited endpoint should return None (no slot)"

        status = scheduler.get_status()
        assert api_base not in status, \
            f"Unlimited endpoint '{api_base}' should not be tracked in status"
