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
                # Explicit timeout (honored directly by acquire()) guards against deadlock.
                release2 = scheduler.acquire(api_base_2, 0, "agent_b", "researcher", timeout=5.0)
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
# Fixed-capacity SlotPool semantics (post-consolidation)
# ============================================================================
# NOTE: The old two-layer design used a per-endpoint threading.Semaphore whose
# capacity could be resized mid-flight. That layer was removed in the slot/
# concurrency consolidation — there is now exactly ONE FIFO queue per slot pool
# (SlotPool), and a pool's capacity is FIXED at creation time. Passing a larger
# concurrency_limit to acquire() does NOT grow an existing pool; it is simply
# ignored for capacity purposes (the first value seen wins). The tests below
# lock in that real behavior.

class TestFixedCapacitySemantics:
    """SlotPool capacity is fixed at creation; later acquires honor it strictly."""

    def test_capacity_is_fixed_at_creation(self, scheduler):
        """The pool created for an endpoint keeps the capacity of its first acquire.
        A later acquire with a larger concurrency_limit does NOT grow the pool."""
        api_base = "http://test-api"

        # First acquire establishes capacity=2.
        release1 = scheduler.acquire(api_base, 2, "agent_1", "coder")
        release2 = scheduler.acquire(api_base, 2, "agent_2", "coder")
        assert scheduler.count_active(api_base, 2) == 2

        # A third acquire at the SAME (fixed) capacity must block — even if we pass a
        # larger concurrency_limit, the pool does not resize. Pass an explicit short
        # timeout (honored directly by acquire()) so the test cannot deadlock.
        with pytest.raises(TimeoutError):
            scheduler.acquire(api_base, 3, "agent_3", "coder", timeout=0.5)

        # The pool's reported capacity is still the original 2 (not 3).
        status = scheduler.get_status()
        assert status[api_base]['max_active'] == 2, \
            f"Pool should keep fixed capacity 2, got {status[api_base]['max_active']}"

        release1()
        release2()
        assert scheduler.count_active(api_base, 2) == 0

    def test_at_capacity_blocks_then_grants_on_release(self, scheduler):
        """At a fixed capacity, a new agent blocks until a permit frees up (FIFO)."""
        api_base = "http://test-api"
        cap = 2

        release1 = scheduler.acquire(api_base, cap, "agent_1", "coder")
        release2 = scheduler.acquire(api_base, cap, "agent_2", "coder")
        assert scheduler.count_active(api_base, cap) == cap

        # Third agent blocks (at capacity).
        granted = [False]
        third_release = [None]

        def try_acquire():
            try:
                third_release[0] = scheduler.acquire(api_base, cap, "agent_3", "coder")
                granted[0] = True
            except Exception:
                pass

        t = threading.Thread(target=try_acquire)
        t.start()
        time.sleep(0.15)
        assert not granted[0], "Third agent should be blocked at capacity"

        # Free one permit → the blocked agent is granted in FIFO order.
        release2()
        t.join(timeout=5)
        assert granted[0], "Blocked agent should be granted after a permit frees"
        assert scheduler.count_active(api_base, cap) == cap

        release1()
        third_release[0]()
        assert scheduler.count_active(api_base, cap) == 0

    def test_full_cycle_no_leak(self, scheduler):
        """Repeated acquire/release cycles at a fixed capacity never leak permits."""
        api_base = "http://test-api"
        cap = 2
        num_cycles = 10

        for _ in range(num_cycles):
            r1 = scheduler.acquire(api_base, cap, "a", "coder")
            r2 = scheduler.acquire(api_base, cap, "b", "coder")
            r1()
            r2()

        status = scheduler.get_status()
        active = status[api_base]['active_count']
        assert active == 0, f"Slot leak after {num_cycles} cycles: {active} still active"


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
        """Idle pools (no active or waiting agents) are removed by cleanup_stale."""
        api_base = "http://test-api"

        # Use the endpoint to create a pool
        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        release()

        assert api_base in scheduler._pools

        # Cleanup should remove it (idle: no running, no waiters)
        scheduler.cleanup_stale()
        assert api_base not in scheduler._pools

    def test_cleanup_preserves_shared_sequential_slot(self, scheduler):
        """The shared sequential slot is never cleaned up."""
        api_base = "http://seq-api"

        # Use a sequential endpoint to create the shared pool
        release = scheduler.acquire(api_base, 0, "agent_1", "coder")
        release()

        shared_key = "_shared_sequential_slot_"
        assert shared_key in scheduler._pools

        scheduler.cleanup_stale()
        assert shared_key in scheduler._pools, "Shared sequential slot should not be cleaned up"

    def test_cleanup_preserves_active_schedules(self, scheduler):
        """Pools with active agents are NOT cleaned up."""
        api_base = "http://test-api"

        release = scheduler.acquire(api_base, 2, "agent_1", "coder")

        # Cleanup should not remove a pool that still has an active holder
        scheduler.cleanup_stale()
        assert api_base in scheduler._pools

        release()

    def test_cleanup_removes_slot_holders_too(self, scheduler):
        """Cleanup removes the whole pool (and thus its slot-holder tracking)."""
        api_base = "http://test-api"

        release = scheduler.acquire(api_base, 2, "agent_1", "coder")
        assert api_base in scheduler._pools

        release()
        scheduler.cleanup_stale()

        assert api_base not in scheduler._pools
        # Holder tracking is gone with the pool.
        assert api_base not in scheduler.get_slot_holders()


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

        # Tuple shape: (instance_name, agent_name, granted_at, acquisition_id).
        # Note: _grant() populates SlotHolder.agent_name with the instance name, so
        # both fields carry the instance name here.
        instance_name, agent_name, _, _ = holders[api_base][0]
        assert instance_name == "agent_1"
        assert agent_name == "agent_1"

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
        
        # Manually backdate the holder's granted_at to simulate a stuck slot.
        pool = scheduler._pools[api_base]
        with pool._cond:
            for holder in pool._running.values():
                holder.granted_at = time.monotonic() - 120.0

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

        # Pass an explicit short timeout (honored directly by acquire()).
        with pytest.raises(TimeoutError) as exc_info:
            scheduler.acquire(api_base, 1, "agent_2", "coder", timeout=0.2)

        assert "Timed out" in str(exc_info.value)
        assert "held by" in str(exc_info.value).lower() or "agent_1" in str(exc_info.value)
        
        release1()

    def test_timeout_error_includes_holder_info(self, scheduler):
        """Timeout error message identifies which agent holds the slot."""
        api_base = "http://test-api"
        
        release1 = scheduler.acquire(api_base, 1, "blocking_agent", "coder")

        # Pass an explicit short timeout (honored directly by acquire()).
        with pytest.raises(TimeoutError) as exc_info:
            scheduler.acquire(api_base, 1, "waiting_agent", "researcher", timeout=0.2)

        error_msg = str(exc_info.value)
        assert "blocking_agent" in error_msg
        
        release1()