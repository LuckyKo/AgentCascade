"""
Unit tests for BUG-1 / BUG-6 / BUG-4+8 fixes (endpoint-slot deadlock, 2026-08-21).

Spec: reports/fix_plans/BUG-1_slot_release_reacquire_on_halt.md,
      reports/fix_plans/BUG-6_wait_loop_sleep.md,
      reports/fix_plans/BUG-4_8_suspension_aware_exit_finally.md.

Covers:
- BUG-1: slot released while suspended by compression halt; re-acquired on resume;
         reacquire timeout degrades to slotless without raising; termination during
         wait returns False with no leak; no-slot agent is a fast no-op.
- BUG-6: wait loop uses time.sleep ticks, never pool.wait_if_paused.
- BUG-4/8: suspension-driven exits preserve pending async registrations and queued
         messages and land SLEEPING; normal exits still clear/drain → IDLE;
         terminal stop during suspension cleans up; suspended-but-drained → IDLE;
         preserved-SLEEPING agents are skipped by IdleManager._is_idle.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import Message


# ============================================================================
# Helpers
# ============================================================================

def make_instance(name="A", parent="Main"):
    """Real AgentInstance (dataclass-slots) — needed for _transition validation."""
    return AgentInstance(
        instance_name=name,
        agent_class="coder",
        conversation=[Message(role="system", content="sys")],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance=parent,
    )


def make_engine(instance):
    """ExecutionEngine over a MagicMock pool wired for _wait_for_compression_to_clear."""
    pool = MagicMock()
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    pool.get_instance.return_value = instance

    engine = ExecutionEngine(pool)
    engine._my_generation = 1

    # Suspension source of truth: a real set we can mutate from tests.
    pool._compression_halted = set()
    return engine, pool


class SlotTracker:
    """Stands in for instance._slot_release; records release + reacquire calls."""

    def __init__(self):
        self.released = 0
        self.acquired = False

    def release(self):
        self.released += 1
        self.acquired = False


def suspend(pool, name="A"):
    pool._compression_halted.add(name)


def resume(pool, name="A"):
    pool._compression_halted.discard(name)


# ============================================================================
# BUG-1 — slot released while suspended / re-acquired on resume
# ============================================================================

class TestBug1SlotReleaseOnHalt:

    def test_release_on_suspend_and_reacquire_on_resume(self):
        """Suspended holder releases its slot; on resume it re-acquires via
        reacquire_for and restores KV state only after success."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        tracker = SlotTracker()

        # Simulate holding a slot at suspension entry.
        inst._slot_release = tracker.release
        inst._slot_key = "_shared_sequential_slot_"

        save_calls = []
        restore_calls = []
        reacquire_results = [True]

        def fake_reacquire(instance_arg, holder_name, context="reacquire"):
            assert context == "after_compression_resume"
            # Halt must already be cleared for the re-acquire to proceed.
            assert not pool._compression_halted
            if reacquire_results.pop(0):
                instance_arg._slot_release = tracker.release
                instance_arg._slot_key = "_shared_sequential_slot_"
                tracker.acquired = True
            return reacquire_results.insert(0, True) or True

        with patch.object(engine, "reacquire_for", side_effect=fake_reacquire), \
             patch("agent_cascade.state_ops.save_instance_state",
                   side_effect=lambda i: save_calls.append(i.instance_name)), \
             patch("agent_cascade.state_ops.restore_instance_state",
                   side_effect=lambda i: restore_calls.append(i.instance_name)):
            suspend(pool)
            # Resume from another thread after a tick, mirroring Compressor completion.
            threading.Timer(0.05, lambda: resume(pool)).start()
            result = engine._wait_for_compression_to_clear("A")

        assert result is True
        assert tracker.released == 1, "slot must be released exactly once on suspension entry"
        assert save_calls == ["A"], "KV state saved BEFORE releasing the slot"
        assert restore_calls == ["A"], "KV state restored AFTER successful re-acquire"
        assert inst._slot_release is not None and tracker.acquired
        assert inst._slot_key == "_shared_sequential_slot_"

    def test_no_slot_agent_is_fast_noop(self):
        """Agent without a slot (unlimited endpoint): release is a no-op and the
        helper returns True once flags clear — no scheduler interaction."""
        inst = make_instance()
        engine, pool = make_engine(inst)

        assert inst._slot_release is None
        with patch.object(engine, "reacquire_for", return_value=True) as racq:
            suspend(pool)
            threading.Timer(0.05, lambda: resume(pool)).start()
            t0 = time.monotonic()
            result = engine._wait_for_compression_to_clear("A")
            elapsed = time.monotonic() - t0

        assert result is True
        assert elapsed < 1.5, f"suspension should end promptly, took {elapsed:.2f}s"
        racq.assert_called_once()

    def test_reacquire_failure_degrades_without_raising(self):
        """reacquire_for returning False after resume must degrade to slotless:
        no exception escapes, _slot_release stays None, helper still returns True."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        inst._slot_release = lambda: None

        with patch.object(engine, "reacquire_for", return_value=False), \
             patch("agent_cascade.state_ops.save_instance_state", return_value=True), \
             patch("agent_cascade.state_ops.restore_instance_state") as restore_mock:
            suspend(pool)
            threading.Timer(0.05, lambda: resume(pool)).start()
            result = engine._wait_for_compression_to_clear("A")

        assert result is True
        assert inst._slot_release is None, "failed re-acquire leaves a clean slotless state"
        restore_mock.assert_not_called(), "KV restore must NOT run after failed re-acquire"

    def test_terminal_stop_during_wait_returns_false(self):
        """Global stop during suspension → return False; slot stays released."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        inst._slot_release = lambda: None
        inst._slot_key = "k"

        def stop_mid_wait():
            pool.stopped = True

        with patch("agent_cascade.state_ops.save_instance_state", return_value=True), \
             patch.object(engine, "reacquire_for") as racq:
            suspend(pool)
            threading.Timer(0.05, stop_mid_wait).start()
            result = engine._wait_for_compression_to_clear("A")

        assert result is False
        assert inst._slot_release is None, "no slot leak on terminal-stop exit"
        racq.assert_not_called(), "terminal stop must not attempt re-acquisition"

    def test_save_before_release_ordering(self):
        """KV save happens before slot release (sleep-transition ordering)."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        events = []
        inst._slot_release = lambda: events.append("release")

        with patch("agent_cascade.state_ops.save_instance_state",
                   side_effect=lambda i: events.append("save")), \
             patch.object(engine, "reacquire_for", return_value=True):
            suspend(pool)
            resume(pool)
            engine._wait_for_compression_to_clear("A")

        assert events[:2] == ["save", "release"]


# ============================================================================
# BUG-6 — sleep tick instead of global-event spin
# ============================================================================

class TestBug6WaitLoopSleeps:

    def test_sleep_ticks_instead_of_wait_if_paused(self):
        """While suspended, the loop advances via time.sleep(_COMPRESSION_WAIT_TIMEOUT)
        and NEVER calls pool.wait_if_paused."""
        from agent_cascade.engine import core as core_mod
        inst = make_instance()
        engine, pool = make_engine(inst)

        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            if len(sleeps) >= 3:
                resume(pool)  # clear flag after 3 ticks

        with patch("agent_cascade.state_ops.save_instance_state", return_value=True), \
             patch.object(engine, "reacquire_for", return_value=True), \
             patch.object(core_mod.time, "sleep", side_effect=fake_sleep), \
             patch.object(pool, "wait_if_paused") as wip:
            suspend(pool)
            result = engine._wait_for_compression_to_clear("A")

        assert result is True
        assert len(sleeps) == 3, f"expected exactly 3 sleep ticks, got {len(sleeps)}"
        assert all(s == core_mod._COMPRESSION_WAIT_TIMEOUT for s in sleeps)
        wip.assert_not_called(), "pool.wait_if_paused (global-event spin) must not be used"

    def test_loop_exits_promptly_when_flag_clears(self):
        """No suspension → zero iterations, immediate True."""
        inst = make_instance()
        engine, pool = make_engine(inst)

        with patch("agent_cascade.state_ops.save_instance_state", return_value=True), \
             patch.object(engine, "reacquire_for", return_value=True):
            result = engine._wait_for_compression_to_clear("A")

        assert result is True


# ============================================================================
# BUG-4/8 — suspension-aware exit finally
# ============================================================================

def drive_run_to_exit(engine, instance, pool, *, suspended=False, outstanding=False,
                      terminate=False):
    """Drive engine.run() minimally to reach the exit finally.

    _setup_turn returns empty messages → run() takes the early-exit path which
    flows through the generic exit finally (the code under test).

    NOTE: run() resets instance._compression_suspended_at = 0.0 at ENTRY (plan
    requirement), so a mid-run suspension is simulated via a _setup_turn
    side-effect — mirroring how _wait_for_compression_to_clear sets the marker
    during the run in production.

    Known extra call: the early-exit block itself drains the message queue
    once (core.py 'Safety: drain any queued user messages') — pre-existing
    behavior, independent of the BUG-4/8 finally logic.
    """
    pool.stopped = False
    if terminate:
        pool.is_instance_terminated.return_value = True
    else:
        pool.is_instance_terminated.return_value = False

    # Outstanding work as read by the exit finally.
    pool.has_pending.return_value = outstanding
    pool.has_messages.return_value = False if outstanding else outstanding
    pool.drain_queue.return_value = []

    def setup_side_effect(inst):
        if suspended:
            inst._compression_suspended_at = time.monotonic()  # mid-run suspension
        return ([], [], [])

    engine._setup_turn = MagicMock(side_effect=setup_side_effect)
    list(engine.run(instance))


class TestBug48SuspensionAwareExit:

    def test_preserve_on_suspension_driven_exit_with_outstanding_work(self):
        """Suspension-driven exit + outstanding work: the EXIT FINALLY does not
        drain/clear (queue preserved), final state SLEEPING, marker log emitted."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.settings.tail_sync_check_enabled = False

        with patch("agent_cascade.engine.core.logger") as log_mock:
            drive_run_to_exit(engine, inst, pool, suspended=True, outstanding=True)

        # Exactly ONE drain call: the pre-existing early-exit safety drain
        # (core.py 'if not messages' block). The BUG-4/8 finally must add none.
        assert pool.drain_queue.call_count == 1
        pool._async_registry.clear_pending.assert_not_called()
        assert inst.state == AgentState.SLEEPING
        assert inst.sleeping_since is not None
        exit_logs = [str(c) for c in log_mock.debug.call_args_list
                     if "EXIT -" in str(c)]
        assert any("[suspension-preserved]" in s for s in exit_logs)

    def test_normal_exit_still_clears_and_goes_idle(self):
        """Regression guard: no suspension → finally drains + clears, IDLE.
        (Early-exit safety drain adds one extra drain call — pre-existing.)"""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.settings.tail_sync_check_enabled = False

        drive_run_to_exit(engine, inst, pool, suspended=False, outstanding=True)

        assert pool.drain_queue.call_count == 2  # early-exit + finally
        pool._async_registry.clear_pending.assert_called_once_with("A")
        assert inst.state == AgentState.IDLE

    def test_suspended_but_everything_completed_exits_idle(self):
        """Suspension happened earlier but no work remains → cleanup runs, IDLE."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.settings.tail_sync_check_enabled = False

        drive_run_to_exit(engine, inst, pool, suspended=True, outstanding=False)

        assert pool.drain_queue.call_count == 2  # early-exit + finally
        pool._async_registry.clear_pending.assert_called_once_with("A")
        assert inst.state == AgentState.IDLE
        assert inst.sleeping_since is None

    def test_terminal_stop_during_suspension_cleans_up(self):
        """Terminal stop wins: preserve=False even when suspended + outstanding."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.settings.tail_sync_check_enabled = False

        drive_run_to_exit(engine, inst, pool, suspended=True, outstanding=True,
                          terminate=True)

        # Terminal guard fires BEFORE _setup_turn → no early-exit safety drain;
        # the finally cleanup drain still runs (preserve=False).
        assert pool.drain_queue.call_count == 1
        pool._async_registry.clear_pending.assert_called_once_with("A")
        assert inst.state != AgentState.SLEEPING

    def test_run_entry_resets_suspension_marker(self):
        """run() clears a stale marker from a previous run so preservation can't
        leak across runs."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.settings.tail_sync_check_enabled = False
        inst._compression_suspended_at = 12345.0  # stale from previous run
        engine._setup_turn = MagicMock(return_value=([], [], []))

        drive_run_to_exit(engine, inst, pool, suspended=False, outstanding=True)

        assert inst._compression_suspended_at == 0.0
        assert inst.state == AgentState.IDLE

    def test_idle_checker_skips_preserved_sleeping_agent(self):
        """IdleManager._is_idle returns False for a SLEEPING agent (dismissal guard)."""
        from agent_cascade.pool.idle_manager import IdleManager

        inst = make_instance()  # parent set → eligible candidate otherwise
        inst.state = AgentState.SLEEPING

        pool = MagicMock()
        pool.instances = {"A": inst}
        manager = IdleManager.__new__(IdleManager)
        manager.pool = pool

        assert manager._is_idle("A") is False

        inst.state = AgentState.IDLE
        # IDLE would proceed past the sleeping gate (hits execution-stack check).
        pool._execution = MagicMock()
        pool._execution.active_stack = []
        pool.is_instance_halted.return_value = False
        inst.last_activity = time.monotonic() - 10_000.0
        settings = MagicMock(idle_timeout_seconds=60.0,
                             system_agent_idle_timeout_seconds=60.0)
        pool.settings = settings
        with patch("agent_cascade.pool.idle_manager.IdleManager._is_system_agent",
                   return_value=False):
            assert manager._is_idle("A") is True
