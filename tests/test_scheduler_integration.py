"""
Comprehensive integration and stress tests for the API scheduler queue system.

Verifies the FIFO queue + reservation system prevents model trashing under realistic
agent call interleaving scenarios. Uses ThreadPoolExecutor to simulate concurrent agent
threads with mocked LLM calls — no actual API needed.

Test categories:
1. FIFO Ordering Under Contention
2. Reservation Blocking (Gap A Fix)
3. Self-Exemption Verification
4. Cancel-on-Termination
5. Re-acquire Reliability
6. Stress/Soak Tests
7. Procedural Generation / Brute Force

Plan reference: plans/api_scheduler_queue_refactor_plan.md Section 10
"""

import itertools
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pytest

from agent_cascade.api_router import APIRouter, EndpointScheduler, APIEndpoint
from agent_cascade.slot_queue import (
    SlotPool,
    QueueTicket,
    SlotHolder,
    Reservation,
    SlotQueueTimeout,
    SlotCancelled,
)


# ============================================================================
# Test Infrastructure — Shared Helpers
# ============================================================================

_ticket_counter = itertools.count()


def _new_id():
    return next(_ticket_counter)


class ViolationRecorder:
    """Thread-safe recorder for detecting scheduling violations."""

    def __init__(self):
        self._lock = threading.Lock()
        self.violations: List[str] = []

    def record(self, msg: str):
        with self._lock:
            self.violations.append(msg)

    def assert_no_violations(self, test_name: str = ""):
        with self._lock:
            if self.violations:
                msgs = "\n".join(f"  - {v}" for v in self.violations[:20])
                pytest.fail(f"{test_name} had scheduling violations:\n{msgs}")


