"""COLL-2 ancestor-chain collision tests for call_agent sync/async selection.

Plan: plans/slot-deadlock-fix_PLAN.md §4.1 — decision logic (§3.2) + chain release
(§3.3). These tests are written TESTS-FIRST against the approved plan; they fail on
the pre-fix code (no `_chain_held_slots` / stale key comparison) and pass after the
primary+secondary fix lands in tool_dispatcher.py.

Decision-logic table (§4.1):
  - test_async_when_child_pool_disjoint_from_whole_chain
  - test_sync_when_child_needs_grandparent_pool
  - test_collision_uses_held_slot_key_not_chain_head
  - test_no_collision_when_ancestor_holds_no_permit
  - test_chain_walk_stops_at_dismissed_ancestor
  - test_chain_walk_depth_bounded

Chain-release table (§4.1):
  - test_colliding_ancestor_permit_is_free_when_child_acquires
  - test_ancestor_reacquired_after_sync_child
  - test_ancestor_reacquired_on_child_exception
  - test_ancestor_reacquired_on_child_termination
  - test_no_double_release_on_concurrent_drop
  - test_chain_released_when_caller_holds_no_permit
  - test_no_reacquire_when_caller_held_no_permit

No LLM or network. Uses the same mock-pool harness shape as
test_call_agent_sync_async_selection.py but with explicit `slot_key` /
`parent_instance` on every instance (plan §5.2 — bare auto-Mocks make the chain
walk untestable and non-terminating).
"""

import threading
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.api_router import APIEndpoint, APIRouter
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.tool_dispatcher import ToolDispatcher

SHARED_KEY = '_shared_sequential_slot_'


# ============================================================================
# Fixtures and helpers
# ============================================================================


def _make_instance(instance_name: str,
                   agent_class: str = 'coder',
                   slot_release=None,
                   slot_key: Optional[str] = None,
                   parent_instance: Optional[str] = None,
                   nest_depth: int = 0):
    """Mock AgentInstance with EXPLICIT slot state (plan §5.2).

    `_slot_key` and `parent_instance` are set explicitly — a bare MagicMock would
    auto-create them as truthy stubs, defeating both the held-key read and the
    chain-walk break guard.
    """
    inst = MagicMock()
    inst.instance_name = instance_name
    inst.agent_class = agent_class
    inst._state_lock = threading.RLock()
    inst.state = MagicMock(name=f"{instance_name}_state")
    inst.state.name = 'RUNNING'
    inst._slot_release = slot_release
    inst._slot_key = slot_key
    inst.parent_instance = parent_instance
    inst._nest_depth = nest_depth
    return inst


def _make_pool(router: APIRouter, instances):
    """Mock AgentPool with real router and an explicit instance registry."""
    pool = MagicMock()
    pool.api_router = router
    pool.settings = MagicMock()
    pool.settings.max_nesting_depth = 10
    pool.instances = {inst.instance_name: inst for inst in instances}
    pool.get_instance.side_effect = lambda name: pool.instances.get(name)
    pool.register_async_call = MagicMock()
    pool.instance_conversations = {}
    pool.instance_classes = {}

    def _resolve(name, exclude=None):
        name = name.strip()
        for n in pool.instances:
            if n != exclude and n.lower() == name.lower():
                return n
        return name

    pool._resolve_instance_name.side_effect = _resolve
    return pool


def _create_dispatcher(pool, mock_engine=None):
    """ToolDispatcher with a controllable mocked engine."""
    if mock_engine is None:
        mock_engine = MagicMock()
    dispatcher = ToolDispatcher(pool)
    dispatcher.set_engine(mock_engine)
    return dispatcher, mock_engine


def _build_router(tmp_path_factory):
    """APIRouter with three parallel pools (A/B/C) + the shared conc=0 pool."""
    import os
    from unittest.mock import patch as _patch

    test_config_dir = str(tmp_path_factory.mktemp('ancestor_collision_test'))
    with _patch.dict(os.environ, {'AGENT_CASCADE_TEST_CONFIG_DIR': test_config_dir}):
        r = APIRouter(default_llm_cfg={
            'api_base': 'http://default-api',
            'model': 'default-model',
            'max_tokens': 2048,
        })
        for name in ('a', 'b', 'c'):
            ep = APIEndpoint(
                id=f'ep_{name}',
                name=name.upper(),
                api_base=f'http://api-{name}',
                model=f'model-{name}',
                enabled=True,
                concurrency_limit=3,
            )
            r.add_endpoint(ep)
        # Zero-concurrency endpoint (shared sequential slot)
        zero_ep = APIEndpoint(
            id='ep_zero',
            name='ZeroConcurrency',
            api_base='http://zero-api',
            model='zero-model',
            enabled=True,
            concurrency_limit=0,
        )
        r.add_endpoint(zero_ep)
    return r


