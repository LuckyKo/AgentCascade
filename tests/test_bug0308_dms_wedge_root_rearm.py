"""BUG_0308 (fix B): make a DMS wedge-termination of the ROOT agent recoverable.

The holder-DMS ROOT pass (``idle_manager._is_wedged_root`` → ``pool.terminate_instance``) abandons a
root whose in-flight tool made no turn-progress past its bound. Unlike a loop-detector termination,
that path left ``_terminated_reason`` unset, so the only re-arm path (``utils/termination_recovery``,
gated on ``_terminated_reason``) refused and the pool stayed permanently IDLE.

Fix B writes a machine-readable recoverable wedge reason on ROOT terminations, and the re-arm helper
re-arms it — but ONLY once the parked engine.run() thread has unwound (``_in_blocking_tool is None``),
because terminate_instance abandons the wedged root's thread un-joined (BUG_0244) and re-arming while
it is parked would re-introduce the double-transition / orphaned-permit race (reviewer Finding A).

Revert-proof mapping:
  1  repro + recovery: wedged root terminated -> reason is a non-empty *wedge* str; re-arm DEFERS while
     the thread is parked, succeeds once it unwinds. (Reason-assert FAILS pre-fix: reason stays None.)
  2  L1 guard no longer wedges: a DMS-wedged root passes ``_run_preflight`` (IDLE->RUNNING) after re-arm.
  3  parked-thread quiescence gate (Finding A): re-arm a still-parked DMS-wedged root -> False + deferred.
  4  contract preserved: a DMS-wedge root re-arms; a later reason-less Stop stays dead.
  5  reason distinguishability: a wedge reason is NOT a loop reason and vice versa.

Self-contained — no LLM or API server required.
"""

import threading
import time

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.pool.idle_manager import IdleManager, is_dms_wedge_reason
from agent_cascade.utils.termination_recovery import rearm_terminated_instance

WINDOW = 2.0  # test window; production holder_dms_window_s is 300s


# ──────────────────────────────────────────────
# Fixtures / doubles
# ──────────────────────────────────────────────


class _Settings:
    holder_dms_window_s = WINDOW
    holder_dms_max_bound_s = 900.0


class _OM:
    """operation_manager double: no pending approvals."""

    def list_pending_approvals(self):
        return []


class _Pool:
    """Pool surface for BOTH ``_run_holder_dms`` and ``rearm_terminated_instance``.

    ``api_router`` is None on purpose: with no scheduler the HOLDER pass is skipped (a wedged root
    owns no slot), so only the ROOT pass can collect the root — exactly the production shape.
    """

    def __init__(self, inst):
        self.instances = {inst.instance_name: inst}
        self.settings = _Settings()
        self.operation_manager = _OM()
        self.api_router = None  # no scheduler -> holder pass skipped, root pass still runs
        self._halted = set()
        self.terminated_instances = set()
        self._pool_lock = threading.RLock()

    def get_instance(self, name):
        return self.instances.get(name.strip())

    def is_paused(self):
        return False

    def is_instance_halted(self, name):
        return name in self._halted

    def terminate_instance(self, name):
        # Mirrors lifecycle.terminate_instance for the bits the re-arm helper depends on:
        # added to terminated_instances, is_terminated=True, state -> TERMINATED.
        name = name.strip()
        with self._pool_lock:
            self.terminated_instances.add(name)
        inst = self.instances.get(name)
        if inst is not None:
            inst.is_terminated = True
            inst.state = AgentState.TERMINATED


def _make_root(name='Main'):
    """A healthy root double (parent_instance None), IDLE by default."""
    return AgentInstance(
        instance_name=name, agent_class='orchestrator', conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(),
        latest_marker_index=0, parent_instance=None,
    )


def _arm_wedge(inst):
    """Drive the root double into the wedged-root posture the ROOT pass targets:
    RUNNING, aged last_activity, and a PAST-bound blocking-tool marker (thread parked in a tool)."""
    inst.state = AgentState.RUNNING
    inst.last_activity = time.monotonic() - 1000.0  # aged well past WINDOW
    inst._in_blocking_tool = ('some_tool', time.monotonic() - 1000.0, 1.0)  # bound 1s < age -> 'past'


def _make_engine():
    """Bind the real _run_preflight to a mock engine (mirrors test_bug0205_main_agent_rearm)."""
    from unittest.mock import MagicMock
    from agent_cascade.execution_engine import ExecutionEngine

    engine = MagicMock(spec=ExecutionEngine)
    engine._run_preflight = ExecutionEngine._run_preflight.__get__(engine, ExecutionEngine)
    engine.pool = MagicMock()
    engine.pool._run_generation = 0
    engine._last_user_task_text = MagicMock(return_value='')
    engine._discard_stale_permit = MagicMock()
    return engine


# ══════════════════════════════════════════════
# 1. Repro + recovery end-to-end (fix B)
# ══════════════════════════════════════════════


