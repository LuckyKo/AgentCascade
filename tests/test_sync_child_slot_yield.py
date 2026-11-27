"""BUG_0430 — sync-child slot yield + advisor acquire-window bound.

Proves the two complementary fixes:

  (b) ``ToolDispatcher._run_child_sync`` routes the caller-slot release through the
      shared ``slot_yield_utils.yield_caller_slot`` three-path helper and gates the
      ``finally`` re-acquire on whether a yield actually happened, so a phantom/leaked
      permit (callback cleared but pool still lists the holder) is force-released and the
      ``finally`` never self-deadlocks.

  (a) The lightweight advisor binds its INITIAL slot acquire strictly below
      ``holder_dms_window_s`` so it fails fast instead of converging with the
      holder-DMS deadline, and ``pool/slots._acquire_slot`` honors that override as a
      plain fixed wait (holder activity must not slide it).

No LLM, no network. Reverts to a failure if the corresponding production guard is
removed. ``run_child_core`` is patched so the child "runs" without an engine; the slot
machinery (``yield_caller_slot``) is the real production code under test.
"""

import inspect
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.advisor_runner import (
    _ADVISOR_ACQUIRE_DMS_FRACTION,
    _compute_advisor_acquire_timeout,
    run_lightweight_advisor,
)
from agent_cascade.pool.slots import SlotsMixin
from agent_cascade.settings import HOLDER_DMS_WINDOW_S
from agent_cascade.slot_queue import QUEUE_WAIT_TIMEOUT

from tests.slot_test_helpers import create_dispatcher, make_mock_instance

# Test window kept equal to the production DMS window so the clamp assertion is meaningful.
_WINDOW = float(QUEUE_WAIT_TIMEOUT)
_CALLER = 'test_0430_caller'
_CHILD = 'test_0430_child'


def _fake_router(sched_pool):
    """Router satisfying yield_caller_slot's pool inspection (Path 2 / describe_holders)."""
    router = MagicMock()
    router.get_agent_slot_info.return_value = {
        'needs_slot': True, 'api_base': 'shared', 'concurrency_limit': 0,
    }
    router.get_effective_slot_info.return_value = {
        'api_base': 'shared', 'concurrency_limit': 0, 'endpoint_name': '', 'model': '',
    }
    router.scheduler._get_or_create_pool.return_value = sched_pool
    return router


def _saturated_sched_pool(holder_key, holder_inst):
    """A fake scheduling pool whose ``_running`` already lists ``holder_key``."""
    sched_pool = MagicMock()
    running = {holder_key: holder_inst}
    sched_pool._running = running

    def _release(holder):
        running.pop(getattr(holder, 'instance_name', holder_key), None)

    sched_pool.release.side_effect = _release
    return sched_pool