@pytest.fixture
def router(tmp_path_factory):
    return _build_router(tmp_path_factory)


# ============================================================================
# Decision logic (§3.2 collision check)
# ============================================================================


class TestAncestorChainDecision:

    def test_async_when_child_pool_disjoint_from_whole_chain(self, router):
        """A(P1) → B(P2) async → C(P3): no chain member holds C's pool → ASYNC (no over-blocking)."""
        a = _make_instance('A', 'class_a', slot_release=lambda: None, slot_key='http://api-a')
        b = _make_instance('B', 'class_b', slot_release=None, parent_instance='A')
        router.set_agent_priorities('class_c', ['ep_c'])

        pool = _make_pool(router, [a, b])
        dispatcher, _ = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_c', 'task': 't'},
            messages=[],
            instance=b,
        )

        pool.register_async_call.assert_called()
        assert 'launched asynchronously' in result.lower(), \
            f"Expected ASYNC for disjoint child pool but got: {result}"

    def test_sync_when_child_needs_grandparent_pool(self, router):
        """A(P1) → B(async, no permit) → C(P1): COLL-2 fires on the grandparent → SYNC."""
        a = _make_instance('A', 'class_a', slot_release=lambda: None, slot_key='http://api-a')
        b = _make_instance('B', 'class_b', slot_release=None, parent_instance='A')
        router.set_agent_priorities('class_a', ['ep_a'])

        pool = _make_pool(router, [a, b])
        dispatcher, _ = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
            messages=[],
            instance=b,
        )

        # SYNC: register_async_call must NOT be called.
        pool.register_async_call.assert_not_called()
        assert 'launched asynchronously' not in result.lower(), \
            f"Expected SYNC (grandparent collision) but got async: {result}"

    def test_collision_uses_held_slot_key_not_chain_head(self, router):
        """Gap 1a regression: child resolves to the caller's HELD key while the chain-head
        re-derivation disagrees → must still be SYNC (held key wins)."""
        # Caller holds 'http://api-a' (cursor rotated away from its chain head 'ep_b').
        b = _make_instance('B', 'class_b', slot_release=lambda: None, slot_key='http://api-a')
        router.set_agent_priorities('class_b', ['ep_b'])   # chain head → http://api-b
        router.set_agent_priorities('class_a', ['ep_a'])   # child resolves to held key

        pool = _make_pool(router, [b])
        dispatcher, _ = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
            messages=[],
            instance=b,
        )

        pool.register_async_call.assert_not_called()
        assert 'launched asynchronously' not in result.lower(), \
            f"Expected SYNC (held-key collision) but got async: {result}"

    def test_no_collision_when_ancestor_holds_no_permit(self, router):
        """Ancestor with _slot_release=None is not a collision source → ASYNC."""
        a = _make_instance('A', 'class_a', slot_release=None, slot_key=None)
        b = _make_instance('B', 'class_b', slot_release=lambda: None,
                           slot_key='http://api-b', parent_instance='A')
        router.set_agent_priorities('class_c', ['ep_c'])

        pool = _make_pool(router, [a, b])
        dispatcher, _ = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_c', 'task': 't'},
            messages=[],
            instance=b,
        )

        pool.register_async_call.assert_called()
        assert 'launched asynchronously' in result.lower(), \
            f"Expected ASYNC (ancestor holds no permit) but got: {result}"

    def test_chain_walk_stops_at_dismissed_ancestor(self, router):
        """parent_instance names a dead (absent) instance → no exception, no phantom collision."""
        b = _make_instance('B', 'class_b', slot_release=None, parent_instance='GONE')
        router.set_agent_priorities('class_c', ['ep_c'])

        pool = _make_pool(router, [b])  # 'GONE' is not in the registry
        dispatcher, _ = _create_dispatcher(pool)

        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_c', 'task': 't'},
            messages=[],
            instance=b,
        )

        pool.register_async_call.assert_called()
        assert 'launched asynchronously' in result.lower(), \
            f"Expected ASYNC (dead ancestor) but got: {result}"

    def test_chain_walk_depth_bounded(self, router):
        """A cycle in parent_instance links must terminate at AGENT_MAX_NESTING_DEPTH."""
        x = _make_instance('X', 'class_a', slot_release=lambda: None, slot_key='http://api-a')
        y = _make_instance('Y', 'class_b', slot_release=lambda: None, slot_key='http://api-b')
        x.parent_instance = 'Y'  # X ↔ Y cycle
        y.parent_instance = 'X'

        router.set_agent_priorities('class_c', ['ep_c'])

        pool = _make_pool(router, [x, y])
        dispatcher, _ = _create_dispatcher(pool)

        # Must return (bounded walk), not loop forever.
        result = dispatcher.handle_call_agent(
            args={'instance_name': 'C', 'agent_class': 'class_c', 'task': 't'},
            messages=[],
            instance=x,
        )

        pool.register_async_call.assert_called()


