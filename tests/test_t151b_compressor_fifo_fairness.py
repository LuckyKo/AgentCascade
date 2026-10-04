"""t151b — the Compressor is a fair FIFO citizen: it queues behind a sibling and acquires on release.

Pre-t151b, forced compression HALTED every non-exempt pool-mate so the Compressor could grab the
slot instantly. t151b removes that halt: the Compressor simply acquires its OWN endpoint slot
through the normal router FIFO. This test proves the fairness property end to end against a REAL
APIRouter + AgentPool (conc=0 shared sequential slot):

  1. A sibling holds the only permit (background thread — the main thread must never acquire on a
     full conc=0 pool, or it blocks for QUEUE_WAIT_TIMEOUT).
  2. The Compressor's acquire BLOCKS and enqueues as a FIFO ticket BEHIND that holder.
  3. While queued, ``pool._compression_halted`` stays EMPTY — the sibling was NOT halted to make
     room (the whole point of t151b: no halt, just fair waiting).
  4. Releasing the holder grants the Compressor's ticket and its acquire returns.

Run serially: ``pytest -n 0 --timeout=120`` (threading/timing-sensitive; xdist hides hang diagnostics).
"""
import os
os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"t151b_{os.getpid()}")

import threading
import time

import pytest

SHARED_KEY = '_shared_sequential_slot_'
BASE_A = 'http://127.0.0.1:9/v1'    # conc=0 → shared sequential slot


def _build_router(tmp_path):
    """Real APIRouter with a single conc=0 endpoint (shared sequential slot) as the default."""
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


def _build_pool(router, tmp_path):
    """Real AgentPool wired to the real router (real _acquire_slot / halt bookkeeping)."""
    from agent_cascade.agent_pool import AgentPool
    llm_cfg = {'model': 'mock', 'api_base': BASE_A, 'model_server': BASE_A, 'api_key': 'EMPTY'}
    return AgentPool(llm_cfg, agents_dir=str(tmp_path), api_router=router)


class TestCompressorFIFOFairness:

    def test_compressor_queues_behind_sibling_and_acquires_on_release(self, tmp_path):
        """The Compressor queues behind a running sibling (no halt) and acquires when the
        sibling releases — proving it is a fair FIFO citizen, not a halting force."""
        router = _build_router(tmp_path)
        pool = _build_pool(router, tmp_path)

        # Sanity: both the sibling and the Compressor resolve to the SAME shared slot.
        comp_key = router.get_effective_slot_info('Compressor')['slot_key']
        coder_key = router.get_effective_slot_info('coder', instance_name='coder')['slot_key']
        assert comp_key == SHARED_KEY and coder_key == SHARED_KEY, \
            f"Compressor and sibling must share the FIFO pool: {comp_key} vs {coder_key}"

        # 1. Sibling holds the only permit in a background thread (main thread must not acquire).
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

        # 2. Wait until the sibling actually holds the shared permit.
        deadline = time.monotonic() + 5.0
        shared = None
        while time.monotonic() < deadline:
            shared = router.scheduler._pools.get(SHARED_KEY)
            if shared is not None and 'coder' in shared._running:
                break
            time.sleep(0.02)
        assert shared is not None and 'coder' in shared._running, \
            f"sibling never held the shared permit: {getattr(shared, '_running', None)}"

        # 3. Compressor's acquire BLOCKS (pool full) → run it in a background thread and observe
        #    its FIFO ticket from the main thread.
        comp_box = {}

        def _comp_acquire():
            try:
                comp_box['rel'] = pool._acquire_slot('Compressor', 'Compressor_t151b')
            except Exception as e:  # noqa: BLE001 — surfaced by the assertion below
                comp_box['exc'] = e

        comp_thread = threading.Thread(target=_comp_acquire, daemon=True)
        comp_thread.start()

        try:
            # The Compressor is queued (not granted) behind the running sibling.
            deadline = time.monotonic() + 5.0
            queued = False
            while time.monotonic() < deadline:
                if any(t.instance_name == 'Compressor_t151b' for t in shared._waiters.values()):
                    queued = True
                    break
                time.sleep(0.02)
            assert queued, \
                f"Compressor ticket not in FIFO waiters behind the sibling: " \
                f"{[t.instance_name for t in shared._waiters.values()]}"

            # THE t151b invariant: while the Compressor waits its fair turn, NO sibling is halted.
            assert pool._compression_halted == set(), \
                f"Compressor must NOT halt a sibling to grab the slot; got {pool._compression_halted}"

            # 4. Release the sibling → the Compressor's queued ticket is granted and returns.
            released.set()
            holder.join(timeout=5)
            comp_thread.join(timeout=10)
            assert 'exc' not in comp_box, f"Compressor acquire raised: {comp_box.get('exc')!r}"
            assert comp_box.get('rel') is not None, \
                'Compressor must have acquired the slot after the sibling released it'
        finally:
            # Belt-and-suspenders cleanup so a daemon thread never leaks into later tests.
            released.set()
            holder.join(timeout=5)
            if comp_box.get('rel') is not None:
                try:
                    comp_box['rel']()
                except Exception:  # noqa: BLE001 — release may be idempotent/no-op
                    pass


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
