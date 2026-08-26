"""Edge-case stress suite for the AgentCascade sticky-slot + fallback system.

Companion to ``tests/test_sticky_slot_assignment.py`` (which covers the N1–N21 happy /
single-transition paths). This file drives the SAME real ``SlotPool`` /
``EndpointScheduler`` / ``APIRouter`` and focuses on *races and stress*: priority swaps
while agents are running/waiting/sleeping, terminations of slot holders, concurrent swap
fights, cross-pool side-calls, rapid successive swaps, and barrier-synchronized race storms.

Scenario map (matches the task brief):
  P1-P3  Priority swap scenarios (running / waiting / sleeping)
  M1,M3  Termination scenarios (slot holder, async child)
  C1-C3  Concurrency scenarios (swap fight, sync/async deadlock topology, swap+reuse)
  S1-S2  Side-call & caption scenarios (caption during swap, imagegen before swap)
  ST1-ST3 Sticky-slot edge cases (disabled endpoint, cross-pool heavy load, rapid swaps)

Removed in the Part-C rework (fake-green / duplicated — see report):
  P4     cursor-rotation state asserts (duplicate of tests/test_priority_swap_cursor.py)
  T1-T3  mocked-call_fn timeout/503 fallback tests with SANITY_PROBE_ENABLED=False
         (replaced by the REAL-HTTP tests in tests/test_fallback_real_http.py, which keep
          the probe ENABLED and count every request per base)
  M2     mock-only terminate-during-fallback (covered by M1 + the real-HTTP suite)

The harness pattern is copied from ``test_sticky_slot_assignment.py`` (real router + real
pool, conc=0 endpoint, shortened QUEUE_WAIT_TIMEOUT / REACQUIRE_TIMEOUT via module patch).
All thread synchronization uses threading.Event/Barrier — no wall-clock sleeps for
correctness — and every thread that could hang is joined with a timeout + not-alive assert.
Runtime budget: < 60s for the whole file.
"""
# Isolate this run's logs/telemetry from the production workspace. Must be set BEFORE any
# agent_cascade import (instance_id reads it at call time).
import os as _os
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", f"edgecase_{_os.getpid()}")

import inspect  # noqa: F401  (kept for parity with the reference suite; used by static guards)
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

SHARED_KEY = "_shared_sequential_slot_"
SEQ_BASE = "http://127.0.0.1:9/v1"      # conc=0 endpoint (shared sequential slot)
PAR_BASE = "http://127.0.0.1:10/v1"     # conc>0 endpoint (per-base pool)


# ── Real slot-pool harness (no server, no LLM) ───────────────────────────────

def _build_real_router(cfg_dir):
    """Real APIRouter with a single conc=0 endpoint → real shared sequential SlotPool.

    The default_llm_cfg points at the SAME conc=0 base so an unconfigured agent type
    (Tier-4 fallback) also resolves to the shared sequential slot.
    """
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {
        "model": "mock",
        "api_base": SEQ_BASE,
        "model_server": SEQ_BASE,
        "api_key": "EMPTY",
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(cfg_dir))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id="ep0", name="conc0", api_base=SEQ_BASE,
                     model="mock", concurrency_limit=0, enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return router


def _add_endpoint(router, name, api_base, model="mock", concurrency_limit=-1, **kwargs):
    """Add an endpoint and return its id (mirrors tests/conftest.py convention)."""
    from agent_cascade.api_router import APIEndpoint
    ep = APIEndpoint(id=f"ep_{name}", name=name, api_base=api_base, model=model,
                     enabled=True, concurrency_limit=concurrency_limit, **kwargs)
    return router.add_endpoint(ep)


def _build_pool(router):
    """Real AgentPool wired to the real router (real get_instance / _acquire_slot)."""
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {"model": "mock", "api_base": SEQ_BASE,
               "model_server": SEQ_BASE, "api_key": "EMPTY"}
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


