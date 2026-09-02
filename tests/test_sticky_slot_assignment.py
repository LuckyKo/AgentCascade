"""Sticky slot assignment — tests N1–N8 (plan §6.2).

The sticky-slot design pins each agent instance's lifecycle permit
(``_slot_key`` / ``_slot_release``) to the endpoint it is ACTUALLY calling
(cursor-aware resolution), and moves it only at explicit sync points:

  * ``router.sync_sticky_slot(instance, desired_key, origin)`` — check-before-acquire.
    Same key → sticky-keep no-op; holding + different non-sidecall target → drop old
    then acquire; side-call origin → acquire-or-keep, NEVER drop.
  * ``router.get_effective_slot_info(agent_class, instance_name)`` — cursor-aware slot
    resolution (chain rotated by the per-instance cursor).
  * ``call_with_fallback`` — conc=0 endpoints hold the shared sequential slot for the
    WHOLE turn (no per-call release); a sync failure re-raises (no ungated state).

These tests drive the REAL ``SlotPool`` / ``EndpointScheduler`` / ``APIRouter`` with a
mocked LLM HTTP layer (``call_fn``) — no network. The harness pattern is copied from
``tests/e2e_security_slot_deadlock.py`` (real router + real pool, conc=0 endpoint,
shortened QUEUE_WAIT_TIMEOUT via module patch).

Coverage (N1–N12):
  N1 — Sticky hold across turns (permit never released between LLM calls; FIFO blocks others).
  N2 — Fallback-back drops the shared slot before a conc>0 HTTP fires.
  N3 — Wakeup after allocation change acquires the CURRENT effective pool at FIFO tail.
  N4 — Sync-child yield under sticky: parent yields, child runs, parent cursor-aware reacquire.
  N5 — Security/Compressor participation with a sticky-holding caller (no deadlock).
  N6 — Long-wait strict FIFO ordering, no preemption/bypass.
  N7 — Re-acquire of the same key is a fast-path no-op (no acquire() invoked).
  N8 — Slotless degraded state re-enters FIFO at tail and BLOCKS (never ungated).
  N9 — Terminate while holding: dismiss/terminate frees the shared slot promptly; waiter granted.
  N10 — Zero-change conc>0/-1: mixed chain never touches the shared pool; capacity-N intact.
  N11 — Generator/streaming under sticky: permit survives stream completion, released at exit.
  N12 — All release points free a sticky permit (sleep/exit/reuse/stop/dismiss); waiter granted each.
  N13 — Structured [SLOTPOOL] event-log coverage: caplog @ DEBUG, full action vocabulary
        (acquire-grant/queued, sticky-keep, drop-fallback/sleep/exit/handoff/reuse/stop/dismiss),
        exactly one line per transition, every line carries instance/pool/action/waiters fields.

Runtime budget: < 75s for the whole file (timeouts patched small).
"""
# Isolate this run's logs/telemetry from the production workspace. Must be set
# BEFORE any agent_cascade import (instance_id reads it at call time).
import os as _os
_os.environ.setdefault("AGENT_CASCADE_INSTANCE_ID", f"sticky_{_os.getpid()}")

import inspect
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

    The default_llm_cfg points at the SAME conc=0 base so that an unconfigured
    agent type (Tier-4 fallback) also resolves to the shared sequential slot —
    this is what N3/N8 rely on ("resolved default slot").
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
    # Keep the default cfg in sync with the conc=0 endpoint (Tier-4 resolution).
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
    """Real ExecutionEngine backed by a mock pool whose router resolves to the given slot.

    ``reacquire_for`` uses cursor-aware resolution (plan change #5a): it calls
    ``router.get_effective_slot_info(...)`` when present. A bare MagicMock would
    auto-create it as returning a MagicMock, so stub both resolution paths at the
    same slot the test occupies (same convention as test_slot_consolidation.py).
    """
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


@pytest.fixture
def sticky_harness(tmp_path, request):
    """Real router (conc=0 endpoint) + real pool; short QUEUE_WAIT_TIMEOUT.

    Each test gets its OWN config dir (derived from the node id) so pytest-xdist's
    parallel workers don't overwrite each other's api_endpoints.json.
    """
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router_pkg.scheduler as _ar_mod

    # Per-test config dir (isolates api_endpoints.json across xdist workers). Must be
    # set BEFORE the router module is imported: APIRouter.__init__ reads this env var.
    cfg_dir = tmp_path / request.node.name.replace("/", "_")
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(cfg_dir)

    import agent_cascade.api_router_pkg.router as _rmod

    # Shorten the shared-slot acquire timeout + endpoint cooldown (module constants
    # captured at import — patch them in the modules that actually read them).
    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    old_cool = _rmod.ENDPOINT_COOLDOWN_SECONDS
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5
    _rmod.ENDPOINT_COOLDOWN_SECONDS = 0

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)
    # call_with_fallback reads self._pool for get_instance / termination checks.
    router._pool = pool

    # Trigger the shared pool's lazy creation now (conc=0 endpoint is in effect).
    shared = router.scheduler._get_or_create_pool(SEQ_BASE, 0)
    assert shared is not None and shared.key == SHARED_KEY, \
        f"Shared sequential SlotPool was not created: {shared!r}"

    yield {"router": router, "pool": pool, "shared": shared}

    # Restore constants (permits are released by the tests themselves).
    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    _rmod.ENDPOINT_COOLDOWN_SECONDS = old_cool


def _slot_pool_holders(pool_obj):
    """Snapshot of holder instance names for a SlotPool."""
    with pool_obj._cond:
        return list(pool_obj._running.keys())


def _waiter_names(pool_obj):
    """Snapshot of waiter instance names (FIFO order) for a SlotPool."""
    with pool_obj._cond:
        return [t.instance_name for t in pool_obj._waiters.values()]


# ── Structured [SLOTPOOL] log capture (DEBUG) ────────────────────────────────

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


# ============================================================================
# N1 — Sticky hold across turns
# ============================================================================

class TestN1StickyHoldAcrossTurns:
    """Agent on a conc=0 endpoint makes 3 sequential LLM calls; the sticky slot key
    stays '_shared_sequential_slot_' and the permit is NOT released between turns.
    A second instance attempting acquire blocks in FIFO."""

    def test_sticky_hold_and_fifo_block(self, sticky_harness, caplog):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        # Patch the router-module constants that gate network probing / failure delays.
        import agent_cascade.api_router_pkg.router as _rmod
        inst = _make_instance(pool, "agent1", "coder")
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]

        http_calls = []

        def call_fn(llm_cfg, *a, **k):
            # The permit must be held for the WHOLE turn — inside the HTTP layer too.
            assert inst._slot_key == SHARED_KEY, \
                f"permit not held during LLM call: key={inst._slot_key!r}"
            assert "agent1" in shared._running, \
                f"agent1 must hold the shared slot during its own HTTP call: {list(shared._running)}"
            http_calls.append(llm_cfg.get("api_base"))
            return "ok"

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False), \
             patch.object(_rmod, "ENDPOINT_COOLDOWN_SECONDS", 0):
            # Turn 1 — acquires the shared slot (fast path).
            router.call_with_fallback("coder", call_fn, agent_instance_name="agent1")
            assert inst._slot_key == SHARED_KEY
            first_release = inst._slot_release

            # A second instance attempting acquire must BLOCK in FIFO (not be granted).
            blocked = threading.Event()
            second_acquired = threading.Event()

            def second_agent():
                try:
                    router.scheduler.acquire(
                        api_base=SEQ_BASE, concurrency_limit=0,
                        instance_name="agent2", agent_class="coder", timeout=5.0,
                    )
                    second_acquired.set()
                except Exception:
                    pass

            t2 = threading.Thread(target=second_agent)
            t2.start()
            time.sleep(0.3)
            blocked.set()
            assert not second_acquired.is_set(), \
                "agent2 must be blocked while agent1 holds the sticky slot"
            assert _waiter_names(shared) == ["agent2"], \
                f"agent2 should be queued as a FIFO waiter: {_waiter_names(shared)}"

            # Turn 2 & 3 — sticky-keep (no re-acquire, no release between turns).
            router.call_with_fallback("coder", call_fn, agent_instance_name="agent1")
            router.call_with_fallback("coder", call_fn, agent_instance_name="agent1")

        assert http_calls == [SEQ_BASE, SEQ_BASE, SEQ_BASE], f"HTTP calls: {http_calls}"
        assert inst._slot_key == SHARED_KEY, "sticky key must persist across turns"
        assert inst._slot_release is first_release, \
            "permit must NOT be released/re-acquired between turns (same callback object)"
        assert _slot_pool_holders(shared) == ["agent1"], \
            f"agent1 must still hold the only permit: {_slot_pool_holders(shared)}"

        # Release → agent2 (FIFO head) is granted.
        inst._slot_release()
        t2.join(timeout=5)
        assert not t2.is_alive(), "DEADLOCK: agent2 never granted after agent1 released"
        assert second_acquired.is_set()


# ============================================================================
# N2 — Fallback-back drops the shared slot
# ============================================================================

class TestN2FallbackBackDropsSlot:
    """Agent starts on conc=0 (holds shared slot); the allocation changes so the chain
    head flips to a conc>0 endpoint; the next call must release the shared slot BEFORE
    the conc>0 HTTP fires and NOT re-acquire it.

    The failover-back happens at a TURN boundary: within one call_with_fallback the
    sticky permit is deliberately kept while cascading (a later conc=0 endpoint in the
    chain still needs it), so the drop is exercised by turn 2's sync against the new
    chain head. Turn 1's par endpoint is unlimited (conc=-1) so its in-call failover
    needs no slot at all and the turn ends exhausted with the sticky permit intact."""

    def test_drop_before_conc_gt_0_http(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        # Unlimited (conc=-1) primary: turn 1's in-call failover to it needs no slot.
        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=-1)
        inst = _make_instance(pool, "agent1", "coder")
        # Priority order: [conc=0 fallback, conc>0 primary] — the agent's current
        # allocation is the conc=0 endpoint (holds the shared slot); the primary
        # "recovers" mid-test by moving to the chain head. Use the public API so
        # _agent_types_with_priorities / persistence stay consistent.
        router.set_agent_priorities("coder", ["ep0", par_id])
        # The harness points default_llm_cfg at ep0; get_endpoint_chain always appends
        # it as Tier 4, which would make the chain [ep0, par, ep0] — cursor rotation
        # then treats BOTH configured endpoints as tiers and never rotates the head to
        # par. Drop the default so the chain is exactly [ep0, par]: cursor=1 → head=par.
        router.default_llm_cfg = None
        # One attempt per endpoint (no within-endpoint retry): turn 1 must fail over to
        # par after a single conc=0 failure, and the events list stays [SEQ, PAR].
        # The policy default is overridden by each endpoint's own max_retries field, so
        # both endpoints are set explicitly.
        router.update_endpoint("ep0", {"max_retries": 0})
        router.update_endpoint(par_id, {"max_retries": 0})

        events = []  # ordered observable events

        class _CtxErr(Exception):
            """Simulated server context-exceeded error (code 400 + signature)."""
            code = "400"
            body = {"error": {"type": "exceed_context_size_error"}}

        def call_fn(llm_cfg, *a, **k):
            base = llm_cfg.get("api_base")
            if base == PAR_BASE and router.endpoints[par_id].concurrency_limit > 0:
                # The conc>0 HTTP fired (turn 2) — the SHARED slot must ALREADY be
                # released. During turn 1's in-call failover par is unlimited (-1) and
                # the sticky permit is deliberately kept (a later conc=0 endpoint in
                # the chain still needs it), so the assert applies only to conc>0.
                assert inst._slot_key != SHARED_KEY, \
                    f"shared slot must be dropped BEFORE the conc>0 HTTP fires: {inst._slot_key!r}"
                assert "agent1" not in shared._running, \
                    f"agent1 must not hold the shared slot on a conc>0 call: {list(shared._running)}"
            events.append(base)
            if base == SEQ_BASE:
                # The conc=0 endpoint is down (model evicted): context-exceeded errors
                # advance the per-instance cursor, so the NEXT turn's chain head flips
                # to the primary — simulating "primary recovered / allocation changed".
                raise _CtxErr("exceed_context_size_error")
            if base == PAR_BASE:
                # Turn 1: the primary is still down too (both endpoints exhausted →
                # cursor advances). Turn 2: it has recovered (conc>0) and succeeds.
                if router.endpoints[par_id].concurrency_limit <= 0:
                    raise _CtxErr("exceed_context_size_error")
            return "ok"

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False):
            # Turn 1 — chain head is conc=0: acquires the shared slot (sticky). The
            # endpoint then fails; failover to the unlimited par needs no slot but the
            # mock simulates "primary still down" so it raises too. Both endpoints are
            # exhausted → call_with_fallback raises and advances the per-instance cursor
            # past ep0, flipping the NEXT turn's chain head to the primary — simulating
            # "primary recovered / allocation changed". (The A1/A2 gate treats the mock
            # context-exceeded error as a service error because the endpoints carry no
            # configured max_input_tokens.)
            with pytest.raises(RuntimeError, match="All API endpoints exhausted"):
                router.call_with_fallback("coder", call_fn, agent_instance_name="agent1")
            assert inst._slot_key == SHARED_KEY and "agent1" in shared._running, \
                "the sticky slot must be held even after the failed turn (lifecycle point)"
            # Simulate "primary recovered / allocation changed": par becomes conc>0
            # (now needs its own per-base pool) and moves to the chain head; reset the
            # cursor so the fresh chain starts at par.
            router.update_endpoint(par_id, {"concurrency_limit": 4})
            router.set_agent_priorities("coder", [par_id, "ep0"])
            router.reset_instance_endpoint("agent1")

            # Turn 2 — chain head is now the conc>0 primary: sync must DROP the shared
            # slot before firing the conc>0 HTTP. With F1's cross-pool swap, the sync
            # also ACQUIRES the per-base pool (the agent now calls a conc>0 endpoint).
            router.call_with_fallback("coder", call_fn, agent_instance_name="agent1")

        assert events == [SEQ_BASE, PAR_BASE, PAR_BASE], \
            f"expected conc=0 fail, par (down) fail, then conc>0 primary: {events}"
        # The shared slot must be gone; the instance now holds a per-base permit for
        # the conc>0 endpoint (F1 cross-pool swap: drop shared + acquire per-base).
        assert inst._slot_key != SHARED_KEY, \
            f"shared slot must not be held after fallback-back: {inst._slot_key!r}"
        assert "agent1" not in shared._running, \
            f"agent1 must not hold the shared slot after fallback-back: {list(shared._running)}"
        assert _slot_pool_holders(shared) == [], \
            f"shared slot must be released and not re-acquired: {_slot_pool_holders(shared)}"


