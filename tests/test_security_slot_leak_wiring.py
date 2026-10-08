"""Integration tests for the security slot-permit leak fix (slot_leak_fix_PLAN.md).

Fix 1 (Commit A — the actual fix): on the dead-man's-switch (inactivity-timeout) /
abandon ``break`` path, ``SecurityAdvisorHandler._execute_check``'s ``finally`` must
deterministically release the Security instance's slot permit so the shared ``conc=0``
pool (``_shared_sequential_slot_``) is not pinned forever. The load-bearing assertion is
the END-TO-END break-path test (``test_execute_check_timeout_break_releases_permit``):
it drives the real handler through the timeout ``break`` and asserts the pool no longer
holds the holder. A direct ``_discard_stale_permit`` unit test (below) is only a sanity
check — it proves nothing about the wiring, which is why the integration test is the
primary one (plan R4).

Fix 3 (Commit B — insurance ONLY): the reuse Gate 4 must clear a provably-stale permit
instead of rejecting, for the ``IDLE`` state it guards. The primary leak leaves
``state=RUNNING``, so Gate 3 rejects before Gate 4 is reached — the test
``test_fix3_running_leak_still_gate3_rejected`` documents that honest outcome (Fix 3 does NOT
fix the reported symptom).

Harness: real ``APIRouter`` + ``conc=0`` endpoint → real shared ``_shared_sequential_slot_``
pool + real ``AgentPool``. Only the LLM (``engine.run``) is faked. No network, no server.
"""
import os as _os

_os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"seclwiring_{_os.getpid()}")

import threading
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.security_handler import SecurityAdvisorHandler

SHARED_KEY = '_shared_sequential_slot_'
SEQ_BASE = 'http://127.0.0.1:9/v1'


# ── Real slot-pool harness (copied from tests/e2e_security_slot_deadlock.py) ──

def _build_real_router(cfg_dir):
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {'model': 'mock', 'api_base': SEQ_BASE,
               'model_server': SEQ_BASE, 'api_key': 'EMPTY'}
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(cfg_dir))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id='ep0', name='conc0', api_base=SEQ_BASE, model='mock',
                     concurrency_limit=0, enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return router


def _build_pool(router):
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {'model': 'mock', 'api_base': SEQ_BASE,
               'model_server': SEQ_BASE, 'api_key': 'EMPTY'}
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


@pytest.fixture
def wiring_harness(tmp_path, request):
    """Real router (conc=0) + real pool + real handler; short timeouts."""
    import agent_cascade.api_router_pkg.scheduler as _ar_mod
    import agent_cascade.slot_queue as _sq_mod

    cfg_dir = tmp_path / request.node.name.replace('/', '_')
    cfg_dir.mkdir(parents=True, exist_ok=True)
    old_cfg_dir = _os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(cfg_dir)

    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)
    router._pool = pool
    old_slot_window = getattr(pool.settings, 'slot_queue_timeout_seconds', None)
    pool.settings.slot_queue_timeout_seconds = 5

    # The real AgentPool has operation_manager=None — give it a minimal one so
    # _execute_check's prompt-building + timeout handling don't crash on None.
    om = MagicMock()
    om.base_dir = str(cfg_dir)
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    om.enable_timeout = True
    om.approval_timeout_seconds = 180
    pool.operation_manager = om

    if getattr(pool, '_execution', None) is None:
        pool._execution = MagicMock()
    pool._execution._state_lock = threading.Lock()
    pool.instance_state = {}

    shared = router.scheduler._get_or_create_pool(SEQ_BASE, 0)
    assert shared is not None and shared.key == SHARED_KEY, \
        f"Shared sequential SlotPool was not created: {shared!r}"

    app = type('App', (), {})()
    session = {'session_name': 'Maine', 'generate_cfg': {}}
    handler = SecurityAdvisorHandler(pool, session, app, MagicMock(), lambda *a, **k: None)

    yield {'router': router, 'pool': pool, 'shared': shared,
           'app': app, 'session': session, 'handler': handler}

    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    if old_slot_window is not None:
        pool.settings.slot_queue_timeout_seconds = old_slot_window
    if old_cfg_dir is None:
        _os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
    else:
        _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = old_cfg_dir


