"""
Integration and stress tests for the API scheduler queue system.

Verifies the FIFO queue scheduler prevents model trashing under realistic
agent call interleaving scenarios. Uses ThreadPoolExecutor to simulate concurrent agent
threads with mocked LLM calls — no actual API needed.

Test categories:
1. FIFO Ordering Under Contention
2. Cancel-on-Termination
3. Stress/Soak Tests
4. Procedural Generation / Brute Force
5. Endpoint Scheduler Integration
6. Edge Cases and Negatives
"""

import itertools
import random
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Set, Tuple

import pytest

from agent_cascade.api_router import APIRouter, EndpointScheduler, APIEndpoint
from agent_cascade.slot_queue import (
    SlotPool,
    SlotQueueTimeout,
    SlotCancelled,
)


# ============================================================================
# Constants
# ============================================================================

FIFO_TEST_WAITERS = 5          # Small contended queue for FIFO tests
STRESS_TEST_WAITERS = 20       # Larger contention for stress tests
PROCEDURAL_SEEDS = [42, 123, 999, 7777]


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


class AgentNode:
    """Represents a node in a procedurally generated agent call graph."""

    def __init__(self, name: str):
        self.name = name
        self.children: List["AgentNode"] = []
        self.is_async_child: bool = False  # True if spawned async


# ============================================================================
# Pytest Fixtures
# ============================================================================

@pytest.fixture
def test_pool(capacity: int = 1) -> SlotPool:
    """Create a SlotPool with automatic cleanup."""
    pool = SlotPool(key=f"test_{_new_id()}", capacity=capacity)
    yield pool


@pytest.fixture
def test_router():
    """Create an isolated APIRouter with default sequential endpoint."""
    import tempfile, os
    from unittest.mock import patch

    test_dir = tempfile.mkdtemp()
    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_dir}):
        router = APIRouter(default_llm_cfg={
            'api_base': 'http://default', 'model': 'default-model', 'max_tokens': 2048,
        })
        ep = APIEndpoint(
            id="ep_default", name="Default", api_base='http://default', model='default-model',
            enabled=True, concurrency_limit=0,
        )
        router.add_endpoint(ep)
        yield router


@pytest.fixture
def violation_tracker() -> ViolationRecorder:
    """Create a fresh ViolationRecorder."""
    return ViolationRecorder()


# ============================================================================
# Helper Functions
# ============================================================================

def _get_scheduler(router: APIRouter) -> EndpointScheduler:
    return router.scheduler


def _make_test_router(conc_map: Optional[Dict[str, int]] = None) -> APIRouter:
    """Create an isolated APIRouter with configurable endpoint concurrency."""
    import tempfile, os
    from unittest.mock import patch

    test_dir = tempfile.mkdtemp()
    with patch.dict(os.environ, {"AGENT_CASCADE_TEST_CONFIG_DIR": test_dir}):
        router = APIRouter(default_llm_cfg={
            'api_base': 'http://default', 'model': 'default-model', 'max_tokens': 2048,
        })

        if conc_map:
            for api_base, conc in conc_map.items():
                ep = APIEndpoint(
                    id=f"ep_{_new_id()}", name=f"Test-{api_base}", api_base=api_base,
                    model='test-model', enabled=True, concurrency_limit=conc,
                )
                router.add_endpoint(ep)
        else:
            ep = APIEndpoint(
                id="ep_default", name="Default", api_base='http://default', model='default-model',
                enabled=True, concurrency_limit=0,
            )
            router.add_endpoint(ep)

        return router


