"""Regression tests for the slot-permit leak fixes (plan §5, V1–V9b).

Every test that claims "fails pre-fix" was demonstrated red against the
un-fixed tree before the fix landed — see the per-test docstrings for the
assertion that fails. The harness pattern (real router + real conc=0 pool)
is copied from tests/test_edge_cases_sticky_slot.py so these tests exercise
the production SlotPool / EndpointScheduler / APIRouter with no LLM or
network.

Mapping to the plan verification matrix:
  V1  test_run_entry_stale_permit_is_released            (LEAK #1)
  V2  test_reacquire_over_does_not_orphan_prior_permit[same_pool]   (LEAK #2)
  V3  test_reacquire_over_does_not_orphan_prior_permit[cross_pool]  (LEAK #2)
  V4  test_reacquire_for_unlimited_releases_stale[×3]     (LEAK #2b)
  V5  test_terminate_releases_held_permit                (LEAK #3 / BUG_0034)
  V6  test_double_acquire_is_detected[grant]             (LEAK #4a)
  V7  test_double_acquire_is_detected[stale_release]     (LEAK #4b)
  V8  test_unified_runner_closes_generator_on_stop       (LEAK #5)
  V9  test_release_slot_permit_strict_reraises          (hardening)
  V9b test_drop_held_permit_retryable_on_callback_failure (LEAK #6)

Runtime budget: < 30s for the whole file (real short waits only, no sleeps
for correctness — Events/Barriers everywhere, per house style).
"""
import os as _os

_os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"slotleak_{_os.getpid()}")

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

SHARED_KEY = '_shared_sequential_slot_'
SEQ_BASE = 'http://127.0.0.1:9/v1'      # conc=0 endpoint (shared sequential slot)
PAR_BASE = 'http://127.0.0.1:10/v1'     # conc>0 endpoint (per-base pool)


# ── Real slot-pool harness (copied from test_edge_cases_sticky_slot.py) ─────

def _build_real_router(cfg_dir):
    """Real APIRouter with a single conc=0 endpoint → real shared sequential SlotPool."""
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {
        'model': 'mock',
        'api_base': SEQ_BASE,
        'model_server': SEQ_BASE,
        'api_key': 'EMPTY',
    }
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(cfg_dir))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep = APIEndpoint(id='ep0', name='conc0', api_base=SEQ_BASE,
                     model='mock', concurrency_limit=0, enabled=True)
    router.add_endpoint(ep)
    router.default_llm_cfg = ep.to_llm_cfg()
    return router


def _add_endpoint(router, name, api_base, model='mock', concurrency_limit=-1, **kwargs):
    from agent_cascade.api_router import APIEndpoint
    ep = APIEndpoint(id=f"ep_{name}", name=name, api_base=api_base, model=model,
                     enabled=True, concurrency_limit=concurrency_limit, **kwargs)
    return router.add_endpoint(ep)


def _build_pool(router):
    from agent_cascade.agent_pool import AgentPool

    llm_cfg = {'model': 'mock', 'api_base': SEQ_BASE,
               'model_server': SEQ_BASE, 'api_key': 'EMPTY'}
    return AgentPool(llm_cfg, agents_dir=str(router._config_dir), api_router=router)


