"""t151 — forced compression must halt only the Compressor's genuine pool-mates.

The todo premise ("forced compression does not queue the compressor in the router FIFO") is
half wrong: the Compressor IS already FIFO-queued (agent_invoker.py:372 → engine.run →
core.py:1020 _acquire_slot_with_logging → slots.py:54 scheduler.acquire). The real defect was
the blanket ``halt_all_instances()`` at compression/handler.py:852, which halted EVERY non-exempt
sibling — including agents on a completely different endpoint pool that could never starve the
Compressor. Those unrelated siblings then hit _wait_for_compression_to_clear (engine/core.py:1922)
and were forced through a save-state → drop-slot → sleep → re-acquire → restore-state reprocess
cycle for no reason.

The fix replaces the blanket halt with a pool-aware, bounded halt
(CompressionHandler._build_compression_halt_scope): only instances sharing the Compressor's
resolved slot pool are halted, minus exemptions, minus already-halted.

Status legend (plan §6.2):
  ⛔ = MUST fail on pre-fix code (revert-proof bug test) — Test 2
  ✅ = passes today / regression guard — Tests 1, 3, 4, 5, 6

Tests 1-3 and 6 drive the REAL CompressionHandler against a REAL APIRouter/AgentPool so the
slot resolution is exercised end to end (a MagicMock pool would make _build_compression_halt_scope
return [] vacuously — see the mock-pool-real-lifecycle-wiring skill). Test 4 drives the REAL
ExecutionEngine acquire funnel.
"""
import os
os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"t151_{os.getpid()}")

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.compression.handler import CompressionHandler
from agent_cascade.compression.result import CompressResult
from agent_cascade.llm.schema import Message

SHARED_KEY = '_shared_sequential_slot_'
BASE_A = 'http://127.0.0.1:9/v1'    # conc=0 → shared sequential slot (Compressor's pool)
BASE_B = 'http://127.0.0.1:10/v1'   # conc=2  → per-base pool (a DIFFERENT pool)


# ── Real router / pool harness (no server, no LLM) ───────────────────────────

def _build_router(tmp_path):
    """Real APIRouter with a conc=0 endpoint (shared sequential slot) as the default."""
    from agent_cascade.api_router import APIEndpoint, APIRouter

    llm_cfg = {'model': 'mock', 'api_base': BASE_A, 'model_server': BASE_A, 'api_key': 'EMPTY'}
    router = APIRouter(default_llm_cfg=llm_cfg, config_dir=str(tmp_path))
    with router._lock:
        router.endpoints.clear()
        router.agent_priorities.clear()
        router._agent_types_with_priorities.clear()
    ep_a = APIEndpoint(id='epA', name='conc0', api_base=BASE_A, model='mock',
                       concurrency_limit=0, enabled=True)
    router.add_endpoint(ep_a)
    router.default_llm_cfg = ep_a.to_llm_cfg()
    return router


def _add_par_endpoint(router):
    """Add a distinct conc>0 endpoint (its own per-base pool) and return its id."""
    from agent_cascade.api_router import APIEndpoint
    ep_b = APIEndpoint(id='epB', name='par', api_base=BASE_B, model='mock',
                       concurrency_limit=2, enabled=True)
    router.add_endpoint(ep_b)
    return 'epB'


def _build_pool(router, tmp_path):
    """Real AgentPool wired to the real router (real halt/resume/_acquire_slot)."""
    from agent_cascade.agent_pool import AgentPool
    llm_cfg = {'model': 'mock', 'api_base': BASE_A, 'model_server': BASE_A, 'api_key': 'EMPTY'}
    return AgentPool(llm_cfg, agents_dir=str(tmp_path), api_router=router)


def _make_instance(pool, name, agent_class='coder'):
    """Real AgentInstance registered in the pool (so get_instance() / snapshot find it)."""
    inst = AgentInstance(
        instance_name=name, agent_class=agent_class, conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(), latest_marker_index=0,
    )
    inst._pool_ref = pool
    pool.instances[name] = inst
    return inst


def _make_handler(pool):
    handler = CompressionHandler(pool)
    engine = MagicMock()  # only used on the success path we stub around; halt scope needs no engine
    handler.set_engine(engine)
    return handler, engine


def _ok_result():
    return CompressResult(success=True, summary_text='s', marker_message=None,
                          messages_discarded=5, tail_count=3, error=None, mode='auto')