class TestSyncChildYield:
    """Fix (b): the sync child must actually yield the caller's slot via the helper."""

    def _dispatcher(self, caller, sched_pool):
        router = _fake_router(sched_pool)
        pool = MagicMock()
        pool.api_router = router
        pool.get_instance.return_value = None  # child not registered; skip child-state save
        pool._mark_activity = MagicMock()
        dispatcher, engine = create_dispatcher(pool, MagicMock())
        return dispatcher, engine, pool

    def test_live_release_yields_child_acquires_finally_reacquires(self):
        """Path 1: caller holds a live ``_slot_release`` → the helper releases it
        (``engine._release_slot``), the child proceeds, and the ``finally`` re-acquires."""
        released = []
        caller = make_mock_instance(_CALLER, slot_release=lambda: released.append('rel'))
        sched_pool = _saturated_sched_pool(_CALLER, MagicMock(instance_name=_CALLER))
        dispatcher, engine, _ = self._dispatcher(caller, sched_pool)

        with patch('agent_cascade.child_runner.run_child_core', return_value='child-ok') as run:
            result = dispatcher._run_child_sync(
                'coder', _CHILD, {}, caller, _CALLER, child_depth=1, is_last_ca=True)

        assert result == 'child-ok'
        run.assert_called_once()  # child proceeded (slot was freed for it)
        # Path 1 released the live permit via the engine, not a direct pool.release.
        engine._release_slot.assert_called_once()
        engine.reacquire_after_slot_yield.assert_called_once()  # yielded → finally re-acquired

    def test_leaked_permit_force_released_no_self_deadlock(self):
        """Path 2: ``_slot_release`` is None but the pool STILL lists the caller as a
        holder → the helper force-releases the phantom permit and the ``finally`` still
        re-acquires (``_yielded`` True) WITHOUT re-entering the held permit."""
        caller = make_mock_instance(_CALLER, slot_release=None)  # callback cleared
        phantom = MagicMock(instance_name=_CALLER)
        sched_pool = _saturated_sched_pool(_CALLER, phantom)
        dispatcher, engine, _ = self._dispatcher(caller, sched_pool)

        with patch('agent_cascade.child_runner.run_child_core', return_value='child-ok'):
            result = dispatcher._run_child_sync(
                'coder', _CHILD, {}, caller, _CALLER, child_depth=1, is_last_ca=True)

        assert result == 'child-ok'
        assert _CALLER not in sched_pool._running, 'phantom permit was not force-released'
        sched_pool.release.assert_called_once_with(phantom)
        engine._release_slot.assert_not_called()  # no live callback, so Path 1 not taken
        engine.reacquire_after_slot_yield.assert_called_once()  # force-released → re-acquired

    def test_no_holder_marks_activity_no_reacquire(self):
        """Path 3 / condition 1: ``caller_slot_holder`` is None → nothing to yield.
        No exception, no spurious re-acquire, and activity is re-stamped via
        ``pool._mark_activity`` so a long child run does not age the parent into the DMS."""
        dispatcher, engine, pool = self._dispatcher(None, _saturated_sched_pool('other', MagicMock()))

        with patch('agent_cascade.child_runner.run_child_core', return_value='child-ok'):
            result = dispatcher._run_child_sync(
                'coder', _CHILD, {}, None, _CALLER, child_depth=1, is_last_ca=True)

        assert result == 'child-ok'
        engine.reacquire_after_slot_yield.assert_not_called()  # nothing yielded → no reacquire
        engine._release_slot.assert_not_called()
        pool._mark_activity.assert_called_once_with(_CALLER)  # activity re-stamped