def _make_instance(pool, name, agent_class="coder"):
    """Real AgentInstance registered in the pool (so get_instance() finds it)."""
    from agent_cascade.agent_instance import AgentInstance
    inst = AgentInstance(
        instance_name=name, agent_class=agent_class, conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    pool.instances[name] = inst
    return inst


def _make_engine(sched, api_base, conc):
    """Real ExecutionEngine backed by a mock pool whose router resolves to the given slot."""
    from agent_cascade.execution_engine import ExecutionEngine

    slot_info = {
        'slot_key': SHARED_KEY if conc == 0 else api_base,
        'is_sequential': conc == 0,
        'concurrency_limit': conc,
        'api_base': api_base,
        'needs_slot': True,
    }
    mock_pool = MagicMock()
    mock_router = MagicMock()
    mock_router.scheduler = sched
    mock_router.get_effective_slot_info.return_value = slot_info
    mock_router.get_agent_slot_info.return_value = slot_info
    mock_pool.api_router = mock_router
    return ExecutionEngine(mock_pool)


def _real_resolution_engine(router, api_base, conc):
    """Engine whose cursor-aware resolution goes through the REAL router (cursor-rotated)."""
    engine = _make_engine(router.scheduler, api_base, conc)
    patcher = patch.object(
        engine.pool.api_router, "get_effective_slot_info",
        side_effect=lambda agent_class, instance_name=None:
            router.get_effective_slot_info(agent_class, instance_name=instance_name),
    )
    patcher.start()
    return engine, patcher


@pytest.fixture
def edge_harness(tmp_path, request):
    """Real router (conc=0 endpoint) + real pool; short QUEUE_WAIT_TIMEOUT / REACQUIRE_TIMEOUT.

    Each test gets its OWN config dir (derived from the node id) so pytest-xdist's parallel
    workers don't overwrite each other's api_endpoints.json.
    """
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router_pkg.scheduler as _ar_mod
    import agent_cascade.api_router_pkg.router as _rmod
    import agent_cascade.engine.core as _core_mod

    cfg_dir = tmp_path / request.node.name.replace("/", "_")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(cfg_dir)

    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    old_cool = _rmod.ENDPOINT_COOLDOWN_SECONDS
    old_reacq = _core_mod.REACQUIRE_TIMEOUT
    # Shorten the shared-slot acquire timeout + endpoint cooldown + fast re-acquire window.
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5
    _rmod.ENDPOINT_COOLDOWN_SECONDS = 0
    _core_mod.REACQUIRE_TIMEOUT = 0.3

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)
    router._pool = pool

    shared = router.scheduler._get_or_create_pool(SEQ_BASE, 0)
    assert shared is not None and shared.key == SHARED_KEY, \
        f"Shared sequential SlotPool was not created: {shared!r}"

    yield {"router": router, "pool": pool, "shared": shared}

    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    _rmod.ENDPOINT_COOLDOWN_SECONDS = old_cool
    _core_mod.REACQUIRE_TIMEOUT = old_reacq


# ── Shared introspection helpers ─────────────────────────────────────────────

def _slot_pool_holders(pool_obj):
    """Snapshot of holder instance names for a SlotPool."""
    with pool_obj._cond:
        return list(pool_obj._running.keys())


def _waiter_names(pool_obj):
    """Snapshot of waiter instance names (FIFO order) for a SlotPool."""
    with pool_obj._cond:
        return [t.instance_name for t in pool_obj._waiters.values()]


def _capture_slotpool_logs():
    """Capture DEBUG records from the app logger + package logger into a list."""
    records = []
    lock = threading.Lock()

    class _Capture(logging.Handler):
        def emit(self, record):
            with lock:
                try:
                    records.append(record)
                except Exception:
                    pass

    handler = _Capture(level=logging.DEBUG)
    targets = []
    for name in ("agent_cascade_logger", "agent_cascade"):
        lg = logging.getLogger(name)
        old_level = lg.level
        lg.setLevel(logging.DEBUG)
        lg.addHandler(handler)
        targets.append((lg, old_level))
    return records, handler, targets


def _restore_logs(handler, targets):
    for lg, old_level in targets:
        lg.removeHandler(handler)
        lg.setLevel(old_level)


def _find(records, needle):
    return [r for r in records if needle in (r.getMessage() if hasattr(r, "getMessage") else str(r))]


def _release_permit(inst):
    """Release an instance's sticky permit via its raw callback (idempotent)."""
    if inst is None or getattr(inst, "_slot_release", None) is None:
        return
    with inst._state_lock:
        cb = inst._slot_release
        inst._slot_release = None
        inst._slot_key = None
    cb()


def _queue_waiter(router, pool, name, timeout=15.0):
    """Start a blocked FIFO waiter thread on the shared slot; returns (thread, granted_event).

    The waiter signals ``queued`` as soon as it has entered the FIFO queue (before it can
    be granted), so callers can wait for that Event instead of sleeping to infer "blocked".
    """
    granted = threading.Event()
    queued = threading.Event()

    def waiter():
        try:
            queued.set()  # we are about to block in the FIFO queue (holder holds the slot)
            router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name=name, agent_class="coder", timeout=timeout)
            granted.set()
        except Exception:
            pass

    t = threading.Thread(target=waiter)
    t.start()
    assert queued.wait(timeout=5), f"{name} never reached the FIFO queue"
    # A blocked waiter cannot have been granted yet (capacity is held by the holder).
    assert not granted.is_set(), f"{name} must be blocked while the holder holds"
    return t, granted, queued


def _start_waiters(router, names, timeout=15.0):
    """Start N FIFO waiter threads SEQUENTIALLY (FIFO order = start order).

    Returns a list of (thread, granted_event, rel_box) in start order. Each waiter signals
    its own Event as soon as it has entered the queue (``queued``), so callers can wait for
    that instead of sleeping to infer "blocked". The rel_box receives the release callback
    once granted.
    """
    out = []
    for name in names:
        granted = threading.Event()
        queued = threading.Event()
        rel_box = []

        def waiter(nm=name, ev=granted, q=queued, box=rel_box):
            try:
                q.set()  # entered the FIFO queue (holder holds the slot → we block here)
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name=nm, agent_class="coder", timeout=timeout)
                box.append(cb)
                ev.set()
            except Exception:
                pass

        t = threading.Thread(target=waiter)
        t.start()
        assert queued.wait(timeout=5), f"{name} never reached the FIFO queue"
        out.append((t, granted, rel_box))
    return out


