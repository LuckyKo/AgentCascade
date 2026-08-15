"""Regression tests for the slot/concurrency consolidation (plan §7).

The old design had TWO concurrency layers:
  Layer 1 — a per-endpoint ``threading.Semaphore`` gate.
  Layer 2 — an "ancestor-walk" that forced sync when a child's caller held the
            target endpoint's semaphore (``_find_ancestor_with_slot`` /
            ``_skip_slot_acquire``).

The consolidation removed BOTH layers. There is now exactly ONE FIFO queue per
slot pool (``SlotPool``, driven by ``EndpointScheduler``):

  * Every agent acquires its own permit through the single FIFO queue.
  * If the caller (or anyone else) already holds the same pool, a child simply
    WAITS in FIFO order and is granted when a permit frees up — it is never
    skipped and never force-synced by walking ancestors.

These tests lock in that behavior. They exercise the real ``EndpointScheduler`` /
``SlotPool`` (and the real ``ExecutionEngine._release_slot`` / ``reacquire_for``
helpers) so they catch regressions in the actual code paths — no LLM or network
required.

Coverage:
  T1 — Single-layer FIFO ordering (N agents, conc=0 endpoint).
  T3 — Security yield/reacquire (parent yields slot → child runs → parent reacquires).
  T4 — Compressor yield/reacquire (same pattern for compression).
  T5 — Reacquire timeout degrades to clean slotless state.
  T6 — A→B(async)→C: the KEY behavioral change. The removed ancestor-walk used to
        deadlock here; now C waits in FIFO and completes when A releases.
  T8 — conc=N capacity (up to N concurrent, (N+1)th waits, permit count ≤ N).
"""

import threading
import time
from typing import List
from unittest.mock import MagicMock

import pytest

from agent_cascade.api_router import EndpointScheduler
from agent_cascade.slot_queue import SlotQueueTimeout


# ============================================================================
# Fixtures / helpers
# ============================================================================

@pytest.fixture
def scheduler():
    """Fresh EndpointScheduler for each test."""
    return EndpointScheduler()


def _acquire(sched: EndpointScheduler, api_base: str, conc: int, name: str,
             agent_class: str = "coder", timeout: float = 5.0):
    """Convenience wrapper around the real scheduler.acquire()."""
    return sched.acquire(
        api_base=api_base,
        concurrency_limit=conc,
        instance_name=name,
        agent_class=agent_class,
        timeout=timeout,
    )


# ============================================================================
# T1 — Single-layer FIFO ordering
# ============================================================================

class TestT1SingleLayerFIFO:
    """N agents targeting one conc=0 endpoint run strictly in FIFO order via the
    single SlotPool queue — no interleaving, no barging."""

    def test_fifo_order_strict_on_conc_zero(self, scheduler):
        """A holds the shared sequential slot; T1..T4 wait and are granted in
        strict enqueue order once A releases."""
        api_base = "http://seq-api"
        conc = 0

        # Occupy the single permit so all challengers queue up.
        holder_release = _acquire(scheduler, api_base, conc, "A", "orchestrator")

        grant_order: List[str] = []
        order_lock = threading.Lock()

        def waiter(name: str):
            release = _acquire(scheduler, api_base, conc, name)
            with order_lock:
                grant_order.append(name)
            time.sleep(0.1)  # Hold briefly so grants are strictly sequential.
            release()

        names = ["T1", "T2", "T3", "T4"]
        threads = []
        for name in names:
            t = threading.Thread(target=waiter, args=(name,))
            t.start()
            time.sleep(0.05)  # Guarantee FIFO enqueue order (single-threaded setup).
            threads.append(t)

        # Release A — T1 (head) gets it first, then T2, T3, T4 in order.
        holder_release()

        for t in threads:
            t.join(timeout=10)
        assert all(not t.is_alive() for t in threads), "A waiter never completed"

        assert grant_order == names, f"FIFO order violated: {grant_order}"

    def test_conc_zero_never_exceeds_one(self, scheduler):
        """Under contention on a conc=0 endpoint, active count is always exactly 1."""
        api_base = "http://seq-api"
        conc = 0
        n = 20

        peak = [0]
        peak_lock = threading.Lock()
        done = [0]
        done_lock = threading.Lock()

        def worker(i: int):
            release = _acquire(scheduler, api_base, conc, f"agent_{i}")
            try:
                with peak_lock:
                    active = scheduler.count_active(api_base, conc)
                    peak[0] = max(peak[0], active)
                time.sleep(0.01)
            finally:
                release()
                with done_lock:
                    done[0] += 1

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert done[0] == n, f"Only {done[0]}/{n} agents completed"
        assert peak[0] == 1, f"conc=0 endpoint allowed {peak[0]} concurrent (expected 1)"