def _make_sec_instance(sec_name):
    """Lightweight stand-in for engine._create_system_agent's Security instance.

    Real attribute storage (so _discard_stale_permit / the yield code can read/write
    _slot_release/_slot_key under a real _state_lock) + a dummy conversation.
    """
    inst = MagicMock(name=sec_name)
    inst.instance_name = sec_name
    inst.agent_class = 'Security'
    inst._state_lock = threading.RLock()
    inst.conversation = [{'role': 'assistant', 'content': 'I will analyze this request.'}]
    return inst


def _run_execute_check(handler, ap, rid, caller_agent, run_side_effect):
    """Run the REAL _execute_check with the ExecutionEngine class patched.

    ``run_side_effect`` is a callable ``inst -> generator`` installed on
    ``engine.run.side_effect`` — it is what lets us control the slot/permit lifecycle.
    """
    engine_instance = MagicMock()
    engine_instance.run.side_effect = run_side_effect
    engine_instance._create_system_agent.side_effect = lambda **kw: _make_sec_instance(
        kw.get('instance_name', 'Security'))
    engine_instance._telemetry.return_value = None
    engine_instance.reacquire_after_slot_yield.return_value = True

    mock_engine_cls = MagicMock(return_value=engine_instance)
    # The handler calls the CLASS-level staticmethod ExecutionEngine._discard_stale_permit in
    # its finally. Patching the whole class would make that a no-op MagicMock and hide the very
    # wiring under test — so delegate the staticmethod to the REAL one (class access returns the
    # underlying function for a @staticmethod).
    from agent_cascade.engine.core import ExecutionEngine as _RealEE
    mock_engine_cls._discard_stale_permit = _RealEE._discard_stale_permit
    with patch('agent_cascade.security_handler.SECURITY_LOCK_ACQUIRE_TIMEOUT_SECONDS', 5):
        with patch('agent_cascade.execution_engine.ExecutionEngine', mock_engine_cls):
            handler._execute_check(
                ap=ap,
                sec_inst=None,
                rid=rid,
                auto_apply=False,
                instance_name='Maine',
                caller_agent=caller_agent,
                prompt_template='Analyze {tool_name}: {description} args={arguments}',
                timeout_seconds=3600,
                warning_seconds=2400,
            )
    return engine_instance


def _make_security_ap(rid):
    """Build the approval payload (``ap``) that ``_execute_check`` consumes."""
    return {
        'request_id': rid,
        'tool_name': 'shell_cmd',
        'description': 'echo hi',
        'tool_args': {'command': 'echo hi'},
        'agent_name': 'caller',
    }


# ── Fix 1 — PRIMARY: end-to-end dead-man's-switch break path ─────────────────