class FIFOWaiterSetup:
    """Reusable setup for FIFO ordering tests with deterministic enqueue via gates."""

    def __init__(self, pool: SlotPool, names: List[str], work_duration: float = 0.05):
        self.pool = pool
        self.names = names
        self.work_duration = work_duration
        self.n = len(names)
        self.gates = [threading.Event() for _ in range(self.n)]
        self.ready = [threading.Event() for _ in range(self.n)]
        self.threads: List[threading.Thread] = []
        self.granted_order: List[str] = []
        self.lock = threading.Lock()

    def start_threads(self):
        """Start all waiter threads (they block on gates until opened)."""
        for i, name in enumerate(self.names):
            t = threading.Thread(target=self._waiter, args=(name, i))
            t.start()
            self.threads.append(t)

    def _waiter(self, name: str, idx: int):
        self.gates[idx].wait()
        self.ready[idx].set()
        release_cb = self.pool.acquire(instance_name=name, agent_class="test", timeout=5.0)
        with self.lock:
            self.granted_order.append(name)
        time.sleep(self.work_duration)
        release_cb()

    def open_gates_sequentially(self):
        """Open gates one at a time, confirming each waiter started acquiring."""
        for i in range(self.n):
            self.gates[i].set()
            assert self.ready[i].wait(timeout=2), f"{self.names[i]} should have started acquiring"

    def join_all(self, timeout: float = 10):
        """Join all threads with timeout."""
        for t in self.threads:
            t.join(timeout=timeout)


class SchedulerFIFOWaiterSetup:
    """Like FIFOWaiterSetup but acquires through EndpointScheduler instead of SlotPool directly.

    Uses the same gate-based deterministic enqueue pattern for robust FIFO ordering tests.
    Adds a small delay between gates to account for EndpointScheduler's extra locking layer.
    """

    def __init__(self, scheduler: EndpointScheduler, api_base: str, names: List[str], work_duration: float = 0.05):
        self.scheduler = scheduler
        self.api_base = api_base
        self.names = names
        self.work_duration = work_duration
        self.n = len(names)
        self.gates = [threading.Event() for _ in range(self.n)]
        self.ready = [threading.Event() for _ in range(self.n)]
        self.threads: List[threading.Thread] = []
        self.granted_order: List[str] = []
        self.lock = threading.Lock()

    def start_threads(self):
        """Start all waiter threads (they block on gates until opened)."""
        for i, name in enumerate(self.names):
            t = threading.Thread(target=self._waiter, args=(name, i))
            t.start()
            self.threads.append(t)

    def _waiter(self, name: str, idx: int):
        self.gates[idx].wait()
        self.ready[idx].set()
        release_cb = self.scheduler.acquire(
            api_base=self.api_base,
            concurrency_limit=0,
            instance_name=name,
            agent_class="test",
            timeout=5.0,
        )
        with self.lock:
            self.granted_order.append(name)
        time.sleep(self.work_duration)
        release_cb()

    def open_gates_sequentially(self):
        """Open gates one at a time with small delay between each to ensure deterministic enqueue order through EndpointScheduler."""
        for i in range(self.n):
            self.gates[i].set()
            assert self.ready[i].wait(timeout=2), f"{self.names[i]} should have started acquiring"
            # Small delay ensures previous waiter's ticket is fully enqueued before next starts acquire.
            # Needed because EndpointScheduler.acquire() has extra locking (_get_or_create_pool) before pool.acquire().
            time.sleep(0.01)

    def join_all(self, timeout: float = 10):
        """Join all threads with timeout."""
        for t in self.threads:
            t.join(timeout=timeout)


def sequential_open_gates(gates: List[threading.Event], ready_events: List[threading.Event], names: List[str]):
    """Open gates one at a time, confirming each waiter has started acquiring before opening the next."""
    for i in range(len(names)):
        gates[i].set()
        assert ready_events[i].wait(timeout=2), f"{names[i]} should have started acquiring"


def sequential_open_gates_with_delay(gates: List[threading.Event], ready_events: List[threading.Event], names: List[str]):
    """Like sequential_open_gates — waits for each waiter to start acquiring before opening next gate."""
    for i in range(len(names)):
        gates[i].set()
        assert ready_events[i].wait(timeout=2), f"{names[i]} should have started acquiring"


def assert_fifo_order(granted: List[str], expected: Optional[List[str]] = None):
    """Verify FIFO ordering: all present, no duplicates, in expected order."""
    if expected is not None:
        assert granted == expected, f"FIFO order violated: expected {expected}, got {granted}"
    else:
        assert len(granted) == len(set(granted)), "Duplicate grants detected"