# ============================================================================
# T3 — Security yield/reacquire
# ============================================================================

class TestT3SecurityYieldReacquire:
    """Parent holds a shared sequential slot; invoking Security releases the parent's
    slot BEFORE the child runs, the child acquires & completes, and the parent
    reacquires afterward. No deadlock."""

    def test_security_yield_run_reacquire_no_deadlock(self, scheduler):
        from agent_cascade.execution_engine import ExecutionEngine

        api_base = "http://seq-api"
        conc = 0

        # Parent holds the shared sequential slot (lifecycle acquisition).
        parent_release = _acquire(scheduler, api_base, conc, "parent", "orchestrator")
        assert parent_release is not None

        # Build a real engine backed by a mock pool that resolves to this scheduler.
        # Stub get_agent_slot_info so reacquire_for resolves the SAME conc=0 shared
        # sequential slot the parent holds (mirrors EndpointScheduler.get_slot_info).
        mock_pool = MagicMock()
        mock_router = MagicMock()
        mock_router.scheduler = scheduler
        mock_router.get_agent_slot_info.return_value = {
            'slot_key': '_shared_sequential_slot_',
            'is_sequential': True,
            'concurrency_limit': 0,
            'api_base': api_base,
            'needs_slot': True,
        }
        mock_pool.api_router = mock_router
        engine = ExecutionEngine(mock_pool)

        # Parent instance holds the slot via _slot_release.
        parent_inst = MagicMock()
        parent_inst.instance_name = "parent"
        parent_inst.agent_class = "orchestrator"
        parent_inst._state_lock = threading.RLock()
        parent_inst._slot_release = parent_release
        parent_inst._slot_key = None

        # Child (Security) will run on a separate thread; it needs the SAME pool.
        child_held_during_run = [False]

        def security_child():
            release = _acquire(scheduler, api_base, conc, "security", "security", timeout=10.0)
            try:
                # While the child holds the permit, verify the parent does NOT also
                # hold it (the yield actually released it).
                pool = scheduler._pools['_shared_sequential_slot_']
                with pool._cond:
                    child_held_during_run[0] = (
                        "security" in pool._running and "parent" not in pool._running
                    )
            finally:
                release()

        t = threading.Thread(target=security_child)
        t.start()

        # Give the child a moment to attempt acquisition. While the parent still holds
        # the slot, the child must be blocked (not granted).
        time.sleep(0.2)
        pool = scheduler._pools['_shared_sequential_slot_']
        with pool._cond:
            assert "security" not in pool._running, \
                "Child acquired before parent yielded — yield did not happen"

        # Parent YIELDS its slot (the real engine helper). This frees the permit.
        engine._release_slot(parent_inst, "parent", "before_security_check")
        assert parent_inst._slot_release is None, "Yield must nullify _slot_release"

        # Child should now acquire and complete without deadlock.
        t.join(timeout=10)
        assert not t.is_alive(), "DEADLOCK: Security child never completed after yield"
        assert child_held_during_run[0], \
            "While the child ran, the parent should have released its slot"

        # Parent REACQUIRES its slot (the real engine helper).
        reacquired = engine.reacquire_for(parent_inst, "parent", "after_security_check")
        assert reacquired is True, "Parent failed to reacquire after Security check"
        assert parent_inst._slot_release is not None, \
            "Reacquire must re-bind _slot_release on the parent"

        # Verify exactly one holder remains (the parent) and it's released cleanly.
        with pool._cond:
            assert "parent" in pool._running, "Parent should hold the slot after reacquire"
            assert "security" not in pool._running, "Security should have released"

        # Clean up.
        parent_inst._slot_release()
        with pool._cond:
            assert len(pool._running) == 0, "Slot leak after final release"