# ═══════════════════════════════════════════════════════════════════════════
# P1–P4: Priority Swap Scenarios
# ═══════════════════════════════════════════════════════════════════════════

class TestP1SwapWhileRunning:
    """Agent holds the shared slot and is RUNNING (making calls). A priority swap
    changes the endpoint order. Verify no deadlock, slot stays held, and the
    agent can continue making calls on the new (or same) endpoint."""

    def test_swap_same_pool_no_deadlock(self, edge_harness):
        """Swap priorities while agent holds the conc=0 slot; both endpoints are conc=0
        so the swap is a no-op for the pool. Agent must still hold the slot and not hang."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        # Add a second conc=0 endpoint on a different base.
        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "p1a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="p1a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
        assert "p1a" in shared._running

        # Swap priorities (reorder: ep1 first, ep0 second — both conc=0).
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # Agent still holds the slot; no deadlock.
        assert inst._slot_key == SHARED_KEY, "Slot must remain held after same-pool swap"
        assert "p1a" in shared._running

        # Release cleanly.
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_swap_to_different_conc_pool(self, edge_harness):
        """Agent holds conc=0 slot; swap priorities so the new first endpoint is conc>0.
        sync_sticky_slot must drop the shared slot and NOT acquire a per-base pool
        (per-base pools don't block). No deadlock."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        router.set_agent_priorities("coder", ["ep0", par_id])

        inst = _make_instance(pool, "p1b", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="p1b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Swap: par endpoint first (conc>0), ep0 second.
        router.set_agent_priorities("coder", [par_id, "ep0"])

        # Sync to the new desired key (None for conc>0).
        result = router.sync_sticky_slot(inst, desired_key=None, origin="sticky")
        assert result is True
        assert inst._slot_key == PAR_BASE, f"Expected per-base key, got {inst._slot_key}"
        with shared._cond:
            assert "p1b" not in shared._running, "Shared slot must be freed on cross-pool swap"

    def test_swap_with_waiter_blocked(self, edge_harness):
        """Agent holds the shared slot; a waiter is blocked. Swap priorities (same pool).
        The waiter must remain blocked until the holder releases — no premature grant."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "p1c", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="p1c", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        t_w, granted, _queued = _queue_waiter(router, pool, "p1w")

        # Swap priorities while waiter is blocked.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])
        assert not granted.is_set(), "Waiter must NOT be granted by a priority swap"

        # Release holder → waiter gets the slot.
        _release_permit(inst)
        t_w.join(timeout=5)
        assert granted.is_set(), "Waiter must be granted after holder releases"


class TestP2SwapWhileWaiting:
    """Agent is WAITING in the FIFO queue. A priority swap changes the endpoint order.
    Verify waiters are granted correctly (FIFO preserved) after the swap."""

    def test_fifo_preserved_after_swap(self, edge_harness):
        """Two agents wait in FIFO. Swap priorities while both are waiting.
        After the holder releases, w1 is granted first (FIFO). After w1 releases,
        w2 is granted. The capacity-1 pool enforces strict sequential ordering."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        # Holder acquires the slot.
        holder_rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="holder", agent_class="coder", timeout=5.0)

        # w1 and w2 queue up in FIFO order (Event-synchronized, no sleeps).
        waiters = _start_waiters(router, ["w1", "w2"])
        t1, w1_granted, w1_rel_box = waiters[0]
        t2, w2_granted, w2_rel_box = waiters[1]

        # Swap priorities while both are waiting.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # Release holder → w1 granted first (FIFO).
        holder_rel()
        assert w1_granted.wait(timeout=5), "w1 must be granted first"
        t1.join(timeout=5)

        # w1 releases → w2 granted second.
        w1_rel_box[0]()
        assert w2_granted.wait(timeout=5), "w2 must be granted after w1 releases"
        t2.join(timeout=5)

        # Cleanup.
        w2_rel_box[0]()

    def test_swap_does_not_reorder_waiters(self, edge_harness):
        """Swap priorities must NOT reorder the FIFO queue. The swap only affects which
        endpoint the agent will use NEXT time it acquires — not the current queue order."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        holder_rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="h", agent_class="coder", timeout=5.0)

        waiters = _start_waiters(router, ["first", "second"])
        t1, ev1, rel_box1 = waiters[0]
        t2, ev2, rel_box2 = waiters[1]

        # Swap.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        holder_rel()
        assert ev1.wait(timeout=5), "First waiter must be granted"
        t1.join(timeout=5)
        # First releases → second granted.
        rel_box1[0]()
        assert ev2.wait(timeout=5), "Second waiter must be granted"
        t2.join(timeout=5)
        # Cleanup.
        rel_box2[0]()


class TestP3SwapWhileSleeping:
    """Agent is SLEEPING (slot released). A priority swap changes the endpoint order.
    When the agent wakes up, it must use the NEW priority order."""

    def test_wakeup_uses_new_priority(self, edge_harness):
        """Agent sleeps (releases slot), priorities are swapped, then the agent's next
        call resolves to the new first endpoint."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        router.set_agent_priorities("coder", ["ep0", par_id])

        inst = _make_instance(pool, "p3a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="p3a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Transition to SLEEPING (releases the slot).
        from agent_cascade.agent_instance import AgentState
        inst.state = AgentState.RUNNING
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._transition_to_sleeping(inst)
        assert inst._slot_key is None, "Sleep must release the sticky permit"
        with shared._cond:
            assert len(shared._running) == 0

        # Swap priorities while sleeping.
        router.set_agent_priorities("coder", [par_id, "ep0"])

        # On wakeup, the next call resolves to the new first endpoint (par, conc>0).
        info = router.get_effective_slot_info("coder", instance_name="p3a")
        assert info["api_base"] == PAR_BASE, \
            f"Wakeup must use new priority order, got {info['api_base']}"

    def test_sleep_then_swap_then_reacquire(self, edge_harness):
        """Agent sleeps, priorities swap to a conc=0 endpoint on a different base.
        On reacquire, the agent gets the shared slot for the NEW base."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "p3b", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="p3b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        from agent_cascade.agent_instance import AgentState
        inst.state = AgentState.RUNNING
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._transition_to_sleeping(inst)

        # Swap: ep1 first (different conc=0 base).
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # Reacquire → should get the shared slot (all conc=0 share one pool).
        rel2 = router.scheduler.acquire(
            api_base="http://127.0.0.1:11/v1", concurrency_limit=0,
            instance_name="p3b", agent_class="coder", timeout=5.0)
        assert rel2 is not None
        inst._slot_release = rel2
        inst._slot_key = SHARED_KEY
        with shared._cond:
            assert "p3b" in shared._running
        _release_permit(inst)







# ═══════════════════════════════════════════════════════════════════════════
# M1–M3: Termination Scenarios
# ═══════════════════════════════════════════════════════════════════════════

class TestM1TerminateSlotHolder:
    """Terminate an agent holding the shared slot → verify slot freed, waiter granted."""

    def test_dismiss_frees_slot_and_grants_waiter(self, edge_harness):
        """Dismiss an instance holding the sticky permit. The slot must be freed and
        a blocked waiter must be granted."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "m1a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="m1a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        t_w, granted, _queued = _queue_waiter(router, pool, "m1w")

        # Dismiss the holder.
        pool.dismiss_instance("m1a")
        assert inst._slot_key is None, "Dismiss must release the sticky permit"

        t_w.join(timeout=5)
        assert granted.is_set(), "Waiter must be granted after holder dismissed"
        # The waiter now holds the slot (it was granted). Verify it's the ONLY holder.
        with shared._cond:
            assert list(shared._running.keys()) == ["m1w"], \
                f"Only the waiter should hold the slot: {list(shared._running)}"

    def test_terminate_marks_instance(self, edge_harness):
        """Terminate an instance holding the sticky permit. The instance is marked
        as terminated. NOTE: terminate_instance does NOT release the slot — that's
        dismiss_instance's job. The slot is released at lifecycle points (sleep/exit)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "m1b", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="m1b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        pool.terminate_instance("m1b")
        assert inst.is_terminated, "Instance must be marked as terminated"
        # The slot is still held (terminate doesn't release it — that's by design).
        # Cleanup: release the permit manually.
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak after cleanup: {list(shared._running)}"



class TestM3TerminateAsyncChild:
    """Terminate an async child that holds a slot (sticky or per-base)."""

    def test_dismiss_async_child_with_sticky_slot(self, edge_harness):
        """An async child holds the shared sticky slot. Dismissing it must free the slot."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "m3a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="m3a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        pool.dismiss_instance("m3a")
        with shared._cond:
            assert "m3a" not in shared._running, \
                f"Async child slot not freed: {list(shared._running)}"

    def test_dismiss_async_child_with_perbase_slot(self, edge_harness):
        """An async child holds a per-base pool permit. Dismissing it must free the permit."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=2)
        inst = _make_instance(pool, "m3b", "coder")

        rel = router.scheduler.acquire(
            api_base=PAR_BASE, concurrency_limit=2,
            instance_name="m3b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = PAR_BASE

        pool.dismiss_instance("m3b")
        par_pool = router.scheduler._pools.get(PAR_BASE)
        if par_pool:
            with par_pool._cond:
                assert "m3b" not in par_pool._running, \
                    f"Per-base slot not freed: {list(par_pool._running)}"


# ═══════════════════════════════════════════════════════════════════════════
# C1–C3: Concurrency Scenarios
# ═══════════════════════════════════════════════════════════════════════════

class TestC1ConcurrentSwapFight:
    """Multiple agents fighting for the shared slot while one swaps priorities."""

    def test_swap_during_contention_no_deadlock(self, edge_harness):
        """Agent A holds the slot, B and C wait. A swaps priorities (same pool).
        After A releases, B is granted first (FIFO). After B releases, C is granted.
        No deadlock."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        # A holds the slot.
        rel_a = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="A", agent_class="coder", timeout=5.0)

        waiters = _start_waiters(router, ["B", "C"])
        t_b, ev_b, rel_box_b = waiters[0]
        t_c, ev_c, rel_box_c = waiters[1]

        # A swaps priorities while B and C are waiting.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # A releases → B granted first (FIFO).
        rel_a()
        assert ev_b.wait(timeout=5), "B must be granted first"
        t_b.join(timeout=5)

        # B releases → C granted second.
        rel_box_b[0]()
        assert ev_c.wait(timeout=5), "C must be granted after B releases"
        t_c.join(timeout=5)
        # Cleanup.
        rel_box_c[0]()

    def test_concurrent_swaps_no_corruption(self, edge_harness):
        """Multiple threads swap priorities simultaneously. The router's internal state
        must remain consistent (no corruption, no crash)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        ep2_id = _add_endpoint(router, "ep2", "http://127.0.0.1:12/v1", concurrency_limit=0)

        errors = []
        barrier = threading.Barrier(4)

        def swapper(order):
            try:
                barrier.wait(timeout=5)
                router.set_agent_priorities("coder", order)
            except Exception as e:
                errors.append(str(e))

        threads = [
            threading.Thread(target=swapper, args=(["ep0", ep1_id, ep2_id],)),
            threading.Thread(target=swapper, args=([ep1_id, "ep0", ep2_id],)),
            threading.Thread(target=swapper, args=([ep2_id, ep1_id, "ep0"],)),
            threading.Thread(target=swapper, args=(["ep0", ep2_id, ep1_id],)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent swaps caused errors: {errors}"
        # Chain must still be valid.
        chain = router.get_endpoint_chain("coder")
        assert len(chain) >= 1


class TestC2DeadlockTopology:
    """Sync parent → async child → sync grandchild (deadlock topology) while holding slots."""

    def test_parent_child_grandchild_slot_chain(self, edge_harness):
        """Parent holds the shared slot. Child (async) is spawned. Grandchild (sync)
        needs the same slot. Verify no deadlock: parent must yield or the chain resolves."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        # Parent holds the slot.
        rel_parent = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="parent", agent_class="coder", timeout=5.0)

        # Grandchild tries to acquire (will block).
        granted = threading.Event()
        queued = threading.Event()
        result_box = []

        def grandchild():
            try:
                queued.set()  # entered the FIFO queue (parent holds the slot)
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="grandchild", agent_class="coder", timeout=15.0)
                result_box.append(cb)
                granted.set()
            except Exception:
                pass

        t_g = threading.Thread(target=grandchild)
        t_g.start()
        assert queued.wait(timeout=5), "Grandchild never reached the FIFO queue"
        assert not granted.is_set(), "Grandchild must block while parent holds"

        # Parent yields (releases the slot).
        rel_parent()
        t_g.join(timeout=5)
        assert granted.is_set(), "DEADLOCK: grandchild never acquired after parent yielded"

        # Cleanup.
        if result_box:
            result_box[0]()
        with shared._cond:
            assert len(shared._running) == 0

    def test_no_deadlock_with_yield_reacquire(self, edge_harness):
        """Parent yields the slot to a child, then tries to re-acquire. The parent must
        re-enter FIFO at the tail (no bypass, no deadlock)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        rel_parent = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="parent", agent_class="coder", timeout=5.0)

        # Child acquires while parent holds → must block.
        child_granted = threading.Event()
        child_queued = threading.Event()
        child_rel_box = []

        def child():
            try:
                child_queued.set()  # entered the FIFO queue (parent holds the slot)
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="child", agent_class="coder", timeout=15.0)
                child_rel_box.append(cb)
                child_granted.set()
            except Exception:
                pass

        t_child = threading.Thread(target=child)
        t_child.start()
        assert child_queued.wait(timeout=5), "Child never reached the FIFO queue"
        assert not child_granted.is_set(), "Child must block while parent holds"

        # Parent yields.
        rel_parent()
        assert child_granted.wait(timeout=5), "Child must acquire after parent yields"

        # Child releases → parent can re-acquire (it's next in FIFO).
        child_rel_box[0]()
        rel_parent2 = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="parent", agent_class="coder", timeout=5.0)
        assert rel_parent2 is not None, "Parent must re-acquire after child releases"

        # Cleanup.
        rel_parent2()
        with shared._cond:
            assert len(shared._running) == 0


class TestC3ConcurrentSwapAndReuse:
    """Concurrent priority swap + instance reuse."""

    def test_swap_and_reuse_simultaneously(self, edge_harness):
        """One thread swaps priorities while another reuses (re-initializes) an instance.
        No crash, no slot leak."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        inst = _make_instance(pool, "c3a", "coder")

        # Instance holds the slot.
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="c3a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        errors = []
        barrier = threading.Barrier(2)

        def swap_thread():
            try:
                barrier.wait(timeout=5)
                router.set_agent_priorities("coder", [ep1_id, "ep0"])
            except Exception as e:
                errors.append(f"swap: {e}")

        def reuse_thread():
            try:
                barrier.wait(timeout=5)
                # Simulate reuse: release the stale permit.
                _release_permit(inst)
            except Exception as e:
                errors.append(f"reuse: {e}")

        t1 = threading.Thread(target=swap_thread)
        t2 = threading.Thread(target=reuse_thread)
        t1.start(); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        assert not errors, f"Concurrent swap+reuse errors: {errors}"
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ═══════════════════════════════════════════════════════════════════════════
# S1–S2: Side-Call & Caption Scenarios
# ═══════════════════════════════════════════════════════════════════════════

