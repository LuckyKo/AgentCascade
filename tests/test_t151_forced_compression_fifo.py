"""t151b — forced compression no longer halts ANY sibling; the Compressor is a pure FIFO citizen.

Pre-t151b (the original t151 fix), forced compression called a pool-scoped
``halt_all_instances()`` and halted every non-exempt sibling on the Compressor's resolved
slot pool. t151b removes that halt entirely: the Compressor simply acquires its OWN endpoint
slot through the normal router FIFO (bounded by ``QUEUE_WAIT_TIMEOUT``), so it can never
force a sibling into a save-state → drop-slot → sleep → re-acquire → restore cycle. The
sibling just keeps running; if the Compressor's acquire times out, the handler records the
fail-streak (BUG-7 backoff gate) and reports failure — no halt was ever issued to resume.

Status legend (plan §8.A):
  T1 = forced compression leaves ``pool._compression_halted`` EMPTY (nothing halted).
       Collapses the old "does-not-halt unrelated pool agent" + "manual-halt not resurrected"
       tests: with no halt at all, both properties hold trivially and are asserted in one place.
  KEEP (unmodified) = the two literal todo-151 FIFO guards proving the Compressor acquires
       through the SAME real SlotPool as a normal agent (no bypass) and that a queued-behind-
       sibling acquire is BOUNDED (raises TimeoutError), not an infinite hang.

Tests drive the REAL CompressionHandler against a REAL APIRouter/AgentPool so the slot
resolution is exercised end to end (a MagicMock pool would make _acquire_slot return a
MagicMock and hide any bypass). Run serially: ``pytest -n 0 --timeout=120``.
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
    With t151b's no-halt design this must be empty — nothing is ever compression-halted.
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


class TestForcedCompressionNoHalt:

    def test_forced_compression_halts_nothing(self, tmp_path):
        """T1 — forced compression leaves ``pool._compression_halted`` EMPTY.

        The Compressor acquires its own slot through the router FIFO; it never halts a sibling.
        This collapses the two old tests: (a) an unrelated pool agent is trivially not halted
        because NO ONE is halted, and (b) a manually-halted sibling is preserved by
        resume_all_instances() because compression added nothing to _compression_halted to
        clear. Both invariants hold by construction now — asserted here in one place.

        A manually-halted sibling is included so the "not resurrected" property is exercised:
        it must still be halted after the forced-compression cycle completes (its manual halt
        is untouched, and resume_all_instances() only clears compression halts — of which there
        are none).
        """
        router = _build_router(tmp_path)
        ep_b = _add_par_endpoint(router)
        # writer is assigned to the per-base endpoint → resolves to BASE_B (a different pool);
        # researcher shares the Compressor's shared conc=0 pool. Both must end up untouched.
        router.set_agent_priorities('writer', [ep_b])
        pool = _build_pool(router, tmp_path)
        handler, engine = _make_handler(pool)

        target = _make_instance(pool, 'coder', 'coder')          # compression target (exempt)
        unrelated = _make_instance(pool, 'writer', 'writer')     # different pool (BASE_B)
        mate = _make_instance(pool, 'researcher', 'researcher')  # same shared conc=0 pool

        # Sanity: the two siblings really resolve to DIFFERENT pools than / each other.
        comp_key = router.get_effective_slot_info('Compressor')['slot_key']
        writer_key = router.get_effective_slot_info('writer', instance_name='writer')['slot_key']
        mate_key = router.get_effective_slot_info('researcher', instance_name='researcher')['slot_key']
        assert comp_key == SHARED_KEY and writer_key != comp_key, \
            f"fixture broken: compressor={comp_key} writer={writer_key}"

        # A manual halt on the pool-mate must survive the whole cycle (not resurrected).
        pool.halt_instance('researcher')

        result, halted = _run_forced(handler, target)

        assert result is True
        # THE t151b invariant: nothing was compression-halted at any point during the run.
        assert halted == set(), \
            f"forced compression must halt NOTHING (Compressor is a FIFO citizen); got {halted}"
        # And the pool's compression-halt bookkeeping is empty afterwards.
        assert pool._compression_halted == set(), \
            f"pool._compression_halted must be empty after forced compression: {pool._compression_halted}"
        # The manually-halted sibling was NOT resurrected by resume_all_instances().
        assert 'researcher' in pool._halted_instances, \
            'manual halt must be preserved (resume only clears compression halts)'


class TestCompressorFIFOGuards:

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
        finally:
            _sched_mod.QUEUE_WAIT_TIMEOUT = old_timeout
            released.set()
            holder.join(timeout=5)
            if holder.is_alive():
                pytest.fail('holder thread did not finish (release leaked)')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