# ============================================================================
# T4 — Compressor yield/reacquire
# ============================================================================

class TestT4CompressorYieldReacquire:
    """Same yield→run→reacquire pattern as Security, but for the Compressor. The
    caller's slot is released before compression runs; the compressor acquires &
    completes; the caller resumes holding its slot."""

    def test_compression_yield_run_reacquire_in_order(self, scheduler):
        from agent_cascade.execution_engine import ExecutionEngine

        api_base = "http://seq-api"
        conc = 0

        # Caller holds the shared sequential slot.
        caller_release = _acquire(scheduler, api_base, conc, "caller", "coder")
        assert caller_release is not None

        # Stub get_agent_slot_info so reacquire_for resolves the SAME conc=0 shared
        # sequential slot the caller holds (mirrors EndpointScheduler.get_slot_info).
        mock_pool = MagicMock()
        mock_router = MagicMock()
        mock_router.scheduler = scheduler
        mock_router.get_agent_slot_info.return_value = {
            'slot_key': '_shared_sequential_slot_',
            'is_sequential': True,
            'concurrency_limit': 0,
            'api_base': api_base,
            'needs_slot': True,
        }
        mock_pool.api_router = mock_router
        engine = ExecutionEngine(mock_pool)

        caller_inst = MagicMock()
        caller_inst.instance_name = "caller"
        caller_inst.agent_class = "coder"
        caller_inst._state_lock = threading.RLock()
        caller_inst._slot_release = caller_release
        caller_inst._slot_key = None

        # Record the order of observable events to assert in-order yield→run→reacquire.
        events: List[str] = []
        ev_lock = threading.Lock()

        compressor_ran = threading.Event()

        def compressor_child():
            release = _acquire(scheduler, api_base, conc, "compressor", "compressor", timeout=10.0)
            try:
                with ev_lock:
                    events.append("run")
                compressor_ran.set()
            finally:
                release()

        t = threading.Thread(target=compressor_child)
        t.start()
        time.sleep(0.2)  # Let the compressor enqueue and block on the caller's slot.

        # YIELD (real helper) — must free the permit so the compressor can proceed.
        engine._release_slot(caller_inst, "caller", "before_compression")
        with ev_lock:
            events.append("yield_done")

        t.join(timeout=10)
        assert not t.is_alive(), "DEADLOCK: Compressor never completed after yield"
        assert compressor_ran.is_set()

        # REACQUIRE (real helper) — caller resumes holding its slot.
        reacquired = engine.reacquire_for(caller_inst, "caller", "after_compression")
        with ev_lock:
            events.append("reacquire_done")

        assert reacquired is True
        # In-order: yield happened before run, and reacquire happened after run.
        assert events.index("yield_done") < events.index("run"), \
            f"Yield must precede compressor run: {events}"
        assert events.index("run") < events.index("reacquire_done"), \
            f"Reacquire must follow compressor run: {events}"

        # Caller holds the slot again; compressor released.
        pool = scheduler._pools['_shared_sequential_slot_']
        with pool._cond:
            assert "caller" in pool._running, "Caller should hold its slot after reacquire"
            assert "compressor" not in pool._running, "Compressor should have released"

        caller_inst._slot_release()
        with pool._cond:
            assert len(pool._running) == 0, "Slot leak after final release"


# ============================================================================
# T6 — A→B(async)→C (regression for removed ancestor-walk)
# ============================================================================