def _run_forced(handler, inst):
    """Run execute_force_compression with compress_context stubbed to a success result.

    Returns (result, halted_snapshot) where halted_snapshot is the set of names in
    pool._compression_halted observed *while* compression runs (i.e. what was actually halted).
    """
    from agent_cascade.compression import core as _ccore
    seen = {}

    def fake_compress(agent_pool, target_agent_name, **kwargs):
        seen['halted'] = set(agent_pool._compression_halted)
        return _ok_result()

    with patch.object(_ccore, 'compress_context', side_effect=fake_compress), \
         patch.object(handler.engine, '_rebuild_working_set'), \
         patch.object(handler, '_sync_logger_after_compression'), \
         patch.object(handler, '_inject_compression_notification'):
        result = handler.execute_force_compression(inst, [], [], 96.0)
    return result, seen.get('halted', set())


class TestForcedCompressionHaltScope:

    def test_forced_compression_halts_genuine_pool_mate(self, tmp_path):
        """✅ CONTROL (passes today; guards against over-narrowing).

        A sibling that shares the Compressor's pool must STILL be halted — otherwise the
        Compressor can starve at >95% context (plan §3.3).
        """
        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # compression target (exempt)
        mate = _make_instance(pool, 'writer', 'writer')          # same shared conc=0 pool
        mate._slot_key = SHARED_KEY                              # authoritative: holds the shared permit

        result, halted = _run_forced(handler, target)

        assert result is True
        assert 'writer' in halted, \
            f"pool-mate 'writer' (shared {SHARED_KEY}) must be halted; got {halted}"
        # Symmetry: resume cleared the compression-halt set.
        assert pool._compression_halted == set()

    def test_forced_compression_does_not_halt_unrelated_pool_agent(self, tmp_path):
        """⛔ THE revert-proof bug test — MUST fail on pre-fix code.

        A sibling on a DIFFERENT endpoint pool (different api_base) cannot starve the
        Compressor and must NOT be halted. Pre-fix halt_all_instances() reached it anyway.
        """
        router = _build_router(tmp_path)
        ep_b = _add_par_endpoint(router)
        # writer is assigned to the per-base endpoint → resolves to BASE_B, a different pool.
        router.set_agent_priorities('writer', [ep_b])
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # compression target (exempt)
        unrelated = _make_instance(pool, 'writer', 'writer')     # different pool (BASE_B)
        unrelated._slot_key = None                               # not holding the shared permit

        # Sanity: writer really resolves to a different pool than the Compressor.
        comp_key = router.get_effective_slot_info('Compressor')['slot_key']
        writer_key = router.get_effective_slot_info('writer', instance_name='writer')['slot_key']
        assert comp_key == SHARED_KEY and writer_key != comp_key, \
            f"fixture broken: compressor={comp_key} writer={writer_key}"

        result, halted = _run_forced(handler, target)

        assert result is True
        assert 'writer' not in halted, \
            f"unrelated agent 'writer' on a different pool must NOT be halted; got {halted}"
        assert pool._compression_halted == set()  # nothing was compression-halted

    def test_manual_halt_not_resurrected_by_forced_compression(self, tmp_path):
        """✅ regression guard — the lifecycle.py:211-215 invariant.

        A manually halted sibling must survive resume_all_instances(): it is not recorded into
        _compression_halted, so resume only clears what compression actually halted.
        """
        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # exempt
        manual = _make_instance(pool, 'writer', 'writer')        # same pool, but manually halted
        mate = _make_instance(pool, 'researcher', 'researcher')  # same pool, not yet halted

        pool.halt_instance('writer')   # MANUAL halt (not via compression)
        manual._slot_key = SHARED_KEY
        mate._slot_key = SHARED_KEY

        result, halted = _run_forced(handler, target)

        assert result is True
        # The genuinely-halted-by-compression pool-mate was resumed…
        assert 'researcher' not in pool._halted_instances
        # …but the manually halted sibling was NOT resurrected.
        assert 'writer' in pool._halted_instances, \
            'manual halt must be preserved by resume_all_instances()'

    def test_compressor_acquires_slot_through_router_fifo(self, tmp_path):
        """✅ regression guard — the literal todo-151 requirement.

        The Compressor instance appears as a ticket on the SAME real SlotPool as a normal agent
        via the real acquire funnel (engine.run → _acquire_slot_with_logging → pool._acquire_slot
        → scheduler.acquire). Guards against any future "optimisation" into a bypass. Uses a REAL
        router + pool (a MagicMock pool would make _acquire_slot return a MagicMock and hide the bug).

        The holder acquires in a background thread (a conc=0 pool is at capacity once one holder
        exists, so a direct main-thread acquire would block); the Compressor's acquire is then
        observed to enqueue as a FIFO ticket behind that holder.
        """
        import agent_cascade.slot_queue as _sq

        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)

        # A normal agent acquires first and holds the shared permit (background thread).
        released = threading.Event()

        def _hold():
            rel = pool._acquire_slot('coder', 'coder')
            assert rel is not None
            try:
                released.wait(timeout=30)
            finally:
                rel()

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()

        deadline = time.monotonic() + 5.0
        shared = None
        while time.monotonic() < deadline:
            shared = router.scheduler._pools.get(SHARED_KEY)
            if shared is not None and 'coder' in shared._running:
                break
            time.sleep(0.02)
        assert shared is not None and 'coder' in shared._running, \
            f"holder never held the shared permit: {getattr(shared, '_running', None)}"

        # The Compressor goes through the SAME funnel — spy on scheduler.acquire to prove no bypass.
        # Its acquire BLOCKS (pool full), so run it in a background thread and observe the FIFO
        # ticket from the main thread; releasing the holder then grants it and unblocks the thread.
        with patch.object(router.scheduler, 'acquire',
                          wraps=router.scheduler.acquire) as spy:
            comp_box = {}

            def _comp_acquire():
                try:
                    comp_box['rel'] = pool._acquire_slot('Compressor', 'Compressor_t151')
                except Exception as e:  # noqa: BLE001 — surface via assertion below
                    comp_box['exc'] = e

            comp_thread = threading.Thread(target=_comp_acquire, daemon=True)
            comp_thread.start()

        assert spy.called, 'Compressor must acquire through the router scheduler (no bypass)'
        # Both resolved to the shared sequential slot.
        comp_key = router.get_effective_slot_info('Compressor')['slot_key']
        coder_key = router.get_effective_slot_info('coder', instance_name='coder')['slot_key']
        assert comp_key == SHARED_KEY and coder_key == SHARED_KEY, \
            f"Compressor and normal agent must share the FIFO pool: {comp_key} vs {coder_key}"

        # The Compressor is queued (not granted) behind the running holder.
        deadline = time.monotonic() + 5.0
        queued = False
        while time.monotonic() < deadline:
            if any(t.instance_name == 'Compressor_t151' for t in shared._waiters.values()):
                queued = True
                break
            time.sleep(0.02)
        assert queued, \
            f"Compressor ticket not in FIFO waiters: {[t.instance_name for t in shared._waiters.values()]}"

        # Cleanup: release the holder so the Compressor's queued acquire is granted and returns.
        released.set()
        holder.join(timeout=5)
        comp_thread.join(timeout=10)
        assert 'exc' not in comp_box, f"Compressor acquire raised: {comp_box.get('exc')!r}"
        if comp_box.get('rel') is not None:
            comp_box['rel']()

    def test_compressor_queued_behind_sibling_times_out_not_hangs(self, tmp_path):
        """✅ regression guard — bounded wait, never a hang.

        With a pool-mate holding the only permit that never releases, the Compressor's acquire
        must raise SlotQueueTimeout within QUEUE_WAIT_TIMEOUT (not block forever). The handler
        catches it, returns False, and records the fail-streak (BUG-7 backoff gate).

        Uses the REAL scheduler acquire against a real conc=0 pool (shortened timeout), so this
        is not a MagicMock stand-in: the Compressor genuinely queues behind the holder.
        """
        import agent_cascade.api_router_pkg.scheduler as _sched_mod

        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # exempt (holds the permit)
        mate = _make_instance(pool, 'writer', 'writer')          # pool-mate that will be halted
        mate._slot_key = SHARED_KEY

        # Hold the shared permit in a background thread that never releases it. (The main thread
        # must NOT acquire — a conc=0 pool is at capacity once one holder exists, so a direct
        # _acquire_slot here would block for QUEUE_WAIT_TIMEOUT and hang the suite.)
        released = threading.Event()

        def _hold():
            rel = pool._acquire_slot('coder', 'coder')
            assert rel is not None
            try:
                released.wait(timeout=30)  # hold until told to finish
            finally:
                rel()

        holder = threading.Thread(target=_hold, daemon=True)
        holder.start()

        # Wait until the holder actually holds the shared permit.
        deadline = time.monotonic() + 5.0
        shared = None
        while time.monotonic() < deadline:
            shared = router.scheduler._pools.get(SHARED_KEY)
            if shared is not None and 'coder' in shared._running:
                break
            time.sleep(0.02)
        assert shared is not None and 'coder' in shared._running, \
            f"holder never held the shared permit: {getattr(shared, '_running', None)}"

        # Shorten the acquire timeout so the bounded wait surfaces as a quick TimeoutError
        # instead of the 300s default. The effective timeout is resolved in
        # api_router_pkg/scheduler.py (line 170) from ITS module-level QUEUE_WAIT_TIMEOUT, so we
        # patch that one (not slot_queue's). The scheduler wraps SlotQueueTimeout into a plain
        # TimeoutError (scheduler.py:218), which the handler catches via `except Exception`.
        # The acquire BLOCKS, so run it in a background thread and poll for the timeout —
        # proving it is BOUNDED (raises within ~timeout), not an infinite hang.
        old_timeout = _sched_mod.QUEUE_WAIT_TIMEOUT
        _sched_mod.QUEUE_WAIT_TIMEOUT = 2
        try:
            comp_box = {}

            def _comp_acquire():
                try:
                    rel = pool._acquire_slot('Compressor', 'Compressor_t151')
                    comp_box['rel'] = rel
                except TimeoutError as e:
                    comp_box['exc'] = e

            comp_thread = threading.Thread(target=_comp_acquire, daemon=True)
            comp_thread.start()
            deadline = time.monotonic() + 10.0
            while 'exc' not in comp_box and 'rel' not in comp_box \
                    and time.monotonic() < deadline:
                time.sleep(0.05)
            assert isinstance(comp_box.get('exc'), TimeoutError), \
                f"Compressor acquire must raise TimeoutError (bounded), got {comp_box!r}"

            # And the handler path reports that failure honestly: False + fail-streak (BUG-7 gate).
            with patch('agent_cascade.compression.core.compress_context',
                       side_effect=TimeoutError('Compressor_t151 timed out')), \
                 patch.object(handler.engine, '_rebuild_working_set'), \
                 patch.object(handler, '_inject_compression_notification'):
                result = handler.execute_force_compression(target, [], [], 96.0)

            assert result is False, 'a queued-behind-sibling timeout must report failure'
            assert target._force_compress_fail_streak == 1, \
                'fail-streak must be recorded so the BUG-7 backoff gate suppresses instant re-halt'
            # Symmetry: the pool-mate that was halted is resumed in the finally block.
            assert 'writer' not in pool._halted_instances
        finally:
            _sched_mod.QUEUE_WAIT_TIMEOUT = old_timeout
            released.set()
            holder.join(timeout=5)
            if holder.is_alive():
                pytest.fail('holder thread did not finish (release leaked)')

    def test_halt_set_snapshot_is_immutable_during_iteration(self, tmp_path):
        """✅ regression guard — the list(...) snapshot introduced by the fix.

        Creating/removing instances concurrently while _build_compression_halt_scope runs must
        not raise RuntimeError: dictionary changed size during iteration (pool mutation without
        a dedicated lock during iteration). The snapshot is taken once up front.
        """
        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # exempt
        mate = _make_instance(pool, 'writer', 'writer')          # pool-mate (halted)
        mate._slot_key = SHARED_KEY

        stop = threading.Event()
        errors = []

        def _churn():
            i = 0
            while not stop.is_set():
                try:
                    name = f'churn_{i}'
                    inst = _make_instance(pool, name, 'writer')
                    inst._slot_key = SHARED_KEY
                    pool.instances.pop(name, None)
                except Exception as e:  # noqa: BLE001 - we want to catch the iteration error
                    errors.append(e)
                    break
                i += 1

        churner = threading.Thread(target=_churn, daemon=True)
        churner.start()
        try:
            halted = handler._build_compression_halt_scope(target)
        except RuntimeError as e:
            pytest.fail(f"RuntimeError escaped halt-scope computation (snapshot bug): {e}")
        finally:
            stop.set()
            churner.join(timeout=5)

        # The real pool-mate was still halted; no iteration error escaped.
        assert 'writer' in halted, f"pool-mate must be halted amid churn: {halted}"
        assert not errors, f"churn thread hit an unexpected error: {errors!r}"


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
