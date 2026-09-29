"""Lock-ordering liveness tests for the COLL-2 ancestor-chain fix (plan §4.2).

Kept SEPARATE from test_call_agent_ancestor_collision.py on purpose: these are the
bounded-join liveness tests, easy to find and run in isolation when diagnosing a
suspected lock inversion (contract: self._lock → instance._state_lock, never inverted;
see .agent_lessons/sticky-slot-incident-fix-chain.md §Lock Ordering Contract).

Deliberately NO lock-release-timing assertion — deadlock is a liveness property, so it
is verified by concurrent execution completing within a bounded join (established
convention in test_edge_cases_sticky_slot.py: every hangable thread uses
join(timeout=...) + an explicit assert that the thread terminated).

  - test_concurrent_collision_check_and_sticky_sync_do_not_deadlock
  - test_concurrent_chain_release_and_wakeup_reacquire_do_not_deadlock
"""

import os as _os

_os.environ.setdefault('AGENT_CASCADE_INSTANCE_ID', f"lockorder_{_os.getpid()}")

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

SHARED_KEY = '_shared_sequential_slot_'
SEQ_BASE = 'http://127.0.0.1:9/v1'  # conc=0 endpoint (shared sequential slot)


# ── Real infra (copied from test_edge_cases_sticky_slot.py so the real router lock is exercised) ──

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


def _build_pool(router):
    """Real AgentPool wired to the real router."""
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
    pool.instances[name] = inst
    return inst


@pytest.fixture
def lockorder_harness(tmp_path, request):
    """Real router (conc=0 endpoint) + real pool; shortened timeouts for bounded runs."""
    import agent_cascade.slot_queue as _sq_mod
    import agent_cascade.api_router_pkg.scheduler as _ar_mod

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

    yield {'router': router, 'pool': pool}

    _sq_mod.QUEUE_WAIT_TIMEOUT = old_sq
    _ar_mod.QUEUE_WAIT_TIMEOUT = old_ar
    if old_cfg_dir is None:
        _os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
    else:
        _os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = old_cfg_dir


class TestLockOrderingLiveness:

    def test_concurrent_collision_check_and_sticky_sync_do_not_deadlock(self, lockorder_harness):
        """Run the COLL-2 chain walk (_chain_held_slots) and sync_sticky_slot concurrently
        against the same instance. If the lock order were inverted (state_lock held across
        router._lock in a path taken oppositely by another thread), one of these would never
        finish and the bounded join assert fires."""
        router = lockorder_harness['router']
        pool = lockorder_harness['pool']

        inst = _make_instance(pool, 'L1')
        # Give it a live permit on the shared pool so both paths take their real branches.
        release_cb = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0, instance_name='L1', agent_class='coder')
        with inst._state_lock:
            inst._slot_release = release_cb
            inst._slot_key = SHARED_KEY

        from agent_cascade.tool_dispatcher import ToolDispatcher
        dispatcher = ToolDispatcher(pool)

        errors = []
        done_walk = threading.Event()
        done_sticky = threading.Event()

        def walk_thread():
            try:
                for _ in range(20):
                    dispatcher._chain_held_slots(inst)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(f'walk: {e!r}')
            finally:
                done_walk.set()

        def sticky_thread():
            try:
                for _ in range(20):
                    router.sync_sticky_slot(inst, origin='lockorder-test')
            except Exception as e:  # pragma: no cover - failure path
                errors.append(f'sticky: {e!r}')
            finally:
                done_sticky.set()

        t1 = threading.Thread(target=walk_thread)
        t2 = threading.Thread(target=sticky_thread)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive(), 'chain-walk thread did not finish within 10s (lock inversion?)'
        assert not t2.is_alive(), 'sticky-sync thread did not finish within 10s (lock inversion?)'
        assert done_walk.is_set() and done_sticky.is_set()
        assert not errors, f"concurrent execution raised: {errors}"

    def test_concurrent_chain_release_and_wakeup_reacquire_do_not_deadlock(self, lockorder_harness):
        """Race _release_chain_collision (§3.3) against engine.reacquire_for on the same
        ancestor permit. Covers the double-release/idempotency contract (slot_queue.py)."""
        router = lockorder_harness['router']
        pool = lockorder_harness['pool']

        ancestor = _make_instance(pool, 'L2')
        release_cb = router.scheduler.acquire(
            api_base=SEQ_BASE, concurrency_limit=0, instance_name='L2', agent_class='coder')
        with ancestor._state_lock:
            ancestor._slot_release = release_cb
            ancestor._slot_key = SHARED_KEY

        from agent_cascade.engine.core import ExecutionEngine
        mock_pool = MagicMock()
        mock_router = MagicMock()
        mock_router.scheduler = router.scheduler
        slot_info = {'slot_key': SHARED_KEY, 'is_sequential': True,
                     'concurrency_limit': 0, 'api_base': SEQ_BASE, 'needs_slot': True}
        mock_router.get_effective_slot_info.return_value = slot_info
        mock_pool.api_router = mock_router
        engine = ExecutionEngine(mock_pool)

        from agent_cascade.tool_dispatcher import ToolDispatcher
        dispatcher = ToolDispatcher(pool)

        errors = []
        done_release = threading.Event()
        done_reacquire = threading.Event()

        def release_thread():
            try:
                chain = [(ancestor, SHARED_KEY)]
                for _ in range(10):
                    # caller is a distinct instance so the ancestor is not skipped.
                    dispatcher._release_chain_collision(chain, _make_instance(pool, 'L2c'), SHARED_KEY)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(f'release: {e!r}')
            finally:
                done_release.set()

        def reacquire_thread():
            try:
                for _ in range(10):
                    engine.reacquire_for(ancestor, 'L2', 'lockorder-test')
            except Exception as e:  # pragma: no cover - failure path
                errors.append(f'reacquire: {e!r}')
            finally:
                done_reacquire.set()

        t1 = threading.Thread(target=release_thread)
        t2 = threading.Thread(target=reacquire_thread)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive(), 'chain-release thread did not finish within 10s (lock inversion?)'
        assert not t2.is_alive(), 'reacquire thread did not finish within 10s (lock inversion?)'
        assert done_release.is_set() and done_reacquire.is_set()
        assert not errors, f"concurrent execution raised: {errors}"