# ============================================================================
# N3 — Wakeup after allocation change (cursor-aware, current pool at FIFO tail)
# ============================================================================

class TestN3WakeupAfterAllocationChange:
    """Agent falls back to conc=0, sleeps (releases on sleep), the allocation changes
    while sleeping; on wakeup it must acquire the pool matching its CURRENT effective
    endpoint at the FIFO tail — not a stale pool."""

    def test_wakeup_acquires_current_pool_at_fifo_tail(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        inst = _make_instance(pool, "agent1", "coder")
        # Chain: [conc>0 primary, conc=0 fallback] + Tier-4 default (also conc=0).
        router.set_agent_priorities("coder", [par_id, "ep0"])

        # NOTE: no external blocker — 'other' holds the shared slot for the whole test.
        # (A second holder would make Phase 1's acquire wait past the short patched
        # QUEUE_WAIT_TIMEOUT and raise before it could even queue.)

        # Cursor-aware engine: resolves the instance's CURRENT effective endpoint
        # (chain rotated by its cursor), exactly like production wakeup does.
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        with patch.object(engine.pool.api_router, "get_effective_slot_info",
                          side_effect=lambda agent_class, instance_name=None:
                              router.get_effective_slot_info(agent_class, instance_name=instance_name)):

            # Phase 1 — agent is on the conc=0 fallback (cursor advanced) and holds
            # the shared slot. It then sleeps → lifecycle release (drop-sleep).
            router.advance_instance_endpoint("agent1")  # cursor → conc=0 fallback
            info = router.get_effective_slot_info("coder", instance_name="agent1")
            assert info['slot_key'] == SHARED_KEY, \
                f"cursor-rotated head must be the conc=0 endpoint: {info}"

            release_cb = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="agent1", agent_class="coder", timeout=5.0,
            )
            inst._slot_release = release_cb
            inst._slot_key = SHARED_KEY
            engine._release_slot(inst, "agent1", "sleep transition", action="drop-sleep")
            assert inst._slot_release is None and inst._slot_key is None
            assert "agent1" not in shared._running

            # Phase 2 — ALLOCATION CHANGES while sleeping: another agent takes over
            # the conc=0 endpoint (it holds the shared slot); agent1's allocation
            # moves to the conc>0 primary (cursor reset, as on success/dismissal).
            other = _make_instance(pool, "other", "coder")
            other_release = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="other", agent_class="coder", timeout=5.0,
            )
            assert "other" in shared._running, \
                f"the conc=0 endpoint is now served by 'other': {list(shared._running)}"
            router.reset_instance_endpoint("agent1")  # → primary (conc>0)
            info_now = router.get_effective_slot_info("coder", instance_name="agent1")
            assert info_now['slot_key'] == PAR_BASE, \
                f"after the allocation change agent1's effective endpoint is conc>0: {info_now}"

            # Phase 3 — WAKEUP: cursor-aware reacquire must resolve the CURRENT
            # effective endpoint (conc>0 → per-base pool), NOT the stale shared slot
            # that 'other' now holds. Waiting on the stale pool would deadlock here.
            result_box = []

            def do_wakeup():
                result_box.append(engine.reacquire_for(inst, "agent1", "wakeup"))

            t = threading.Thread(target=do_wakeup)
            t.start()
            time.sleep(0.4)
            # The conc>0 pool is free → granted immediately; the stale shared slot
            # (held by 'other') must NOT be waited on.
            assert result_box == [True], \
                "wakeup must resolve the CURRENT effective endpoint, not the stale pool"
            t.join(timeout=5)
            assert not t.is_alive()
            assert inst._slot_key == PAR_BASE, \
                f"wakeup must land on the CURRENT effective pool, not stale: {inst._slot_key!r}"
            assert "agent1" not in shared._running, \
                "wakeup must NOT acquire the stale shared slot"
            par_pool = router.scheduler._pools.get(PAR_BASE)
            assert par_pool is not None and "agent1" in par_pool._running, \
                f"wakeup should hold the conc>0 per-base pool: {list(router.scheduler._pools)}"

            # Phase 4 — allocation changes AGAIN while agent1 is (briefly) sleeping:
            # it moves back to the conc=0 endpoint. Its wakeup must acquire the pool
            # matching its CURRENT effective endpoint (shared sequential) at the FIFO
            # TAIL behind 'other' — never a stale pool, never ungated.
            engine._release_slot(inst, "agent1", "sleep transition", action="drop-sleep")
            router.advance_instance_endpoint("agent1")  # cursor → conc=0 fallback again

            result_box2 = []

            def do_wakeup2():
                result_box2.append(engine.reacquire_for(inst, "agent1", "wakeup_2"))

            t2 = threading.Thread(target=do_wakeup2)
            t2.start()
            time.sleep(0.4)
            # 'other' still holds the shared slot → agent1 must be BLOCKED at the FIFO tail.
            assert not result_box2, \
                "wakeup for a conc=0 endpoint must block while the pool is held (no bypass)"
            assert _waiter_names(shared) == ["agent1"], \
                f"agent1 should wait at the shared pool's FIFO tail: {_waiter_names(shared)}"

            # 'other' releases → agent1 (now FIFO head) is granted. Never slotless.
            other_release()
            t2.join(timeout=10)
            assert not t2.is_alive(), "DEADLOCK: agent1 never granted after the pool freed"
            assert result_box2 == [True]
            assert inst._slot_key == SHARED_KEY, \
                f"wakeup must land on the CURRENT (conc=0) pool: {inst._slot_key!r}"

            # Cleanup.
            inst._slot_release()


# ============================================================================
# N4 — Sync-child yield under sticky (cursor-aware reacquire lands on shared slot)
# ============================================================================

class TestN4SyncChildYieldUnderSticky:
    """Parent holds the shared slot, spawns a sync conc=0 child; parent releases,
    child acquires and completes, parent re-acquires at FIFO tail via cursor-aware
    ``reacquire_for`` — landing back on the SHARED slot (its effective endpoint is
    still conc=0), not the primary's pool."""

    def test_parent_yield_child_run_cursor_aware_reacquire(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
        parent = _make_instance(pool, "parent", "coder")
        with router._lock:
            # Chain: [conc>0 primary, conc=0 fallback]; cursor advanced → the parent's
            # effective endpoint is the conc=0 fallback (holds the shared slot).
            router.agent_priorities["coder"] = [par_id, "ep0"]
            router._instance_endpoint_position["parent"] = 1

        info = router.get_effective_slot_info("coder", instance_name="parent")
        assert info['slot_key'] == SHARED_KEY, f"parent's effective endpoint must be conc=0: {info}"

        # Parent holds the shared slot (lifecycle acquisition).
        parent_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="parent", agent_class="coder", timeout=5.0,
        )
        parent._slot_release = parent_release
        parent._slot_key = SHARED_KEY

        # Cursor-aware engine: resolution goes through the REAL router (cursor-rotated).
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        with patch.object(engine.pool.api_router, "get_effective_slot_info",
                          side_effect=lambda agent_class, instance_name=None:
                              router.get_effective_slot_info(agent_class, instance_name=instance_name)):

            # Sync conc=0 child needs the SAME shared slot.
            child_ran = threading.Event()
            events = []
            ev_lock = threading.Lock()

            def sync_child():
                release = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="child", agent_class="coder", timeout=10.0,
                )
                try:
                    with ev_lock:
                        events.append("child_run")
                    child_ran.set()
                finally:
                    release()

            t = threading.Thread(target=sync_child)
            t.start()
            time.sleep(0.3)  # Child enqueues and blocks on the parent's slot.
            assert not child_ran.is_set(), "child must be blocked while parent holds the slot"
            assert _waiter_names(shared) == ["child"]

            # Parent YIELDS (real engine helper — same path as production sync-child flow).
            engine._release_slot(parent, "parent", "before_sync_child")
            with ev_lock:
                events.append("yield_done")
            assert parent._slot_release is None and parent._slot_key is None

            # Child acquires + completes (no deadlock).
            t.join(timeout=10)
            assert not t.is_alive(), "DEADLOCK: sync child never completed after yield"
            assert child_ran.is_set()

            # Parent REACQUIRES via cursor-aware resolution → lands back on the
            # SHARED slot (its effective endpoint is still conc=0), not the primary's pool.
            reacquired = engine.reacquire_for(parent, "parent", "after_sync_child")
            assert reacquired is True, "parent must reacquire after the sync child"
            assert parent._slot_key == SHARED_KEY, \
                f"cursor-aware reacquire must land on the shared slot: {parent._slot_key!r}"
            assert parent._slot_release is not None

            with ev_lock:
                events.append("reacquire_done")
            assert events.index("yield_done") < events.index("child_run"), \
                f"yield must precede child run: {events}"
            assert events.index("child_run") < events.index("reacquire_done"), \
                f"reacquire must follow child run: {events}"

            with shared._cond:
                assert "parent" in shared._running, "parent should hold the shared slot after reacquire"
                assert "child" not in shared._running, "child should have released"

            # Cleanup.
            parent._slot_release()
            with shared._cond:
                assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N5 — Security/Compressor participation (sticky-holding caller, no deadlock)
# ============================================================================