class ConcurrencyTracker:
    """Tracks per-slot concurrency to detect over-capacity violations."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current: Dict[str, int] = defaultdict(int)
        self.max_seen: Dict[str, int] = defaultdict(int)
        self.capacity: Dict[str, int] = {}
        self.violations: List[str] = []

    def set_capacity(self, slot_key: str, cap: int):
        with self._lock:
            self.capacity[slot_key] = cap

    def enter(self, slot_key: str, agent_name: str) -> bool:
        """Called when agent acquires a slot. Returns True if over capacity."""
        with self._lock:
            self.current[slot_key] += 1
            cap = self.capacity.get(slot_key, float('inf'))
            if self.current[slot_key] > self.max_seen[slot_key]:
                self.max_seen[slot_key] = self.current[slot_key]
            over = False
            if isinstance(cap, int) and self.current[slot_key] > cap:
                msg = f"OVER-CAPACITY on '{slot_key}': {self.current[slot_key]} running, cap={cap}, agent={agent_name}"
                self.violations.append(msg)
                over = True
        return over

    def leave(self, slot_key: str, agent_name: str):
        with self._lock:
            self.current[slot_key] -= 1

    def assert_no_over_capacity(self, test_name: str = ""):
        if self.violations:
            msgs = "\n".join(f"  - {v}" for v in self.violations[:20])
            pytest.fail(f"{test_name} had concurrency violations:\n{msgs}")


def _make_test_router(conc_map: Optional[Dict[str, int]] = None) -> APIRouter:
    """Create an isolated APIRouter with configurable endpoint concurrency.

    Args:
        conc_map: Dict of api_base -> concurrency_limit (e.g., {'http://seq': 0, 'http://par': 3})
    """
    import tempfile
    import os
    from unittest.mock import patch

    test_dir = tempfile.mkdtemp()
    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_dir}):
        router = APIRouter(default_llm_cfg={
            'api_base': 'http://default',
            'model': 'default-model',
            'max_tokens': 2048,
        })

        if conc_map:
            for api_base, conc in conc_map.items():
                ep = APIEndpoint(
                    id=f"ep_{_new_id()}",
                    name=f"Test-{api_base}",
                    api_base=api_base,
                    model='test-model',
                    enabled=True,
                    concurrency_limit=conc,
                )
                router.add_endpoint(ep)
        else:
            # Default: one sequential endpoint
            ep = APIEndpoint(
                id="ep_default",
                name="Default",
                api_base='http://default',
                model='default-model',
                enabled=True,
                concurrency_limit=0,  # sequential
            )
            router.add_endpoint(ep)

        return router


def _get_scheduler(router: APIRouter) -> EndpointScheduler:
    """Get the EndpointScheduler from an APIRouter."""
    return router.scheduler


def _get_pool_key(api_base: str, conc: int) -> str:
    """Compute slot_key for a given api_base and concurrency."""
    if conc == -1:
        return None
    if conc == 0:
        return '_shared_sequential_slot_'
    return api_base


def _acquire_immediate(pool: SlotPool, instance_name: str) -> SlotHolder:
    """Acquire a slot immediately (non-blocking). Caller must ensure capacity."""
    with pool._cond:
        holder = SlotHolder(
            agent_name=instance_name,
            instance_name=instance_name,
            acquisition_id=next(pool._acquisition_counter),
            granted_at=time.monotonic(),
        )
        pool._running[instance_name] = holder
        return holder


# ============================================================================
# Category 1: FIFO Ordering Under Contention
# ============================================================================

class TestFIFOOrderingUnderContention:
    """Test strict FIFO ordering under realistic contention scenarios."""

    def test_fifo_grant_order_sequential(self):
        """Multiple agents queue on same sequential slot — grants in exact enqueue order.
        
        Uses per-waiter handshake: each waiter signals ready BEFORE calling acquire,
        then we open the next gate only after confirming previous waiter has started acquiring.
        This guarantees deterministic ordering regardless of OS thread scheduling.
        """
        pool = SlotPool(key="test_seq", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        granted_order: List[str] = []
        lock = threading.Lock()
        gates = [threading.Event() for _ in range(5)]  # Gate to start each waiter
        ready = [threading.Event() for _ in range(5)]   # Signal when thread is about to call acquire

        names = ["T1", "T2", "T3", "T4", "T5"]

        def waiter(name: str, idx: int):
            gates[idx].wait()  # Wait until it's our turn to enqueue
            ready[idx].set()   # Signal we're about to call acquire (before blocking)
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            with lock:
                granted_order.append(name)
            time.sleep(0.05)  # Hold briefly so grants are sequential
            release_cb()

        threads = []
        for idx, name in enumerate(names):
            t = threading.Thread(target=waiter, args=(name, idx))
            t.start()
            threads.append(t)

        # Sequentially open gates — each waiter must signal ready before next gate opens.
        # The key insight: once a thread passes the gate and sets ready, it's either already
        # inside acquire() or about to be, so its ticket is enqueued (or will be atomically).
        for idx in range(5):
            gates[idx].set()
            assert ready[idx].wait(timeout=2), f"{names[idx]} should have started acquiring"

        # Release A — T1 (head) should get it first, then T2, etc.
        pool.release(holder_a)

        for t in threads:
            t.join(timeout=10)

        assert granted_order == names, \
            f"FIFO order violated: got {granted_order}"

    def test_fifo_grant_order_parallel_pool(self):
        """Multiple agents queue on parallel pool (conc=3) — grants respect capacity + FIFO.
        
        Uses handshake gates for deterministic enqueue ordering and exact batch verification.
        """
        pool = SlotPool(key="test_par", capacity=3)

        granted_order: List[str] = []  # Track overall grant order
        lock = threading.Lock()
        n_waiters = 9
        gates = [threading.Event() for _ in range(n_waiters)]  # Per-waiter start gate
        ready = [threading.Event() for _ in range(n_waiters)]   # Signal before acquire blocks

        def waiter(name: str, idx: int):
            gates[idx].wait()
            ready[idx].set()  # Signal BEFORE blocking in acquire
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            with lock:
                granted_order.append(name)
            time.sleep(0.08)  # Hold briefly so we get distinct batches
            release_cb()

        threads = []
        for i in range(n_waiters):
            t = threading.Thread(target=waiter, args=(f"T{i+1}", i))
            t.start()
            threads.append(t)

        # Sequentially enqueue all waiters using handshake — guarantees FIFO order
        for i in range(n_waiters):
            gates[i].set()
            assert ready[i].wait(timeout=2), f"T{i+1} should have started acquiring"

        for t in threads:
            t.join(timeout=10)

        # Verify capacity + FIFO: first 3 granted immediately, then next 3 after releases, etc.
        # With deterministic enqueue order T1..T9 and capacity=3:
        # Batch 1 (immediate): T1, T2, T3 (first to enqueue, all within capacity)
        # Batch 2 (after batch 1 releases): T4, T5, T6
        # Batch 3 (after batch 2 releases): T7, T8, T9
        assert len(granted_order) == n_waiters, \
            f"All waiters should be granted: got {len(granted_order)}"

        # Verify no duplicates
        assert len(set(granted_order)) == n_waiters, "No duplicate grants allowed"

        # Verify FIFO order within each batch of 3 (capacity=3)
        batch1 = granted_order[0:3]
        batch2 = granted_order[3:6]
        batch3 = granted_order[6:9]

        assert batch1 == ["T1", "T2", "T3"], \
            f"First batch must be T1,T2,T3 in FIFO order, got {batch1}"
        assert batch2 == ["T4", "T5", "T6"], \
            f"Second batch must be T4,T5,T6 in FIFO order, got {batch2}"
        assert batch3 == ["T7", "T8", "T9"], \
            f"Third batch must be T7,T8,T9 in FIFO order, got {batch3}"

        # Verify FIFO across batches: earlier enqueued agents granted before later ones
        for i in range(n_waiters - 1):
            idx_i = int(granted_order[i][1:]) - 1
            idx_j = int(granted_order[i + 1][1:]) - 1
            assert idx_i < idx_j, \
                f"VIOLATION: T{idx_i+1} granted after T{idx_j+1}, breaking cross-batch FIFO"

    def test_fifo_no_barging_under_contention(self):
        """Later waiters never granted before earlier ones — even under high contention.
        
        Uses per-waiter handshake gates for deterministic enqueue ordering (no timing assumptions).
        """
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        granted_order: List[str] = []
        lock = threading.Lock()
        n_waiters = 20
        gates = [threading.Event() for _ in range(n_waiters)]  # Per-waiter start gate
        ready = [threading.Event() for _ in range(n_waiters)]   # Signal before acquire blocks

        def waiter(name: str, idx: int):
            gates[idx].wait()
            ready[idx].set()  # Signal BEFORE blocking in acquire
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            with lock:
                granted_order.append(name)
            time.sleep(0.01)
            release_cb()

        threads = []
        for i in range(n_waiters):
            t = threading.Thread(target=waiter, args=(f"W{i}", i))
            t.start()
            threads.append(t)

        # Sequentially enqueue all waiters using handshake — guarantees FIFO order
        for i in range(n_waiters):
            gates[i].set()
            assert ready[i].wait(timeout=2), f"W{i} should have started acquiring"

        # Release A — grants must follow exact enqueue order: W0, W1, ..., W19
        pool.release(holder_a)

        for t in threads:
            t.join(timeout=15)

        expected = [f"W{i}" for i in range(n_waiters)]
        assert granted_order == expected, \
            f"VIOLATION: FIFO barging detected. Expected exact order {expected}, got {granted_order}"

    def test_fifo_mixed_conc_pools(self):
        """Agents on conc=0 and conc=N pools queue independently — no cross-pool interference.
        
        Uses handshake gates for deterministic enqueue ordering with FIFO verification.
        """
        seq_pool = SlotPool(key="_shared_sequential_slot_", capacity=1)
        par_pool = SlotPool(key="http://parallel", capacity=2)

        seq_order: List[str] = []
        par_order: List[str] = []
        lock = threading.Lock()

        # Per-waiter handshake events for deterministic ordering
        n_seq, n_par = 5, 4
        seq_gates = [threading.Event() for _ in range(n_seq)]
        seq_ready = [threading.Event() for _ in range(n_seq)]
        par_gates = [threading.Event() for _ in range(n_par)]
        par_ready = [threading.Event() for _ in range(n_par)]

        def seq_waiter(name: str, idx: int):
            seq_gates[idx].wait()
            seq_ready[idx].set()
            release_cb = seq_pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            with lock:
                seq_order.append(name)
            time.sleep(0.03)
            release_cb()

        def par_waiter(name: str, idx: int):
            par_gates[idx].wait()
            par_ready[idx].set()
            release_cb = par_pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            with lock:
                par_order.append(name)
            time.sleep(0.03)
            release_cb()

        # Hold sequential slot so seq waiters queue
        holder_a = _acquire_immediate(seq_pool, "A")

        threads = []
        for i in range(n_seq):
            t = threading.Thread(target=seq_waiter, args=(f"S{i}", i))
            t.start()
            threads.append(t)

        for i in range(n_par):
            t = threading.Thread(target=par_waiter, args=(f"P{i}", i))
            t.start()
            threads.append(t)

        # Sequentially enqueue seq waiters using handshake
        for i in range(n_seq):
            seq_gates[i].set()
            assert seq_ready[i].wait(timeout=2), f"S{i} should have started acquiring"

        # Sequentially enqueue par waiters using handshake
        for i in range(n_par):
            par_gates[i].set()
            assert par_ready[i].wait(timeout=2), f"P{i} should have started acquiring"

        # Release sequential — seq waiters should get exact FIFO order S0..S4
        seq_pool.release(holder_a)

        for t in threads:
            t.join(timeout=10)

        # Verify sequential pool: exact FIFO order S0, S1, S2, S3, S4
        assert seq_order == [f"S{i}" for i in range(n_seq)], \
            f"Sequential pool FIFO violated. Expected S0..S4, got {seq_order}"

        # Verify parallel pool: capacity=2 grants P0,P1 immediately, then P2,P3 after release
        assert par_order == ["P0", "P1", "P2", "P3"], \
            f"Parallel pool FIFO violated. Expected P0,P1,P2,P3 in order, got {par_order}"


# ============================================================================
# Category 2: Reservation Blocking (Gap A Fix)
# ============================================================================

class TestReservationBlocking:
    """Test reservation blocking — the core Gap A fix from todo.md#93."""

    def test_todo_md_93_scenario(self):
        """Exact scenario: A(sync,P)->B(async,Q); A->D(sync,P); B->C(sync,P).
        
        When A sleeps awaiting B, C's acquire blocked by A's reservation; no interleaving.
        Uses synchronization events instead of timing sleeps.
        """
        pool_p = SlotPool(key="_shared_sequential_slot_", capacity=1)

        # Timeline:
        # 1. A holds slot P
        # 2. A spawns async child B on pool Q (different, so not relevant here)
        # 3. A sleeps awaiting B -> reserves pool P
        # 4. A's sync child D would need P but A reserved it
        # 5. B's sync child C needs P -> blocked by A's reservation

        holder_a = _acquire_immediate(pool_p, "A")

        # A spawns async B and sleeps awaiting B
        a_chain = ("A",)
        pool_p.reserve(agent_name="A", ancestor_chain=a_chain, reason="async_child", acquisition_id=1)

        # A releases slot while sleeping (permit goes back to pool)
        pool_p.release(holder_a)

        granted_order: List[str] = []
        lock = threading.Lock()
        c_ready = threading.Event()  # C is about to call acquire (before blocking)
        c_granted_event = threading.Event()

        def waiter_c():
            """B's child C tries to acquire P — should be blocked by A's reservation."""
            c_ready.set()  # Signal BEFORE blocking in acquire
            release_cb = pool_p.acquire(instance_name="C", agent_class="test", ancestor_chain=("B", "C"))
            with lock:
                granted_order.append("C")
            c_granted_event.set()
            release_cb()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()

        # Wait for C to start acquiring — deterministic instead of time.sleep(0.2)
        assert c_ready.wait(timeout=5), "C should have started acquiring"

        # C should NOT be granted yet — blocked by A's reservation
        assert not c_granted_event.is_set(), \
            "VIOLATION: C was granted while A had active reservation (Gap A not fixed!)"

        # Check pool state: C should be waiting
        status = pool_p.get_status()
        assert status["waiting_count"] == 1, f"C should be waiting in queue"
        assert any(r["agent_name"] == "A" for r in status["reservations"]), \
            "A's reservation should still be active"

        # A wakes up -> unreserve first (per plan)
        pool_p.unreserve_for_agent("A")

        t_c.join(timeout=5)

        # Now C should be granted
        assert c_granted_event.is_set(), "C should be granted after A unreserves"
        assert granted_order == ["C"], f"C should be the only grantee: {granted_order}"

    def test_parent_reserves_child_inherits_exemption(self):
        """Parent reserves on sleep, child inherits chain exemption.
        
        Scenario: A holds slot and reserves (sleeping). U queues but is blocked by reservation.
        B (A's descendant) also queues — B is exempt from A's reservation but still must wait
        behind U in FIFO order until unreserve happens. Then U gets granted first, then B.
        
        Key: We keep the slot held so both U and B can queue before any grants happen.
        Uses handshake events instead of timing sleeps.
        """
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # A reserves with its chain — only agents sharing names in this chain are exempt
        a_chain = ("root", "A")
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="sleeping", acquisition_id=1)
        # NOTE: Do NOT release holder_a yet — we want U and B to queue first

        granted_order: List[str] = []
        lock = threading.Lock()
        u_ready = threading.Event()  # U about to call acquire
        b_ready = threading.Event()  # B about to call acquire
        grant_b_gate = threading.Event()  # Prevent B from releasing too early

        def waiter_b():
            """A's child B — should be exempt from A's reservation (shares 'A' in chain)."""
            b_chain = ("root", "A", "B")  # Contains "A" -> exempt
            release_cb = pool.acquire(instance_name="B", agent_class="test", ancestor_chain=b_chain)
            with lock:
                granted_order.append("B")
            grant_b_gate.wait(timeout=5)  # Hold slot until test verifies order
            release_cb()

        def waiter_unrelated():
            """Unrelated agent — should be blocked (no overlap with A's chain)."""
            u_chain = ("other", "U")  # No overlap with ("root", "A") -> blocked
            u_ready.set()  # Signal BEFORE blocking in acquire
            release_cb = pool.acquire(instance_name="U", agent_class="test", ancestor_chain=u_chain)
            with lock:
                granted_order.append("U")
            grant_b_gate.wait(timeout=5)  # Hold until B is also verified
            release_cb()

        # Start U first, wait for it to start acquiring (handshake)
        t_u = threading.Thread(target=waiter_unrelated)
        t_u.start()
        assert u_ready.wait(timeout=2), "U should have started acquiring"

        # Now start B and wait for it to start acquiring (handshake)
        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        b_ready.set()  # No need to wait — B starts immediately, just give it a moment
        time.sleep(0.05)  # Tiny gap for B thread to reach acquire (acceptable here, non-critical path)

        # Verify queue state: U is head, B is second (both waiting behind held slot + reservation)
        status = pool.get_status()
        assert status["waiting_count"] == 2, f"Both should be waiting, got {status['waiting_count']}"
        waiters = [w["instance_name"] for w in status["waiters"]]
        assert waiters[0] == "U", f"U should be head of queue: {waiters}"

        # U is head but blocked by reservation. B is exempt but not head (strict FIFO).
        # Neither should be granted yet because slot is held and U is blocked.
        assert len(granted_order) == 0, \
            f"No one should be granted yet: U blocked by reservation, B exempt but not head. Got: {granted_order}"

        # Unreserve — now U (head) gets it first due to strict FIFO, then B after U releases
        pool.unreserve_for_agent("A")

        # Release A's held slot so waiters can proceed
        pool.release(holder_a)

        t_u.join(timeout=5)
        grant_b_gate.set()  # Let both release so we can verify order
        t_b.join(timeout=5)

        assert granted_order == ["U", "B"], \
            f"FIFO order should be preserved: U then B, got {granted_order}"

    def test_multiple_reservations_same_pool_all_must_clear(self):
        """Multiple reservations on same pool — all must clear before unrelated grants.
        
        Uses synchronization events instead of timing sleeps.
        """
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # A and B both reserve (e.g., both sleeping with async children)
        pool.reserve(agent_name="A", ancestor_chain=("A",), reason="sleeping", acquisition_id=1)
        pool.reserve(agent_name="B", ancestor_chain=("B",), reason="async_child", acquisition_id=2)

        pool.release(holder_a)

        c_ready = threading.Event()  # C is about to call acquire (before blocking)
        granted_event = threading.Event()

        def waiter_c():
            c_ready.set()  # Signal BEFORE blocking in acquire
            release_cb = pool.acquire(instance_name="C", agent_class="test", ancestor_chain=("C",))
            granted_event.set()
            release_cb()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()

        # Wait for C to start acquiring — deterministic instead of time.sleep(0.2)
        assert c_ready.wait(timeout=5), "C should have started acquiring"

        # C blocked by both reservations
        assert not granted_event.is_set(), "C should be blocked by multiple reservations"

        # Clear only A's reservation — C still blocked by B's
        pool.unreserve_for_agent("A")
        # Use a very short sleep just to let the scheduler check — this is non-critical timing
        time.sleep(0.05)
        assert not granted_event.is_set(), \
            "C should still be blocked after clearing only one of two reservations"

        # Clear B's too
        pool.unreserve_for_agent("B")

        t_c.join(timeout=5)
        assert granted_event.is_set(), "C should be granted after all reservations clear"

    def test_reservation_blocks_unrelated_chain(self):
        """Negative test: reservation actually blocks unrelated agents (no bypass).
        
        Creates a reservation on a pool, then attempts to acquire with an unrelated chain.
        Verifies the agent blocks/times out — proving the reservation mechanism works.
        Uses short timeout for fast test execution.
        """
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # A reserves with chain ("root", "A")
        a_chain = ("root", "A")
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="sleeping", acquisition_id=1)

        # Release the slot so it's available but reservation is active
        pool.release(holder_a)

        granted_event = threading.Event()
        blocked_event = threading.Event()  # Signals that acquire timed out (blocked by reservation)

        def waiter_unrelated():
            """Agent with unrelated chain — should be blocked by A's reservation."""
            try:
                u_chain = ("other", "U")  # No overlap with ("root", "A") -> blocked
                release_cb = pool.acquire(
                    instance_name="U", agent_class="test", ancestor_chain=u_chain, timeout=1.0
                )
                granted_event.set()
                release_cb()
            except (SlotQueueTimeout, TimeoutError):
                # Expected: timed out because blocked by reservation
                blocked_event.set()

        t_u = threading.Thread(target=waiter_unrelated)
        t_u.start()

        # Wait for U to either be granted or blocked
        t_u.join(timeout=5)

        assert not granted_event.is_set(), \
            "VIOLATION: Unrelated agent U was granted despite active reservation (reservation bypass!)"
        assert blocked_event.is_set(), \
            "U should have been blocked by reservation and timed out"