# ============================================================================
# Category 1: FIFO Ordering Under Contention
# ============================================================================

class TestFIFOOrderingUnderContention:
    """Test strict FIFO ordering under realistic contention scenarios."""

    def test_fifo_grant_order_sequential(self):
        """Multiple agents queue on same sequential slot — grants in exact enqueue order."""
        pool = SlotPool(key="test_seq", capacity=1)
        holder_a = pool.create_held_slot("A")

        names = [f"T{i+1}" for i in range(FIFO_TEST_WAITERS)]
        setup = FIFOWaiterSetup(pool, names, work_duration=0.05)
        setup.start_threads()
        setup.open_gates_sequentially()
        pool.release(holder_a)
        setup.join_all(timeout=10)

        assert_fifo_order(setup.granted_order, names)

    def test_fifo_grant_order_parallel_pool(self):
        """Multiple agents queue on parallel pool (conc=3) — grants respect capacity + FIFO."""
        pool = SlotPool(key="test_par", capacity=3)
        n_waiters = 9
        names = [f"T{i+1}" for i in range(n_waiters)]

        setup = FIFOWaiterSetup(pool, names, work_duration=0.08)
        setup.start_threads()
        setup.open_gates_sequentially()
        setup.join_all(timeout=10)

        granted = setup.granted_order
        assert len(granted) == n_waiters, f"All waiters should be granted: got {len(granted)}"

        batch1, batch2, batch3 = granted[0:3], granted[3:6], granted[6:9]
        assert batch1 == ["T1", "T2", "T3"], f"First batch must be T1,T2,T3 in FIFO order, got {batch1}"
        assert batch2 == ["T4", "T5", "T6"], f"Second batch must be T4,T5,T6 in FIFO order, got {batch2}"
        assert batch3 == ["T7", "T8", "T9"], f"Third batch must be T7,T8,T9 in FIFO order, got {batch3}"

        for i in range(n_waiters - 1):
            idx_i = int(granted[i][1:]) - 1
            idx_j = int(granted[i + 1][1:]) - 1
            assert idx_i < idx_j, f"VIOLATION: T{idx_i+1} granted after T{idx_j+1}, breaking cross-batch FIFO"

    def test_fifo_no_barging_under_contention(self):
        """Later waiters never granted before earlier ones — even under high contention."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = pool.create_held_slot("A")

        names = [f"W{i}" for i in range(STRESS_TEST_WAITERS)]
        setup = FIFOWaiterSetup(pool, names, work_duration=0.01)
        setup.start_threads()
        setup.open_gates_sequentially()
        pool.release(holder_a)
        setup.join_all(timeout=15)

        assert_fifo_order(setup.granted_order, names)

    def test_fifo_mixed_conc_pools(self):
        """Agents on conc=0 and conc=N pools queue independently — no cross-pool interference."""
        seq_pool = SlotPool(key="_shared_sequential_slot_", capacity=1)
        par_pool = SlotPool(key="http://parallel", capacity=2)

        holder_a = seq_pool.create_held_slot("A")

        n_seq, n_par = FIFO_TEST_WAITERS, 4
        seq_setup = FIFOWaiterSetup(seq_pool, [f"S{i}" for i in range(n_seq)], work_duration=0.03)
        par_setup = FIFOWaiterSetup(par_pool, [f"P{i}" for i in range(n_par)], work_duration=0.03)

        seq_setup.start_threads()
        par_setup.start_threads()
        seq_setup.open_gates_sequentially()
        par_setup.open_gates_sequentially()

        seq_pool.release(holder_a)
        seq_setup.join_all(timeout=10)
        par_setup.join_all(timeout=10)

        assert_fifo_order(seq_setup.granted_order, [f"S{i}" for i in range(n_seq)])
        assert_fifo_order(par_setup.granted_order, ["P0", "P1", "P2", "P3"])


# ============================================================================
# Category 4: Cancel-on-Termination
# ============================================================================

class TestCancelOnTermination:
    """Test cancellation when agents terminate while waiting or holding slots."""

    def test_cancel_queued_agent_next_waiter_granted(self):
        """Agent queued on slot, terminated -> ticket cancelled within ≤1s, next waiter granted."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = pool.create_held_slot("A")

        b_granted_event = threading.Event()
        b_released_event = threading.Event()
        b_ready = threading.Event()

        def waiter_b():
            b_ready.set()
            release_cb = pool.acquire(instance_name="B", agent_class="test")
            b_granted_event.set()
            time.sleep(0.5)  # Hold slot while C waits in queue
            release_cb()
            b_released_event.set()

        t_b = threading.Thread(target=waiter_b)
        t_b.start()
        assert b_ready.wait(timeout=2), "B should have started acquiring"

        c_cancelled = threading.Event()
        c_ready = threading.Event()

        def waiter_c():
            c_ready.set()
            try:
                pool.acquire(instance_name="C", agent_class="test")
            except SlotCancelled:
                c_cancelled.set()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()
        assert c_ready.wait(timeout=2), "C should have started acquiring"

        pool.release(holder_a)
        assert b_granted_event.wait(timeout=2), "B should be granted after A releases"

        start = time.monotonic()
        cancelled = pool.cancel(agent_name="C")
        elapsed = time.monotonic() - start

        assert cancelled, "C's ticket should be cancelled"
        assert c_cancelled.wait(timeout=2), "C should receive SlotCancelled within 1s tick"
        assert elapsed < 0.5, f"Cancel took {elapsed:.2f}s — should be immediate under lock"

        t_c.join(timeout=3)
        t_b.join(timeout=5)

    def test_mass_termination_active_queue_all_cleaned(self):
        """Mass termination during active queue — all tickets cleaned."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = pool.create_held_slot("A")

        n_agents = 50
        cancelled_events = {f"agent_{i}": threading.Event() for i in range(n_agents)}
        ready_events = {f"agent_{i}": threading.Event() for i in range(n_agents)}

        def waiter(name: str):
            ready_events[name].set()
            try:
                pool.acquire(instance_name=name, agent_class="test", timeout=10)
            except SlotCancelled:
                cancelled_events[name].set()

        threads = []
        for i in range(n_agents):
            t = threading.Thread(target=waiter, args=(f"agent_{i}",))
            t.start()
            threads.append(t)

        # Wait for all agents to reach acquire (be in queue)
        for i in range(n_agents):
            assert ready_events[f"agent_{i}"].wait(timeout=3), f"agent_{i} should have started acquiring"

        status_before = pool.get_status()
        assert status_before["waiting_count"] == n_agents, f"All {n_agents} agents should be waiting"

        start = time.monotonic()
        for i in range(n_agents // 2):
            pool.cancel(agent_name=f"agent_{i}")
        elapsed = time.monotonic() - start

        status_after = pool.get_status()
        expected_remaining = n_agents - (n_agents // 2)
        assert status_after["waiting_count"] == expected_remaining, \
            f"After mass cancel: expected {expected_remaining} remaining, got {status_after['waiting_count']}"

        for i in range(n_agents // 2):
            assert cancelled_events[f"agent_{i}"].wait(timeout=3), f"agent_{i} should have been cancelled"

        for i in range(n_agents // 2, n_agents):
            pool.cancel(agent_name=f"agent_{i}")
        for t in threads:
            t.join(timeout=5)





# ============================================================================
# Category 6: Stress/Soak Tests
# ============================================================================

class TestStressAndSoak:
    """Stress tests with many concurrent agents on shared pools."""

    def _stress_agent_task(self, name: str, pool: SlotPool, tracker: ConcurrencyTracker,
                           pool_key: str, sleep_range: Tuple[float, float]):
        """Common agent task for stress tests."""
        release_cb = pool.acquire(instance_name=name, agent_class="test")
        tracker.enter(pool_key, name)
        try:
            time.sleep(random.uniform(*sleep_range))
        finally:
            tracker.leave(pool_key, name)
            release_cb()

    def test_concurrent_agents_shared_sequential_pool(self):
        """Many concurrent agents on conc=0 pool — never more than 1 running simultaneously."""
        pool = SlotPool(key="_shared_sequential_slot_", capacity=1)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("_shared_sequential_slot_", 1)

        n_agents = 30
        completed = [0]
        lock = threading.Lock()

        def agent_task(name: str):
            self._stress_agent_task(name, pool, tracker, "_shared_sequential_slot_", (0.01, 0.05))
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
            self._stress_agent_task(name, pool, tracker, "http://parallel", (0.01, 0.04))
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
            release_cb = pool.acquire(instance_name=name, agent_class="test")
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
            parent_chain = ("root", name)
            seq_release = seq_pool.acquire(instance_name=name, agent_class="test")
            tracker_seq.enter("_shared_sequential_slot_", name)

            child_done = threading.Event()

            def async_child():
                par_release = par_pool.acquire(
                    instance_name=f"{name}_child",
                    agent_class="test",
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

            time.sleep(random.uniform(0.01, 0.02))
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
        """Extended run with contention — verify no deadlocks under FIFO scheduling."""
        pool = SlotPool(key="soak_test", capacity=2)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("soak_test", 2)

        n_iterations = 100
        completed = [0]
        lock = threading.Lock()

        def agent_task(name: str):
            chain = ("root", name)
            release_cb = pool.acquire(instance_name=name, agent_class="test")
            tracker.enter("soak_test", name)

            try:
                time.sleep(random.uniform(0.005, 0.02))
            finally:
                tracker.leave("soak_test", name)
                release_cb()
            with lock:
                completed[0] += 1

        for round_i in range(n_iterations):
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = []
                for i in range(5):
                    futures.append(executor.submit(agent_task, f"r{round_i}_a{i}"))

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

    @pytest.mark.parametrize("seed", PROCEDURAL_SEEDS)
    def test_random_agent_call_graphs(self, seed: int):
        """Randomly generates agent call graphs and runs them under contention.

        Monitors for violations: >capacity running, FIFO order broken.
        """
        rng = random.Random(seed)
        pool = SlotPool(key="proc_test", capacity=2)
        tracker = ConcurrencyTracker()
        tracker.set_capacity("proc_test", 2)

        violations = ViolationRecorder()
        completed = [0]
        lock = threading.Lock()

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
            release_cb = pool.acquire(instance_name=node.name, agent_class="test")
            over = tracker.enter("proc_test", node.name)
            if over:
                violations.record(f"{node.name}: over capacity")

            try:
                time.sleep(rng.uniform(0.005, 0.015))

                child_threads = []
                for child in node.children:
                    t = threading.Thread(target=run_agent, args=(child, chain))
                    child_threads.append(t)
                    t.start()

                for t in child_threads:
                    t.join(timeout=10)
            finally:
                tracker.leave("proc_test", node.name)
                release_cb()

            with lock:
                completed[0] += 1

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

        Cancels a subset BEFORE releasing the held slot, verifying:
        - Cancelled tickets are removed from queue
        - Only non-cancelled agents are granted in FIFO order
        """
        rng = random.Random(42)
        pool = SlotPool(key="cancel_test", capacity=1)
        holder_a = pool.create_held_slot("A")

        n_waiters = 30
        granted_order: List[str] = []
        lock = threading.Lock()
        names = [f"W{i}" for i in range(n_waiters)]
        cancelled_set: Set[str] = set()

        def waiter(name: str, idx: int):
            gates[idx].wait()
            ready[idx].set()
            try:
                release_cb = pool.acquire(instance_name=name, agent_class="test")
                with lock:
                    granted_order.append(name)
                time.sleep(0.01)
                release_cb()
            except SlotCancelled:
                pass

        gates = [threading.Event() for _ in range(n_waiters)]
        ready = [threading.Event() for _ in range(n_waiters)]
        threads = [threading.Thread(target=waiter, args=(name, i)) for i, name in enumerate(names)]
        for t in threads:
            t.start()

        sequential_open_gates(gates, ready, names)

        to_cancel = rng.sample([f"W{i}" for i in range(1, n_waiters)], k=n_waiters // 3)
        with lock:
            cancelled_set.update(to_cancel)
        for name in to_cancel:
            pool.cancel(agent_name=name)

        status = pool.get_status()
        waiting_names = {w["instance_name"] for w in status["waiters"]}
        assert not (waiting_names & cancelled_set), \
            f"Cancelled agents still in queue: {waiting_names & cancelled_set}"

        pool.release(holder_a)
        for t in threads:
            t.join(timeout=15)

        for name in granted_order:
            assert name not in cancelled_set, f"Cancelled agent {name} was incorrectly granted"

        expected_grantees = {f"W{i}" for i in range(n_waiters)} - cancelled_set
        actual_grantees = set(granted_order)
        assert actual_grantees == expected_grantees, \
            f"Grant set mismatch:\n  Expected: {sorted(expected_grantees)}\n  Got:      {sorted(actual_grantees)}"

        assert len(granted_order) == len(set(granted_order)), "Duplicate grants detected"

        expected_fifo = sorted(expected_grantees, key=lambda x: int(x[1:]))
        assert_fifo_order(granted_order, expected_fifo)


# ============================================================================
# Category 8: Integration with EndpointScheduler (higher-level tests)
# ============================================================================

class TestEndpointSchedulerIntegration:
    """Higher-level integration tests using the real EndpointScheduler."""

    def test_scheduler_fifo_via_api_router(self):
        """Test FIFO ordering through the full EndpointScheduler API."""
        router = _make_test_router({'http://seq': 0})
        scheduler = _get_scheduler(router)

        names = [f"A{i}" for i in range(10)]
        setup = SchedulerFIFOWaiterSetup(scheduler, api_base='http://seq', names=names, work_duration=0.03)

        setup.start_threads()
        setup.open_gates_sequentially()
        setup.join_all(timeout=15)

        assert_fifo_order(setup.granted_order, names)

    def test_scheduler_cancel_for_agent(self):
        """Test cancel via the EndpointScheduler API."""
        router = _make_test_router({'http://seq': 0})
        scheduler = _get_scheduler(router)

        release_a = scheduler.acquire(
            api_base='http://seq',
            concurrency_limit=0,
            instance_name="A",
            agent_class="test",
        )

        c_cancelled = threading.Event()
        c_ready = threading.Event()

        def agent_c():
            c_ready.set()
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
        assert c_ready.wait(timeout=2), "C should have started acquiring"

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

        holder_a = pool.create_held_slot("A")
        holder_b = pool.create_held_slot("B")

        c_granted_event = threading.Event()
        c_ready = threading.Event()

        def waiter_c():
            c_ready.set()
            release_cb = pool.acquire(instance_name="C", agent_class="test")
            c_granted_event.set()
            release_cb()

        t_c = threading.Thread(target=waiter_c)
        t_c.start()
        assert c_ready.wait(timeout=2), "C should have started acquiring"

        assert not c_granted_event.is_set(), "C should be blocked"

        pool.capacity = 1
        pool.release(holder_b)
        # No sleep needed — release doesn't wake C because capacity is still full (A holding, cap=1)
        assert not c_granted_event.is_set(), "C should still be blocked (cap=1, A holding)"

        pool.release(holder_a)
        t_c.join(timeout=5)
        assert c_granted_event.is_set(), "C should be granted after resize + release"

    def test_double_release_idempotent(self):
        """Double release should be idempotent, not crash."""
        pool = SlotPool(key="test", capacity=1)
        holder_a = pool.create_held_slot("A")

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
        holder_a = pool.create_held_slot("A")

        with pytest.raises(SlotQueueTimeout):
            pool.acquire(instance_name="B", agent_class="test", timeout=0.5)

        status = pool.get_status()
        assert status["waiting_count"] == 0, "Timed-out ticket should be removed"
        assert status["running_count"] == 1, "A should still hold the slot"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