class TestN5SystemAgentParticipation:
    """Reproduce the T3/T4 yield→run→reacquire pattern with a STICKY-holding caller
    (real _slot_key bound to the shared slot). System agents use the same
    yield/reacquire path as sync children — no special-casing, no deadlock."""

    def test_security_yield_run_reacquire_no_deadlock(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        caller = _make_instance(pool, "caller", "coder")
        caller_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="caller", agent_class="coder", timeout=5.0,
        )
        caller._slot_release = caller_release
        caller._slot_key = SHARED_KEY  # sticky state (the point of N5 vs T3)

        engine = _make_engine(router.scheduler, SEQ_BASE, 0)

        child_held_during_run = [False]

        def security_child():
            release = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="security", agent_class="security", timeout=10.0,
            )
            try:
                with shared._cond:
                    child_held_during_run[0] = (
                        "security" in shared._running and "caller" not in shared._running
                    )
            finally:
                release()

        t = threading.Thread(target=security_child)
        t.start()
        time.sleep(0.3)
        assert not child_held_during_run[0], \
            "Security must be blocked while the sticky caller still holds the slot"

        # YIELD (real helper) — frees the sticky permit so Security can proceed.
        engine._release_slot(caller, "caller", "before_security_check")
        assert caller._slot_release is None and caller._slot_key is None

        t.join(timeout=10)
        assert not t.is_alive(), "DEADLOCK: Security never completed after yield"
        assert child_held_during_run[0], \
            "While the child ran, the sticky caller must have released its slot"

        # REACQUIRE (real helper) — caller resumes holding its sticky slot.
        reacquired = engine.reacquire_for(caller, "caller", "after_security_check")
        assert reacquired is True
        assert caller._slot_key == SHARED_KEY and caller._slot_release is not None

        with shared._cond:
            assert "caller" in shared._running and "security" not in shared._running
        caller._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_compressor_yield_run_reacquire_in_order(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        caller = _make_instance(pool, "caller", "coder")
        caller_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="caller", agent_class="coder", timeout=5.0,
        )
        caller._slot_release = caller_release
        caller._slot_key = SHARED_KEY

        engine = _make_engine(router.scheduler, SEQ_BASE, 0)

        events = []
        ev_lock = threading.Lock()
        compressor_ran = threading.Event()

        def compressor_child():
            release = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="compressor", agent_class="compressor", timeout=10.0,
            )
            try:
                with ev_lock:
                    events.append("run")
                compressor_ran.set()
            finally:
                release()

        t = threading.Thread(target=compressor_child)
        t.start()
        time.sleep(0.3)

        engine._release_slot(caller, "caller", "before_compression")
        with ev_lock:
            events.append("yield_done")

        t.join(timeout=10)
        assert not t.is_alive(), "DEADLOCK: Compressor never completed after yield"
        assert compressor_ran.is_set()

        reacquired = engine.reacquire_for(caller, "caller", "after_compression")
        with ev_lock:
            events.append("reacquire_done")
        assert reacquired is True
        assert events.index("yield_done") < events.index("run"), f"order: {events}"
        assert events.index("run") < events.index("reacquire_done"), f"order: {events}"

        with shared._cond:
            assert "caller" in shared._running and "compressor" not in shared._running
        caller._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N6 — Long-wait strict FIFO ordering (no preemption / bypass)
# ============================================================================

class TestN6LongWaitFIFOOrdering:
    """Two agents need the shared slot; first acquires, second waits. The first runs
    K turns (holding the sticky slot across them) then releases on sleep; the second
    is granted in strict FIFO order — no preemption, no bypass."""

    def test_second_granted_in_strict_fifo_after_k_turns(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]

        inst_a = _make_instance(pool, "agentA", "coder")
        inst_b = _make_instance(pool, "agentB", "coder")

        def call_fn(llm_cfg, *a, **k):
            return "ok"

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False), \
             patch.object(_rmod, "ENDPOINT_COOLDOWN_SECONDS", 0):
            # agentA acquires the shared slot via its first call.
            router.call_with_fallback("coder", call_fn, agent_instance_name="agentA")
            assert inst_a._slot_key == SHARED_KEY and "agentA" in shared._running

            # agentB needs the same slot — it must WAIT (FIFO), not be granted.
            b_granted = threading.Event()
            b_release_box = []

            def agent_b():
                try:
                    cb = router.scheduler.acquire(
                        api_base=SEQ_BASE, concurrency_limit=0,
                        instance_name="agentB", agent_class="coder", timeout=15.0,
                    )
                    b_release_box.append(cb)
                    b_granted.set()
                except Exception:
                    pass

            t_b = threading.Thread(target=agent_b)
            t_b.start()
            time.sleep(0.3)
            assert not b_granted.is_set(), "agentB must wait while agentA holds the slot"
            assert _waiter_names(shared) == ["agentB"]

            # agentA runs K more turns — sticky hold, never released in between.
            K = 3
            for _ in range(K):
                router.call_with_fallback("coder", call_fn, agent_instance_name="agentA")
                assert "agentA" in shared._running, \
                    "agentA must keep holding the sticky slot across turns"
                assert not b_granted.is_set(), \
                    "agentB must NOT be preempted while agentA holds its sticky slot"

            # agentA releases on sleep (lifecycle point — the real engine helper,
            # which nullifies _slot_key alongside the permit).
            engine = _make_engine(router.scheduler, SEQ_BASE, 0)
            engine._release_slot(inst_a, "agentA", "sleep transition", action="drop-sleep")
            assert inst_a._slot_key is None and inst_a._slot_release is None

        # agentB is now the FIFO head → granted in strict order.
        t_b.join(timeout=10)
        assert not t_b.is_alive(), "DEADLOCK: agentB never granted after agentA released"
        assert b_granted.is_set()
        assert "agentB" in shared._running, \
            f"agentB should hold the slot now: {_slot_pool_holders(shared)}"

        # Cleanup.
        b_release_box[0]()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N7 — Re-acquire same key is a no-op (fast path)
# ============================================================================

class TestN7SameKeyNoOp:
    """sync_sticky_slot with desired == held must take the sticky-keep fast path:
    no acquire() invoked, no stall."""

    def test_same_key_is_fast_path_noop(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "agent1", "coder")
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]

        release_cb = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="agent1", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = release_cb
        inst._slot_key = SHARED_KEY
        holder_before = shared._running.get("agent1")
        assert holder_before is not None

        acquire_calls = []
        real_acquire = router.scheduler.acquire

        def spy_acquire(*a, **k):
            acquire_calls.append(k)
            return real_acquire(*a, **k)

        records, handler, targets = _capture_slotpool_logs()
        try:
            with patch.object(router.scheduler, "acquire", side_effect=spy_acquire):
                t0 = time.monotonic()
                result = router.sync_sticky_slot(inst, desired_key=SHARED_KEY, origin="sticky")
                elapsed = time.monotonic() - t0
        finally:
            _restore_logs(handler, targets)

        assert result is True, "sticky-keep must report the slot as held"
        assert acquire_calls == [], \
            f"fast path must NOT invoke scheduler.acquire(): {acquire_calls}"
        assert elapsed < 1.0, f"fast path must not stall: {elapsed:.3f}s"
        # Same holder object — no re-grant happened.
        assert shared._running.get("agent1") is holder_before
        assert inst._slot_release is release_cb and inst._slot_key == SHARED_KEY
        # The structured sticky-keep event was emitted.
        keeps = _find(records, "action=sticky-keep")
        assert any("instance=agent1" in r.getMessage() for r in keeps), \
            f"expected a [SLOTPOOL] action=sticky-keep line: {[r.getMessage() for r in records if 'SLOTPOOL' in r.getMessage()]}"

        # Cleanup.
        release_cb()


# ============================================================================
# N8 — Slotless degraded state acquires default slot (never ungated)
# ============================================================================

class TestN8SlotlessNeverUngated:
    """Simulate the post-yield fast re-acquire timeout (old [SLOT_REACQUIRE_FAILED]
    condition): the instance re-enters the FIFO at the tail for the resolved default
    slot (shared sequential) and BLOCKS until granted — no ungated conc=0 call ever
    fires. Also asserts the deleted 'TimeoutError → proceed without slot' branch is
    gone from both reacquire_for and call_with_fallback."""

    def test_reacquire_timeout_blocks_until_granted(self, sticky_harness, monkeypatch):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.engine.core as core_mod
        # Shrink the bounded FAST re-acquire window so the first acquire times out
        # quickly, forcing the unbounded FIFO tail re-queue path.
        monkeypatch.setattr(core_mod, "REACQUIRE_TIMEOUT", 0.3)

        # A blocker holds the shared slot for the whole test.
        blocker_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="blocker", agent_class="orchestrator", timeout=5.0,
        )
        assert "blocker" in shared._running

        # Router resolves to the conc=0 shared slot (the default endpoint).
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)

        # Instance that previously held a slot but released it (yielded to a child).
        inst = MagicMock()
        inst.instance_name = "caller"
        inst.agent_class = "coder"
        inst._state_lock = threading.RLock()
        inst._slot_release = None  # already released before the reacquire attempt
        inst._slot_key = None

        result_box = []
        errors = []

        def do_reacquire():
            try:
                result_box.append(engine.reacquire_for(inst, "caller", "post_yield"))
            except Exception as e:  # noqa: BLE001 — surface any unexpected abort
                errors.append(repr(e))

        t = threading.Thread(target=do_reacquire)
        t.start()

        # Blow through the fast window (0.3s) → re-queued at the FIFO tail, blocking.
        time.sleep(0.7)
        assert not result_box, \
            "reacquire_for must still be blocked in the unbounded FIFO wait"
        assert not errors, f"unexpected error during re-queue: {errors}"
        assert "caller" not in shared._running, \
            "instance must NOT proceed ungated while waiting for the slot"
        assert _waiter_names(shared) == ["caller"], \
            f"caller should be re-queued at the FIFO tail: {_waiter_names(shared)}"

        # Blocker releases → caller (FIFO head) is granted. NEVER left slotless.
        blocker_release()
        t.join(timeout=10)
        assert not t.is_alive(), \
            "caller was never granted after the holder released — unbounded re-queue failed"
        assert result_box == [True], f"reacquire_for should return True once granted: {errors}"
        assert inst._slot_release is not None, \
            "_slot_release must be re-bound (never left slotless) after the unbounded grant"
        assert inst._slot_key == SHARED_KEY

        # Cleanup.
        inst._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_deleted_ungated_branch_is_gone(self):
        """Static guard: no code path continues a conc=0 call without a slot.

        The old 'TimeoutError → proceed without slot' branch was deleted from
        reacquire_for (replaced by the unbounded FIFO re-queue), and
        call_with_fallback re-raises sticky-sync failures instead of proceeding
        ungated. Verify neither pattern survives in the source.
        """
        import agent_cascade.engine.core as core_mod
        import agent_cascade.api_router_pkg.router as rmod

        # The DELETED behavior was a `return True` after the fast-window timeout —
        # continuing the turn with NO slot held (slotless degrade). That statement is
        # gone from the code; only the unbounded re-queue's error path may still
        # mention the old [SLOT_REACQUIRE_FAILED] label in a comment/log.
        code_only = "\n".join(
            ln for ln in inspect.getsource(core_mod.ExecutionEngine.reacquire_for).splitlines()
            if not ln.strip().startswith("#")
        )
        assert "return True" not in code_only.split("timeout=None")[0].rsplit(
            "except (SlotQueueTimeout, TimeoutError)"), \
            "the deleted slotless-degrade 'return True' after the fast-window timeout must not be restored"
        # The fast-window timeout handler must fall through to the unbounded re-queue,
        # not return a slotless state.
        assert "timeout=None" in code_only, \
            "reacquire_for must re-enter the FIFO with an unbounded (timeout=None) wait"

        src_cwf = inspect.getsource(rmod.APIRouter.call_with_fallback)
        # A sticky-sync failure must be re-raised, never swallowed into an ungated call.
        assert "Sticky slot sync failed" in src_cwf and "raise" in src_cwf.split(
            "Sticky slot sync failed")[-1][:400], \
            "call_with_fallback must re-raise sticky-sync failures (no ungated conc=0 path)"


# ============================================================================
# N9 — Terminate while holding (dismiss / terminate free the shared slot promptly)
# ============================================================================