def _make_instance(pool, name, agent_class='coder'):
    """Real AgentInstance registered in the pool (so get_instance() finds it)."""
    from agent_cascade.agent_instance import AgentInstance
    inst = AgentInstance(
        instance_name=name, agent_class=agent_class, conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    # Mirror create_instance (pool/lifecycle.py:90): the pool back-reference is what lets
    # release_slot_permit clear the committed-endpoint probe marker on any canonical release.
    inst._pool_ref = pool
    pool.instances[name] = inst
    return inst


def _make_engine(sched, api_base, conc, slot_key=SHARED_KEY):
    """Real ExecutionEngine backed by a mock pool whose router resolves to the given slot."""
    from agent_cascade.execution_engine import ExecutionEngine

    slot_info = {
        'slot_key': slot_key if conc == 0 else api_base,
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
def leak_harness(tmp_path, request):
    """Real router (conc=0 endpoint) + real pool; short QUEUE_WAIT_TIMEOUT.

    Each test gets its OWN config dir (derived from the node id) so pytest-xdist's
    parallel workers don't overwrite each other's api_endpoints.json.
    """
    import agent_cascade.api_router_pkg.router as _rmod
    import agent_cascade.api_router_pkg.scheduler as _ar_mod
    import agent_cascade.engine.core as _core_mod
    import agent_cascade.slot_queue as _sq_mod

    cfg_dir = tmp_path / request.node.name.replace('/', '_')
    cfg_dir.mkdir(parents=True, exist_ok=True)
    old_cfg_dir = _os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = str(cfg_dir)

    old_sq = _sq_mod.QUEUE_WAIT_TIMEOUT
    old_ar = _ar_mod.QUEUE_WAIT_TIMEOUT
    old_cool = _rmod.ENDPOINT_COOLDOWN_SECONDS
    old_reacq = _core_mod.REACQUIRE_TIMEOUT
    _sq_mod.QUEUE_WAIT_TIMEOUT = 5
    _ar_mod.QUEUE_WAIT_TIMEOUT = 5
    _rmod.ENDPOINT_COOLDOWN_SECONDS = 0
    _core_mod.REACQUIRE_TIMEOUT = 0.3

    router = _build_real_router(cfg_dir)
    pool = _build_pool(router)
    router._pool = pool
    # The dead-man's switch resolves its window from the LIVE pool setting FIRST, so the
    # module-constant patches above are a no-op for this real pool — set the setting to
    # match so the 5s bounded window is honored (plan §2.5). Restore in teardown.
    old_slot_window = getattr(pool.settings, 'slot_queue_timeout_seconds', None)
    pool.settings.slot_queue_timeout_seconds = 5

    shared = router.scheduler._get_or_create_pool(SEQ_BASE, 0)
    assert shared is not None and shared.key == SHARED_KEY, \
        f"Shared sequential SlotPool was not created: {shared!r}"

    yield {'router': router, 'pool': pool, 'shared': shared}

    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    _rmod.ENDPOINT_COOLDOWN_SECONDS = old_cool
    _core_mod.REACQUIRE_TIMEOUT = old_reacq
    if old_slot_window is not None:
        pool.settings.slot_queue_timeout_seconds = old_slot_window
    if old_cfg_dir is None:
        _os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
    else:
        _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = old_cfg_dir


# ── Shared helpers ───────────────────────────────────────────────────────────

def _acquire_shared(router, name, timeout=5.0):
    """Acquire the shared conc=0 slot for `name`; returns the release callback.

    Wires the holder-context resolver (plan §3.3 option a) exactly as the real
    production path does: the AgentPool is threaded through so the FIFO head-stall
    alarm can resolve the holder's live activity for its streaming-suppression check.
    """
    return router.scheduler.acquire(
        api_base=SEQ_BASE, concurrency_limit=0,
        instance_name=name, agent_class='coder', timeout=timeout,
        pool=router._pool)


def _hold(inst, rel, key=SHARED_KEY):
    """Stash a freshly acquired permit on the instance under its state lock."""
    with inst._state_lock:
        inst._slot_release = rel
        inst._slot_key = key


def _release_permit(inst):
    """Release an instance's sticky permit via its raw callback (idempotent)."""
    if inst is None or getattr(inst, '_slot_release', None) is None:
        return
    with inst._state_lock:
        cb = inst._slot_release
        inst._slot_release = None
        inst._slot_key = None
    cb()


def _queue_waiter(router, name, timeout=15.0, instance_resolver=None):
    """Start a blocked FIFO waiter thread on the shared slot; returns (thread, granted_event).

    Threads the AgentPool through for holder-context resolution (plan §3.3 option a),
    exactly as the production path does. An explicit ``instance_resolver`` (used by V22
    to spy on lock ownership) always wins over the pool fallback — SlotPool.acquire
    only falls back to ``pool.get_instance`` when no resolver was passed.
    """
    granted = threading.Event()
    queued = threading.Event()

    def waiter():
        try:
            queued.set()  # we are about to block in the FIFO queue (holder holds the slot)
            router.scheduler.acquire(
                api_base=SEQ_BASE, concurrency_limit=0,
                instance_name=name, agent_class='coder', timeout=timeout,
                pool=router._pool, instance_resolver=instance_resolver)
            granted.set()
        except Exception:
            pass

    t = threading.Thread(target=waiter)
    t.start()
    assert queued.wait(timeout=5), f"{name} never reached the FIFO queue"
    assert not granted.is_set(), f"{name} must be blocked while the holder holds"
    return t, granted


def _capture_logs():
    """Capture records from the app logger + package logger into a list."""
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
    for name in ('agent_cascade_logger', 'agent_cascade'):
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
    return [r for r in records if needle in (r.getMessage() if hasattr(r, 'getMessage') else str(r))]


# ═══════════════════════════════════════════════════════════════════════════
# V1 — LEAK #1: run() entry stale permit must be RELEASED, not cleared
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak1RunEntryStalePermit:
    def test_run_entry_stale_permit_is_released(self, leak_harness):
        """A permit left over from a previous run is released at run() entry.

        Pre-fix (RED): the old SLOT_LEAK_GUARD cleared `_slot_release` WITHOUT
        releasing → `shared._running` still contained 'leak1a' and waiter B
        stayed blocked for the full timeout. Post-fix: the helper releases, so
        the pool empties within 1s and B is granted.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'leak1a', 'coder')
        rel = _acquire_shared(router, 'leak1a')
        _hold(inst, rel)
        assert 'leak1a' in shared._running

        t_b, granted_b = _queue_waiter(router, 'leak1b')
        try:
            # Drive the guard region exactly as run() does (plan §2 LEAK #1):
            # the helper call that replaced the nullify-without-release guard.
            from agent_cascade.engine.core import ExecutionEngine
            found = ExecutionEngine._discard_stale_permit(
                inst, 'leak1a', context='run() entry stale permit', action='drop-stale-guard')

            assert found is True, 'helper must report a live stale permit'
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with shared._cond:
                    if not shared._running:
                        break
                time.sleep(0.01)
            assert 'leak1a' not in shared._running, \
                f"stale permit was cleared without releasing: {list(shared._running)}"
            assert granted_b.wait(timeout=2), 'queued waiter B must be granted after release'
        finally:
            t_b.join(timeout=5)


# ═══════════════════════════════════════════════════════════════════════════
# V2/V3 — LEAK #2: _acquire_slot_with_logging must not orphan a prior permit
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak2ReacquireOver:
    @pytest.mark.parametrize('mode', ['same_pool', 'cross_pool'])
    def test_reacquire_over_does_not_orphan_prior_permit(self, leak_harness, mode):
        """Re-acquiring while still holding a permit releases the prior one.

        Pre-fix (RED): `instance._slot_release = self.pool._acquire_slot(...)`
        overwrote the live callback → the old SlotHolder stayed in the OLD
        pool's `_running` (same_pool: 2 entries for the same name impossible,
        but the acquisition_id mismatch left an orphan; cross_pool: a real
        second entry on K1). Post-fix: capture-and-release first, so the old
        pool is empty and exactly one holder exists with P2's acquisition_id.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        if mode == 'cross_pool':
            # A second conc=0 endpoint on a different base. NOTE: both endpoints are
            # sequential (conc=0), so they share the SAME SlotPool ('_shared_sequential_slot_')
            # — cross-pool isolation requires PAR to be concurrent (conc>=1 → its own pool
            # keyed by normalized api_base). The leak mechanism is identical: a prior
            # permit on K1 must be released before acquiring on K2.
            _add_endpoint(router, 'par', PAR_BASE, concurrency_limit=4)
            router.set_agent_priorities('coder', ['ep_par'])

        inst = _make_instance(pool, f'leak2_{mode}', 'coder')
        name = inst.instance_name
        p1 = _acquire_shared(router, name)
        _hold(inst, p1)
        assert name in shared._running

        # The test pool's REAL router (which owns the real scheduler) must be what
        # resolves slot info AND acquires — a mock pool would make the new acquire
        # land on a MagicMock and hide the orphan. So drive _acquire_slot_with_logging
        # with self=engine, engine.pool = the REAL test pool (self.pool is the only
        # attribute of self the helper touches).
        from agent_cascade.engine.core import ExecutionEngine

        inst._slot_key = SHARED_KEY  # pre-set so the re-acquire resolves the same key
        if mode == 'cross_pool':
            # Re-point the instance's effective endpoint to PAR_BASE (cross_pool).
            with router._lock:
                router.agent_priorities[name] = ['ep_par']
        eng = ExecutionEngine.__new__(ExecutionEngine)
        eng.pool = pool
        eng._acquire_slot_with_logging(inst, 'after_message_wakeup')

        # NOTE: SlotPool._running is keyed by instance_name — a same-pool re-grant
        # REPLACES the entry in place, so "name not in _running" would be wrong for
        # same_pool. The orphan signal is the acquisition_id: pre-fix the old P1
        # holder (acq 0) survived with a stale callback; post-fix exactly one holder
        # exists and it carries the NEW permit's acquisition_id.
        if mode == 'cross_pool':
            # Cross-pool: the prior pool must be empty (P1 released, not orphaned).
            assert name not in shared._running, \
                f"prior permit on K1 was orphaned: {list(shared._running)}"
            par_pool = router.scheduler._get_or_create_pool(PAR_BASE, 4)
            with par_pool._cond:
                holders = list(par_pool._running.values())
            assert len(holders) == 1 and holders[0].instance_name == name, \
                f"new pool must hold exactly one permit: {holders}"
        else:
            # Same-pool: exactly one holder for the name, with a NEW acquisition_id.
            with shared._cond:
                holder = shared._running.get(name)
                holders_all = list(shared._running.values())
            assert holder is not None, 'same-pool re-acquire must leave one holder'
            assert len(holders_all) == 1, \
                f"exactly one holder expected in shared pool: {holders_all}"
            # The instance's live callback must release THIS holder (identity check):
            # if the old P1 callback were still held, calling it would present a
            # stale acquisition_id and hit the STALE RELEASE branch.
            with inst._state_lock:
                cb = inst._slot_release
            assert cb is not None and inst._slot_key == SHARED_KEY
            _release_permit(inst)
            with shared._cond:
                assert name not in shared._running, \
                    f"instance's live callback did not match the current holder " \
                    f"(stale P1 callback still held): {list(shared._running)}"
            return  # permit already released above

        _release_permit(inst)


# ═══════════════════════════════════════════════════════════════════════════
# V4 — LEAK #2b: reacquire_for unlimited branches release a stale permit (×3)
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak2bReacquireUnlimited:
    @pytest.mark.parametrize('entry', ['no_slot_info', 'needs_slot_false', 'acquire_none'])
    def test_reacquire_for_unlimited_releases_stale(self, leak_harness, entry):
        """reacquire_for's unlimited branches must RELEASE a live stale permit.

        Pre-fix (RED): all three sites did `_slot_release = None` under
        `_state_lock` with NO release → the SlotHolder stayed in `shared._running`
        forever and no log line was emitted. Post-fix: the helper releases it,
        the pool empties, and a [SLOT_STALE_PERMIT] WARNING is emitted.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, f'leak2b_{entry}', 'coder')
        name = inst.instance_name
        rel = _acquire_shared(router, name)
        _hold(inst, rel)
        assert name in shared._running

        engine = _make_engine(router.scheduler, SEQ_BASE, 0)
        mock_router = engine.pool.api_router

        if entry == 'no_slot_info':
            mock_router.get_effective_slot_info.return_value = None
        elif entry == 'needs_slot_false':
            mock_router.get_effective_slot_info.return_value = {
                'slot_key': None, 'is_sequential': False, 'concurrency_limit': -1,
                'api_base': None, 'needs_slot': False,
            }
        else:  # acquire_none — slot_info says needs_slot but scheduler.acquire → None
            mock_router.get_effective_slot_info.return_value = {
                'slot_key': SHARED_KEY, 'is_sequential': True, 'concurrency_limit': 0,
                'api_base': SEQ_BASE, 'needs_slot': True,
            }
            # The defensive fast-path must NOT trigger: the desired key (SHARED_KEY)
            # differs from the pre-set _slot_key, so reacquire_for proceeds to acquire.
            inst._slot_key = 'other_pool_key'
            # mock_router.scheduler is the REAL scheduler — patch its acquire method.
            sched_patcher = patch.object(
                router.scheduler, 'acquire', return_value=None)
            sched_patcher.start()

        records, handler, targets = _capture_logs()
        try:
            ok = engine.reacquire_for(inst, name, context='test')
        finally:
            _restore_logs(handler, targets)
            if entry == 'acquire_none':
                sched_patcher.stop()

        assert ok is True, 'reacquire_for must return True for unlimited endpoints'
        with shared._cond:
            assert name not in shared._running, \
                f"stale permit was nullified without releasing: {list(shared._running)}"
        assert inst._slot_release is None
        assert _find(records, 'SLOT_STALE_PERMIT'), \
            'a [SLOT_STALE_PERMIT] WARNING must be emitted when a live permit is found'


# ═══════════════════════════════════════════════════════════════════════════
# V5 — LEAK #3 / BUG_0034: terminate_instance must release a held permit
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak3TerminateReleasesPermit:
    def test_terminate_releases_held_permit(self, leak_harness):
        """terminate-without-dismiss frees the pool (BUG_0034).

        Pre-fix (RED — the filed bug): `terminate_instance` never released the
        permit → `shared._running` kept 'leak3a' and waiter B stayed blocked;
        `terminate_for_agent` returned (0, 0) instead of (0, 1). Post-fix:
        terminate releases immediately, B is granted, the count is truthful,
        and a double-terminate + late thread release is a silent no-op.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst_a = _make_instance(pool, 'leak3a', 'coder')
        rel_a = _acquire_shared(router, 'leak3a')
        _hold(inst_a, rel_a)
        assert 'leak3a' in shared._running

        t_b, granted_b = _queue_waiter(router, 'leak3b')
        try:
            # Terminate WITHOUT dismissing (the BUG_0034 path).
            pool.terminate_instance('leak3a', set_global_stopped=False)

            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                with shared._cond:
                    if not shared._running:
                        break
                time.sleep(0.01)
            assert 'leak3a' not in shared._running, \
                f"BUG_0034: terminate left the permit held: {list(shared._running)}"
            assert granted_b.wait(timeout=2), 'waiter B must be granted after terminate'
        finally:
            t_b.join(timeout=5)

        # The released count must be real: a holder present at call time is removed
        # by identity and reported as released=1. Here the permit was ALREADY freed
        # by terminate_instance's own release block (that IS the fix), so the
        # follow-up call correctly reports (0, 0) — idempotency, not a leak. The
        # holder-present case is covered by test_terminate_for_agent_returns_real_count.
        cancelled, released = shared.terminate_for_agent('leak3a')
        assert cancelled == 0 and released == 0, \
            f"BUG_0034: terminate_for_agent after an already-released permit " \
            f"returned ({cancelled}, {released}), expected (0, 0)"

    def test_terminate_for_agent_returns_real_count(self, leak_harness):
        """`SlotPool.terminate_for_agent` reports the permit drop truthfully.

        Pre-fix (RED): returned a hardcoded `(len(cancelled), 0)` — the released
        slot was never computed. Post-fix: a holder present at call time is
        removed by identity and reported as released=1.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst_a = _make_instance(pool, 'leak3c', 'coder')
        rel_a = _acquire_shared(router, 'leak3c')
        _hold(inst_a, rel_a)
        assert 'leak3c' in shared._running

        cancelled, released = shared.terminate_for_agent('leak3c')
        assert cancelled == 0, f"no waiters expected: {cancelled}"
        assert released == 1, \
            f"BUG_0034: terminate_for_agent reported released={released}, expected 1"
        with shared._cond:
            assert 'leak3c' not in shared._running

    def test_double_terminate_and_late_release_are_safe(self, leak_harness):
        """Idempotency: double-terminate + the killed thread's late release no-op."""
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst_a = _make_instance(pool, 'leak3d', 'coder')
        rel_a = _acquire_shared(router, 'leak3d')
        _hold(inst_a, rel_a)

        pool.terminate_instance('leak3d', set_global_stopped=False)
        pool.terminate_instance('leak3d', set_global_stopped=False)  # idempotent

        # The killed thread's run()-finally release arrives late — must not raise.
        _release_permit(inst_a)

        with shared._cond:
            assert 'leak3d' not in shared._running


# ═══════════════════════════════════════════════════════════════════════════
# V6/V7 — LEAK #4: double-acquire detection + loud stale release
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak4DoubleAcquireDetection:
    @pytest.mark.parametrize('which', ['grant', 'stale_release'])
    def test_double_acquire_is_detected(self, leak_harness, which):
        """A same-instance double acquire is LOUD, not silent.

        Pre-fix (RED): `_grant` overwrote the holder with no log and no counter;
        the stale release was a bare `return` (zero log output). Post-fix: an
        ERROR containing DOUBLE-ACQUIRE at grant, a WARNING containing STALE
        RELEASE at the orphaned callback's release, and _orphan_overwrites >= 1.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        name = 'leak4'
        # A conc>=2 pool so the second acquire by the SAME name takes the
        # uncontended fast path and grants immediately (plan §5 V6).
        par_pool = router.scheduler._get_or_create_pool(PAR_BASE, 4)

        records, handler, targets = _capture_logs()
        try:
            cb1 = par_pool.acquire(name, 'coder', timeout=5.0)
            assert cb1 is not None
            cb2 = par_pool.acquire(name, 'coder', timeout=5.0)  # double-acquire
            assert cb2 is not None
            if which == 'grant':
                with par_pool._cond:
                    counter = par_pool._orphan_overwrites
                assert counter >= 1, f"_orphan_overwrites must be set: {counter}"
                assert _find(records, 'DOUBLE-ACQUIRE'), \
                    'an ERROR containing DOUBLE-ACQUIRE must be logged at grant'
            else:
                # The FIRST callback hits the stale branch after the overwrite.
                cb1()  # release the ORPHANED (first) acquisition → stale branch
                assert _find(records, 'STALE RELEASE'), \
                    'a WARNING containing STALE RELEASE must be logged for the orphan'
        finally:
            _restore_logs(handler, targets)
            # Clean up any leftover holders so the pool is not pinned.
            for cb in (cb1, cb2):
                try:
                    cb()
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════
# V8 — LEAK #5: run_agent_unified must close the generator on stop
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak5UnifiedRunnerClosesGenerator:
    def test_unified_runner_closes_generator_on_stop(self, leak_harness):
        """The unified runner closes the engine generator deterministically.

        Pre-fix (RED): the generator was never bound or closed — `close_called`
        stayed False unconditionally (GC-independent flag per plan §5 V8).
        Post-fix: `_run_gen.close()` in the finally sets the flag.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        closed = {'called': False}

        def _gen_with_tick_flag():
            yield ('tick1', True)  # first tick → loop body runs once
            closed['ticked'] = True
            # Block forever; the loop must break via is_stopped() and close us.
            while True:
                yield ('tick2', False)

        class _CloseSpy:
            """Wrap the generator so close() is observable without GC dependence."""

            def __init__(self, inner):
                self._inner = inner

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._inner)

            def close(self):
                closed['called'] = True
                self._inner.close()

        from agent_cascade import run_agent_unified as rau

        # The runner does a FUNCTION-SCOPE import from .api_integration — patch the
        # RE-EXPORT seam (agent_cascade.api_integration.run_agent_in_pool_with_recovery),
        # which is the name actually resolved at call time. Everything the loop body
        # touches is stubbed so only the generator-lifecycle code under test runs.
        import agent_cascade.api_integration as ai_pkg

        inst = _make_instance(pool, 'leak5a', 'coder')
        inst.conversation = [object()]  # non-empty → create_main_agent_instance skipped
        pool.stopped = False
        pool._run_generation = getattr(pool, '_run_generation', 0) + 1
        pool._halted_instances = set()
        # is_stopped() checks termination → report terminated after tick 1.
        pool.is_instance_terminated = lambda name: closed.get('ticked', False)

        import asyncio
        loop = asyncio.new_event_loop()
        send_queue = asyncio.Queue()

        with patch.object(ai_pkg, 'run_agent_in_pool_with_recovery',
                          side_effect=lambda **kw: _CloseSpy(_gen_with_tick_flag())), \
             patch.object(rau, '_reset_run_scoped_tg_state'), \
             patch('agent_cascade.api_integration_pkg.state_builder._apply_ui_config'), \
             patch('agent_cascade.api_integration_pkg.streaming.broadcast_stream_update',
                   return_value=(0.0, 0)), \
             patch('agent_cascade.api_integration_pkg.state_builder.build_state_from_pool',
                   return_value=None):

            def _drive():
                rau.run_agent_thread_unified(
                    pool=pool, instance_name='leak5a',
                    system_message_content=None, ui_cfg={},
                    send_queue=send_queue, loop=loop)

            t = threading.Thread(target=_drive, daemon=True)
            t.start()
            deadline = time.monotonic() + 5.0
            while not closed['called'] and time.monotonic() < deadline:
                time.sleep(0.01)
            pool.stopped = True  # unblock any remaining path
            t.join(timeout=5)
            loop.close()

        assert closed['called'], \
            'generator close() was never called — the abandoned generator leaks its finally'