def test_execute_check_timeout_break_releases_permit(wiring_harness):
    """Drive the REAL dead-man's-switch (mid-stream-silence) ``break`` path end-to-end.

    The fake ``engine.run`` acquires the shared conc=0 permit (as the real run() does at
    core.py:1042), yields one token (which establishes the dead-man's-switch baseline —
    the first yield SKIPS the fail-fast check), goes silent past the (patched) 0.1s
    inactivity window, then yields again WITHOUT a finally that releases (so ``close()``
    in the handler's finally cannot free the permit). The handler sees the window elapsed
    on the second yield and ``break``s. Only the NEW explicit ``_discard_stale_permit`` in
    the finally can then clear the pool. Pre-fix (no close / no discard) the pool stays
    pinned → this fails.
    """
    h = wiring_harness
    pool, shared, handler = h['pool'], h['shared'], h['handler']
    rid = f'sec_{uuid.uuid4().hex[:8]}'
    sec_name = f'Security_{rid}'

    def _stuck_run(inst):
        rel = pool._acquire_slot('Security', inst.instance_name)
        assert rel is not None, 'conc=0 pool should grant the Security permit'
        with inst._state_lock:
            inst._slot_release = rel
            inst._slot_key = SHARED_KEY
        # First yield establishes the dead-man's-switch baseline (skips fail-fast).
        yield ('[partial] still thinking', False)
        # Go silent past the (patched) 0.1s inactivity window, then yield again —
        # the second yield trips the fail-fast break in the handler.
        time.sleep(0.3)
        yield ('[partial] still stuck', False)
        # Abandoned while stuck: NO finally that releases the permit.
        threading.Event().wait(3600)

    with patch('agent_cascade.security_handler.SECURITY_CHECK_INACTIVITY_WINDOW_SECONDS', 0.1):
        _run_execute_check(handler, _make_security_ap(rid), rid, 'Maine', _stuck_run)

    # THE load-bearing assertion: the shared conc=0 pool is no longer pinned by the holder.
    assert sec_name not in shared._running, (
        f"Fix 1 wiring broken: pool still pinned by abandoned Security holder "
        f"{list(shared._running)}")
    # The instance's permit bookkeeping is cleared.
    # (A fresh waiter can now be granted — the pool is free.)
    waiter_rel = pool._acquire_slot('coder', 'waiter_unblock')
    assert waiter_rel is not None, 'a queued waiter must be grantable after the fix'
    waiter_rel()


def test_execute_check_normal_exit_is_safe_noop(wiring_harness):
    """Control: the normal-completion path (generator releases in its own exit-finally, then
    exhausts) must still leave the pool clear, and the added ``close()`` + ``_discard_stale_permit``
    must be safe no-ops (idempotent, no double-release crash)."""
    h = wiring_harness
    pool, shared, handler = h['pool'], h['shared'], h['handler']
    rid = f'sec_{uuid.uuid4().hex[:8]}'
    sec_name = f'Security_{rid}'

    def _normal_run(inst):
        rel = pool._acquire_slot('Security', inst.instance_name)
        assert rel is not None
        with inst._state_lock:
            inst._slot_release = rel
            inst._slot_key = SHARED_KEY
        try:
            yield ('[YES] this is safe', False)
        finally:
            # Mimic run()'s exit-finally releasing the permit on normal completion.
            with inst._state_lock:
                cb = inst._slot_release
                inst._slot_release = None
                inst._slot_key = None
            if cb is not None:
                cb()

    # No timeout constant to patch: a single yield establishes the dead-man's-switch
    # baseline and the generator completes on the same tick — the window never trips.
    _run_execute_check(handler, _make_security_ap(rid), rid, 'Maine', _normal_run)

    assert sec_name not in shared._running, (
        f"normal-exit path should leave the pool clear: {list(shared._running)}")


# ── Fix 1 — helper sanity check (low value; the wiring test above is the proof) ──

def test_discard_stale_permit_releases_pool_holder(wiring_harness):
    """Sanity: _discard_stale_permit on a real conc=0 holder removes it from pool._running
    and lets a queued waiter be granted. (Direct helper call — see the integration test
    above for the actual wiring proof.)"""
    from agent_cascade.execution_engine import ExecutionEngine

    h = wiring_harness
    pool, shared, handler = h['pool'], h['shared'], h['handler']
    rid = f'sec_{uuid.uuid4().hex[:8]}'
    inst = _make_sec_instance(rid)

    rel = pool._acquire_slot('Security', rid)
    assert rel is not None
    with inst._state_lock:
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
    assert rid in shared._running, f"precondition: holder present: {list(shared._running)}"

    cleared = ExecutionEngine._discard_stale_permit(
        inst, rid, context='test', action='drop-exit')
    assert cleared is True, 'a live permit should have been found and released'
    assert rid not in shared._running, f"holder not removed: {list(shared._running)}"
    assert inst._slot_release is None and inst._slot_key is None

    # Idempotent: a second call is a no-op.
    assert ExecutionEngine._discard_stale_permit(inst, rid, context='test', action='drop-exit') is False

    # A queued waiter can now be granted.
    waiter_rel = pool._acquire_slot('coder', 'waiter2')
    assert waiter_rel is not None
    waiter_rel()