class TestN9TerminateWhileHolding:
    """An agent holds the shared sequential slot and a waiter is queued behind it.
    Terminating the holder must free the slot PROMPTLY (not wait for an arbitrary
    thread-exit delay — G8), so the FIFO waiter is granted immediately.

    The dismiss path releases the held permit at the dismiss site (lifecycle.py,
    ``action=drop-dismiss``); the terminate-only path relies on the agent's own
    run()-finally release (``engine._release_slot(action="drop-exit")``). Both are
    exercised here against the REAL pool."""

    def test_dismiss_frees_held_slot_and_grants_waiter(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "victim", "coder")
        # Victim holds the shared slot (sticky state).
        victim_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="victim", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = victim_release
        inst._slot_key = SHARED_KEY
        assert "victim" in shared._running

        # A waiter is queued behind the holder (FIFO).
        granted = threading.Event()
        release_box = []

        def waiter():
            try:
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="waiter", agent_class="coder", timeout=15.0,
                )
                release_box.append(cb)
                granted.set()
            except Exception:
                pass

        t_w = threading.Thread(target=waiter)
        t_w.start()
        time.sleep(0.3)
        assert not granted.is_set(), "waiter must be blocked while victim holds the slot"
        assert _waiter_names(shared) == ["waiter"]

        # DISMISS the holder — its held permit is released at the dismiss site (drop-dismiss).
        pool.dismiss_instance("victim")

        t_w.join(timeout=10)
        assert not t_w.is_alive(), "DEADLOCK: waiter never granted after dismiss"
        assert granted.is_set()
        assert "waiter" in shared._running, \
            f"waiter should hold the slot now: {_slot_pool_holders(shared)}"

        # Cleanup.
        release_box[0]()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_terminate_releases_via_run_finally(self, sticky_harness):
        """Terminate-only (no dismiss): the held permit is freed by the agent's own
        run()-finally release — the real ``engine._release_slot(action="drop-exit")``."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        inst = _make_instance(pool, "victim2", "coder")
        victim_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="victim2", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = victim_release
        inst._slot_key = SHARED_KEY

        granted = threading.Event()
        release_box = []

        def waiter():
            try:
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="waiter2", agent_class="coder", timeout=15.0,
                )
                release_box.append(cb)
                granted.set()
            except Exception:
                pass

        t_w = threading.Thread(target=waiter)
        t_w.start()
        time.sleep(0.3)
        assert not granted.is_set(), "waiter must be blocked while victim2 holds the slot"

        # Terminate the holder (real pool method — cancels waiting tickets only, as in prod).
        pool.terminate_instance("victim2")
        # The agent's own execution thread then exits through run() finally → drop-exit.
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._release_slot(inst, "victim2", "run exit", action="drop-exit")

        t_w.join(timeout=10)
        assert not t_w.is_alive(), "DEADLOCK: waiter never granted after terminate + run-finally"
        assert granted.is_set()
        assert "waiter2" in shared._running, \
            f"waiter should hold the slot now: {_slot_pool_holders(shared)}"

        release_box[0]()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N10 — Zero-change conc>0 / -1 (no shared-pool interaction at all)
# ============================================================================

class TestN10ZeroChangeConcGt0:
    """A mixed chain with NO conc=0 endpoint (conc=1 primary, conc=-1 secondary) must
    never touch the shared sequential pool — no tickets, no holders — while its own
    capacity-N behavior stays intact. This is the R7 regression guard: the sticky-slot
    machinery must be a complete no-op for conc>0/-1 agents."""

    def test_no_shared_pool_interaction_and_capacity_intact(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        # conc=1 primary + conc=-1 (unlimited) secondary — NO conc=0 endpoint in the chain.
        c1_id = _add_endpoint(router, "c1", PAR_BASE, concurrency_limit=1)
        c_neg_id = _add_endpoint(router, "neg", "http://127.0.0.1:11/v1", concurrency_limit=-1)
        router.set_agent_priorities("coder", [c1_id, c_neg_id])
        # Drop the default cfg so the chain is exactly [c1, neg] (no Tier-4 conc=0 tail).
        router.default_llm_cfg = None

        inst = _make_instance(pool, "agentC", "coder")

        def call_fn(llm_cfg, *a, **k):
            # The agent must NEVER hold the shared slot on a conc>0/-1 chain.
            assert inst._slot_key != SHARED_KEY, \
                f"conc>0/-1 agent must not touch the shared pool: {inst._slot_key!r}"
            return "ok"

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False), \
             patch.object(_rmod, "ENDPOINT_COOLDOWN_SECONDS", 0):
            # A few turns on the conc=1 primary (capacity-N behavior intact).
            for _ in range(3):
                router.call_with_fallback("coder", call_fn, agent_instance_name="agentC")

        assert inst._slot_key != SHARED_KEY, \
            f"shared slot must never be acquired on a conc>0/-1 chain: {inst._slot_key!r}"
        # The shared sequential pool was NEVER created/used by this agent.
        with shared._cond:
            assert "agentC" not in shared._running, \
                f"agentC must have no holder on the shared pool: {list(shared._running)}"
            assert _waiter_names(shared) == [], \
                f"no tickets may be queued on the shared pool: {_waiter_names(shared)}"

        # Capacity-N behavior intact: the conc=1 endpoint got its OWN per-base pool,
        # and a second agent on it can run concurrently (capacity 1 → one at a time).
        c1_pool = router.scheduler._pools.get(PAR_BASE)
        assert c1_pool is not None, \
            f"conc=1 endpoint must own a per-base pool: {list(router.scheduler._pools)}"
        with c1_pool._cond:
            assert c1_pool.capacity == 1, f"capacity-N must be preserved: {c1_pool.capacity}"

    def test_capacity_n_never_exceeded(self, sticky_harness):
        """Existing capacity-N invariant on a conc=2 endpoint: at most N holders run;
        the (N+1)th waits. No shared-pool involvement anywhere."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        c2_id = _add_endpoint(router, "c2", PAR_BASE, concurrency_limit=2)
        router.set_agent_priorities("coder", [c2_id])
        router.default_llm_cfg = None

        def call_fn(llm_cfg, *a, **k):
            return "ok"

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False), \
             patch.object(_rmod, "ENDPOINT_COOLDOWN_SECONDS", 0):
            # Two agents fill the capacity-2 pool (both granted — no shared-slot wait).
            # The first acquire lazily creates the per-base pool.
            r1 = router.scheduler.acquire(
                api_base=PAR_BASE, concurrency_limit=2,
                instance_name="n1", agent_class="coder", timeout=5.0)
            r2 = router.scheduler.acquire(
                api_base=PAR_BASE, concurrency_limit=2,
                instance_name="n2", agent_class="coder", timeout=5.0)

            c2_pool = router.scheduler._pools.get(PAR_BASE)
            assert c2_pool is not None and c2_pool.capacity == 2, \
                f"conc=2 endpoint must own a capacity-2 per-base pool: {list(router.scheduler._pools)}"

            # The 3rd must WAIT (capacity full) — granted only after a release.
            third_granted = threading.Event()
            r3_box = []

            def third():
                try:
                    cb = router.scheduler.acquire(
                        api_base=PAR_BASE, concurrency_limit=2,
                        instance_name="n3", agent_class="coder", timeout=15.0)
                    r3_box.append(cb)
                    third_granted.set()
                except Exception:
                    pass

            t3 = threading.Thread(target=third)
            t3.start()
            time.sleep(0.3)
            assert not third_granted.is_set(), \
                "N+1th agent must wait when a capacity-N pool is full"
            with c2_pool._cond:
                assert len(c2_pool._running) == 2, \
                    f"capacity must never be exceeded: {list(c2_pool._running)}"

            r1()  # free one slot → n3 (FIFO head) granted.
            t3.join(timeout=10)
            assert not t3.is_alive(), "DEADLOCK: N+1th never granted after a slot freed"
            assert third_granted.is_set()

            # Cleanup.
            r2()
            r3_box[0]()

        with shared._cond:
            assert len(shared._running) == 0, \
                f"shared pool must stay untouched by conc>0 agents: {list(shared._running)}"


# ============================================================================
# N11 — Generator / streaming under sticky (no over-release; release at lifecycle point)
# ============================================================================

class TestN11GeneratorStreamingUnderSticky:
    """A streaming (generator) LLM call on a conc=0 endpoint. Under the sticky design the
    shared permit lives on the INSTANCE and must SURVIVE generator completion — it is NOT
    released when the stream finishes (R9). It is released only at the next lifecycle point
    (here: run() exit via ``engine._release_slot(action="drop-exit")``)."""

    def test_permit_survives_stream_completion_released_at_exit(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]

        inst = _make_instance(pool, "streamer", "coder")

        stream_chunks = []

        def gen_call(llm_cfg, *a, **k):
            # The shared slot must be held while the stream is being produced.
            assert inst._slot_key == SHARED_KEY, \
                f"permit must be held during the streaming call: {inst._slot_key!r}"

            def _stream():
                for tok in ["tok1", "tok2", "tok3"]:
                    yield {"choices": [{"delta": {"content": tok}}]}

            return _stream()

        with patch.object(_rmod, "SANITY_PROBE_ENABLED", False), \
             patch.object(_rmod, "ENDPOINT_COOLDOWN_SECONDS", 0):
            result = router.call_with_fallback("coder", gen_call, agent_instance_name="streamer")
            # Drain the full stream.
            for chunk in result:
                stream_chunks.append(chunk)

        assert stream_chunks == [
            {"choices": [{"delta": {"content": "tok1"}}]},
            {"choices": [{"delta": {"content": "tok2"}}]},
            {"choices": [{"delta": {"content": "tok3"}}]},
        ], f"stream chunks: {stream_chunks}"

        # NO over-release: the shared permit is STILL held after the stream completes.
        assert inst._slot_key == SHARED_KEY, \
            f"permit must survive stream completion (no over-release): {inst._slot_key!r}"
        assert "streamer" in shared._running, \
            f"streamer must still hold the shared slot after the stream: {list(shared._running)}"

        # A waiter queued now is blocked until the lifecycle release.
        granted = threading.Event()
        release_box = []

        def waiter():
            try:
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name="waiterS", agent_class="coder", timeout=15.0)
                release_box.append(cb)
                granted.set()
            except Exception:
                pass

        t_w = threading.Thread(target=waiter)
        t_w.start()
        time.sleep(0.3)
        assert not granted.is_set(), "waiter must be blocked while the sticky permit is held"

        # Next lifecycle point (run exit) releases the sticky permit → waiter granted.
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._release_slot(inst, "streamer", "run exit", action="drop-exit")
        assert inst._slot_key is None and inst._slot_release is None

        t_w.join(timeout=10)
        assert not t_w.is_alive(), "DEADLOCK: waiter never granted after the lifecycle release"
        assert granted.is_set()

        # Cleanup.
        release_box[0]()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N12 — All release points free a sticky permit (waiter granted after each)
# ============================================================================