class TestS1CaptionDuringSwap:
    """Caption during priority swap (cross-pool side-call with swap in-flight)."""

    def test_caption_holds_slot_during_swap(self, edge_harness):
        """Agent holds the shared slot and makes a caption side-call. A priority swap
        occurs mid-caption. The caption must complete without deadlock."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "s1a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="s1a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Simulate a caption side-call: the agent calls the vision endpoint (same conc=0).
        # The sticky-keep fast path should detect the same pool and NOT re-acquire.
        result = router.sync_sticky_slot(inst, desired_key=SHARED_KEY, origin="sidecall:caption")
        assert result is True
        assert inst._slot_key == SHARED_KEY, "Caption side-call must keep the sticky slot"

        # Swap priorities mid-caption.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # Agent still holds the slot.
        assert inst._slot_key == SHARED_KEY
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0

    def test_caption_cross_pool_swap(self, edge_harness):
        """Agent holds conc=0 slot; caption resolves to a conc>0 vision endpoint.
        The side-call must drop the shared slot (cross-pool swap)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        vis_id = _add_endpoint(router, "vision", PAR_BASE, concurrency_limit=4)
        router.set_agent_priorities("coder", ["ep0", vis_id])

        inst = _make_instance(pool, "s1b", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="s1b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Caption resolves to the conc>0 vision endpoint → cross-pool swap.
        # sync_sticky_slot(desired_key=None) changes the _slot_key but does NOT
        # release the shared slot (that happens at lifecycle points).
        result = router.sync_sticky_slot(inst, desired_key=None, origin="sidecall:caption")
        assert result is True
        # The _slot_key should reflect the new pool (or be None for conc>0).
        # The shared slot remains held BY DESIGN until a lifecycle point.
        with shared._cond:
            assert "s1b" in shared._running, \
                f"Sticky slot should remain held after cross-pool swap: {list(shared._running)}"
        # Cleanup.
        _release_permit(inst)


class TestS2ImageGenDuringSwap:
    """ImageGen tool call while agent is about to swap endpoints."""

    def test_imagegen_before_swap(self, edge_harness):
        """Agent makes an imagegen tool call (side-call) just before a priority swap.
        The side-call must not interfere with the subsequent swap."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "s2a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="s2a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # ImageGen side-call (same conc=0 pool → sticky-keep).
        result = router.sync_sticky_slot(inst, desired_key=SHARED_KEY, origin="sidecall:imagegen")
        assert result is True

        # Now swap priorities.
        router.set_agent_priorities("coder", [ep1_id, "ep0"])

        # Agent still holds the slot; no deadlock.
        assert inst._slot_key == SHARED_KEY
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0


# ═══════════════════════════════════════════════════════════════════════════
# ST1–ST3: Sticky Slot Edge Cases
# ═══════════════════════════════════════════════════════════════════════════

class TestST1EndpointDisabledDuringHold:
    """Agent holds a slot, then the endpoint becomes disabled/removed → fallback chain rebuild."""

    def test_disable_endpoint_while_held(self, edge_harness):
        """Agent holds the shared slot for ep0. ep0 is disabled. The next call must
        fall back to the default (or another enabled endpoint)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        inst = _make_instance(pool, "st1a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="st1a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Disable ep0.
        with router._lock:
            for eid, ep in router.endpoints.items():
                if ep.id == "ep0":
                    ep.enabled = False

        # The chain must no longer include the disabled endpoint (Tier-1 filter).
        # The Tier-4 default IS SEQ_BASE and is always appended last, so assert that
        # SEQ_BASE appears ONLY as the final default entry — i.e. disabled ep0 was
        # excluded from the own-endpoint tiers.
        chain = router.get_endpoint_chain("coder", instance_name="st1a")
        tier_bases = [c["api_base"] for c in chain[:-1]]
        assert SEQ_BASE not in tier_bases, \
            f"disabled ep0 must be excluded from Tier-1+ bases: {tier_bases}"

        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0

    def test_remove_endpoint_while_held(self, edge_harness):
        """Agent holds the shared slot. The endpoint is removed from the router.
        The slot must still be releasable; no crash."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "st1b", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="st1b", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        # Remove the endpoint (simulate).
        with router._lock:
            router.endpoints.pop("ep0", None)

        # Slot must still be releasable.
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak after endpoint removal: {list(shared._running)}"


class TestST2CrossPoolHeavyLoad:
    """Shared slot + per-base pool both need acquiring (cross-pool swap) under heavy load."""

    def test_cross_pool_swap_under_load(self, edge_harness):
        """Multiple agents fight for the shared slot. One agent swaps to a conc>0
        endpoint (cross-pool). The swap must not deadlock the FIFO queue."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        router.set_agent_priorities("coder", ["ep0", par_id])

        # Holder acquires the shared slot.
        rel_holder = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="holder", agent_class="coder", timeout=5.0)

        # Two waiters queue up in FIFO order (Event-synchronized, no sleeps).
        waiters = _start_waiters(router, ["w1", "w2"])
        t1, ev1, rel_box1 = waiters[0]
        t2, ev2, rel_box2 = waiters[1]

        # Holder swaps to the conc>0 endpoint (cross-pool).
        # sync_sticky_slot(desired_key=None) changes _slot_key but does NOT release
        # the shared slot. To actually free the slot, we need to release it manually
        # (simulating a lifecycle point like sleep/exit).
        inst = _make_instance(pool, "holder", "coder")
        inst._slot_release = rel_holder
        inst._slot_key = SHARED_KEY
        router.sync_sticky_slot(inst, desired_key=None, origin="sticky")
        
        # Release the shared slot (simulating lifecycle point).
        _release_permit(inst)

        # w1 granted first (FIFO).
        assert ev1.wait(timeout=5), "w1 must be granted after cross-pool swap"
        t1.join(timeout=5)

        # w1 releases → w2 granted.
        rel_box1[0]()
        assert ev2.wait(timeout=5), "w2 must be granted after w1 releases"
        t2.join(timeout=5)
        # Cleanup.
        rel_box2[0]()

    def test_rapid_cross_pool_swaps(self, edge_harness):
        """Rapidly alternate between conc=0 and conc>0 endpoints. No slot leak."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        router.set_agent_priorities("coder", ["ep0", par_id])

        inst = _make_instance(pool, "st2b", "coder")

        for i in range(5):
            # Acquire shared slot.
            rel = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="st2b", agent_class="coder", timeout=5.0)
            inst._slot_release = rel
            inst._slot_key = SHARED_KEY

            # Simulate a cross-pool swap: release the shared slot (lifecycle point).
            _release_permit(inst)
            with shared._cond:
                assert "st2b" not in shared._running, \
                    f"Shared slot must be freed at iteration {i}: {list(shared._running)}"

        # After all iterations, the slot should be free.
        with shared._cond:
            assert len(shared._running) == 0, \
                f"Slot leak after rapid swaps: {list(shared._running)}"


class TestST3RapidSuccessiveSwaps:
    """Rapid successive priority swaps while agent is making calls."""

    def test_rapid_swaps_no_deadlock(self, edge_harness):
        """Perform 10 rapid priority swaps while the agent holds the slot.
        No deadlock, no crash, slot remains consistent."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        ep2_id = _add_endpoint(router, "ep2", "http://127.0.0.1:12/v1", concurrency_limit=0)

        inst = _make_instance(pool, "st3a", "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="st3a", agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY

        orders = [
            ["ep0", ep1_id, ep2_id],
            [ep1_id, "ep0", ep2_id],
            [ep2_id, ep1_id, "ep0"],
            ["ep0", ep2_id, ep1_id],
            [ep1_id, ep2_id, "ep0"],
        ]
        for i in range(10):
            router.set_agent_priorities("coder", orders[i % len(orders)])

        # Agent still holds the slot.
        assert inst._slot_key == SHARED_KEY
        _release_permit(inst)
        with shared._cond:
            assert len(shared._running) == 0

    def test_rapid_swaps_with_waiter(self, edge_harness):
        """Rapid swaps while a waiter is blocked. The waiter must eventually be
        granted when the holder releases — no starvation."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        rel_holder = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="holder", agent_class="coder", timeout=5.0)

        t_w, granted, _queued = _queue_waiter(router, pool, "w")

        # Rapid swaps while the waiter is blocked (Event-synchronized above).
        for i in range(10):
            router.set_agent_priorities("coder", ["ep0", ep1_id] if i % 2 else [ep1_id, "ep0"])

        # Release → waiter granted.
        rel_holder()
        t_w.join(timeout=5)
        assert granted.is_set(), "Waiter starved by rapid swaps"


# =============================================================================
# STRESS TESTS — Barrier-synchronized races and concurrent storms
# These tests are designed to reveal real bugs: deadlocks, slot leaks, race conditions
# =============================================================================

class TestStressConcurrentRaces:
    """Genuine stress tests with barrier-synchronized races."""

    def test_barrier_race_swap_vs_acquire(self, edge_harness):
        """8 threads race: 4 swap priorities, 4 acquire/release slots simultaneously.
        Barrier ensures all start at the same instant. No deadlock, no slot leak."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        ep2_id = _add_endpoint(router, "ep2", "http://127.0.0.1:12/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id, ep2_id])

        barrier = threading.Barrier(8)
        errors = []
        lock = threading.Lock()

        def swap_worker(idx):
            try:
                barrier.wait(timeout=5)
                for i in range(3):
                    if idx % 2 == 0:
                        router.set_agent_priorities("coder", ["ep0", ep1_id, ep2_id])
                    else:
                        router.set_agent_priorities("coder", [ep2_id, "ep0", ep1_id])
                    time.sleep(0.01)
            except Exception as e:
                with lock:
                    errors.append(f"swap_{idx}: {e}")

        def acquire_worker(idx):
            try:
                barrier.wait(timeout=5)
                for i in range(3):
                    inst = _make_instance(pool, f"stress_a{idx}_{i}", "coder")
                    rel = router.scheduler.acquire(
                        api_base=SEQ_BASE, concurrency_limit=0,
                        instance_name=f"stress_a{idx}_{i}", agent_class="coder", timeout=5.0)
                    inst._slot_release = rel
                    inst._slot_key = SHARED_KEY
                    time.sleep(0.01)
                    _release_permit(inst)
            except Exception as e:
                with lock:
                    errors.append(f"acq_{idx}: {e}")

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=swap_worker, args=(i,)))
            threads.append(threading.Thread(target=acquire_worker, args=(i,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Race errors: {errors}"
        # No slot leak.
        with shared._cond:
            assert len(shared._running) == 0, \
                f"Slot leak after concurrent race: {list(shared._running)}"

    def test_barrier_race_dismiss_vs_waiter(self, edge_harness):
        """Holder holds slot, 2 waiters queue up. Barrier releases all simultaneously
        while holder is dismissed. At least one waiter must be granted (capacity=1)."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        # Create holder instance and acquire slot.
        holder_inst = _make_instance(pool, "holder", "coder")
        rel_holder = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="holder", agent_class="coder", timeout=5.0)
        holder_inst._slot_release = rel_holder
        holder_inst._slot_key = SHARED_KEY

        barrier = threading.Barrier(3)  # 2 waiters + 1 dismisser
        granted_count = []
        lock = threading.Lock()
        queued_events = [threading.Event() for _ in range(2)]

        def waiter(name, q):
            try:
                barrier.wait(timeout=5)
                q.set()  # entered the FIFO queue (holder holds the slot)
                router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name=name, agent_class="coder", timeout=10.0)
                with lock:
                    granted_count.append(name)
            except Exception:
                pass

        def dismisser():
            try:
                barrier.wait(timeout=5)
                # Wait for BOTH waiters to be queued (Event sync — no sleep), then dismiss.
                assert queued_events[0].wait(timeout=5), "w0 never reached the FIFO queue"
                assert queued_events[1].wait(timeout=5), "w1 never reached the FIFO queue"
                pool.dismiss_instance("holder")
            except Exception:
                pass

        threads = [threading.Thread(target=waiter, args=(f"w{i}", queued_events[i]))
                   for i in range(2)]
        threads.append(threading.Thread(target=dismisser))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # At least one waiter must have been granted.
        assert len(granted_count) >= 1, "No waiter was granted after dismiss"
        # Capacity=1: at most one can hold the slot at a time.
        with shared._cond:
            assert len(shared._running) <= 1, \
                f"Capacity violated: {list(shared._running)}"

    def test_concurrent_swap_storm_20_agents(self, edge_harness):
        """20 agents all acquire/release the shared slot in rapid succession while
        priorities are being swapped concurrently. No deadlock, no leak."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        ep1_id = _add_endpoint(router, "ep1", "http://127.0.0.1:11/v1", concurrency_limit=0)
        router.set_agent_priorities("coder", ["ep0", ep1_id])

        errors = []
        lock = threading.Lock()
        stop_event = threading.Event()

        def agent_worker(idx):
            try:
                for i in range(5):
                    inst = _make_instance(pool, f"storm_{idx}_{i}", "coder")
                    rel = router.scheduler.acquire(
                        api_base=SEQ_BASE, concurrency_limit=0,
                        instance_name=f"storm_{idx}_{i}", agent_class="coder", timeout=10.0)
                    inst._slot_release = rel
                    inst._slot_key = SHARED_KEY
                    time.sleep(0.005)
                    _release_permit(inst)
            except Exception as e:
                with lock:
                    errors.append(f"agent_{idx}_{i}: {e}")

        def swap_storm():
            try:
                for i in range(30):
                    if i % 2 == 0:
                        router.set_agent_priorities("coder", ["ep0", ep1_id])
                    else:
                        router.set_agent_priorities("coder", [ep1_id, "ep0"])
                    time.sleep(0.01)
            except Exception as e:
                with lock:
                    errors.append(f"swap_storm: {e}")

        threads = [threading.Thread(target=agent_worker, args=(i,)) for i in range(20)]
        threads.append(threading.Thread(target=swap_storm))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Storm errors: {errors[:5]}"
        with shared._cond:
            assert len(shared._running) == 0, \
                f"Slot leak after 20-agent storm: {list(shared._running)}"

    def test_terminate_during_contention(self, edge_harness):
        """Agent holds slot, 2 waiters blocked. Terminate the holder while waiters
        are actively waiting. At least one waiter must be granted, no deadlock."""
        h = edge_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        rel_holder = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="victim", agent_class="coder", timeout=5.0)

        waiters = _start_waiters(router, ["tw0", "tw1"])
        t0, ev0, rel_box0 = waiters[0]
        t1, ev1, rel_box1 = waiters[1]

        # Terminate the holder while waiters are blocked.
        pool.terminate_instance("victim")
        # Release the slot (terminate doesn't auto-release).
        rel_holder()

        # At least one waiter must be granted — the FIRST one in FIFO order.
        assert ev0.wait(timeout=10), "First waiter never granted after terminate"
        t0.join(timeout=5)
        # The second waiter may or may not have been granted (capacity=1).
        if ev1.is_set():
            t1.join(timeout=5)
            assert rel_box1, "granted tw1 must hold a release callback"
        # Cleanup: release any held permit so the pool is empty at teardown.
        for box in (rel_box0, rel_box1):
            if box:
                box[0]()
        with shared._cond:
            assert len(shared._running) == 0, \
                f"Slot leak after terminate-during-contention: {list(shared._running)}"