# ============================================================================
# Chain release (§3.3 release + reacquire)
# ============================================================================


def _grandparent_collision_setup(router):
    """A holds P1; B (caller) holds nothing; child C needs P1.

    Returns (a, b, pool, dispatcher, mock_engine). The collision is with the
    GRANDPARENT — the direct caller releases nothing.
    """
    a = _make_instance('A', 'class_a', slot_release=lambda: None, slot_key='http://api-a')
    b = _make_instance('B', 'class_b', slot_release=None, parent_instance='A')
    router.set_agent_priorities('class_a', ['ep_a'])

    pool = _make_pool(router, [a, b])
    dispatcher, mock_engine = _create_dispatcher(pool)
    return a, b, pool, dispatcher, mock_engine


class TestAncestorChainRelease:

    def test_colliding_ancestor_permit_is_free_when_child_acquires(self, router):
        """Structural-impossibility assertion: at the child's _acquire_slot, no running
        holder on the pool belongs to the caller's chain (the grandparent permit is free).

        Driven end-to-end through handle_call_agent → _run_child_sync → run_child_core
        with a REAL SlotPool. The child's engine.run() acquires via
        pool._acquire_slot → scheduler.acquire → SlotPool.acquire, so the fake engine
        performs that exact acquisition and we snapshot the real pool's holders at that
        moment (the §3.3 invariant)."""
        from agent_cascade.slot_queue import SlotPool

        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)

        # Real permit for A on the real pool (what A "holds" in this scenario).
        real_pool = router.scheduler._get_or_create_pool('http://api-a', 3)
        assert real_pool is not None
        real_release = real_pool.acquire(instance_name='A', agent_class='class_a')
        assert real_release is not None
        with a._state_lock:
            a._slot_release = real_release

        acquire_seen_holders = []

        def fake_acquire(self, instance_name, agent_class='unknown', timeout=None):
            """Real grant (so the child holds a permit while it "runs") + snapshot.

            The snapshot is taken AFTER granting the child so it reflects the pool state
            at the moment the child would start executing — the §3.3 invariant asserts no
            chain member (the grandparent) is still a running holder then.
            """
            with self._cond:
                from agent_cascade.slot_queue import _grant, _make_release_cb
                holder = _grant(self, instance_name, agent_class)
                acquire_seen_holders.append(list(self._running.keys()))
            return _make_release_cb(self, holder)

        def fake_run_child_core(engine, pool, agent_class, instance_name, args,
                                caller_name, child_depth, **kwargs):
            # Child's engine.run() slot acquisition — the exact real path. The `pool` passed
            # here is the MOCK AgentPool (its _acquire_slot is a no-op MagicMock), so we must
            # acquire through the REAL scheduler — the same entry point the actual engine uses:
            # router.scheduler.acquire → SlotPool.acquire (patched to fake_acquire above). This
            # snapshots the real pool's holders at precisely the moment the child acquires.
            info = router.get_agent_slot_info(agent_class)
            cb = router.scheduler.acquire(
                info['api_base'], info['concurrency_limit'], instance_name, agent_class)
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
            return 'ok'

        # Patch run_child_core in the tool_dispatcher namespace — it is imported at module level,
        # so patching child_runner's module attr would NOT intercept the dispatcher's reference.
        with patch.object(SlotPool, 'acquire', fake_acquire), \
             patch('agent_cascade.tool_dispatcher.run_child_core', side_effect=fake_run_child_core):
            dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        pool.register_async_call.assert_not_called()
        assert acquire_seen_holders, "child's engine.run() never reached a slot acquire"
        for snapshot in acquire_seen_holders:
            assert 'A' not in snapshot, \
                f"grandparent A still held the real pool when the child acquired: {snapshot}"

        # Cleanup: release any permit left behind so no leak escapes the test.
        with a._state_lock:
            if a._slot_release is not None:
                a._slot_release()
                a._slot_release = None

    def test_ancestor_reacquired_after_sync_child(self, router):
        """finally block re-acquires the released ancestor's slot."""
        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)
        with patch('agent_cascade.slot_queue.SlotPool.acquire', return_value=None):
            dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        reacquires = [c.args for c in mock_engine.reacquire_for.call_args_list]
        assert any(arg[0] is a and arg[1] == 'A' for arg in reacquires), \
            f"ancestor A was not re-acquired; reacquire calls: {reacquires}"

    def test_ancestor_reacquired_on_child_exception(self, router):
        """Same on the generic except path."""
        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)
        with patch('agent_cascade.slot_queue.SlotPool.acquire', return_value=None), \
             patch('agent_cascade.tool_dispatcher.run_child_core', side_effect=RuntimeError('boom')):
            result = dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        assert 'Failed' in result
        reacquires = [c.args for c in mock_engine.reacquire_for.call_args_list]
        assert any(arg[0] is a and arg[1] == 'A' for arg in reacquires), \
            f"ancestor A was not re-acquired after child exception: {reacquires}"

    def test_ancestor_reacquired_on_child_termination(self, router):
        """Same on the AgentTerminatedError path."""
        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)
        with patch('agent_cascade.slot_queue.SlotPool.acquire', return_value=None), \
             patch('agent_cascade.tool_dispatcher.run_child_core',
                    side_effect=AgentTerminatedError(instance_name='C')):
            result = dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        assert 'Terminated' in result
        reacquires = [c.args for c in mock_engine.reacquire_for.call_args_list]
        assert any(arg[0] is a and arg[1] == 'A' for arg in reacquires), \
            f"ancestor A was not re-acquired after child termination: {reacquires}"

    def test_no_double_release_on_concurrent_drop(self, router):
        """Two racing releases of the same ancestor permit → callback fires exactly once."""
        from agent_cascade.slot_queue import release_slot_permit

        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)

        fired = []
        real_release = a._slot_release

        def counting_release():
            fired.append(1)
            real_release()

        with a._state_lock:
            a._slot_release = counting_release

        barrier = threading.Barrier(2, timeout=5)

        def racer():
            barrier.wait()
            release_slot_permit(a, 'A', action='drop-handoff', context='race test')

        t1 = threading.Thread(target=racer)
        t2 = threading.Thread(target=racer)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not t1.is_alive() and not t2.is_alive(), 'release racers deadlocked'

        assert len(fired) == 1, f"permit callback fired {len(fired)} times, expected exactly 1"

    def test_chain_released_when_caller_holds_no_permit(self, router):
        """§3.2(c-1) regression: caller holds nothing, GRANDPARENT holds the colliding pool →
        the grandparent permit is still released (unguarded chain release)."""
        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)

        fired = []
        real_release = a._slot_release

        def counting_release():
            fired.append(1)
            real_release()

        with a._state_lock:
            a._slot_release = counting_release

        with patch('agent_cascade.slot_queue.SlotPool.acquire', return_value=None):
            dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        assert len(fired) == 1, \
            f"grandparent permit released {len(fired)} times, expected exactly 1 " \
            '(unguarded chain release — the caller_released guard must NOT gate it)'

    def test_no_reacquire_when_caller_held_no_permit(self, router):
        """§3.2(c-1): the CALLER's own reacquire is skipped when it never released → no permit leak."""
        a, b, pool, dispatcher, mock_engine = _grandparent_collision_setup(router)
        with patch('agent_cascade.slot_queue.SlotPool.acquire', return_value=None):
            dispatcher.handle_call_agent(
                args={'instance_name': 'C', 'agent_class': 'class_a', 'task': 't'},
                messages=[],
                instance=b,
            )

        reacquires = [c.args for c in mock_engine.reacquire_for.call_args_list]
        assert not any(arg[0] is b and arg[1] == 'B' for arg in reacquires), \
            f"caller B re-acquired a permit it never released (leak): {reacquires}"