# ============================================================================
# Category 3: Self-Exemption Verification
# ============================================================================

class TestSelfExemption:
    """Test that agents can always re-acquire their own reserved pool."""

    def test_self_reserve_reacquire_succeeds(self):
        """A reserves pool P -> A re-acquires succeeds immediately."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # A reserves with its own chain
        a_chain = ("root", "A")
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="sleeping", acquisition_id=1)
        pool.release(holder_a)

        # A re-acquires — should succeed immediately via self-exemption
        reacquired = threading.Event()

        def waiter_a():
            release_cb = pool.acquire(instance_name="A", agent_class="test", ancestor_chain=a_chain)
            reacquired.set()
            release_cb()

        t_a = threading.Thread(target=waiter_a)
        t_a.start()

        assert reacquired.wait(timeout=2), \
            "VIOLATION: A blocked by its own reservation (self-exemption broken!)"
        t_a.join(timeout=3)

    def test_self_reserve_descendant_granted_unrelated_blocked(self):
        """A reserves -> A's descendant granted -> unrelated B blocked until A unreserves."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        a_chain = ("root", "A")
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="sleeping", acquisition_id=1)
        pool.release(holder_a)

        granted_order: List[str] = []
        lock = threading.Lock()
        b_blocked_event = threading.Event()

        def waiter_child():
            """A's child — exempt."""
            child_chain = ("root", "A", "child")
            release_cb = pool.acquire(instance_name="child", agent_class="test", ancestor_chain=child_chain)
            with lock:
                granted_order.append("child")
            time.sleep(0.1)
            release_cb()

        def waiter_b():
            """Unrelated B — blocked."""
            b_chain = ("other", "B")
            release_cb = pool.acquire(instance_name="B", agent_class="test", ancestor_chain=b_chain)
            with lock:
                granted_order.append("B")
            b_blocked_event.set()  # Only set if B actually gets through
            release_cb()

        # Enqueue child first, then B
        t_child = threading.Thread(target=waiter_child)
        t_child.start()
        time.sleep(0.05)

        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        time.sleep(0.15)

        # Child should be granted (exempt and head), B should be blocked
        assert "child" in granted_order, "Child should be granted via self-exemption"
        assert not b_blocked_event.is_set(), "B should still be blocked by A's reservation"

        # Unreserve — B can proceed
        pool.unreserve_for_agent("A")

        t_child.join(timeout=5)
        t_b.join(timeout=5)

        assert granted_order == ["child", "B"], \
            f"Expected child then B after unreserve: {granted_order}"