# ═══════════════════════════════════════════════════════════════════════════
# V9 — Hardening: release_slot_permit strict= re-raises
# ═══════════════════════════════════════════════════════════════════════════

class TestHardeningStrictRelease:
    def test_release_slot_permit_strict_reraises(self, leak_harness):
        """strict=True re-raises a release-callback failure instead of absorbing it.

        New behavior (no pre-fix state). The default (strict=False) keeps the
        existing absorb-and-log ERROR contract — verified here too so a future
        "fix" cannot silently change the default.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        from agent_cascade.slot_queue import release_slot_permit

        def boom():
            raise RuntimeError('release-callback-failure')

        # strict=True → re-raise.
        inst1 = MagicMock()
        inst1.instance_name = 'strict1'
        inst1._state_lock = threading.RLock()
        inst1._slot_release = boom
        inst1._slot_key = SHARED_KEY
        with pytest.raises(RuntimeError, match='release-callback-failure'):
            release_slot_permit(inst1, 'strict1', action='drop-test', strict=True)

        # default (strict=False) → absorbed, ERROR logged, returns True.
        inst2 = MagicMock()
        inst2.instance_name = 'strict2'
        inst2._state_lock = threading.RLock()
        inst2._slot_release = boom
        inst2._slot_key = SHARED_KEY
        records, handler, targets = _capture_logs()
        try:
            result = release_slot_permit(inst2, 'strict2', action='drop-test')
        finally:
            _restore_logs(handler, targets)
        assert result is True, 'a captured permit counts as released even if the callback failed'
        assert _find(records, 'SLOT_RELEASE_ERROR'), \
            'the [SLOT_RELEASE_ERROR] ERROR must be logged unconditionally'


# ═══════════════════════════════════════════════════════════════════════════
# V9b — LEAK #6: router._drop_held_permit keeps the permit retryable on failure
# ═══════════════════════════════════════════════════════════════════════════

class TestLeak6DropHeldPermitRetryable:
    def test_drop_held_permit_retryable_on_callback_failure(self, leak_harness):
        """A failing release callback must NOT nullify the permit (LEAK #6).

        Pre-fix (RED): `_drop_held_permit` nullified `_slot_release`/`_slot_key`
        and popped the committed-endpoint marker BEFORE invoking the callback →
        on failure `inst._slot_release is None` (no retry possible) and the
        marker was gone. Post-fix: it re-raises (ungated invariant kept) AND
        leaves `_slot_release` in place AND leaves the marker intact; a later
        dismiss frees the pool.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'leak6a', 'coder')
        rel = _acquire_shared(router, 'leak6a')
        _hold(inst, rel)
        assert 'leak6a' in shared._running

        # Mark the committed endpoint so the marker-preservation is observable.
        with router._lock:
            router._instance_committed_endpoint['leak6a'] = SEQ_BASE

        def boom():
            raise RuntimeError('drop-callback-failure')

        # Swap in a raising callback that still targets the real pool release.
        with inst._state_lock:
            inst._slot_release = boom

        records, handler, targets = _capture_logs()
        try:
            # Signature (router.py): (instance, inst_name, old_key, release_cb_old, origin).
            with pytest.raises(RuntimeError, match='drop-callback-failure'):
                router._drop_held_permit(inst, 'leak6a', SHARED_KEY, boom, 'test')
        finally:
            _restore_logs(handler, targets)

        # The permit must still be retryable — the callback is still in place.
        assert inst._slot_release is boom, \
            'LEAK #6: a failed release nullified the permit — no later release can retry'
        with router._lock:
            assert router._instance_committed_endpoint.get('leak6a') == SEQ_BASE, \
                'LEAK #6: the committed-endpoint marker was popped on a failed release'

        # A later canonical release (dismiss path) frees the pool.
        with inst._state_lock:
            real_cb = rel  # restore the REAL callback for cleanup
            inst._slot_release = real_cb
        pool.dismiss_instance('leak6a')
        with shared._cond:
            assert 'leak6a' not in shared._running, \
                f"pool must be empty after dismiss: {list(shared._running)}"