class TestT6AsyncChildWaitsInFIFO:
    """KEY behavioral change. Setup: Agent A holds a conc=0 slot. A spawns B async.
    B's child C needs A's slot pool.

    OLD behavior (ancestor-walk): deadlock — the walk would force sync and C could
    never be granted because A was blocked waiting on it.

    NEW behavior: C waits in the FIFO queue for A's slot, then completes when A
    releases. Assert C eventually completes within a timeout (no deadlock)."""

    def test_c_waits_then_completes_no_deadlock(self, scheduler):
        api_base = "http://seq-api"
        conc = 0

        # Agent A holds the shared sequential slot.
        release_a = _acquire(scheduler, api_base, conc, "A", "orchestrator")
        assert release_a is not None

        c_completed = threading.Event()
        c_error: List[str] = []

        def child_c():
            """C needs the same pool A holds. It must WAIT (not deadlock), then be
            granted in FIFO order once A releases."""
            try:
                release = _acquire(scheduler, api_base, conc, "C", "coder", timeout=15.0)
                c_completed.set()
                release()
            except SlotQueueTimeout:
                c_error.append("timeout")

        t_c = threading.Thread(target=child_c)
        t_c.start()

        # While A holds the slot, C must be WAITING — not granted, not deadlocked.
        time.sleep(0.3)
        assert not c_completed.is_set(), \
            "C should be blocked while A still holds the slot (it waits, not skips)"
        pool = scheduler._pools['_shared_sequential_slot_']
        with pool._cond:
            assert "A" in pool._running
            assert "C" not in pool._running  # C is queued as a waiter, not running
            # C should appear as a waiter in the FIFO queue.
            waiter_names = [t.instance_name for t in pool._waiters.values()]
            assert "C" in waiter_names, f"C should be queued as a waiter: {waiter_names}"

        # A releases (e.g., finishes its turn / yields). C is now granted in FIFO order.
        release_a()

        # C must complete within a bounded time — NO deadlock.
        assert c_completed.wait(timeout=10), \
            f"DEADLOCK: C never completed after A released (errors={c_error})"
        assert not c_error, f"C raised an unexpected error: {c_error}"

        t_c.join(timeout=5)
        assert not t_c.is_alive()

    def test_b_async_spawns_c_and_a_releases(self, scheduler):
        """Full A→B(async)→C flow: B runs on its own thread and spawns C. A holds the
        slot; when A releases, C (spawned by async B) is granted in FIFO order."""
        api_base = "http://seq-api"
        conc = 0

        release_a = _acquire(scheduler, api_base, conc, "A", "orchestrator")

        c_completed = threading.Event()

        def child_c():
            try:
                release = _acquire(scheduler, api_base, conc, "C", "coder", timeout=15.0)
                c_completed.set()
                release()
            except SlotQueueTimeout:
                pass

        # B (async) spawns C — modelled as a thread that enqueues C's acquisition.
        def agent_b_async():
            t_c = threading.Thread(target=child_c)
            t_c.start()
            return t_c

        b_thread = threading.Thread(target=agent_b_async)
        b_thread.start()
        time.sleep(0.3)  # Let B spawn C and let C enqueue behind A.

        pool = scheduler._pools['_shared_sequential_slot_']
        with pool._cond:
            assert "A" in pool._running
            waiter_names = [t.instance_name for t in pool._waiters.values()]
            assert "C" in waiter_names, f"C (spawned by async B) should be waiting: {waiter_names}"

        # A releases → C completes. No deadlock.
        release_a()
        assert c_completed.wait(timeout=10), \
            "DEADLOCK: C (spawned by async B) never completed after A released"


# ============================================================================
# T8 — conc=N capacity
# ============================================================================

