"""Regression tests for todo.md #132.

Bug: force-terminating an async sub-agent orphans the parent's pending handle in
``AsyncToolRegistry``. After ``dismiss_instance(child)`` the parent still holds a
``BackgroundToolEntry(completed=False, child_instance_name=child)`` in
``_pending[parent]``, so ``pool.has_pending(parent)`` stays True and
``_transition_to_sleeping_if_pending`` (engine/core.py:3044) puts the parent to SLEEPING
after every subsequent natural end of its turn.

The fix (see plans/t132_force_terminate_async_parent_sleep_PLAN.md §4) adds
``AsyncToolRegistry.abandon_child()`` which atomically resolves the pending handle,
removes the child->parent mapping, and reports who to notify; ``dismiss_instance``
uses it in place of the old SLEEPING/IDLE-gated ``get_parent_for_child`` +
``remove_child_mapping`` pair.

These tests exercise the REAL ``AsyncToolRegistry`` and the REAL
``LifecycleMixin.dismiss_instance`` (plus, for T2/T3, the real engine sleep decision),
so they fail on pre-fix code and pass after.

Test map (see plan §6):
    T1  force-terminate leaves no pending handle on the parent   (FAILS before fix)
    T2  engine: parent does not sleep after natural end post-terminate (FAILS before fix)
    T3  control: a genuinely-pending live child still puts parent to SLEEPING (PASSES both)
    T4  sibling async children are unaffected by dismissing one
    T5  double dismiss is idempotent (exactly one message, no exception)
    T6  terminate while parent is RUNNING still enqueues a wakeup (FAILS before fix)
    T7  mapping-less registration (function_id=None) still resolves (FAILS before fix)
    T8  no duplicate stale result when the abandoned worker unwinds late
    T9  worker finishes FIRST then dismiss → exactly one message, not a "Dismissed" (F1 ordering a)
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState


# ---------------------------------------------------------------------------
# Fixture: build a minimal real AgentPool without hitting the filesystem.
# Mirrors the fixture in tests/test_dismiss_termination.py so that the REAL
# LifecycleMixin.dismiss_instance and AsyncToolRegistry run end-to-end.
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_pool():
    """Create an AgentPool with mocked dependencies so it can be instantiated."""
    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector') as mock_telem, \
         patch('agent_cascade.api_router.APIRouter') as mock_router:  # noqa: F841

        op_mgr = MagicMock()
        op_mgr.base_dir = MagicMock()
        op_mgr.base_dir.__str__ = lambda self: '/tmp/test_workspace'
        op_mgr.extra_work_folders_ro = []
        op_mgr.extra_work_folders_rw = []
        mock_op_mgr.return_value = op_mgr

        router = MagicMock()
        router.get_effective_concurrency.return_value = 3
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg={'max_parallel_agents': 2},
            agents_dir='/tmp/fake_agents',
            workspace_dir='/tmp/test_workspace',
        )
        if hasattr(pool, 'settings'):
            pool.settings.idle_timeout_seconds = 60.0
            pool.settings.idle_check_interval = 30.0
        pool.start()
        pool._idle.stop()
        yield pool
        # Best-effort cleanup so the executor threads do not outlive the test.
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass


def _make_instance(name, state=AgentState.RUNNING, parent=None):
    """A minimal AgentInstance for engine-level tests (mirrors test_post_turn_sleeping_order)."""
    inst = AgentInstance(
        instance_name=name,
        agent_class='Orchestrator',
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
    )
    inst.state = state
    return inst


def _count_dismissed(messages):
    """Count messages that are the dismissal-result marker for a child."""
    return sum('Dismissed' in m for m in messages)


# ============================================================================
# T1 — the failing regression test (FAILS before, PASSES after)
# ============================================================================

def test_force_terminate_async_child_leaves_no_pending_handle_on_parent(agent_pool):
    """After dismiss_instance(child), the parent must have NO unresolved pending handle.

    Uses the real AsyncToolRegistry and the real LifecycleMixin.dismiss_instance.
    The child's worker is blocked on an Event so it never finishes — future.cancel()
    is a no-op once started, exactly like the un-interruptible run_child_core thread.
    """
    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id='call_1',
                 child_instance_name='Child')

    agent_pool.create_instance('Parent', 'Orchestrator')
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    # Patch the relaunch thread factory so no real engine thread spawns and races to
    # drain the dismissal message from the parent's queue (the IDLE-parent relaunch path
    # in dismiss_instance). We assert on the queued message directly, not on a live run.
    with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
        agent_pool.dismiss_instance('Child')          # the force-terminate path

    # THE INVARIANT: the parent has no unresolved pending handle left.
    assert agent_pool.has_pending('Parent') is False, \
        'orphaned BackgroundToolEntry keeps has_pending() True → parent sleeps at every natural end'
    # And the parent was told exactly once.
    msgs = agent_pool.drain_queue('Parent')
    assert _count_dismissed(msgs) == 1
    blocker.set()


# ============================================================================
# T3 — control test (passes BEFORE and AFTER — proves the harness is live)
# ============================================================================

def test_control_parent_with_live_async_child_still_sleeps(agent_pool):
    """A parent with a genuinely pending, non-dismissed child must still sleep.

    Without this, T1/T2 could pass trivially because has_pending is stubbed out.
    Drives the REAL _transition_to_sleeping_if_pending (engine/core.py:2941).
    """
    from agent_cascade.execution_engine import ExecutionEngine

    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id='c1', child_instance_name='Child')

    inst = _make_instance('Parent', state=AgentState.RUNNING)
    engine = ExecutionEngine(agent_pool)
    engine._my_generation = 1

    try:
        assert engine._transition_to_sleeping_if_pending(inst, 'Parent') is True
        assert inst.state == AgentState.SLEEPING
    finally:
        blocker.set()


# ============================================================================
# T2 — engine-level, proves the symptom is gone (FAILS before, PASSES after)
# ============================================================================

def test_parent_does_not_sleep_after_natural_end_post_terminate(agent_pool):
    """After dismiss_instance(child), the real _transition_to_sleeping_if_pending must
    NOT put the parent to SLEEPING — mirroring test_post_turn_sleeping_order's harness.

    Pre-fix: has_pending('Parent') is True (orphan) → returns True and state=SLEEPING.
    Post-fix: no pending handle → returns False, state unchanged.
    """
    from agent_cascade.execution_engine import ExecutionEngine

    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id='call_1',
                 child_instance_name='Child')

    agent_pool.create_instance('Parent', 'Orchestrator')
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')
    agent_pool.dismiss_instance('Child')

    inst = _make_instance('Parent', state=AgentState.RUNNING)
    engine = ExecutionEngine(agent_pool)
    engine._my_generation = 1
    try:
        assert engine._transition_to_sleeping_if_pending(inst, 'Parent') is False
        assert inst.state != AgentState.SLEEPING
    finally:
        blocker.set()


# ============================================================================
# T4 — sibling async children are unaffected (multiple async children)
# ============================================================================

def test_sibling_children_unaffected_when_one_dismissed(agent_pool):
    """Dismissing one of two async children must leave the parent pending for the other.

    abandon_child filters by child_instance_name, so only the dismissed child's handle
    resolves; the sibling entry stays uncompleted → has_pending('Parent') is still True.

    The parent is set to RUNNING (mirror T6) to make this REVERT-PROOF: pre-fix code gated
    the dismissal enqueue on the parent being SLEEPING/IDLE, so a RUNNING parent got NO
    message at all AND ChildA's handle stayed orphaned. Post-fix the enqueue is unconditional
    (gated only on resolved=True) and abandon_child clears exactly ChildA's handle.
    """
    reg = agent_pool._async_registry
    blocker_a = threading.Event()
    blocker_b = threading.Event()
    reg.register('Parent', lambda: blocker_a.wait(30), function_id='call_A',
                 child_instance_name='ChildA')
    reg.register('Parent', lambda: blocker_b.wait(30), function_id='call_B',
                 child_instance_name='ChildB')

    parent = agent_pool.create_instance('Parent', 'Orchestrator')
    with parent._state_lock:
        parent.state = AgentState.RUNNING          # mid-turn at the teardown instant
    agent_pool.create_instance('ChildA', 'researcher', parent_instance='Parent')
    agent_pool.create_instance('ChildB', 'researcher', parent_instance='Parent')

    try:
        # Patch the relaunch thread factory so no real engine thread races to drain the
        # parent's queue (see T1). We assert on the queued message directly.
        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
            agent_pool.dismiss_instance('ChildA')

        # The sibling (ChildB) is still live → parent must stay pending.
        assert agent_pool.has_pending('Parent') is True, \
            "dismissing one child must not clear the sibling's pending handle"
        # Exactly one dismissal message (for ChildA), none for ChildB. Pre-fix a RUNNING
        # parent would get zero here → this assertion is what makes T4 fail pre-fix.
        msgs = agent_pool.drain_queue('Parent')
        assert _count_dismissed(msgs) == 1, \
            'RUNNING parent must still be told exactly that ChildA was dismissed'
    finally:
        blocker_a.set()
        blocker_b.set()


# ============================================================================
# T5 — double dismiss is idempotent
# ============================================================================

def test_double_dismiss_is_idempotent(agent_pool):
    """Dismissing the same child twice must enqueue exactly one message and not raise."""
    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id='call_1',
                 child_instance_name='Child')

    agent_pool.create_instance('Parent', 'Orchestrator')
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    try:
        # Patch the relaunch thread factory so no real engine thread races to drain the
        # parent's queue (see T1). We assert on the queued message directly.
        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
            agent_pool.dismiss_instance('Child')
            # Second dismiss: the instance is already gone; must be a safe no-op.
            agent_pool.dismiss_instance('Child')

        assert agent_pool.has_pending('Parent') is False
        msgs = agent_pool.drain_queue('Parent')
        assert _count_dismissed(msgs) == 1, 'double dismiss must not enqueue a second message'
    finally:
        blocker.set()


# ============================================================================
# T6 — terminate while parent is RUNNING (failure path A; FAILS before, PASSES after)
# ============================================================================

def test_terminate_while_parent_running_still_enqueues_wakeup(agent_pool):
    """A RUNNING parent at teardown must still receive the dismissal message.

    The old code gated the enqueue on parent being SLEEPING/IDLE, so a RUNNING parent
    got no wakeup and then slept forever with an empty queue. Post-fix the enqueue is
    unconditional (a RUNNING parent drains its queue at end-of-turn).
    """
    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id='call_1',
                 child_instance_name='Child')

    parent = agent_pool.create_instance('Parent', 'Orchestrator')
    with parent._state_lock:
        parent.state = AgentState.RUNNING          # mid-turn at the teardown instant
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    try:
        # Patch the relaunch thread factory so no real engine thread is spawned.
        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
            agent_pool.dismiss_instance('Child')

        msgs = agent_pool.drain_queue('Parent')
        assert _count_dismissed(msgs) == 1, \
            'RUNNING parent must still be told the child was dismissed'
    finally:
        blocker.set()


# ============================================================================
# T7 — mapping-less registration (failure path B; FAILS before, PASSES after)
# ============================================================================

def test_mapping_less_registration_still_resolves(agent_pool):
    """A tool call with function_id=None must still resolve on dismissal.

    Pre-fix: register() only stored the _child_to_parent mapping when function_id was truthy,
    so a missing id meant no wakeup and the pending handle stayed orphaned (permanent sleep).
    Post-fix: the mapping is stored unconditionally and abandon_child resolves it.
    """
    reg = agent_pool._async_registry
    blocker = threading.Event()
    reg.register('Parent', lambda: blocker.wait(30), function_id=None,
                 child_instance_name='Child')

    agent_pool.create_instance('Parent', 'Orchestrator')
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    try:
        with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
            agent_pool.dismiss_instance('Child')

        assert agent_pool.has_pending('Parent') is False, \
            'mapping-less registration must still resolve the pending handle'
        msgs = agent_pool.drain_queue('Parent')
        assert _count_dismissed(msgs) == 1
    finally:
        blocker.set()


# ============================================================================
# T8 — no duplicate stale result when the abandoned worker unwinds late
# ============================================================================

def test_no_duplicate_result_when_abandoned_worker_unwinds_late(agent_pool):
    """When the abandoned child's worker finally unwinds, it must NOT enqueue a second result.

    Exercises the entry.abandoned guard in _execute's finally: after abandon_child() marks
    the entry abandoned, the late-unwinding worker sets completed=True but returns before
    delivering a stale "[Background Tool Result]" / error message or triggering an idle relaunch.
    """
    reg = agent_pool._async_registry
    blocker = threading.Event()

    def tool():
        blocker.wait(30)
        return 'LATE RESULT'          # must never be delivered to the parent

    entry = reg.register('Parent', tool, function_id='call_1', child_instance_name='Child')

    parent = agent_pool.create_instance('Parent', 'Orchestrator')
    with parent._state_lock:
        # RUNNING (not the default IDLE) so relaunch_idle_agent() returns False early and no
        # real engine thread spawns to drain the queue during dismiss's join window. This lets
        # us assert on the queued message deterministically WITHOUT patching the factory, which
        # matters here because the worker must still unwind AFTER dismiss (so we can't hold a
        # Thread-patch across it). A RUNNING parent drains its own queue at end-of-turn anyway.
        parent.state = AgentState.RUNNING
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    # Dismiss while the worker is blocked → entry becomes abandoned.
    agent_pool.dismiss_instance('Child')
    assert entry.abandoned is True, 'abandon_child must mark the matching entry abandoned'

    # Release the worker so it unwinds late, then wait for its future to finish.
    blocker.set()
    if entry.future is not None:
        entry.future.result(timeout=15)   # blocks until _execute's finally has run

    msgs = agent_pool.drain_queue('Parent')
    assert 'LATE RESULT' not in ' '.join(msgs), \
        'abandoned worker must not deliver a stale duplicate result'
    assert _count_dismissed(msgs) == 1, 'exactly one dismissal message, no second result'


# ============================================================================
# T9 — exactly-one-message invariant: worker finishes FIRST, then dismiss (F1 ordering a)
# ============================================================================

def test_worker_finishes_first_then_dismiss_sends_no_duplicate(agent_pool):
    """If the child's worker already delivered its REAL result before teardown, dismissing
    the child must NOT send a second "[Agent ... Dismissed]" message on top of it.

    This is F1 ordering (a): worker-finishes-first. abandon_child() finds no LIVE entry
    (the entry is already completed) → resolved=False → dismiss_instance enqueues nothing.
    The parent therefore has exactly ONE terminal message total: the real result, and it is
    NOT a "Dismissed" marker.

    Pre-fix / pre-F1 code enqueued the dismissal unconditionally whenever a parent mapping
    existed, so the parent would end up with BOTH the real result AND a "Dismissed" = two
    messages (the double-delivery bug).
    """
    reg = agent_pool._async_registry
    entry = reg.register('Parent', lambda: 'REAL RESULT', function_id='call_1',
                         child_instance_name='Child')

    parent = agent_pool.create_instance('Parent', 'Orchestrator')
    with parent._state_lock:
        parent.state = AgentState.RUNNING          # RUNNING so a naive unconditional enqueue would fire
    agent_pool.create_instance('Child', 'researcher', parent_instance='Parent')

    # Let the worker finish and deliver its real result BEFORE we dismiss.
    if entry.future is not None:
        entry.future.result(timeout=15)            # blocks until _execute's finally has run
    assert entry.completed is True, 'worker must have completed before dismissal'

    # Now dismiss the (already-finished) child.
    with patch('agent_cascade.utils.wakeup_helpers.threading.Thread'):
        agent_pool.dismiss_instance('Child')

    msgs = agent_pool.drain_queue('Parent')
    # Exactly ONE terminal message total, and it is the real result — NOT a "Dismissed".
    assert len(msgs) == 1, \
        f"parent must have exactly one terminal message, got {len(msgs)}: {msgs!r}"
    assert _count_dismissed(msgs) == 0, \
        "worker already delivered its real result → no redundant 'Dismissed' message allowed"
    assert any('REAL RESULT' in m for m in msgs), \
        "the single message must be the worker's real result"