# ═══════════════════════════════════════════════════════════════════════════
# V10–V12 / V22 — FIFO head-stall alarm (plan §3, §5.1)
# ═══════════════════════════════════════════════════════════════════════════

def _alarm_thresholds():
    """Patched threshold constants per plan §5.1 (real clock, no seam)."""
    return (
        patch('agent_cascade.slot_queue.SLOT_HEAD_STALL_WARN_S', 0.2),
        patch('agent_cascade.slot_queue.SLOT_HEAD_STALL_ALARM_S', 0.6),
        patch('agent_cascade.slot_queue.SLOT_HEAD_STALL_REPEAT_S', 10.0),
        patch('agent_cascade.slot_queue.SLOT_HEAD_STALL_ACTIVE_S', 0.3),
    )


def _wait_for_log(records, needle, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _find(records, needle):
            return True
        time.sleep(0.01)
    return bool(_find(records, needle))


def _wait_for_level(records, needle, level, timeout=8.0):
    """Wait for a record containing `needle` at exactly `level`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for r in records:
            if needle in (r.getMessage() if hasattr(r, 'getMessage') else str(r)) \
                    and r.levelno == level:
                return True
        time.sleep(0.01)
    return False


class TestHeadStallAlarm:
    def test_head_stall_warns_at_30s(self, leak_harness):
        """V10a: the head waiter gets a WARN with holder context at the (patched) 30s mark."""
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm1a', 'coder')
        rel = _acquire_shared(router, 'alarm1a')
        _hold(inst, rel)

        records, handler, targets = _capture_logs()
        t_b, granted_b = None, None
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                t_b, granted_b = _queue_waiter(router, 'alarm1b', timeout=4.0)
                assert _wait_for_level(records, 'SLOT_HEAD_STALL_WARN', logging.WARNING), \
                    'head-stall WARN never fired for the head waiter'
                warn = _find(records, 'SLOT_HEAD_STALL_WARN')[0]
                msg = warn.getMessage()
                # Holder context fields must be present (resolver wired via pool path).
                assert 'holder=alarm1a' in msg, f'missing holder name: {msg}'
                assert 'state=' in msg and 'llm_active=' in msg and 'streaming=' in msg, \
                    f'missing holder context fields: {msg}'
        finally:
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

    def test_head_stall_alarms_at_90s(self, leak_harness):
        """V10b: a quiet holder trips the ERROR alarm at the (patched) 90s mark."""
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm2a', 'coder')
        rel = _acquire_shared(router, 'alarm2a')
        _hold(inst, rel)

        records, handler, targets = _capture_logs()
        t_b, granted_b = None, None
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                t_b, granted_b = _queue_waiter(router, 'alarm2b', timeout=4.0)
                assert _wait_for_level(records, 'SLOT_HEAD_STALL]', logging.ERROR), \
                    'head-stall ERROR alarm never fired for a quiet holder'
                alarm = [r for r in records if 'SLOT_HEAD_STALL]' in r.getMessage()
                         and r.levelno == logging.ERROR][0]
                msg = alarm.getMessage()
                assert 'ACTION: manual — diagnostic only, no auto-preemption' in msg, \
                    f'missing explicit non-action clause: {msg}'
        finally:
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

    def test_head_stall_suppressed_while_holder_streaming(self, leak_harness):
        """V11a: a fresh `_last_llm_activity` suppresses the ERROR even at long waiter age.

        The holder is stamped 'active' with a live chunk-stamp thread; the waiter
        outlives the alarm threshold by far. No ERROR may fire while streaming.
        (Companion half of V11 — see test_head_stall_alarms_when_streaming_stops.)
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm3a', 'coder')
        rel = _acquire_shared(router, 'alarm3a')
        _hold(inst, rel)

        stop_stamp = threading.Event()

        from agent_cascade.engine.core import ExecutionEngine

        def stamp_chunks():
            # Simulate a healthy stream: refresh activity every 50ms.
            while not stop_stamp.is_set():
                ExecutionEngine._stamp_llm_activity(inst, 'chunk')
                time.sleep(0.05)

        stamp_thread = threading.Thread(target=stamp_chunks, daemon=True)
        with inst._state_lock:
            inst._llm_call_active = True
            inst._last_llm_activity = time.monotonic()
        stamp_thread.start()

        records, handler, targets = _capture_logs()
        t_b, granted_b = None, None
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                # Waiter outlives ALARM_S (0.6s) by a wide margin while the holder
                # keeps stamping — the ERROR must stay suppressed the whole time.
                t_b, granted_b = _queue_waiter(router, 'alarm3b', timeout=2.5)
                assert _wait_for_level(records, 'SLOT_HEAD_STALL_WARN', logging.WARNING), \
                    'WARN must fire even while streaming (it is never suppressed)'
                time.sleep(1.0)  # well past ALARM_S with fresh activity
        finally:
            stop_stamp.set()
            stamp_thread.join(timeout=2)
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

        assert not [r for r in records if 'SLOT_HEAD_STALL]' in (r.getMessage() if hasattr(r, 'getMessage') else str(r))
                    and r.levelno == logging.ERROR], \
            'ERROR alarm fired while the holder was actively streaming — suppression broken'

    def test_head_stall_alarms_when_streaming_stops(self, leak_harness):
        """V11b: once `_last_llm_activity` ages past ACTIVE_S, the ERROR fires.

        Companion half of V11: same waiter/holder shape as the suppression test,
        but the holder goes quiet — the alarm must fire at ALARM_S.
        """
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm4a', 'coder')
        rel = _acquire_shared(router, 'alarm4a')
        _hold(inst, rel)

        # Holder was streaming but went quiet: active flag set, timestamp stale.
        with inst._state_lock:
            inst._llm_call_active = True
            inst._last_llm_activity = time.monotonic() - 5.0  # far past ACTIVE_S (0.3s)

        records, handler, targets = _capture_logs()
        t_b, granted_b = None, None
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                t_b, granted_b = _queue_waiter(router, 'alarm4b', timeout=4.0)
                assert _wait_for_level(records, 'SLOT_HEAD_STALL]', logging.ERROR), \
                    'ERROR alarm never fired after streaming stopped'
        finally:
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

    def test_head_stall_does_not_preempt(self, leak_harness):
        """V12: the alarm is diagnostic only — after it fires, the holder still holds
        and the waiter is still queued (no preemption, no forced release)."""
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm5a', 'coder')
        rel = _acquire_shared(router, 'alarm5a')
        _hold(inst, rel)

        records, handler, targets = _capture_logs()
        t_b, granted_b = None, None
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                t_b, granted_b = _queue_waiter(router, 'alarm5b', timeout=4.0)
                assert _wait_for_level(records, 'SLOT_HEAD_STALL]', logging.ERROR), \
                    'ERROR alarm never fired'
                # After the alarm: holder's permit is still held, waiter still queued.
                with shared._cond:
                    assert 'alarm5a' in shared._running, \
                        'V12: the alarm preempted the holder — it must be diagnostic only'
                    assert any(t.instance_name == 'alarm5b' for t in shared._waiters.values()), \
                        'V12: the waiter was dropped from the queue by the alarm'
                assert not granted_b.is_set(), \
                    'V12: the waiter was granted while the holder still holds — preemption?'
        finally:
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

    def test_alarm_branch_reacquires_cond(self, leak_harness):
        """V22: the resolver is called with pool._cond NOT owned, and the waiter is
        still granted correctly afterwards (guards R9 — the load-bearing release/acquire)."""
        h = leak_harness
        router, pool, shared = h['router'], h['pool'], h['shared']

        inst = _make_instance(pool, 'alarm6a', 'coder')
        rel = _acquire_shared(router, 'alarm6a')
        _hold(inst, rel)

        cond_owned_at_resolve = []

        def spy_resolver(name):
            # CPython 3.x internal — acceptable in a test (plan §5.1 V22).
            try:
                cond_owned_at_resolve.append(shared._cond._is_owned())
            except Exception:
                cond_owned_at_resolve.append(None)
            return pool.get_instance(name)

        records, handler, targets = _capture_logs()
        try:
            with _alarm_thresholds()[0], _alarm_thresholds()[1], \
                 _alarm_thresholds()[2], _alarm_thresholds()[3]:
                # Route the waiter through the real scheduler path (as every other
                # alarm test does) but pass the spy resolver explicitly — it overrides
                # the pool fallback in SlotPool.acquire, so we observe lock ownership.
                t_b, granted_b = _queue_waiter(router, 'alarm6b', timeout=4.0,
                                               instance_resolver=spy_resolver)
                # The wait loop ticks at 1s cadence (WARN_S patched to 0.2), so give it
                # >1 tick for the head-stall branch — and thus the lock-free resolver
                # call — to run at least once.
                assert _wait_for_log(records, 'SLOT_HEAD_STALL_WARN', timeout=4.0), \
                    'V22: head-stall WARN never fired — alarm branch did not run'
        finally:
            _restore_logs(handler, targets)
            if t_b is not None:
                t_b.join(timeout=5)
            _release_permit(inst)

        assert cond_owned_at_resolve, 'resolver was never called — alarm branch did not run'
        assert all(v is False for v in cond_owned_at_resolve), \
            f"V22: resolver observed pool._cond owned ({cond_owned_at_resolve}) — lock-order inversion"


# Note: V10–V12 / V22 are new behavior (no pre-fix state) — they pin the
# threshold/suppression/lock-discipline contract of the head-stall alarm.