class TestT8ConcurrencyCapacity:
    """N agents on a conc=N endpoint run up to N concurrently; the (N+1)th waits in
    FIFO. Permit count never exceeds N."""

    def test_never_exceeds_capacity_n(self, scheduler):
        api_base = "http://par-api"
        n = 4
        total_agents = 20

        peak = [0]
        peak_lock = threading.Lock()
        done = [0]
        done_lock = threading.Lock()

        def worker(i: int):
            release = _acquire(scheduler, api_base, n, f"agent_{i}")
            try:
                with peak_lock:
                    active = scheduler.count_active(api_base, n)
                    peak[0] = max(peak[0], active)
                time.sleep(0.02)  # Overlap so up to N run concurrently.
            finally:
                release()
                with done_lock:
                    done[0] += 1

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(total_agents)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert done[0] == total_agents, f"Only {done[0]}/{total_agents} completed"
        assert peak[0] <= n, f"Peak active ({peak[0]}) exceeded capacity ({n})"
        # We must actually have reached the full capacity (not under-utilized).
        assert peak[0] == n, f"Expected to reach full capacity {n}, only hit {peak[0]}"

    def test_nth_plus_one_waits_in_fifo(self, scheduler):
        """With N permits held, an (N+1)th agent must WAIT (block), then be granted
        once one permit frees."""
        api_base = "http://par-api"
        n = 2

        # Occupy all N permits.
        holders = [_acquire(scheduler, api_base, n, f"h_{i}") for i in range(n)]

        extra_ran = threading.Event()

        def extra_agent():
            release = _acquire(scheduler, api_base, n, "extra", timeout=10.0)
            try:
                extra_ran.set()
            finally:
                release()

        t = threading.Thread(target=extra_agent)
        t.start()
        time.sleep(0.3)

        # While all N permits are held, the (N+1)th must be blocked as a waiter.
        assert not extra_ran.is_set(), \
            "Extra agent should be blocked while all N permits are held"
        pool = scheduler._pools[api_base]
        with pool._cond:
            assert len(pool._running) == n, f"Expected {n} running, got {len(pool._running)}"
            waiter_names = [w.instance_name for w in pool._waiters.values()]
            assert "extra" in waiter_names, f"'extra' should be waiting: {waiter_names}"

        # Free one permit → the extra agent is granted.
        holders[0]()
        t.join(timeout=10)
        assert not t.is_alive(), "DEADLOCK: extra agent never granted after a permit freed"
        assert extra_ran.is_set()

        # Release remaining holders and confirm no leak.
        for h in holders[1:]:
            h()
        with pool._cond:
            assert len(pool._running) == 0, "Slot leak after all releases"


# ============================================================================
# T5 — Reacquire timeout degrades to slotless
# ============================================================================

class TestT5ReacquireTimeout:
    """When reacquire_for times out, the instance is left in a clean slotless state."""

    def test_reacquire_timeout_degrades_to_slotless(self, scheduler):
        from agent_cascade.execution_engine import ExecutionEngine
        from agent_cascade.slot_queue import SlotQueueTimeout

        api_base = "http://seq-api"
        conc = 0

        # Build engine with a fully mocked router whose scheduler.acquire raises.
        mock_pool = MagicMock()
        mock_router = MagicMock()
        mock_router.get_agent_slot_info.return_value = {
            'slot_key': '_shared_sequential_slot_',
            'is_sequential': True,
            'concurrency_limit': 0,
            'api_base': api_base,
            'needs_slot': True,
        }
        # scheduler.acquire raises SlotQueueTimeout to simulate a timeout.
        mock_ticket = MagicMock()
        mock_ticket.ticket_id = "test-ticket"
        mock_ticket.agent_name = "caller"
        mock_ticket.instance_name = "caller"
        mock_router.scheduler.acquire.side_effect = SlotQueueTimeout(
            mock_ticket, "simulated timeout for test"
        )
        mock_pool.api_router = mock_router
        engine = ExecutionEngine(mock_pool)

        # Instance that previously held a slot but released it (e.g., yielded to child).
        inst = MagicMock()
        inst.instance_name = "caller"
        inst.agent_class = "coder"
        inst._state_lock = threading.RLock()
        inst._slot_release = None  # Already released before reacquire attempt.
        inst._slot_key = None

        result = engine.reacquire_for(inst, "caller", "test_timeout")

        assert result is False, "reacquire_for should return False on timeout"
        assert inst._slot_release is None, "_slot_release must be None after failed reacquire"
        assert inst._slot_key is None, "_slot_key must be None after failed reacquire"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