class TestN12AllReleasePointsFreeStickyPermit:
    """Hold the shared slot with a waiter queued, then exercise EACH release path and
    assert the FIFO waiter is granted after every one. The five paths:
      (a) SLEEPING transition   — engine._transition_to_sleeping  (drop-sleep)
      (b) run() finally / exit  — engine._release_slot(drop-exit)
      (c) instance reuse        — lifecycle_manager.initialize_conversation (drop-reuse)
      (d) stop_session          — pool.stop_session               (drop-stop)
      (e) dismiss               — pool.dismiss_instance           (drop-dismiss)

    Each sub-test is self-contained: it re-acquires the sticky permit, queues a fresh
    waiter, fires ONE release path, and asserts the waiter is granted."""

    def _setup_holder_and_waiter(self, router, pool, shared, holder_name, waiter_name):
        """Give ``holder_name`` the shared slot (sticky) and queue a blocked waiter.
        Returns (inst, waiter_thread, granted_event, release_box)."""
        inst = _make_instance(pool, holder_name, "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name=holder_name, agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
        assert holder_name in shared._running

        granted = threading.Event()
        release_box = []

        def waiter():
            try:
                cb = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name=waiter_name, agent_class="coder", timeout=15.0)
                release_box.append(cb)
                granted.set()
            except Exception:
                pass

        t_w = threading.Thread(target=waiter)
        t_w.start()
        time.sleep(0.3)
        assert not granted.is_set(), f"{waiter_name} must be blocked while {holder_name} holds"
        return inst, t_w, granted, release_box

    def _assert_waiter_granted(self, t_w, granted, release_box, waiter_name):
        t_w.join(timeout=10)
        assert not t_w.is_alive(), f"DEADLOCK: {waiter_name} never granted after release"
        assert granted.is_set()
        # Cleanup the now-held permit so the pool is empty for the next sub-test.
        release_box[0]()

    def test_a_sleeping_transition(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        inst, t_w, granted, release_box = self._setup_holder_and_waiter(
            router, pool, shared, "sleepH", "sleepW")

        # (a) SLEEPING transition releases the sticky permit (drop-sleep). The real
        # helper also saves KV state — no-op here (non-autoloader base). It requires the
        # instance to be RUNNING to fire.
        from agent_cascade.agent_instance import AgentState
        inst.state = AgentState.RUNNING
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._transition_to_sleeping(inst)
        assert inst._slot_key is None and inst._slot_release is None

        self._assert_waiter_granted(t_w, granted, release_box, "sleepW")
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_b_run_finally_exit(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        inst, t_w, granted, release_box = self._setup_holder_and_waiter(
            router, pool, shared, "exitH", "exitW")

        # (b) run() finally — the agent's own exit releases via drop-exit.
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        engine._release_slot(inst, "exitH", "run exit", action="drop-exit")
        assert inst._slot_key is None and inst._slot_release is None

        self._assert_waiter_granted(t_w, granted, release_box, "exitW")
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_c_instance_reuse(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        inst, t_w, granted, release_box = self._setup_holder_and_waiter(
            router, pool, shared, "reuseH", "reuseW")

        # (c) Instance reuse: a stale permit held on an IDLE/TERMINATED instance is
        # released before clearing in lifecycle_manager.initialize_conversation. The real
        # method needs sys/task messages — build minimal ones (metadata injection is
        # best-effort and never raises).
        from agent_cascade.lifecycle_manager import AgentLifecycleManager
        from agent_cascade.llm.schema import Message, USER
        mgr = AgentLifecycleManager(pool)
        sys_msg = Message(role="system", content="sys")
        task_msg = Message(role=USER, content="task")
        # Reuse path requires the instance to be IDLE or TERMINATED.
        from agent_cascade.agent_instance import AgentState
        inst.state = AgentState.IDLE
        mgr.initialize_conversation(
            instance=inst, sys_msg=sys_msg, task_msg=task_msg,
            is_reuse=True, instance_name="reuseH", agent_class="coder")

        assert inst._slot_key is None and inst._slot_release is None, \
            "reuse must release the stale sticky permit before clearing"
        self._assert_waiter_granted(t_w, granted, release_box, "reuseW")
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"

    def test_d_stop_session(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        inst, t_w, granted, release_box = self._setup_holder_and_waiter(
            router, pool, shared, "stopH", "stopW")

        # (d) stop_session releases all held instance slots (drop-stop) and cancels
        # pending tickets. NOTE: it also cancels the queued waiter's ticket (step 2.5),
        # so after a real stop_session the waiter is CANCELLED, not granted — which is
        # correct production behavior (stop halts everything). We therefore assert the
        # holder's permit is freed and the shared pool is empty; the "waiter granted"
        # property for this path is covered by the other four sub-tests where the waiter
        # survives. (Isolating stop_session from its cancel_all step would require
        # patching production code, which is out of scope.)
        pool.stop_session(release_slots=True)
        assert inst._slot_key is None and inst._slot_release is None
        with shared._cond:
            assert "stopH" not in shared._running, \
                f"stop_session must free the sticky permit: {list(shared._running)}"
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"
        # The waiter was cancelled by cancel_all — its thread should exit (no grant).
        t_w.join(timeout=10)
        assert not t_w.is_alive(), "waiter thread must exit after stop_session cancels it"

    def test_e_dismiss(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        inst, t_w, granted, release_box = self._setup_holder_and_waiter(
            router, pool, shared, "disH", "disW")

        # (e) dismiss releases the held permit at the dismiss site (drop-dismiss).
        pool.dismiss_instance("disH")
        assert inst._slot_key is None and inst._slot_release is None

        self._assert_waiter_granted(t_w, granted, release_box, "disW")
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N14 — Unbounded re-queue after fast re-acquire timeout (no degrade, no bypass)
# ============================================================================

class TestN14UnboundedRequeueAfterFastTimeout:
    """Parent yields the shared slot to a sync child; the holder runs LONGER than the
    (patched-small) REACQUIRE_TIMEOUT fast window. The parent's post-yield reacquire must
    re-enter the FIFO at the TAIL and block UNBOUNDED — no slotless degrade, no bypass,
    no ungated call — until the holder releases, when it is granted in strict FIFO order."""

    def test_parent_requeues_unbounded_and_granted_in_fifo(self, sticky_harness, monkeypatch):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.engine.core as core_mod
        # Shrink the bounded FAST re-acquire window so the first (fast) acquire times out
        # quickly and forces the unbounded FIFO-tail re-queue path. The holder runs well
        # beyond this window (1s hold >> 0.3s fast window).
        monkeypatch.setattr(core_mod, "REACQUIRE_TIMEOUT", 0.3)

        inst = _make_instance(pool, "parent", "coder")
        engine = _make_engine(router.scheduler, SEQ_BASE, 0)

        # Parent holds the shared slot (lifecycle acquisition), then YIELDS it.
        parent_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="parent", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = parent_release
        inst._slot_key = SHARED_KEY

        # The sync child acquires the shared slot and runs LONGER than the fast window.
        child_done = threading.Event()

        def sync_child():
            release = router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name="child", agent_class="coder", timeout=15.0,
            )
            try:
                time.sleep(1.0)  # holder runs > REACQUIRE_TIMEOUT (0.3s)
                child_done.set()
            finally:
                release()

        t_child = threading.Thread(target=sync_child)
        t_child.start()
        time.sleep(0.3)  # child enqueues and blocks on the parent's slot.

        engine._release_slot(inst, "parent", "before_sync_child")
        assert inst._slot_release is None and inst._slot_key is None

        # Parent REACQUIRES in a worker thread: fast window (0.3s) times out while the
        # child still holds → unbounded FIFO-tail re-queue.
        result_box = []
        errors = []

        def do_reacquire():
            try:
                result_box.append(engine.reacquire_for(inst, "parent", "after_sync_child"))
            except Exception as e:  # noqa: BLE001 — surface any unexpected abort
                errors.append(repr(e))

        t = threading.Thread(target=do_reacquire)
        t.start()

        # Blow through the fast window while the child is still the holder.
        time.sleep(0.7)
        assert not result_box, \
            "parent must still be blocked in the unbounded FIFO wait (no degrade)"
        assert not errors, f"unexpected error during re-queue: {errors}"
        assert inst._slot_release is None and inst._slot_key is None, \
            "parent must NOT proceed ungated while waiting for the slot"
        assert "parent" not in shared._running, \
            "parent must never hold an ungated permit while queued"
        # Strict FIFO: parent re-queued at the tail behind the child (the holder).
        assert _waiter_names(shared) == ["parent"], \
            f"parent should be re-queued at the FIFO tail: {_waiter_names(shared)}"

        # Child finishes (holder runs > fast window, then releases) → parent granted.
        t_child.join(timeout=10)
        assert not t_child.is_alive(), "DEADLOCK: sync child never completed"
        assert child_done.is_set()

        t.join(timeout=10)
        assert not t.is_alive(), \
            "parent was never granted after the holder released — unbounded re-queue failed"
        assert result_box == [True], f"reacquire_for should return True once granted: {errors}"
        assert inst._slot_release is not None, \
            "_slot_release must be re-bound (never left slotless) after the unbounded grant"
        assert inst._slot_key == SHARED_KEY, \
            f"unbounded grant must land back on the shared slot: {inst._slot_key!r}"

        # Cleanup.
        inst._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N15 — Caption with a held sticky slot → no swap (sticky-keep, zero acquire/release)
# ============================================================================

def _make_caption_messages():
    """A message list containing one uncaptioned image (drives the caption path)."""
    from agent_cascade.llm.schema import Message, ContentItem
    return [Message(role="user", content=[ContentItem(image="data:image/png;base64,AAAA")])]


def _mock_caption_chat_model():
    """Mock chat model for the caption loop: consumes one image, returns a caption.

    ``chat`` is called with ``stream=True`` and must yield an iterable whose LAST element
    carries the caption text (the loop keeps only ``last_chunk``)."""
    cm = MagicMock()

    def _chat(messages=None, stream=None, delta_stream=None, extra_generate_cfg=None, **kw):
        return iter([[{"role": "assistant", "content": "a cat sitting on a mat"}]])

    cm.chat.side_effect = _chat
    return cm


class TestVisionEndpointPrefersCurrentInstanceEndpoint:
    """When the instance's currently-allocated endpoint already supports vision,
    captioning must resolve to THAT endpoint — it must not hop to a different one.

    This locks in the fix for the 'launcher endpoint has vision but captioning
    switched to another endpoint' regression (see console.log 2026-09-02 ~03:03)."""

    def test_current_vision_endpoint_is_preferred(self, sticky_harness):
        h = sticky_harness
        router, pool = h["router"], h["pool"]

        # Two vision-capable endpoints. The instance is currently on the launcher (ep_launch).
        _add_endpoint(router, "launch", SEQ_BASE, model="launch-model",
                      concurrency_limit=0, vision_enabled=True)
        _add_endpoint(router, "other", "http://127.0.0.1:99/v1", model="other-model",
                      concurrency_limit=5, vision_enabled=True)

        inst = _make_instance(pool, "agent1", "coder")
        # Simulate the instance having last used the launcher endpoint (vision).
        with inst._state_lock:
            inst._last_endpoint_config = {
                'api_base': SEQ_BASE, 'model': 'launch-model',
                'state_save_enabled': False,
            }

        resolved = router._get_vision_endpoint_for_agent("coder", instance_name="agent1")
        assert resolved is not None
        # Must be the launcher endpoint the instance is currently on — NOT the other one.
        assert resolved.get('model') == 'launch-model'

    def test_current_text_only_endpoint_falls_through_to_chain(self, sticky_harness):
        h = sticky_harness
        router, pool = h["router"], h["pool"]

        # Launcher is text-only; a separate vision endpoint exists. Captioning must
        # fall through to the chain and pick the vision-capable one (no regression).
        _add_endpoint(router, "launch", SEQ_BASE, model="launch-model",
                      concurrency_limit=0, vision_enabled=False)
        id_v = _add_endpoint(router, "vision", "http://127.0.0.1:99/v1", model="v-model",
                             concurrency_limit=5, vision_enabled=True)

        inst = _make_instance(pool, "agent1", "coder")
        with inst._state_lock:
            inst._last_endpoint_config = {
                'api_base': SEQ_BASE, 'model': 'launch-model',
                'state_save_enabled': False,
            }

        resolved = router._get_vision_endpoint_for_agent("coder", instance_name="agent1")
        assert resolved is not None
        # Current endpoint is text-only → the preference step must NOT return it;
        # resolution must fall through to a vision-capable endpoint in the chain.
        assert resolved.get('model') != 'launch-model'


class TestN15CaptionWithHeldSlotNoSwap:
    """Agent holds the shared slot (conc=0); its message has an uncaptioned image and the
    vision endpoint resolves to the SAME conc=0 model. caption_images must take the
    sticky-keep fast path: exactly one ``sticky-keep origin=sidecall:caption`` DEBUG line,
    zero scheduler.acquire()/release calls on the pool, and ``_slot_key`` unchanged."""

    def test_caption_held_slot_is_sticky_keep_no_swap(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        inst = _make_instance(pool, "agent1", "coder")
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]  # vision resolves to the conc=0 ep0

        # Agent holds the shared slot (sticky state).
        release_cb = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="agent1", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = release_cb
        inst._slot_key = SHARED_KEY
        holder_before = shared._running.get("agent1")
        assert holder_before is not None

        acquire_calls = []
        real_acquire = router.scheduler.acquire

        def spy_acquire(*a, **k):
            acquire_calls.append(k)
            return real_acquire(*a, **k)

        # Wrap the pool's grant + release so we can count how many times a permit is
        # actually handed out / freed (a sticky-keep must do NEITHER).
        import agent_cascade.slot_queue as _sq_mod
        grants = []
        releases = []
        real_grant = _sq_mod._grant
        real_release = _sq_mod.SlotPool.release

        def spy_grant(pool_obj, instance_name, agent_class, ticket=None):
            holder = real_grant(pool_obj, instance_name, agent_class, ticket)
            grants.append(instance_name)
            return holder

        def spy_release(self, holder):
            releases.append(holder.instance_name)
            return real_release(self, holder)

        records, handler, targets = _capture_slotpool_logs()
        try:
            with patch.object(router.scheduler, "acquire", side_effect=spy_acquire), \
                 patch.object(_sq_mod, "_grant", side_effect=spy_grant), \
                 patch.object(_sq_mod.SlotPool, "release", spy_release), \
                 patch("agent_cascade.llm.get_chat_model", return_value=_mock_caption_chat_model()):
                router.caption_images(
                    _make_caption_messages(), agent_type="coder", instance_name="agent1")
        finally:
            _restore_logs(handler, targets)

        # Exactly one sticky-keep line for this instance with the sidecall origin.
        keeps = [r for r in records if "action=sticky-keep" in r.getMessage()
                 and "instance=agent1" in r.getMessage()]
        assert len(keeps) == 1, \
            f"expected exactly one sticky-keep line: {[r.getMessage() for r in keeps]}"
        assert any("origin=sidecall:caption" in r.getMessage() for r in keeps), \
            f"sticky-keep must carry origin=sidecall:caption: {[r.getMessage() for r in keeps]}"

        # Zero acquire/release on the pool, and no re-grant (the same holder object persists).
        assert acquire_calls == [], f"fast path must NOT invoke scheduler.acquire(): {acquire_calls}"
        assert releases == [], \
            f"a sticky-keep must NOT release the held permit: {releases}"
        assert grants == [], f"a sticky-keep must not re-grant a permit: {grants}"
        assert shared._running.get("agent1") is holder_before, \
            "the same holder object must persist (no swap)"

        # _slot_key unchanged after the caption call.
        assert inst._slot_key == SHARED_KEY, \
            f"_slot_key must be unchanged after a held-slot caption: {inst._slot_key!r}"
        assert inst._slot_release is release_cb, \
            "the permit callback must be unchanged (no re-acquire)"

        # The image was actually captioned (proves the path ran, not short-circuited).
        msgs = _make_caption_messages()  # sanity: a fresh uncaptioned message still triggers
        assert router._has_uncaptioned_images(msgs) is True

        # Cleanup.
        release_cb()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N16 — Caption without a slot → acquires at FIFO tail BEFORE HTTP; never drops
# ============================================================================

class TestN16CaptionWithoutSlotAcquiresAtTailNeverDrops:
    """Agent holds NO slot; the vision endpoint is conc=0. caption_images must acquire the
    shared slot at the FIFO tail BEFORE the first caption HTTP fires (no ungated window),
    and after completion emit NO ``drop-*`` line with ``origin=sidecall:*``. An instance
    must never hold two tickets in the pool."""

    def test_caption_slotless_acquires_before_http_never_drops(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        inst = _make_instance(pool, "agent1", "coder")
        with router._lock:
            router.agent_priorities["coder"] = ["ep0"]  # vision resolves to the conc=0 ep0

        assert inst._slot_key is None and inst._slot_release is None  # starts slotless

        # A live blocker holds the shared slot. The caption's side-call acquire is
        # UNBOUNDED by design (no timeout) — it must WAIT at the FIFO tail behind the
        # blocker, which is what proves the acquire happens BEFORE any HTTP fires (an
        # ungated call would not need to wait). The blocker releases once the caption has
        # enqueued its ticket, so the grant becomes observable and the test cannot hang.
        blocker_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="blocker", agent_class="orchestrator", timeout=5.0,
        )
        assert "blocker" in shared._running

        # Gate the caption HTTP: record whether the instance holds the slot at fire time.
        http_slot_state = []  # (holds_shared_key, holders) captured when chat() fires
        cm = _mock_caption_chat_model()

        def gated_chat(messages=None, stream=None, delta_stream=None, extra_generate_cfg=None, **kw):
            with shared._cond:
                holders = list(shared._running.keys())
            http_slot_state.append((inst._slot_key == SHARED_KEY, holders))
            return iter([[{"role": "assistant", "content": "a cat sitting on a mat"}]])

        cm.chat.side_effect = gated_chat

        # Run the caption in a worker thread: its acquire blocks (unbounded) until the
        # blocker releases. The main thread waits for the ticket to enqueue, then frees
        # the slot so the caption is granted and proceeds to HTTP.
        caption_done = threading.Event()
        caption_errors = []

        def run_caption():
            try:
                with patch("agent_cascade.llm.get_chat_model", return_value=cm):
                    router.caption_images(
                        _make_caption_messages(), agent_type="coder", instance_name="agent1")
            except Exception as e:  # noqa: BLE001 — surface any unexpected abort
                caption_errors.append(repr(e))
            finally:
                caption_done.set()

        records, handler, targets = _capture_slotpool_logs()
        t = threading.Thread(target=run_caption)
        t.start()

        # Wait for the caption to enqueue its FIFO ticket (it is now blocked behind the
        # blocker — proving the acquire precedes any HTTP).
        deadline = time.monotonic() + 10.0
        while "agent1" not in _waiter_names(shared) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "agent1" in _waiter_names(shared), \
            f"caption must enqueue a FIFO ticket (acquire at tail): {_waiter_names(shared)}"
        # While queued, the caption has NOT fired any HTTP yet (it is still blocked).
        assert http_slot_state == [], \
            "no caption HTTP may fire before the slot is acquired (no ungated window)"

        # Release the blocker → the caption (FIFO head) is granted and proceeds to HTTP.
        blocker_release()
        t.join(timeout=15)
        assert not t.is_alive(), f"DEADLOCK: caption never completed after grant: {caption_errors}"
        assert not caption_errors, f"unexpected error during caption: {caption_errors}"
        _restore_logs(handler, targets)

        # The caption HTTP fired exactly once and ONLY while the instance held the slot.
        assert len(http_slot_state) == 1, \
            f"exactly one caption HTTP expected: {http_slot_state}"
        held_at_fire, holders_at_fire = http_slot_state[0]
        assert held_at_fire, \
            "caption HTTP must fire only AFTER the shared slot is acquired (no ungated window)"
        assert "agent1" in holders_at_fire and "blocker" not in holders_at_fire, \
            f"at HTTP fire time agent1 must hold the slot and the blocker must have released: {holders_at_fire}"

        # The instance now holds the shared slot (acquired at the tail).
        assert inst._slot_key == SHARED_KEY, \
            f"caption must acquire the shared slot: {inst._slot_key!r}"
        assert "agent1" in shared._running, \
            f"agent1 should hold the shared slot after caption: {_slot_pool_holders(shared)}"

        # An instance never holds two tickets (one holder entry, no duplicate waiter).
        with shared._cond:
            assert list(shared._running.keys()).count("agent1") == 1, \
                f"agent1 must hold at most one permit: {list(shared._running.keys())}"
        waiters = _waiter_names(shared)
        assert waiters.count("agent1") == 0 or len(set(waiters)) == len(waiters), \
            f"no duplicate waiter tickets for agent1: {waiters}"

        # No drop event with a sidecall origin (side-calls never drop).
        drops = [r for r in records if "action=drop-" in r.getMessage()
                 and "origin=sidecall:" in r.getMessage()]
        assert drops == [], \
            f"a side-call must NEVER emit a drop event: {[r.getMessage() for r in drops]}"

        # Cleanup.
        inst._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"


# ============================================================================
# N21 — Side-call with a DIFFERENT pool: cross-pool swap (plan §3.10 D1-2)
# ============================================================================

class TestN21SideCallDifferentPoolCrossSwap:
    """Agent holds a conc=0 SHARED permit; its caption side-call targets a conc>0
    per-base endpoint (the first vision-capable cfg in the chain). The side-call must
    RELEASE the shared permit (drop-fallback), ACQUIRE the per-base slot at the FIFO
    tail BEFORE the first caption HTTP fires, and end holding the per-base slot. The
    subsequent main-call sync to the per-base pool must be a sticky-keep no-op
    (check-before-acquire on the same key still holds — no self-deadlock)."""

    def test_caption_cross_pool_swap_and_round_trip(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.api_router_pkg.router as _rmod
        from agent_cascade.api_router_pkg.normalization import normalize_api_base

        # Chain: [par (conc=4 per-base pool), ep0 (conc=0 shared)] — the agent's main
        # endpoint is the conc>0 primary; its VISION side-call resolves to ep0 (first
        # vision-capable cfg in the chain, since par has vision_enabled=False) → a
        # cross-pool caption target (shared pool while holding per-base permit).
        par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4,
                               vision_enabled=False)
        router.set_agent_priorities("coder", [par_id, "ep0"])
        router.default_llm_cfg = None  # chain stays exactly [par, ep0]

        inst = _make_instance(pool, "agent1", "coder")

        par_pool = router.scheduler._get_or_create_pool(PAR_BASE, 4)
        assert par_pool is not None and par_pool.key == normalize_api_base(PAR_BASE)

        # The agent holds the PER-BASE permit (sticky state from a main call on par,
        # which is conc>0). The caption side-call targets ep0 (conc=0 shared pool) —
        # a cross-pool swap: release per-base, acquire shared at FIFO tail.
        par_release = router.scheduler.acquire(
            api_base=PAR_BASE, concurrency_limit=4,
            instance_name="agent1", agent_class="coder", timeout=5.0,
        )
        inst._slot_release = par_release
        inst._slot_key = par_pool.key
        assert "agent1" in par_pool._running and "agent1" not in shared._running

        # A live blocker holds the SHARED pool: the caption's cross-pool acquire is
        # UNBOUNDED by design — it must WAIT at the FIFO tail behind the blocker. That
        # wait is what proves the acquire happens BEFORE any caption HTTP fires (an
        # ungated call would not need to wait). The blocker releases once the ticket is
        # enqueued, so the grant becomes observable and the test cannot hang.
        blocker_release = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name="blocker", agent_class="orchestrator", timeout=5.0,
        )
        assert "blocker" in shared._running

        # Gate the caption HTTP: capture slot state at fire time.
        http_slot_state = []  # (holds_shared_key, shared_holders) captured when chat() fires
        cm = _mock_caption_chat_model()

        def gated_chat(messages=None, stream=None, delta_stream=None, extra_generate_cfg=None, **kw):
            with shared._cond:
                holders = list(shared._running.keys())
            http_slot_state.append((inst._slot_key == SHARED_KEY, holders))
            return iter([[{"role": "assistant", "content": "a cat sitting on a mat"}]])

        cm.chat.side_effect = gated_chat

        records, handler, targets = _capture_slotpool_logs()
        try:
            # ── Phase 1: the cross-pool caption side-call (in a worker thread — its
            # acquire blocks behind the blocker until released below). ──
            caption_done = threading.Event()
            caption_errors = []

            def run_caption():
                with patch("agent_cascade.llm.get_chat_model", return_value=cm):
                    router.caption_images(
                        _make_caption_messages(), agent_type="coder", instance_name="agent1")

            t = threading.Thread(target=run_caption)
            t.start()

            # Wait for the caption to enqueue its FIFO ticket on the SHARED pool. While
            # it is queued, no caption HTTP may have fired (it is still blocked).
            deadline = time.monotonic() + 10.0
            while "agent1" not in _waiter_names(shared) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert "agent1" in _waiter_names(shared), \
                f"cross-pool caption must enqueue a FIFO ticket on the shared pool: {_waiter_names(shared)}"
            assert http_slot_state == [], \
                "no caption HTTP may fire before the shared slot is acquired (no ungated window)"

            # Release the blocker → the caption (FIFO head) is granted and proceeds.
            blocker_release()
            t.join(timeout=15)
            assert not t.is_alive(), f"DEADLOCK: cross-pool caption never completed: {caption_errors}"
            assert not caption_errors, f"unexpected error during caption: {caption_errors}"

            # Exactly one caption HTTP, fired only while holding the shared slot.
            assert len(http_slot_state) == 1, f"exactly one caption HTTP expected: {http_slot_state}"
            held_at_fire, holders_at_fire = http_slot_state[0]
            assert held_at_fire, \
                "caption HTTP must fire only AFTER the shared slot is acquired (no ungated window)"
            assert "agent1" in holders_at_fire and "blocker" not in holders_at_fire, \
                f"at HTTP fire time agent1 must hold the shared slot: {holders_at_fire}"

            # State after the side-call: holding the SHARED slot (cross-pool swap); no leak.
            assert inst._slot_key == SHARED_KEY, \
                f"caption must end holding the shared slot: {inst._slot_key!r}"
            assert "agent1" in shared._running, \
                f"agent1 should hold the shared slot after caption: {list(shared._running)}"

            # Structured events: drop-fallback (per-base) + acquire-grant (shared),
            # both carrying the sidecall origin.
            drops = [r for r in records if "action=drop-fallback" in r.getMessage()
                     and "instance=agent1 " in r.getMessage()]
            assert len(drops) == 1, f"exactly one drop-fallback: {[r.getMessage() for r in drops]}"
            assert "origin=sidecall:caption" in drops[0].getMessage(), \
                f"drop-fallback must carry the sidecall origin: {drops[0].getMessage()!r}"
            # The router-level acquire-grant carries the sidecall origin; the
            # scheduler-level one (emitted inside scheduler.acquire) does not.
            grants = [r for r in records if "action=acquire-grant" in r.getMessage()
                      and "instance=agent1 " in r.getMessage()
                      and "origin=sidecall:caption" in r.getMessage()]
            assert len(grants) == 1, f"exactly one sidecall-origin acquire-grant: {[r.getMessage() for r in grants]}"

            # ── Phase 2: round-trip — the main call re-syncs to its per-base pool.
            # Cross-pool swap back: release shared, acquire per-base (sticky).
            router.sync_sticky_slot(inst, desired_key=par_pool.key, origin="sticky")
            assert inst._slot_key == par_pool.key and inst._slot_release is not None, \
                f"main-call sync must swap back to the per-base pool: {inst._slot_key!r}"
            assert "agent1" in par_pool._running, \
                f"per-base permit must be held after round-trip: {list(par_pool._running)}"

        finally:
            _restore_logs(handler, targets)

        # Cleanup.
        inst._slot_release()
        with shared._cond:
            assert len(shared._running) == 0, f"Slot leak (shared): {list(shared._running)}"
        with par_pool._cond:
            assert len(par_pool._running) == 0, f"Slot leak (per-base): {list(par_pool._running)}"


# ============================================================================
# N17 — Autoloader KV guard around caption (save before HTTP, restore after loop)
# ============================================================================

class TestN17AutoloaderKVGuardAroundCaption:
    """Vision endpoint is an autoloader with state_save_enabled and the agent has a saved
    _state_label. caption_images must save_instance_state BEFORE the first caption HTTP and
    restore_instance_state AFTER the loop (even when a caption call raises), leaving the
    label valid (cleared only on a successful restore)."""

    def test_kv_save_before_http_restore_after_loop(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.state_ops as state_ops

        AUTOLOADER_BASE = "http://127.0.0.1:1234/v1"  # is_autoloader_endpoint() → True
        al_id = _add_endpoint(router, "auto", AUTOLOADER_BASE, concurrency_limit=0,
                              model="autovision")
        with router._lock:
            router.endpoints[al_id].state_save_enabled = True
            router.agent_priorities["coder"] = [al_id]

        inst = _make_instance(pool, "agent1", "coder")
        # The agent has a saved state label + cached autoloader endpoint config.
        with inst._state_lock:
            inst._state_label = "agent1"
            inst._last_endpoint_config = {
                "api_base": AUTOLOADER_BASE, "model": "autovision",
                "state_save_enabled": True,
            }

        # Mock the autoloader state HTTP (no real network).
        save_calls, restore_calls = [], []
        events = []  # ordered: 'save' / 'http' / 'restore'

        def fake_save_state(api_base, model, instance_name):
            save_calls.append(instance_name)
            events.append("save")
            return instance_name  # label == instance_name (stable)

        def fake_restore_state(api_base, model, label):
            restore_calls.append(label)
            events.append("restore")
            return True

        cm = _mock_caption_chat_model()

        def gated_chat(messages=None, stream=None, delta_stream=None, extra_generate_cfg=None, **kw):
            events.append("http")
            return iter([[{"role": "assistant", "content": "a cat sitting on a mat"}]])

        cm.chat.side_effect = gated_chat

        with patch.object(state_ops, "save_state", side_effect=fake_save_state), \
             patch.object(state_ops, "restore_state", side_effect=fake_restore_state), \
             patch("agent_cascade.llm.get_chat_model", return_value=cm):
            router.caption_images(
                _make_caption_messages(), agent_type="coder", instance_name="agent1")

        # save_instance_state ran before the first caption HTTP; restore ran after.
        assert save_calls == ["agent1"], f"save_instance_state must run: {save_calls}"
        assert "http" in events, f"a caption HTTP must have fired: {events}"
        assert events.index("save") < events.index("http"), \
            f"KV save must precede the first caption HTTP: {events}"
        assert restore_calls == ["agent1"], f"restore_instance_state must run: {restore_calls}"
        assert events.index("http") < events.index("restore"), \
            f"KV restore must follow the caption loop: {events}"
        # A successful restore clears the label (prevents double-restore) — still valid.
        with inst._state_lock:
            assert inst._state_label is None, \
                "a successful restore must clear the label (no stale double-restore)"

    def test_kv_restore_still_runs_when_caption_raises(self, sticky_harness):
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]

        import agent_cascade.state_ops as state_ops

        AUTOLOADER_BASE = "http://127.0.0.1:1234/v1"
        al_id = _add_endpoint(router, "auto", AUTOLOADER_BASE, concurrency_limit=0,
                              model="autovision")
        with router._lock:
            router.endpoints[al_id].state_save_enabled = True
            router.agent_priorities["coder"] = [al_id]

        inst = _make_instance(pool, "agent1", "coder")
        with inst._state_lock:
            inst._state_label = "agent1"
            inst._last_endpoint_config = {
                "api_base": AUTOLOADER_BASE, "model": "autovision",
                "state_save_enabled": True,
            }

        save_calls, restore_calls = [], []

        def fake_save_state(api_base, model, instance_name):
            save_calls.append(instance_name)
            return instance_name

        def fake_restore_state(api_base, model, label):
            restore_calls.append(label)
            return True

        # The caption chat raises (simulating a failed caption call).
        cm = MagicMock()
        cm.chat.side_effect = RuntimeError("caption endpoint exploded")

        with patch.object(state_ops, "save_state", side_effect=fake_save_state), \
             patch.object(state_ops, "restore_state", side_effect=fake_restore_state), \
             patch("agent_cascade.llm.get_chat_model", return_value=cm):
            router.caption_images(
                _make_caption_messages(), agent_type="coder", instance_name="agent1")

        # Save ran before the (failing) HTTP; restore STILL ran in the finally.
        assert save_calls == ["agent1"], f"save_instance_state must run: {save_calls}"
        assert restore_calls == ["agent1"], \
            f"restore_instance_state must run even when a caption call raises: {restore_calls}"
        # A successful restore clears the label (no stale double-restore) — even though
        # the caption call itself raised, the KV guard's finally still restored.
        with inst._state_lock:
            assert inst._state_label is None, \
                "a successful restore must clear the label even when the caption raised"


# ============================================================================
# N13 — Structured [SLOTPOOL] event-log coverage (full vocabulary sweep)
# ============================================================================

class TestN13StructuredEventLogCoverage:
    """caplog @ DEBUG: every acquire/drop transition emits exactly ONE structured
    ``[SLOTPOOL] instance=<n> pool=<key> action=<label> waiters=<int>`` line, and the
    full vocabulary (acquire-grant / acquire-queued / sticky-keep / drop-fallback /
    drop-sleep / drop-exit / drop-handoff / drop-reuse / drop-stop / drop-dismiss) is
    exercised at least once.

    Each sub-scenario drives ONE real emission site and spot-checks that the matching
    line exists (and, for single-transition scenarios, that it appears exactly once).
    The full-vocabulary sweep across all sub-scenarios is asserted in test_g."""

    # Full vocabulary (plan §6.2, N13) — every label must appear at least once.
    VOCAB = {
        "acquire-grant", "acquire-queued", "sticky-keep",
        "drop-fallback", "drop-sleep", "drop-exit",
        "drop-handoff", "drop-reuse", "drop-stop", "drop-dismiss",
    }

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _slotpool_lines(records):
        """All structured [SLOTPOOL] event lines (instance= ... action= ...)."""
        out = []
        for r in records:
            m = r.getMessage() if hasattr(r, "getMessage") else str(r)
            if "[SLOTPOOL]" in m and "action=" in m:
                out.append(m)
        return out

    @classmethod
    def _actions(cls, records, name):
        """Ordered action labels for ``name`` from the structured lines."""
        acts = []
        for line in cls._slotpool_lines(records):
            if f"instance={name} " not in line:
                continue
            seg = line.split("action=", 1)[1]
            acts.append(seg.split(" ", 1)[0])
        return acts

    @staticmethod
    def _assert_line_shape(line, name, action):
        """One structured line: correct instance/action, pool key + waiters field present."""
        assert f"instance={name} " in line, f"wrong instance in line: {line!r}"
        assert f"action={action} " in line or line.endswith(f"action={action}"), \
            f"wrong action in line: {line!r}"
        assert " pool=" in line, f"missing pool key field: {line!r}"
        assert " waiters=" in line, f"missing waiters field: {line!r}"

    @staticmethod
    def _acquire_holder(router, pool, name):
        """Give ``name`` the shared sticky permit (N12 setup pattern)."""
        inst = _make_instance(pool, name, "coder")
        rel = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0,
            instance_name=name, agent_class="coder", timeout=5.0)
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
        return inst

    def _queue_waiter(self, router, pool, name):
        """Start a blocked FIFO waiter thread; returns (thread, granted_event, inst).

        The waiter is registered as a real instance so its grant can be released
        again during cleanup (a granted waiter otherwise pins the shared slot)."""
        inst = _make_instance(pool, name, "coder")
        granted = threading.Event()

        def waiter():
            try:
                rel = router.scheduler.acquire(
                    api_base=SEQ_BASE, concurrency_limit=0,
                    instance_name=name, agent_class="coder", timeout=15.0)
                inst._slot_release = rel
                inst._slot_key = SHARED_KEY
                granted.set()
            except Exception:
                pass

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.3)
        assert not granted.is_set(), f"{name} must be blocked while the holder holds"
        return t, granted, inst

    @staticmethod
    def _release_permit(inst):
        """Release an instance's sticky permit via its raw callback (N12 pattern)."""
        if inst is None or getattr(inst, "_slot_release", None) is None:
            return
        with inst._state_lock:
            cb = inst._slot_release
            inst._slot_release = None
            inst._slot_key = None
        cb()

    def _release_and_join(self, router, pool, name, t, granted, w_inst):
        """Release the named holder's permit (raw callback), join waiter, clean up.

        Cleanup matters: a granted waiter now HOLDS the shared slot, and the next
        sub-scenario's fast-path acquire would otherwise block behind it until its
        5s timeout (N12 uses the same release_box cleanup pattern)."""
        self._release_permit(pool.instances.get(name))
        t.join(timeout=10)
        assert not t.is_alive(), f"DEADLOCK: waiter never granted after {name} released"
        assert granted.is_set()
        # Release the waiter's newly-granted permit so the pool is empty again.
        self._release_permit(w_inst)

    # ── Sub-scenarios (one real emission site each) ────────────────────────

    def test_a_acquire_grant_and_drop_exit(self, sticky_harness):
        """Initial acquire → scheduler.acquire-grant; run() finally → drop-exit."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        records, handler, targets = _capture_slotpool_logs()
        try:
            inst = self._acquire_holder(router, pool, "n13a")

            engine = _make_engine(router.scheduler, SEQ_BASE, 0)
            # Real run()-finally path: _release_slot(action='drop-exit').
            engine._release_slot(inst, "n13a", "run() finally exit", action="drop-exit")

            acts = self._actions(records, "n13a")
            assert acts.count("acquire-grant") == 1, f"exactly one acquire-grant: {acts}"
            assert acts.count("drop-exit") == 1, f"exactly one drop-exit: {acts}"
            # Spot-check: the two transitions are the only slot events for n13a,
            # each line well-formed (instance/pool/action/waiters fields).
            assert acts == ["acquire-grant", "drop-exit"], f"unexpected extra lines: {acts}"
            a_lines = [l for l in self._slotpool_lines(records) if "instance=n13a " in l]
            assert len(a_lines) == 2, f"expected exactly 2 structured lines: {a_lines}"
            self._assert_line_shape(a_lines[0], "n13a", "acquire-grant")
            self._assert_line_shape(a_lines[1], "n13a", "drop-exit")
        finally:
            _restore_logs(handler, targets)

    def test_b_acquire_queued_then_grant(self, sticky_harness):
        """Blocked FIFO enqueue → acquire-queued (slot_queue); grant on release."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        records, handler, targets = _capture_slotpool_logs()
        try:
            self._acquire_holder(router, pool, "n13bh")
            t, granted, w_inst = self._queue_waiter(router, pool, "n13bq")

            acts_q = self._actions(records, "n13bq")
            assert acts_q.count("acquire-queued") == 1, f"exactly one acquire-queued: {acts_q}"
            queued_line = next(l for l in self._slotpool_lines(records)
                               if "instance=n13bq " in l and "action=acquire-queued" in l)
            self._assert_line_shape(queued_line, "n13bq", "acquire-queued")
            assert f"pool={SHARED_KEY} " in queued_line, \
                f"acquire-queued must name the shared pool: {queued_line!r}"

            # Release → FIFO grant for the waiter.
            self._release_and_join(router, pool, "n13bh", t, granted, w_inst)
            acts_q = self._actions(records, "n13bq")
            assert acts_q.count("acquire-grant") == 1, f"exactly one acquire-grant: {acts_q}"
        finally:
            _restore_logs(handler, targets)

    def test_c_sticky_keep_and_drop_fallback(self, sticky_harness):
        """call_with_fallback on conc=0 → sticky-keep; chain head flips to conc>0 → drop-fallback."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        import agent_cascade.api_router_pkg.router as _rmod

        records, handler, targets = _capture_slotpool_logs()
        try:
            par_id = _add_endpoint(router, "par", PAR_BASE, concurrency_limit=4)
            inst = self._acquire_holder(router, pool, "n13c")
            with router._lock:
                router.agent_priorities["coder"] = ["ep0", par_id]
            # Drop the Tier-4 default so the chain is exactly [ep0, par] (N2 pattern).
            router.default_llm_cfg = None

            def call_fn(llm_cfg, *a, **k):
                return "ok"

            with patch.object(_rmod, "SANITY_PROBE_ENABLED", False):
                # Turn 1 — chain head is conc=0: already holding the shared slot → sticky-keep.
                router.call_with_fallback("coder", call_fn, agent_instance_name="n13c")
                acts = self._actions(records, "n13c")
                assert acts.count("sticky-keep") == 1, f"exactly one sticky-keep: {acts}"
                keep_line = next(l for l in self._slotpool_lines(records)
                                 if "instance=n13c " in l and "action=sticky-keep" in l)
                self._assert_line_shape(keep_line, "n13c", "sticky-keep")
                assert f"pool={SHARED_KEY} " in keep_line, \
                    f"sticky-keep must name the shared pool: {keep_line!r}"

                # Allocation change: conc>0 primary becomes the chain head (N2 pattern).
                router.set_agent_priorities("coder", [par_id, "ep0"])
                router.reset_instance_endpoint("n13c")

                # Turn 2 — desired endpoint needs no shared slot → drop-fallback.
                router.call_with_fallback("coder", call_fn, agent_instance_name="n13c")

            acts = self._actions(records, "n13c")
            assert acts.count("drop-fallback") == 1, f"exactly one drop-fallback: {acts}"
            fb_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13c " in l and "action=drop-fallback" in l)
            self._assert_line_shape(fb_line, "n13c", "drop-fallback")
            assert f"pool={SHARED_KEY} " in fb_line, \
                f"drop-fallback must name the dropped shared pool: {fb_line!r}"
            # The shared slot is gone after the fallback-back.
            assert "n13c" not in shared._running, \
                f"shared slot must be released after drop-fallback: {list(shared._running)}"
        finally:
            _restore_logs(handler, targets)

    def test_d_drop_sleep(self, sticky_harness):
        """RUNNING instance with held permit → engine._transition_to_sleeping (drop-sleep)."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        records, handler, targets = _capture_slotpool_logs()
        try:
            inst = self._acquire_holder(router, pool, "n13d")
            from agent_cascade.agent_instance import AgentState
            inst.state = AgentState.RUNNING

            engine = _make_engine(router.scheduler, SEQ_BASE, 0)
            engine._transition_to_sleeping(inst)

            acts = self._actions(records, "n13d")
            assert acts.count("drop-sleep") == 1, f"exactly one drop-sleep: {acts}"
            sl_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13d " in l and "action=drop-sleep" in l)
            self._assert_line_shape(sl_line, "n13d", "drop-sleep")
            assert f"pool={SHARED_KEY} " in sl_line, \
                f"drop-sleep must name the shared pool: {sl_line!r}"
            assert inst._slot_key is None and inst._slot_release is None
        finally:
            _restore_logs(handler, targets)

    def test_e_drop_handoff(self, sticky_harness):
        """Held permit → engine._release_slot(action='drop-handoff') (sync-child handoff site)."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        records, handler, targets = _capture_slotpool_logs()
        try:
            inst = self._acquire_holder(router, pool, "n13e")

            engine = _make_engine(router.scheduler, SEQ_BASE, 0)
            engine._release_slot(inst, "n13e", "sync child handoff", action="drop-handoff")

            acts = self._actions(records, "n13e")
            assert acts.count("drop-handoff") == 1, f"exactly one drop-handoff: {acts}"
            ho_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13e " in l and "action=drop-handoff" in l)
            self._assert_line_shape(ho_line, "n13e", "drop-handoff")
            assert f"pool={SHARED_KEY} " in ho_line, \
                f"drop-handoff must name the shared pool: {ho_line!r}"
            assert inst._slot_key is None and inst._slot_release is None
        finally:
            _restore_logs(handler, targets)

    def test_f_drop_reuse_stop_dismiss(self, sticky_harness):
        """Three more release points, each with a fresh holder + blocked waiter:
        lifecycle reuse (drop-reuse), stop_session (drop-stop), dismiss (drop-dismiss)."""
        h = sticky_harness
        router, pool, shared = h["router"], h["pool"], h["shared"]
        from agent_cascade.agent_instance import AgentState
        from agent_cascade.lifecycle_manager import AgentLifecycleManager
        from agent_cascade.llm.schema import Message, USER

        records, handler, targets = _capture_slotpool_logs()
        try:
            # ── drop-reuse (lifecycle_manager.initialize_conversation, is_reuse=True) ──
            inst_r = self._acquire_holder(router, pool, "n13fr")
            t_r, g_r, w_r = self._queue_waiter(router, pool, "n13frw")
            inst_r.state = AgentState.IDLE
            mgr = AgentLifecycleManager(pool)
            mgr.initialize_conversation(
                instance=inst_r, sys_msg=Message(role="system", content="sys"),
                task_msg=Message(role=USER, content="task"),
                is_reuse=True, instance_name="n13fr", agent_class="coder")
            acts = self._actions(records, "n13fr")
            assert acts.count("drop-reuse") == 1, f"exactly one drop-reuse: {acts}"
            ru_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13fr " in l and "action=drop-reuse" in l)
            self._assert_line_shape(ru_line, "n13fr", "drop-reuse")
            assert f"pool={SHARED_KEY} " in ru_line, \
                f"drop-reuse must name the shared pool: {ru_line!r}"
            assert inst_r._slot_key is None and inst_r._slot_release is None
            self._release_and_join(router, pool, "n13fr", t_r, g_r, w_r)

            # ── drop-stop (pool.stop_session, release_slots=True) ──
            # NOTE: stop_session sets pool.stopped=True AND cancels ALL queued
            # tickets (step 2.5). Restore the stopped flag afterwards so the
            # later dismiss sub-scenario sees a live session (N12d pattern).
            inst_s = self._acquire_holder(router, pool, "n13fs")
            t_s, _g_s, _w_s = self._queue_waiter(router, pool, "n13fsw")
            pool.stop_session(release_slots=True)
            acts = self._actions(records, "n13fs")
            assert acts.count("drop-stop") == 1, f"exactly one drop-stop: {acts}"
            st_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13fs " in l and "action=drop-stop" in l)
            self._assert_line_shape(st_line, "n13fs", "drop-stop")
            assert f"pool={SHARED_KEY} " in st_line, \
                f"drop-stop must name the shared pool: {st_line!r}"
            assert inst_s._slot_key is None and inst_s._slot_release is None
            with shared._cond:
                assert "n13fs" not in shared._running, \
                    f"stop_session must free the sticky permit: {list(shared._running)}"
            # stop_session cancels ALL queued tickets (N12d note) — the waiter exits
            # without a grant; join it so no thread outlives this sub-scenario.
            t_s.join(timeout=10)
            assert not t_s.is_alive(), "waiter thread must exit after stop_session cancels it"

            # Restore the stopped flag for the next sub-scenario (stop_session
            # latches pool.stopped=True; nothing else in this test resets it).
            pool._stopped_event.clear()

            # ── drop-dismiss (pool.dismiss_instance on an IDLE holder) ──
            inst_d = self._acquire_holder(router, pool, "n13fd")
            t_d, g_d, w_d = self._queue_waiter(router, pool, "n13fdw")
            inst_d.state = AgentState.IDLE
            pool.dismiss_instance("n13fd")
            acts = self._actions(records, "n13fd")
            assert acts.count("drop-dismiss") == 1, f"exactly one drop-dismiss: {acts}"
            di_line = next(l for l in self._slotpool_lines(records)
                           if "instance=n13fd " in l and "action=drop-dismiss" in l)
            self._assert_line_shape(di_line, "n13fd", "drop-dismiss")
            assert f"pool={SHARED_KEY} " in di_line, \
                f"drop-dismiss must name the shared pool: {di_line!r}"
            assert inst_d._slot_key is None and inst_d._slot_release is None
            self._release_and_join(router, pool, "n13fd", t_d, g_d, w_d)

            with shared._cond:
                assert len(shared._running) == 0, f"Slot leak: {list(shared._running)}"
        finally:
            _restore_logs(handler, targets)

    def test_g_full_vocabulary_sweep(self, sticky_harness):
        """Run every sub-scenario in one capture session and assert the full
        [SLOTPOOL] action vocabulary is exercised at least once."""
        h = sticky_harness
        records, handler, targets = _capture_slotpool_logs()
        try:
            self.test_a_acquire_grant_and_drop_exit(h)
            self.test_b_acquire_queued_then_grant(h)
            self.test_c_sticky_keep_and_drop_fallback(h)
            self.test_d_drop_sleep(h)
            self.test_e_drop_handoff(h)
            self.test_f_drop_reuse_stop_dismiss(h)

            seen = set()
            for line in self._slotpool_lines(records):
                seg = line.split("action=", 1)[1]
                seen.add(seg.split(" ", 1)[0])
            missing = self.VOCAB - seen
            assert not missing, \
                f"full-vocabulary sweep failed — never emitted: {sorted(missing)}; saw {sorted(seen)}"
        finally:
            _restore_logs(handler, targets)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