class TestAdvisorAcquireBound:
    """Fix (a): the advisor's initial acquire is bounded strictly below the DMS window,
    and ``_acquire_slot`` honors the override as a fixed wait (not 6× window)."""

    def test_advisor_timeout_strictly_below_dms_window(self):
        """The production helper's bound is strictly under ``HOLDER_DMS_WINDOW_S`` at
        default settings — every input is imported from the code under test, no literals."""
        fy = float(inspect.signature(run_lightweight_advisor).parameters['first_yield_timeout'].default)
        bound = _compute_advisor_acquire_timeout(fy, HOLDER_DMS_WINDOW_S)
        assert 0 < bound < HOLDER_DMS_WINDOW_S, (
            f'advisor acquire bound {bound} must be strictly below '
            f'holder_dms_window_s {HOLDER_DMS_WINDOW_S}')
        # The bound must be a strict fraction of the window (never a bare equality edge).
        assert _ADVISOR_ACQUIRE_DMS_FRACTION < 1.0

    def test_bound_stays_below_window_when_dms_window_lowered(self):
        """Even a small DMS window keeps the bound under it (the fraction, not a fixed
        number, is what guarantees the invariant)."""
        bound = _compute_advisor_acquire_timeout(first_yield_timeout=30.0, holder_dms_window_s=10.0)
        assert 0 < bound < 10.0

    def test_run_lightweight_advisor_sets_bound_on_instance(self):
        """End-to-end guard: ``run_lightweight_advisor`` must actually SET a bounded
        ``_slot_acquire_timeout`` on the instance it runs. Deleting the
        ``instance._slot_acquire_timeout = ...`` assignment in advisor_runner.py leaves the
        attribute unset and fails this test. No LLM: ExecutionEngine, the reuse acquire, and
        the cleanup/permit-release hooks are mocked; ``engine.run`` yields nothing so the body
        halts immediately after the bound is applied to the real instance."""
        instance = SimpleNamespace(
            instance_name='adv_test_inst', agent_class='Security',
            max_turns=0, conversation=[], _generate_cfg_override=None,
            _exclusive_disabled_tools=False, _slot_acquire_timeout=None,
        )
        pool = MagicMock()
        pool.settings.holder_dms_window_s = HOLDER_DMS_WINDOW_S
        pool.stopped = False
        engine = MagicMock()
        engine.run.return_value = []  # no iterations: halts right after the bound is set

        with patch('agent_cascade.execution_engine.ExecutionEngine', return_value=engine), \
             patch('agent_cascade.security_reuse.acquire_security_agent',
                   return_value=(instance, False, 'adv_test_inst')), \
             patch('agent_cascade.security_reuse.release_permit_on_abandon'), \
             patch('agent_cascade.security_reuse.release_claim'), \
             patch('agent_cascade.advisor_runner._cleanup_advisor_instance'):
            run_lightweight_advisor(pool, 'Security', 'adv_test_inst', 'task', 'caller')

        timeout = instance._slot_acquire_timeout
        assert isinstance(timeout, float), (
            'run_lightweight_advisor must set instance._slot_acquire_timeout to a float '
            f'(got {timeout!r}) — a missing assignment leaves it unset')
        assert 0 < timeout < HOLDER_DMS_WINDOW_S

    def test_acquire_slot_clamps_to_fixed_override(self):
        """``SlotsMixin._acquire_slot`` collapses BOTH timeout and hard_cap to
        ``min(window, override)`` when a positive ``slot_acquire_timeout`` is passed
        (fixed wait), and leaves them at ``(window, 6×window)`` otherwise."""
        calls = []

        class _RecordingScheduler:
            def acquire(self, *args, **kwargs):
                calls.append(kwargs)
                return lambda: None

        router = MagicMock()
        router.get_effective_slot_info.return_value = {
            'api_base': 'x', 'concurrency_limit': 0, 'endpoint_name': '', 'model': '',
        }
        router.scheduler = _RecordingScheduler()

        pool = SimpleNamespace(
            api_router=router,
            settings=SimpleNamespace(slot_queue_timeout_seconds=_WINDOW),
        )

        class _Pool(SlotsMixin):
            pass

        p = _Pool()
        p.api_router = pool.api_router
        p.settings = pool.settings

        # No override → today's live-activity pair (window, 6×window).
        p._acquire_slot('coder', 'a')
        assert calls[-1]['timeout'] == _WINDOW
        assert calls[-1]['hard_cap'] == 6 * _WINDOW

        # Positive override → both collapse to min(window, override): a fixed wait.
        override = 1.0
        p._acquire_slot('coder', 'b', slot_acquire_timeout=override)
        assert calls[-1]['timeout'] == override
        assert calls[-1]['hard_cap'] == override  # NOT 6× — holder activity cannot slide it
        assert calls[-1]['timeout'] < HOLDER_DMS_WINDOW_S

        # Override larger than the window is clamped to the window (never widens the wait).
        p._acquire_slot('coder', 'c', slot_acquire_timeout=_WINDOW * 3)
        assert calls[-1]['timeout'] == _WINDOW


class TestNoDoubleKvSave:
    """Fix (b) invariant: the sync-child yield passes ``save_fn=None`` so the parent KV
    is saved exactly once (by ``_save_parent_state_before_delegation`` earlier in dispatch)."""

    def test_run_child_sync_passes_save_fn_none(self):
        from agent_cascade.tool_dispatcher import ToolDispatcher
        src = inspect.getsource(ToolDispatcher._run_child_sync)
        assert 'yield_caller_slot(' in src, 'sync child must route through the shared helper'
        assert 'save_fn=None' in src, (
            'sync-child yield must pass save_fn=None — the parent KV is already saved by '
            '_save_parent_state_before_delegation, a save_fn here would double-save')
        assert 'save_before_slot_yield' not in src

    def test_parent_save_helper_is_the_single_save(self):
        """The delegation-side save still exists and is wired into the dispatch path, so
        dropping save_fn on the yield does not remove the parent-KV save entirely."""
        from agent_cascade.tool_dispatcher import ToolDispatcher
        assert hasattr(ToolDispatcher, '_save_parent_state_before_delegation')
        dispatch_src = inspect.getsource(ToolDispatcher.handle_call_agent)
        assert '_save_parent_state_before_delegation(' in dispatch_src, (
            '_save_parent_state_before_delegation must remain the single parent-KV save')
