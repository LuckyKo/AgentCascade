"""Stress and concurrency tests for EndpointScheduler.

Tests cover:
- Concurrent slot acquisition/release under high load (50+ agents)
- Semaphore resize during active use
- Double-release protection
- Stale schedule cleanup behavior
- Shared sequential slot behavior across endpoints

No LLM or network connections required.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import EndpointScheduler


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def scheduler():
    return EndpointScheduler()


# ============================================================================
# Concurrent slot acquisition/release under high load
# ============================================================================

class TestConcurrentSlotAcquisition:
    """Test that EndpointScheduler handles 50+ concurrent agents correctly."""

    def test_concurrent_acquire_release_50_agents(self, scheduler):
        """50 agents acquiring/releasing slots on a concurrency=3 endpoint — no leaks."""
        api_base = "http://test-api"
        concurrency_limit = 3
        num_agents = 50

        errors = []
        acquired = []
        acquired_lock = threading.Lock()

        def worker(agent_id):
            try:
                release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
                with acquired_lock:
                    acquired.append(agent_id)
                time.sleep(0.01)  # Small hold time to create contention
                release()
            except Exception as e:
                errors.append((agent_id, str(e)))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during concurrent acquire/release: {errors}"
        assert len(acquired) == num_agents, f"Expected {num_agents} acquires, got {len(acquired)}"

        # Verify no slots are leaked
        status = scheduler.get_status()
        active = status.get(api_base, {}).get('active_count', 0)
        assert active == 0, f"Slot leak detected: {active} still active after all releases"

    def test_max_active_never_exceeded(self, scheduler):
        """Active count never exceeds concurrency_limit under contention."""
        api_base = "http://test-api"
        concurrency_limit = 5
        num_agents = 100

        peak_seen = [0]
        peak_lock = threading.Lock()
        # Use a simple event-based sync instead of Barrier to avoid fragility under xdist
        start_event = threading.Event()
        blocked_count = [0]
        blocked_lock = threading.Lock()

        def worker(agent_id):
            release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "researcher")
            try:
                with peak_lock:
                    current = scheduler.count_active(api_base, concurrency_limit)
                    peak_seen[0] = max(peak_seen[0], current)
                # Signal that this worker has acquired and is now blocked waiting
                with blocked_lock:
                    blocked_count[0] += 1
                start_event.wait(timeout=5)  # Wait for signal to release
                time.sleep(0.01)
            finally:
                release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()

        # Wait until all agents have acquired and are blocked waiting on start_event
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with blocked_lock:
                if blocked_count[0] >= num_agents:
                    break
            time.sleep(0.01)

        start_event.set()  # Release all at once
        
        for t in threads:
            t.join(timeout=30)

        assert peak_seen[0] <= concurrency_limit, \
            f"Peak active ({peak_seen[0]}) exceeded limit ({concurrency_limit})"

    def test_sequential_endpoint_strict_serialization(self, scheduler):
        """Concurrency=0 endpoints serialize all agents — only 1 active at a time."""
        api_base = "http://sequential-api"
        concurrency_limit = 0  # Sequential
        num_agents = 30

        peak_seen = [0]
        peak_lock = threading.Lock()

        def worker(agent_id):
            release = scheduler.acquire(api_base, concurrency_limit, f"agent_{agent_id}", "coder")
            try:
                with peak_lock:
                    current = scheduler.count_active(api_base, concurrency_limit)
                    peak_seen[0] = max(peak_seen[0], current)
                time.sleep(0.02)
            finally:
                release()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert peak_seen[0] == 1, \
            f"Sequential endpoint allowed {peak_seen[0]} concurrent agents (expected 1)"

    def test_shared_sequential_slot_across_endpoints(self, scheduler):
        """All concurrency=0 endpoints share the same slot — serialized globally."""
        api_base_1 = "http://seq-api-1"
        api_base_2 = "http://seq-api-2"
        
        # Acquire on first endpoint
        release1 = scheduler.acquire(api_base_1, 0, "agent_a", "coder")
        
        # Second sequential endpoint should BLOCK (shares the slot)
        acquired = [False]

        def try_acquire():
            try:
                import agent_cascade.api_router as ar
                with patch.object(ar, 'ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 1.0):
                    release2 = scheduler.acquire(api_base_2, 0, "agent_b", "researcher")
                    acquired[0] = True
                    release2()
            except TimeoutError:
                pass

        t = threading.Thread(target=try_acquire)
        t.start()
        # Give the thread time to start and block on acquire (not yet succeeded)
        time.sleep(0.1)
        assert not acquired[0], "Second sequential endpoint should have blocked"
        
        # Release first, now second should succeed
        release1()
        t.join(timeout=5)
        assert acquired[0], "Second sequential endpoint should succeed after first releases"


# ============================================================================
# Semaphore resize during active use
# ============================================================================

class TestSemaphoreResizeDuringActiveUse:
    """Test that resizing the semaphore while agents are running is safe."""

    def test_resize_upwards_no_starvation(self, scheduler):
        """Increasing concurrency_limit allows more agents without dropping existing ones."""
        api_base = "http://test-api"
        
        # Start with 2 active agents at limit
        release1 = scheduler.acquire(api_base, 2, "agent_1", "coder")
        release2 = scheduler.acquire(api_base, 2, "agent_2", "coder")
        
        assert scheduler.count_active(api_base, 2) == 2
        
        # Third agent should block at limit=2
        blocked = [True]
        third_release = [None]

        def try_acquire():
            try:
                third_release[0] = scheduler.acquire(api_base, 3, "agent_3", "coder")
                blocked[0] = False
            except Exception as e:
                blocked[0] = f"error: {e}"

        t = threading.Thread(target=try_acquire)
        t.start()
        # Wait for thread to complete acquire (resize allows it through immediately)
        deadline = time.monotonic() + 5.0
        while blocked[0] is True and time.monotonic() < deadline:
            time.sleep(0.01)
        
        # Resize to 3 — agent_3 should now be able to acquire
        assert not blocked[0], f"Agent 3 failed after resize: {blocked[0]}"
        assert third_release[0] is not None
        
        # All three active
        assert scheduler.count_active(api_base, 3) == 3
        
        release1()
        release2()
        third_release[0]()

    def test_resize_downwards_blocks_new_agents(self, scheduler):
        """Decreasing concurrency_limit blocks new agents but keeps existing ones.

        The resize happens during acquire when a different concurrency_limit is passed.
        Existing agents keep their slots; new acquires with the reduced limit block.
        """
        api_base = "http://test-api"
        
        # Start with 3 agents at limit=3
        release1 = scheduler.acquire(api_base, 3, "agent_1", "coder")
        release2 = scheduler.acquire(api_base, 3, "agent_2", "coder")
        release3 = scheduler.acquire(api_base, 3, "agent_3", "coder")
        
        assert scheduler.count_active(api_base, 3) == 3
        
        # Release one agent so active_count (2) < new_limit (2).
        # Then acquire with reduced limit=2 — the resize should succeed.
        release3()
        assert scheduler.count_active(api_base, 3) == 2
        
        # Now acquire a fourth agent with limit=2.
        # The resize will reduce max_active from 3 to 2.
        # Since active_count is now 2 and new_max is 2, the new semaphore has no free permits.
        # This call should time out waiting for a slot.
        import agent_cascade.api_router as ar
        with patch.object(ar, 'ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 0.5):
            with pytest.raises(TimeoutError, match="Timed out"):
                scheduler.acquire(api_base, 2, "agent_4", "coder")
        
        # Existing agents' slots are still valid — releasing them works normally.
        release1()
        release2()
        
        assert scheduler.count_active(api_base, 3) == 0

    def test_resize_preserves_semaphore_integrity(self, scheduler):
        """After resize and full cycle, semaphore permits are correct."""
        api_base = "http://test-api"
        num_cycles = 10
        
        for _ in range(num_cycles):
            # Acquire at limit 2
            r1 = scheduler.acquire(api_base, 2, "a", "coder")
            r2 = scheduler.acquire(api_base, 2, "b", "coder")
            r1()
            r2()
            
            # Resize to 4 mid-cycle
            r3 = scheduler.acquire(api_base, 4, "c", "coder")
            r4 = scheduler.acquire(api_base, 4, "d", "coder")
            r5 = scheduler.acquire(api_base, 4, "e", "coder")
            r6 = scheduler.acquire(api_base, 4, "f", "coder")
            r3()
            r4()
            r5()
            r6()
        
        # Verify no leak after all cycles
        status = scheduler.get_status()
        active = status.get(api_base, {}).get('active_count', 0)
        assert active == 0, f"Semaphore integrity broken: {active} leaked slots"


# ============================================================================
# Double-release protection
# ============================================================================

class TestDoubleReleaseProtection:
    """Test that calling release() twice is safely ignored."""

    def test_double_release_no_error(self, scheduler):
        """Calling release() twice should be a no-op after first call."""
        api_base = "http://test-api"
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        
        release()  # First release — normal
        assert scheduler.count_active(api_base, 2) == 0
        
        release()  # Second release — should be silently ignored
        assert scheduler.count_active(api_base, 2) == 0

    def test_triple_release_no_error(self, scheduler):
        """Multiple redundant releases are all safely handled."""
        api_base = "http://test-api"
        release = scheduler.acquire(api_base, 1, "agent_1", "coder")
        
        for _ in range(5):
            release()  # All should be safe
        
        assert scheduler.count_active(api_base, 1) == 0

    def test_double_release_preserves_other_slots(self, scheduler):
        """Double-releasing one slot doesn't corrupt other agents' slots."""
        api_base = "http://test-api"
        
        release1 = scheduler.acquire(api_base, 3, "agent_1", "coder")
        release2 = scheduler.acquire(api_base, 3, "agent_2", "coder")
        
        assert scheduler.count_active(api_base, 3) == 2
        
        release1()
        # After first release: agent_1's slot freed, agent_2 still active
        assert scheduler.count_active(api_base, 3) == 1
        
        release1()  # Double-release agent_1's slot — should be no-op
        
        # Agent 2's slot must still be intact; count unchanged from before double-release
        assert scheduler.count_active(api_base, 3) == 1
        
        release2()
        assert scheduler.count_active(api_base, 3) == 0


# ============================================================================
# Stale schedule cleanup behavior
# ============================================================================

class TestStaleScheduleCleanup:
    """Test that stale schedules are cleaned up correctly."""

    def test_cleanup_removes_idle_schedules(self, scheduler):
        """Schedules with no activity and full permits are removed."""
        api_base = "http://test-api"
        
        # Use the endpoint to create a schedule
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        release()
        
        assert api_base in scheduler._schedules
        
        # Cleanup should remove it
        scheduler.cleanup_stale()
        assert api_base not in scheduler._schedules

    def test_cleanup_preserves_shared_sequential_slot(self, scheduler):
        """The shared sequential slot is never cleaned up."""
        api_base = "http://seq-api"
        
        # Use a sequential endpoint to create the shared slot
        release = scheduler.acquire(api_base, 0, "agent_1", "coder")
        release()
        
        shared_key = "_shared_sequential_slot_"
        assert shared_key in scheduler._schedules
        
        scheduler.cleanup_stale()
        assert shared_key in scheduler._schedules, "Shared sequential slot should not be cleaned up"

    def test_cleanup_preserves_active_schedules(self, scheduler):
        """Schedules with active agents are NOT cleaned up."""
        api_base = "http://test-api"
        
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        
        # Cleanup should not remove an active schedule
        scheduler.cleanup_stale()
        assert api_base in scheduler._schedules
        
        release()

    def test_cleanup_removes_slot_holders_too(self, scheduler):
        """Cleanup also removes slot holder tracking for stale endpoints."""
        api_base = "http://test-api"
        
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        assert api_base in scheduler._slot_holders
        
        release()
        scheduler.cleanup_stale()
        
        assert api_base not in scheduler._schedules
        assert api_base not in scheduler._slot_holders


# ============================================================================
# Slot holder tracking and diagnostics
# ============================================================================

class TestSlotHolderTracking:
    """Test slot holder tracking for debugging stuck slots."""

    def test_slot_holder_recorded_on_acquire(self, scheduler):
        """Acquiring a slot records the instance as holder."""
        api_base = "http://test-api"
        
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        
        holders = scheduler.get_slot_holders(api_base)
        assert api_base in holders
        assert len(holders[api_base]) == 1
        
        name, agent_class, _, _ = holders[api_base][0]
        assert name == "agent_1"
        assert agent_class == "coder"
        
        release()

    def test_slot_holder_removed_on_release(self, scheduler):
        """Releasing a slot removes the holder record."""
        api_base = "http://test-api"
        
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        assert len(scheduler.get_slot_holders(api_base)[api_base]) == 1
        
        release()
        assert len(scheduler.get_slot_holders(api_base).get(api_base, [])) == 0

    def test_detect_stuck_slots(self, scheduler):
        """Slots held longer than threshold are flagged as stuck."""
        api_base = "http://test-api"
        
        # Acquire a slot
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        
        # Immediately it should not be stuck (threshold 60s)
        stuck = scheduler.detect_stuck_slots(threshold_seconds=60.0)
        assert len(stuck) == 0
        
        # Manually set acquired_at to old time to simulate stuck slot
        with scheduler._lock:
            if api_base in scheduler._slot_holders and scheduler._slot_holders[api_base]:
                name, agent_class, _, acq_id = scheduler._slot_holders[api_base][0]
                old_time = time.monotonic() - 120.0
                scheduler._slot_holders[api_base][0] = (name, agent_class, old_time, acq_id)
        
        stuck = scheduler.detect_stuck_slots(threshold_seconds=60.0)
        assert len(stuck) == 1
        assert stuck[0]['instance_name'] == "agent_1"
        assert stuck[0]['held_duration'] > 60.0
        
        release()

    def test_get_slot_holders_returns_deep_copy(self, scheduler):
        """get_slot_holders returns deep copies to prevent external mutation."""
        api_base = "http://test-api"
        
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        
        holders = scheduler.get_slot_holders(api_base)
        # Mutate the returned copy
        holders[api_base][0] = ("hacked", "hacker", 0.0, 999)
        
        # Internal state should be unchanged
        real = scheduler.get_slot_holders(api_base)
        assert real[api_base][0][0] == "agent_1"
        
        release()


# ============================================================================
# Timeout behavior
# ============================================================================

class TestAcquireTimeout:
    """Test that acquire respects timeout and provides useful error messages."""

    def test_acquire_times_out_at_capacity(self, scheduler):
        """Acquire raises TimeoutError when at capacity and timeout expires."""
        api_base = "http://test-api"
        
        # Fill the slot
        release1 = scheduler.acquire(api_base, 1, "agent_1", "coder")
        
        import agent_cascade.api_router as ar
        with patch.object(ar, 'ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 0.2):
            with pytest.raises(TimeoutError) as exc_info:
                scheduler.acquire(api_base, 1, "agent_2", "coder")
        
        assert "Timed out" in str(exc_info.value)
        assert "held by" in str(exc_info.value).lower() or "agent_1" in str(exc_info.value)
        
        release1()

    def test_timeout_error_includes_holder_info(self, scheduler):
        """Timeout error message identifies which agent holds the slot."""
        api_base = "http://test-api"
        
        release1 = scheduler.acquire(api_base, 1, "blocking_agent", "coder")
        
        import agent_cascade.api_router as ar
        with patch.object(ar, 'ENDPOINT_SLOT_ACQUIRE_TIMEOUT', 0.2):
            with pytest.raises(TimeoutError) as exc_info:
                scheduler.acquire(api_base, 1, "waiting_agent", "researcher")
        
        error_msg = str(exc_info.value)
        assert "blocking_agent" in error_msg
        
        release1()