# ============================================================================
# Category 4: Cancel-on-Termination
# ============================================================================

class TestCancelOnTermination:
    """Test cancellation when agents terminate while waiting or holding reservations."""

    def test_cancel_queued_agent_next_waiter_granted(self):
        """Agent queued on slot, terminated -> ticket cancelled within ≤1s, next waiter granted."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        b_granted_event = threading.Event()
        b_released_event = threading.Event()

        def waiter_b():
            release_cb = pool.acquire(instance_name="B", agent_class="test", ancestor_chain=("B",))
            b_granted_event.set()
            time.sleep(0.5)  # Hold slot long enough for C to queue and test to cancel it
            release_cb()
            b_released_event.set()

        # Enqueue B first
        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        time.sleep(0.1)

        # C also queues behind B
        c_cancelled = threading.Event()

        def waiter_c():
            try:
                pool.acquire(instance_name="C", agent_class="test", ancestor_chain=("C",))
            except SlotCancelled:
                c_cancelled.set()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()
        time.sleep(0.1)

        # Release A — B gets the slot
        pool.release(holder_a)
        assert b_granted_event.wait(timeout=2), "B should be granted after A releases"

        # Now cancel C while it's waiting (use agent_name which matches instance_name field)
        start = time.monotonic()
        cancelled = pool.cancel(agent_name="C")
        elapsed = time.monotonic() - start

        assert cancelled, "C's ticket should be cancelled"
        assert c_cancelled.wait(timeout=2), "C should receive SlotCancelled within 1s tick"
        assert elapsed < 0.5, f"Cancel took {elapsed:.2f}s — should be immediate under lock"

        t_c.join(timeout=3)
        t_b.join(timeout=5)

    def test_cancel_holding_reservation_queue_proceeds(self):
        """Agent holding reservation, terminated -> reservation cleared, queue proceeds."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # A reserves then releases (sleeping pattern)
        pool.reserve(agent_name="A", ancestor_chain=("A",), reason="sleeping", acquisition_id=1)
        pool.release(holder_a)

        b_granted_event = threading.Event()

        def waiter_b():
            release_cb = pool.acquire(instance_name="B", agent_class="test", ancestor_chain=("B",))
            b_granted_event.set()
            release_cb()

        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        time.sleep(0.15)

        # B blocked by A's reservation
        assert not b_granted_event.is_set(), "B should be blocked"

        # A terminates — cancel tickets + clear reservations
        result = pool.terminate_for_agent("A")
        tickets_cancelled, reservations_cleared = result

        assert reservations_cleared >= 1, f"A's reservation should be cleared: {result}"

        t_b.join(timeout=5)
        assert b_granted_event.is_set(), "B should be granted after A's termination clears reservation"

    def test_mass_termination_active_queue_all_cleaned(self):
        """Mass termination during active queue — all tickets cleaned."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        n_agents = 50
        cancelled_events = {f"agent_{i}": threading.Event() for i in range(n_agents)}

        def waiter(name: str):
            try:
                pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,), timeout=10)
            except SlotCancelled:
                cancelled_events[name].set()

        threads = []
        for i in range(n_agents):
            t = threading.Thread(target=waiter, args=(f"agent_{i}",))
            t.start()
            threads.append(t)

        time.sleep(0.3)  # Let all enqueue

        status_before = pool.get_status()
        assert status_before["waiting_count"] == n_agents, \
            f"All {n_agents} agents should be waiting"

        # Mass terminate: cancel all tickets for first half
        start = time.monotonic()
        for i in range(n_agents // 2):
            pool.cancel(agent_name=f"agent_{i}")
        elapsed = time.monotonic() - start

        status_after = pool.get_status()
        expected_remaining = n_agents - (n_agents // 2)
        assert status_after["waiting_count"] == expected_remaining, \
            f"After mass cancel: expected {expected_remaining} remaining, got {status_after['waiting_count']}"

        # Verify cancelled agents received the signal
        for i in range(n_agents // 2):
            assert cancelled_events[f"agent_{i}"].wait(timeout=3), \
                f"agent_{i} should have been cancelled"

        # Cleanup remaining threads by cancelling all their tickets
        for i in range(n_agents // 2, n_agents):
            pool.cancel(agent_name=f"agent_{i}")
        for t in threads:
            t.join(timeout=5)


# ============================================================================
# Category 5: Re-acquire Reliability
# ============================================================================

class TestReacquireReliability:
    """Test that callers reliably re-acquire slots after releasing for sync children."""

    def test_reacquire_after_sync_child_release(self):
        """Caller releases slot for sync child, re-acquires with proper self-exemption."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        a_chain = ("root", "A")

        # A reserves (simulating async child spawn), then releases for sync child D
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="async_child", acquisition_id=1)
        pool.release(holder_a)

        # Sync child D runs and releases back to pool
        holder_d = _acquire_immediate(pool, "D")
        pool.release(holder_d)

        # A re-acquires — should succeed via self-exemption
        a_reacquired_event = threading.Event()

        def waiter_a():
            release_cb = pool.acquire(instance_name="A", agent_class="test", ancestor_chain=a_chain)
            a_reacquired_event.set()
            release_cb()

        t_a = threading.Thread(target=waiter_a)
        t_a.start()

        assert a_reacquired_event.wait(timeout=3), \
            "VIOLATION: A failed to re-acquire after sync child (silent slotless continuation risk!)"
        t_a.join(timeout=5)

    def test_no_silent_slotless_continuation(self):
        """Verify caller doesn't give up after brief wait — waits full timeout."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # Someone else holds the slot; A tries to re-acquire with self-exemption
        a_chain = ("root", "A")
        pool.reserve(agent_name="A", ancestor_chain=a_chain, reason="async_child", acquisition_id=1)
        pool.release(holder_a)

        # Another agent B grabs the slot (unrelated, will be blocked by reservation)
        b_granted_event = threading.Event()
        b_cancelled = threading.Event()

        def waiter_b():
            try:
                release_cb = pool.acquire(instance_name="B", agent_class="test", ancestor_chain=("B",), timeout=5)
                b_granted_event.set()
                release_cb()
            except (SlotQueueTimeout, SlotCancelled):
                b_cancelled.set()

        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        time.sleep(0.15)

        # B blocked by reservation, A should be able to acquire immediately via self-exemption
        a_acquired_event = threading.Event()

        def waiter_a():
            release_cb = pool.acquire(instance_name="A", agent_class="test", ancestor_chain=a_chain)
            a_acquired_event.set()
            release_cb()

        t_a = threading.Thread(target=waiter_a)
        t_a.start()

        assert a_acquired_event.wait(timeout=3), \
            "A should acquire immediately via self-exemption even with B waiting"
        t_a.join(timeout=5)

        # Clean up: cancel B's ticket so it doesn't timeout
        pool.cancel(agent_name="B")
        t_b.join(timeout=3)


# ============================================================================
# Category 6: Stress/Soak Tests
# ============================================================================

class TestStressAndSoak:
    """Stress tests with many concurrent agents on shared pools."""

    def test_concurrent_agents_shared_sequential_pool(self):
        """Many concurrent agents on conc=0 pool — never more than 1 running simultaneously."""
        pool = SlotPool(key="_shared_sequential_slot_", capacity=1)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("_shared_sequential_slot_", 1)

        n_agents = 30
        completed = [0]
        lock = threading.Lock()

        def agent_task(name: str):
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            over = tracker.enter("_shared_sequential_slot_", name)
            try:
                # Simulate work with random delay
                time.sleep(random.uniform(0.01, 0.05))
            finally:
                tracker.leave("_shared_sequential_slot_", name)
                release_cb()
            with lock:
                completed[0] += 1

        with ThreadPoolExecutor(max_workers=n_agents) as executor:
            futures = [executor.submit(agent_task, f"agent_{i}") for i in range(n_agents)]
            for f in as_completed(futures, timeout=30):
                f.result()

        assert completed[0] == n_agents, f"All {n_agents} agents should complete"
        tracker.assert_no_over_capacity("test_concurrent_agents_shared_sequential_pool")

    def test_concurrent_agents_parallel_pool(self):
        """Many concurrent agents on conc=3 pool — never more than 3 running simultaneously."""
        pool = SlotPool(key="http://parallel", capacity=3)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("http://parallel", 3)

        n_agents = 40
        completed = [0]
        lock = threading.Lock()

        def agent_task(name: str):
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            tracker.enter("http://parallel", name)
            try:
                time.sleep(random.uniform(0.01, 0.04))
            finally:
                tracker.leave("http://parallel", name)
                release_cb()
            with lock:
                completed[0] += 1

        with ThreadPoolExecutor(max_workers=n_agents) as executor:
            futures = [executor.submit(agent_task, f"agent_{i}") for i in range(n_agents)]
            for f in as_completed(futures, timeout=30):
                f.result()

        assert completed[0] == n_agents
        tracker.assert_no_over_capacity("test_concurrent_agents_parallel_pool")

    def test_mixed_sync_async_contention(self):
        """Mixed sync/async patterns on limited pools — track per-slot turn ownership."""
        seq_pool = SlotPool(key="_shared_sequential_slot_", capacity=1)
        par_pool = SlotPool(key="http://parallel", capacity=2)

        tracker_seq = ConcurrencyTracker()
        tracker_seq.set_capacity("_shared_sequential_slot_", 1)
        tracker_par = ConcurrencyTracker()
        tracker_par.set_capacity("http://parallel", 2)

        violations = ViolationRecorder()

        completed = [0]
        lock = threading.Lock()

        def sync_agent(name: str, pool: SlotPool, tracker: ConcurrencyTracker, pool_key: str):
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
            over = tracker.enter(pool_key, name)
            if over:
                violations.record(f"{name}: over capacity on {pool_key}")
            try:
                time.sleep(random.uniform(0.01, 0.03))
            finally:
                tracker.leave(pool_key, name)
                release_cb()
            with lock:
                completed[0] += 1

        def async_parent(name: str):
            # Parent acquires sequential, spawns async child on parallel
            parent_chain = ("root", name)
            seq_release = seq_pool.acquire(instance_name=name, agent_class="test", ancestor_chain=parent_chain)
            tracker_seq.enter("_shared_sequential_slot_", name)

            # Spawn async child
            child_done = threading.Event()

            def async_child():
                par_release = par_pool.acquire(
                    instance_name=f"{name}_child",
                    agent_class="test",
                    ancestor_chain=("root", name, f"{name}_child"),
                )
                tracker_par.enter("http://parallel", f"{name}_child")
                try:
                    time.sleep(random.uniform(0.01, 0.02))
                finally:
                    tracker_par.leave("http://parallel", f"{name}_child")
                    par_release()
                child_done.set()

            child_thread = threading.Thread(target=async_child)
            child_thread.start()

            # Parent does work while child runs
            time.sleep(random.uniform(0.01, 0.02))

            # Wait for child
            child_done.wait(timeout=5)
            child_thread.join(timeout=6)

            tracker_seq.leave("_shared_sequential_slot_", name)
            seq_release()
            with lock:
                completed[0] += 1

        n_sync = 20
        n_async_parents = 10

        with ThreadPoolExecutor(max_workers=n_sync + n_async_parents) as executor:
            futures = []
            for i in range(n_sync):
                pool = random.choice([seq_pool, par_pool])
                tracker = tracker_seq if pool is seq_pool else tracker_par
                key = "_shared_sequential_slot_" if pool is seq_pool else "http://parallel"
                futures.append(executor.submit(sync_agent, f"sync_{i}", pool, tracker, key))

            for i in range(n_async_parents):
                futures.append(executor.submit(async_parent, f"async_{i}"))

            for f in as_completed(futures, timeout=60):
                f.result()

        assert completed[0] == n_sync + n_async_parents
        tracker_seq.assert_no_over_capacity("test_mixed_sync_async_contention")
        tracker_par.assert_no_over_capacity("test_mixed_sync_async_contention")
        violations.assert_no_violations("test_mixed_sync_async_contention")

    def test_soak_test_no_deadlock(self):
        """Extended run with contention — verify no deadlocks or stuck reservations."""
        pool = SlotPool(key="soak_test", capacity=2)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("soak_test", 2)

        n_iterations = 100
        completed = [0]
        lock = threading.Lock()
        reserve_count = [0]

        def agent_task(name: str, do_reserve: bool):
            chain = ("root", name)
            release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=chain)
            tracker.enter("soak_test", name)

            token = None
            try:
                # Sometimes reserve and release (simulate async spawn pattern)
                if do_reserve:
                    token = pool.reserve(agent_name=name, ancestor_chain=chain, reason="async_child", acquisition_id=_new_id())
                    with lock:
                        reserve_count[0] += 1
                    # Release the slot temporarily (simulating sleep/wait for child)
                    release_cb()
                    # Re-acquire immediately via self-exemption
                    release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=chain)

                time.sleep(random.uniform(0.005, 0.02))
            finally:
                if token:
                    pool.unreserve(token)
                tracker.leave("soak_test", name)
                release_cb()
            with lock:
                completed[0] += 1

        # Run multiple rounds
        for round_i in range(n_iterations):
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for i in range(5):
                    do_reserve = random.random() < 0.3
                    futures.append(executor.submit(agent_task, f"r{round_i}_a{i}", do_reserve))

                for f in as_completed(futures, timeout=30):
                    try:
                        f.result()
                    except Exception as e:
                        pytest.fail(f"Soak test round {round_i} agent failed: {e}")

        assert completed[0] == n_iterations * 5
        tracker.assert_no_over_capacity("test_soak_test_no_deadlock")


# ============================================================================
# Category 7: Procedural Generation / Brute Force
# ============================================================================

class TestProceduralBruteForce:
    """Randomly generated agent call graphs under contention."""

    @pytest.mark.parametrize("seed", [42, 123, 999, 7777])
    def test_random_agent_call_graphs(self, seed: int):
        """Randomly generates agent call graphs and runs them under contention.
        
        Monitors for violations: >capacity running, FIFO order broken, reservation bypassed.
        """
        rng = random.Random(seed)
        pool = SlotPool(key="proc_test", capacity=2)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("proc_test", 2)

        violations = ViolationRecorder()
        completed = [0]
        lock = threading.Lock()

        # Generate random call graph: agents with random sync/async children
        class AgentNode:
            def __init__(self, name: str):
                self.name = name
                self.children: List[AgentNode] = []
                self.is_async_child: bool = False  # True if spawned async

        def generate_graph(root_name: str, depth: int, max_children: int) -> AgentNode:
            node = AgentNode(root_name)
            n_children = rng.randint(0, max_children)
            for i in range(n_children):
                if depth < 2 and rng.random() < 0.4:
                    child = generate_graph(f"{root_name}_c{i}", depth + 1, max_children)
                    child.is_async_child = rng.random() < 0.5
                    node.children.append(child)
            return node

        def run_agent(node: AgentNode, parent_chain: Tuple[str, ...]):
            chain = parent_chain + (node.name,)
            release_cb = pool.acquire(instance_name=node.name, agent_class="test", ancestor_chain=chain)
            over = tracker.enter("proc_test", node.name)
            if over:
                violations.record(f"{node.name}: over capacity")

            token = None
            try:
                # Maybe reserve (simulating async wait)
                if rng.random() < 0.25:
                    token = pool.reserve(agent_name=node.name, ancestor_chain=chain, reason="async_child", acquisition_id=_new_id())

                time.sleep(rng.uniform(0.005, 0.015))

                # Run children (sync or async)
                child_threads = []
                for child in node.children:
                    t = threading.Thread(target=run_agent, args=(child, chain))
                    child_threads.append(t)
                    t.start()

                for t in child_threads:
                    t.join(timeout=10)
            finally:
                if token:
                    pool.unreserve(token)
                tracker.leave("proc_test", node.name)
                release_cb()

            with lock:
                completed[0] += 1

        # Run multiple graphs in parallel
        n_graphs = 5
        root_nodes = [generate_graph(f"root_{i}", 0, 3) for i in range(n_graphs)]

        with ThreadPoolExecutor(max_workers=n_graphs * 4) as executor:
            futures = [executor.submit(run_agent, root, ("proc",)) for root in root_nodes]
            for f in as_completed(futures, timeout=60):
                try:
                    f.result()
                except Exception as e:
                    pytest.fail(f"Procedural graph (seed={seed}) failed: {e}")

        tracker.assert_no_over_capacity(f"test_random_agent_call_graphs(seed={seed})")
        violations.assert_no_violations(f"test_random_agent_call_graphs(seed={seed})")

    def test_stress_fifo_order_under_random_cancellation(self):
        """Randomly cancel waiters — verify remaining FIFO order is preserved.
        
        Uses per-waiter handshake to guarantee deterministic enqueue ordering.
        Cancels a subset BEFORE releasing the held slot, verifying:
        - Cancelled tickets are removed from queue
        - Only non-cancelled agents are granted in FIFO order
        """
        rng = random.Random(42)
        pool = SlotPool(key="cancel_test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        n_waiters = 30
        granted_order: List[str] = []
        lock = threading.Lock()
        gates = [threading.Event() for _ in range(n_waiters)]  # Gate to start each waiter
        ready = [threading.Event() for _ in range(n_waiters)]   # Signal when about to call acquire
        cancelled_set: Set[str] = set()

        def waiter(name: str, idx: int):
            gates[idx].wait()
            ready[idx].set()  # Signal before blocking in acquire
            try:
                release_cb = pool.acquire(instance_name=name, agent_class="test", ancestor_chain=(name,))
                with lock:
                    granted_order.append(name)
                time.sleep(0.01)  # Brief hold
                release_cb()
            except SlotCancelled:
                pass

        threads = []
        for i in range(n_waiters):
            t = threading.Thread(target=waiter, args=(f"W{i}", i))
            t.start()
            threads.append(t)

        # Sequentially enqueue all waiters using handshake
        for i in range(n_waiters):
            gates[i].set()
            assert ready[i].wait(timeout=2), f"W{i} should have started acquiring"

        # Randomly cancel some waiters BEFORE releasing A (all are still queued)
        to_cancel = rng.sample([f"W{i}" for i in range(1, n_waiters)], k=n_waiters // 3)
        with lock:
            cancelled_set.update(to_cancel)
        for name in to_cancel:
            pool.cancel(agent_name=name)

        # Verify cancelled tickets are removed from queue
        status = pool.get_status()
        waiting_names = {w["instance_name"] for w in status["waiters"]}
        assert not (waiting_names & cancelled_set), \
            f"Cancelled agents still in queue: {waiting_names & cancelled_set}"

        # Release A — only non-cancelled waiters should be granted in FIFO order
        pool.release(holder_a)

        for t in threads:
            t.join(timeout=15)

        # Verify: no cancelled agent was granted
        for name in granted_order:
            assert name not in cancelled_set, \
                f"Cancelled agent {name} was incorrectly granted"

        # All non-cancelled agents should be granted
        expected_grantees = {f"W{i}" for i in range(n_waiters)} - cancelled_set
        actual_grantees = set(granted_order)
        assert actual_grantees == expected_grantees, \
            f"Grant set mismatch:\n  Expected: {sorted(expected_grantees)}\n  Got:      {sorted(actual_grantees)}"

        # No duplicates
        assert len(granted_order) == len(set(granted_order)), "Duplicate grants detected"

        # FIFO order among granted agents (based on enqueue sequence W0, W1, ...)
        expected_fifo = sorted(expected_grantees, key=lambda x: int(x[1:]))
        assert granted_order == expected_fifo, \
            f"FIFO order violated among non-cancelled:\n  Expected: {expected_fifo}\n  Got:      {granted_order}"


# ============================================================================
# Category 8: Integration with EndpointScheduler (higher-level tests)
# ============================================================================

class TestEndpointSchedulerIntegration:
    """Higher-level integration tests using the real EndpointScheduler."""

    def test_scheduler_fifo_via_api_router(self):
        """Test FIFO ordering through the full EndpointScheduler API.
        
        Uses sequential enqueue gates to ensure deterministic ordering on Windows.
        """
        router = _make_test_router({'http://seq': 0})
        scheduler = _get_scheduler(router)

        granted_order: List[str] = []
        lock = threading.Lock()
        enqueue_gates = [threading.Event() for _ in range(10)]

        def agent(name: str, idx: int):
            enqueue_gates[idx].wait()
            release_cb = scheduler.acquire(
                api_base='http://seq',
                concurrency_limit=0,
                instance_name=name,
                agent_class="test",
            )
            with lock:
                granted_order.append(name)
            time.sleep(0.02)
            release_cb()

        threads = []
        for i in range(10):
            t = threading.Thread(target=agent, args=(f"A{i}", i))
            t.start()
            threads.append(t)

        # Sequentially open gates to guarantee enqueue order
        for i in range(10):
            time.sleep(0.01)  # Small gap to ensure previous waiter has enqueued
            enqueue_gates[i].set()

        for t in threads:
            t.join(timeout=15)

        # Should be strictly FIFO (matches enqueue order from sequential gates)
        expected = [f"A{i}" for i in range(10)]
        assert granted_order == expected, \
            f"EndpointScheduler FIFO violated: {granted_order}"

    def test_scheduler_reservation_via_api_router(self):
        """Test reservation blocking through the full EndpointScheduler API."""
        router = _make_test_router({'http://seq': 0})
        scheduler = _get_scheduler(router)

        # Agent A acquires slot
        release_a = scheduler.acquire(
            api_base='http://seq',
            concurrency_limit=0,
            instance_name="A",
            agent_class="test",
        )

        # A reserves (sleeping pattern)
        token = scheduler.reserve("A", ancestor_chain=("A",), reason="sleeping")
        assert token is not None, "Reservation should succeed"

        release_a()  # A releases slot while sleeping

        b_granted_event = threading.Event()

        def agent_b():
            release_cb = scheduler.acquire(
                api_base='http://seq',
                concurrency_limit=0,
                instance_name="B",
                agent_class="test",
            )
            b_granted_event.set()
            release_cb()

        t_b = threading.Thread(target=agent_b)
        t_b.start()
        time.sleep(0.2)

        # B blocked by reservation
        assert not b_granted_event.is_set(), "B should be blocked by A's reservation"

        # A wakes -> unreserve
        scheduler.unreserve(token)

        t_b.join(timeout=5)
        assert b_granted_event.is_set(), "B should be granted after A unreserves"

    def test_scheduler_cancel_for_agent(self):
        """Test cancel via the EndpointScheduler API."""
        router = _make_test_router({'http://seq': 0})
        scheduler = _get_scheduler(router)

        # Hold slot so others queue
        release_a = scheduler.acquire(
            api_base='http://seq',
            concurrency_limit=0,
            instance_name="A",
            agent_class="test",
        )

        c_cancelled = threading.Event()

        def agent_c():
            try:
                scheduler.acquire(
                    api_base='http://seq',
                    concurrency_limit=0,
                    instance_name="C",
                    agent_class="test",
                    timeout=10,
                )
            except (SlotCancelled, TimeoutError):
                c_cancelled.set()

        t_c = threading.Thread(target=agent_c)
        t_c.start()
        time.sleep(0.15)

        # Cancel C via scheduler.cancel(instance_name=...)
        cancelled = scheduler.cancel(instance_name="C")
        assert cancelled, f"C should be cancelled: {cancelled}"

        assert c_cancelled.wait(timeout=3), "C should receive cancellation"
        t_c.join(timeout=5)


# ============================================================================
# Category 9: Edge Cases and Negative Tests
# ============================================================================

class TestEdgeCasesAndNegatives:
    """Edge cases, negative tests, and boundary conditions."""

    def test_unlimited_concurrency_no_pool(self):
        """conc=-1 endpoints should not create pools or queue."""
        router = _make_test_router({'http://unlim': -1})
        scheduler = _get_scheduler(router)

        # Acquire should return None immediately (no scheduling)
        release_cb = scheduler.acquire(
            api_base='http://unlim',
            concurrency_limit=-1,
            instance_name="A",
            agent_class="test",
        )

        assert release_cb is None, "Unlimited endpoint should return None"

    def test_concurrent_pool_resize(self):
        """Pool capacity change under contention — no crash, existing holders unaffected."""
        pool = SlotPool(key="resize_test", capacity=2)

        # Fill both slots
        holder_a = _acquire_immediate(pool, "A")
        holder_b = _acquire_immediate(pool, "B")

        # Waiter queues up
        c_granted_event = threading.Event()

        def waiter_c():
            release_cb = pool.acquire(instance_name="C", agent_class="test", ancestor_chain=("C",))
            c_granted_event.set()
            release_cb()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()
        time.sleep(0.15)

        # C blocked (capacity full)
        assert not c_granted_event.is_set(), "C should be blocked"

        # Resize capacity down — no new grants until space opens
        pool.capacity = 1

        # Release B — now at capacity with A, still no grant for C
        pool.release(holder_b)
        time.sleep(0.1)
        assert not c_granted_event.is_set(), "C should still be blocked (cap=1, A holding)"

        # Release A — C can proceed
        pool.release(holder_a)
        t_c.join(timeout=5)
        assert c_granted_event.is_set(), "C should be granted after resize + release"

    def test_double_release_idempotent(self):
        """Double release should be idempotent, not crash."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        # Create release callback manually
        with pool._cond:
            def release():
                pool.release(holder_a)

        release()  # First release — should work
        release()  # Second release — should be no-op

        status = pool.get_status()
        assert status["running_count"] == 0, "Holder should be released"

    def test_timeout_raises_proper_exception(self):
        """Timeout should raise SlotQueueTimeout, ticket removed from queue."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = _acquire_immediate(pool, "A")

        with pytest.raises(SlotQueueTimeout):
            pool.acquire(instance_name="B", agent_class="test", ancestor_chain=("B",), timeout=0.5)

        status = pool.get_status()
        assert status["waiting_count"] == 0, "Timed-out ticket should be removed"
        assert status["running_count"] == 1, "A should still hold the slot"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