# ── Fix 3 — insurance only (Gate-4 stale-permit clear) ─────────────────────────

def _real_reuse_instance(name, state):
    """A real AgentInstance registered in the pool so the reuse gates read real state."""
    from agent_cascade.agent_instance import AgentInstance
    inst = AgentInstance(
        instance_name=name, agent_class='Security', conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    inst.state = state
    return inst


def test_fix3_running_leak_still_gate3_rejected(wiring_harness):
    """HONEST OUTCOME: the primary leak leaves state=RUNNING, so Gate 3 (state_not_idle)
    rejects BEFORE Gate 4 is reached. Fix 3 does NOT clear the permit for the reported
    case and does NOT make the instance reusable — it is insurance only (plan R1)."""
    from agent_cascade.agent_instance import AgentState
    from agent_cascade.execution_engine import ExecutionEngine

    h = wiring_harness
    pool, shared = h['pool'], h['shared']
    name = f'sec3_{uuid.uuid4().hex[:8]}'
    inst = _real_reuse_instance(name, AgentState.RUNNING)
    pool.instances[name] = inst

    rel = pool._acquire_slot('Security', name)
    assert rel is not None
    with inst._state_lock:
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
    assert name in shared._running

    engine = ExecutionEngine(pool)
    try:
        result = engine._acquire_reusable_system_agent(
            agent_class='Security', instance_name=name, task='t', caller='Maine',
            rid=f'r_{uuid.uuid4().hex[:8]}')

        # Rejected by Gate 3 (state=RUNNING), NOT cleared by Gate 4.
        assert result is None, (
            f"a RUNNING instance must stay rejected (Gate 3); got {result!r}")
        # The permit is NOT cleared — documents that Fix 3 does not fix the primary symptom.
        assert inst._slot_release is not None, (
            'Fix 3 must NOT clear the permit for a RUNNING leak (Gate 3 rejects first)')
    finally:
        # Cleanup in a finally so a failing assertion mid-test does not leak the permit.
        with inst._state_lock:
            cb = inst._slot_release
            inst._slot_release = None
            inst._slot_key = None
        if cb is not None:
            cb()


def test_fix3_idle_stale_permit_cleared_at_gate4(wiring_harness):
    """IF the IDLE+stale-permit state is reached, Gate 4 must CLEAR the permit (not reject)
    so the warm instance can be reused. Pre-fix Gate 4 returned None leaving the permit
    set; post-fix the permit is cleared and the pool entry removed."""
    from agent_cascade.agent_instance import AgentState
    from agent_cascade.execution_engine import ExecutionEngine

    h = wiring_harness
    pool, shared = h['pool'], h['shared']
    name = f'sec3_{uuid.uuid4().hex[:8]}'
    inst = _real_reuse_instance(name, AgentState.IDLE)
    pool.instances[name] = inst

    rel = pool._acquire_slot('Security', name)
    assert rel is not None
    with inst._state_lock:
        inst._slot_release = rel
        inst._slot_key = SHARED_KEY
    assert name in shared._running

    engine = ExecutionEngine(pool)
    engine._acquire_reusable_system_agent(
        agent_class='Security', instance_name=name, task='t', caller='Maine',
        rid=f'r_{uuid.uuid4().hex[:8]}')

    # Gate 4 cleared the stale permit (regardless of any later gate's outcome).
    assert inst._slot_release is None, (
        'Gate 4 should have cleared the stale permit; it is still set')
    assert name not in shared._running, (
        f"the cleared permit's pool entry must be removed: {list(shared._running)}")