def test_dms_wedge_root_records_recoverable_reason_and_rearms_after_unwind():
    inst = _make_root('Main')
    pool = _Pool(inst)
    _arm_wedge(inst)

    IdleManager(pool)._run_holder_dms()

    # Terminated by the root pass AND now carries a recoverable wedge reason (this is the bug: it was None).
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances
    assert isinstance(inst._terminated_reason, str) and inst._terminated_reason
    assert is_dms_wedge_reason(inst._terminated_reason), (
        f"wedge reason must be self-describing, got {inst._terminated_reason!r}")

    # Thread is STILL parked (marker present): re-arm must DEFER (Finding A), leaving it dead.
    assert inst._in_blocking_tool is not None
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False
    assert inst.state == AgentState.TERMINATED  # deferral changes nothing

    # Simulate the parked thread unwinding (its dispatch-boundary finally cleared the marker).
    inst._in_blocking_tool = None
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True

    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert 'Main' not in pool.terminated_instances
    # One-shot marker consumed by the re-arm.
    assert inst._terminated_reason is None


# ══════════════════════════════════════════════
# 2. L1 race guard no longer wedges after re-arm
# ══════════════════════════════════════════════


def test_dms_wedged_root_passes_preflight_after_rearm():
    inst = _make_root('Main')
    pool = _Pool(inst)
    _arm_wedge(inst)
    IdleManager(pool)._run_holder_dms()
    assert inst.state == AgentState.TERMINATED

    # Without unwinding, preflight would raise the L1 guard; first unwind + re-arm.
    inst._in_blocking_tool = None
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True

    engine = _make_engine()
    engine._run_preflight(inst)  # must NOT raise; IDLE -> RUNNING
    assert inst.state == AgentState.RUNNING


# ══════════════════════════════════════════════
# 3. Parked-thread quiescence gate (reviewer Finding A)
# ══════════════════════════════════════════════


def test_rearm_defers_while_wedge_thread_still_parked():
    inst = _make_root('Main')
    pool = _Pool(inst)
    _arm_wedge(inst)
    IdleManager(pool)._run_holder_dms()
    assert is_dms_wedge_reason(inst._terminated_reason)

    # Marker STILL set => parked thread has not unwound => re-arm must be refused (double-run guard).
    assert inst._in_blocking_tool is not None
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False
    # Nothing mutated: stays TERMINATED with the recoverable reason intact (later message retries).
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances
    assert is_dms_wedge_reason(inst._terminated_reason)


# ══════════════════════════════════════════════
# 4. "TERMINATED stays dead" contract preserved
# ══════════════════════════════════════════════


def test_wedge_rearms_then_later_reason_less_stop_stays_dead():
    inst = _make_root('Main')
    pool = _Pool(inst)
    _arm_wedge(inst)
    IdleManager(pool)._run_holder_dms()
    inst._in_blocking_tool = None
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True
    assert inst._terminated_reason is None  # consumed

    # A LATER user Stop (no recoverable reason) must stay dead.
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    pool.terminated_instances.add('Main')
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances


# ══════════════════════════════════════════════
# 5. Wedge reason is distinguishable from the loop-detector reason (§6 risk #4)
# ══════════════════════════════════════════════


def test_wedge_and_loop_reasons_are_distinguishable():
    from agent_cascade.engine.llm_call import _build_loop_termination_reason
    from agent_cascade.pool.idle_manager import _build_dms_wedge_reason

    reason = _build_dms_wedge_reason('Main', 'http_get', 300.0)
    assert is_dms_wedge_reason(reason) is True
    # Must not be mislabeled as a loop-termination.
    assert 'exhausted loop-recovery budget' not in reason
    loop = _build_loop_termination_reason(3, 2, 'repeat')
    assert is_dms_wedge_reason(loop) is False
    # None / non-str must not satisfy the predicate.
    assert is_dms_wedge_reason(None) is False
    assert is_dms_wedge_reason(123) is False


def test_superseded_finalize_does_not_clobber_running_state():
    """Generation gate (reviewer F1): a stale/superseded finalize must leave a live run's
    RUNNING instance untouched. Reverts to a clobber (RUNNING->IDLE) if the gen guard is removed.
    """
    from unittest.mock import MagicMock
    from agent_cascade.execution_engine import ExecutionEngine

    inst = _make_root('Main')
    inst.state = AgentState.RUNNING  # the live (re-armed) run owns it

    # Engine double bound to the real method under test.
    engine = MagicMock(spec=ExecutionEngine)
    engine._finalize_state_transition = ExecutionEngine._finalize_state_transition.__get__(engine, ExecutionEngine)
    engine.pool = MagicMock()
    engine.pool._mark_activity = MagicMock()

    # Case A (superseded): stale engine gen != pool gen -> must NOT transition.
    engine._my_generation = 5
    engine.pool._run_generation = 6
    engine._finalize_state_transition(inst, preserve=False)
    assert inst.state == AgentState.RUNNING  # not clobbered — this fails without the gen gate
    engine.pool._mark_activity.assert_not_called()

    # Case B (owner, inert): gen matches -> transitions to IDLE exactly as before.
    engine._my_generation = 6
    engine.pool._run_generation = 6
    engine._finalize_state_transition(inst, preserve=False)
    assert inst.state == AgentState.IDLE
    engine.pool._mark_activity.assert_called_once_with('Main')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
