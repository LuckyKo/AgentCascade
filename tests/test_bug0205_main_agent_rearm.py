"""BUG_0205 (part b): re-arm a loop-terminated main agent on the next user-initiated run.

Covers ``rearm_terminated_instance`` (agent_cascade/utils/termination_recovery.py):
- Baseline: a loop-terminated root instance trips the L1 race guard in ``_run_preflight``
  (``[BUG] ... state TERMINATED``) before the fix clears it.
- After re-arm: state/flag/pool-set all reset and ``_run_preflight`` succeeds (IDLE→RUNNING).
- Silent-exit variant: an instance terminated while IDLE (state stays IDLE, flag set) is
  still cleared — the case a BUG_0001-shaped state guard would miss.
- Negative: a non-recoverable termination (``_terminated_reason`` unset) is NOT re-armed,
  preserving the "TERMINATED must stay dead" contract.

Self-contained — no LLM or API server required.
"""

import logging
import threading

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.utils.termination_recovery import rearm_terminated_instance


# ──────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────


def _make_instance(name='Main'):
    return AgentInstance(
        instance_name=name, agent_class='orchestrator', conversation=[],
        created_at=0.0, last_activity=0.0, latest_marker_index=-1,
    )


class _FakePool:
    """Minimal pool surface for the re-arm helper (mirrors AgentPool's termination API)."""

    def __init__(self, instance):
        self.instances = {instance.instance_name: instance}
        self.terminated_instances = set()
        self._pool_lock = threading.RLock()

    def get_instance(self, instance_name: str):
        """Mirror pool/lifecycle.py::get_instance (strips the name)."""
        return self.instances.get(instance_name.strip())

    def is_instance_terminated(self, instance_name: str) -> bool:
        # Mirrors pool/slots.py::is_instance_terminated (set membership OR instance flag).
        name = instance_name.strip()
        with self._pool_lock:
            in_set = name in self.terminated_instances
            inst = self.instances.get(name)
            inst_flag = inst.is_terminated if inst else False
            return in_set or inst_flag


def _make_engine():
    """Bind the real _run_preflight to a mock engine (fake-engine pattern)."""
    from unittest.mock import MagicMock
    from agent_cascade.execution_engine import ExecutionEngine

    engine = MagicMock(spec=ExecutionEngine)
    engine._run_preflight = ExecutionEngine._run_preflight.__get__(engine, ExecutionEngine)
    # Success-path collaborators (only reached when state is IDLE).
    engine.pool = MagicMock()
    engine.pool._run_generation = 0
    engine._last_user_task_text = MagicMock(return_value='')
    engine._discard_stale_permit = MagicMock()
    return engine


def _loop_terminate(pool, inst):
    """Simulate the loop-detector termination: state TERMINATED + flag + pool set + reason."""
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = 'exhausted loop-recovery budget (4 rollbacks, max_auto_rollbacks=3); pattern: repeat'
    pool.terminated_instances.add(inst.instance_name)


# ══════════════════════════════════════════════
# 1. Baseline: loop-terminated root trips the L1 race guard
# ══════════════════════════════════════════════


def test_preflight_raises_for_terminated_instance():
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)
    engine = _make_engine()

    with pytest.raises(RuntimeError) as exc_info:
        engine._run_preflight(inst)
    assert 'state TERMINATED' in str(exc_info.value), str(exc_info.value)
    assert '[BUG]' in str(exc_info.value)


# ══════════════════════════════════════════════
# 2. After re-arm: state/flag/set reset and preflight succeeds
# ══════════════════════════════════════════════


def test_rearm_restores_idle_and_preflight_succeeds():
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)

    rearmed = rearm_terminated_instance(pool, 'Main', origin='user-message')
    assert rearmed is True

    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert 'Main' not in pool.terminated_instances
    assert pool.is_instance_terminated('Main') is False
    # Loop state reset so the re-armed run gets a fresh rollback budget.
    assert inst._loop_rollback_count == 0
    assert inst._fuzzy_warn_armed is True
    assert inst._fuzzy_escalation_armed is False

    # The L1 race guard now passes: IDLE -> RUNNING.
    engine = _make_engine()
    engine._run_preflight(inst)
    assert inst.state == AgentState.RUNNING


# ══════════════════════════════════════════════
# 3. Silent-exit variant: terminated while IDLE (state stays IDLE, flag set)
# ══════════════════════════════════════════════


def test_rearm_clears_flag_and_set_for_idle_terminated_instance():
    """AgentInstance.terminate() only transitions from ACTIVE_STATES, so an instance
    terminated while IDLE keeps state==IDLE with is_terminated=True. The re-arm helper
    must clear the flag + set unconditionally (a state==TERMINATED guard would miss it)."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    # State stays IDLE (silent-exit variant), flag + set set, reason recorded.
    inst.state = AgentState.IDLE
    inst.is_terminated = True
    inst._terminated_reason = 'exhausted loop-recovery budget (2 rollbacks, max_auto_rollbacks=1); pattern: x'
    pool.terminated_instances.add('Main')
    assert pool.is_instance_terminated('Main') is True

    rearmed = rearm_terminated_instance(pool, 'Main', origin='user-message')
    assert rearmed is True
    assert inst.is_terminated is False
    assert 'Main' not in pool.terminated_instances
    assert pool.is_instance_terminated('Main') is False
    assert inst.state == AgentState.IDLE


# ══════════════════════════════════════════════
# 4. Negative: non-recoverable termination is NOT re-armed
# ══════════════════════════════════════════════


def test_rearm_refuses_non_recoverable_termination():
    """A dismissal / cascade teardown leaves _terminated_reason unset → stays dead."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = None  # no recoverable marker
    pool.terminated_instances.add('Main')

    rearmed = rearm_terminated_instance(pool, 'Main', origin='user-message')
    assert rearmed is False
    # Nothing changed — the "TERMINATED must stay dead" contract holds.
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances
    assert pool.is_instance_terminated('Main') is True


def test_rearm_refuses_unknown_instance():
    """An instance not present in the pool is not re-armed."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    assert rearm_terminated_instance(pool, 'Nobody', origin='user-message') is False


# ══════════════════════════════════════════════
# 5. One-shot marker: re-arm consumes it, so a later non-loop termination stays dead
# ══════════════════════════════════════════════


def test_rearm_consumes_marker_then_non_loop_termination_stays_dead():
    """Re-arm clears _terminated_reason; a LATER non-loop termination (dismiss/teardown)
    does not re-set it, so the instance must stay dead (the invariant this fix protects)."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True
    # Marker consumed by the re-arm.
    assert inst._terminated_reason is None

    # A LATER non-loop termination sets state/flag/set but NOT _terminated_reason.
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    pool.terminated_instances.add('Main')
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False
    # Stays dead — nothing changed.
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances


def test_rearm_does_not_false_success_after_marker_consumed():
    """A second re-arm call (marker already consumed, instance now healthy) must NOT
    claim a re-arm. Fails against the pre-fix sticky-reason version (stale reason -> True
    + spurious [REARM] log)."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True
    # Marker consumed, instance now healthy/IDLE → a second call must not re-arm.
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False
    assert inst._loop_rollback_count == 0


# ══════════════════════════════════════════════
# 6. Root-only gate: child instances are NOT re-armed
# ══════════════════════════════════════════════


def test_rearm_refuses_child_instance():
    """A child instance (parent_instance set) is NOT re-armed by this helper —
    children are managed by the child_runner dismissal path."""
    inst = _make_instance('Child')
    inst.parent_instance = 'Main'  # this is a child
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)

    assert rearm_terminated_instance(pool, 'Child', origin='user-message') is False
    # Nothing changed — the child stays dead.
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Child' in pool.terminated_instances


# ══════════════════════════════════════════════
# 7. Wiring: helper works with a get_instance-only pool (no raw .instances dict)
# ══════════════════════════════════════════════


def test_rearm_works_with_get_instance_only_pool():
    """Regression guard (finding #1): the helper must use pool.get_instance()
    (the standard accessor), not pool.instances directly. A pool that exposes
    get_instance but NOT a raw .instances dict must not cause an AttributeError."""

    class _GetOnlyPool:
        """Pool that exposes get_instance but NOT a raw .instances attribute."""

        def __init__(self, instance):
            self._inst = instance
            self.terminated_instances = set()
            self._pool_lock = threading.RLock()

        def get_instance(self, name):
            return self._inst if name == self._inst.instance_name else None

    inst = _make_instance('Main')
    inst.parent_instance = None  # root
    pool = _GetOnlyPool(inst)
    _loop_terminate(pool, inst)
    # Would raise AttributeError on the old pool.instances.get() code path.
    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False


# ══════════════════════════════════════════════
# 8. Wiring: _guarded_rearm (run_agent_unified.py) swallows a helper
#    exception and the run proceeds
# ══════════════════════════════════════════════


def test_guarded_rearm_swallows_exception_and_proceeds(caplog):
    """Regression guard (polish item 2): _guarded_rearm in run_agent_unified.py
    must swallow ANY exception from rearm_terminated_instance (e.g. a pool whose
    get_instance raises, a shape regression, etc.) so the run proceeds.

    Exercises the production code path:
      a. _guarded_rearm returns False (not True, not an exception)
      b. the WARNING log line is emitted (greppable above DEBUG)
    """
    from agent_cascade.run_agent_unified import _guarded_rearm

    class _BrokenPool:
        """Pool whose get_instance raises — simulates a shape regression."""
        terminated_instances = set()
        _pool_lock = threading.RLock()

        def get_instance(self, name):
            raise RuntimeError('pool shape regression: get_instance unavailable')

    pool = _BrokenPool()

    # Must return False (swallowed), NOT raise.
    result = _guarded_rearm(pool, 'Main')
    assert result is False, '_guarded_rearm should return False when the helper raises'

    # The WARNING must be captured by caplog (greppable above DEBUG).
    rearm_warnings = [
        r for r in caplog.records
        if '[REARM] skipped for' in r.getMessage() and r.levelno == logging.WARNING
    ]
    assert rearm_warnings, (
        f'expected a [REARM] skipped WARNING, got '
        f'{[r.getMessage() for r in caplog.records]}'
    )


# ══════════════════════════════════════════════
# 9. Name normalization: padded names are stripped before discard
# ══════════════════════════════════════════════


def test_rearm_strips_padded_name_before_discard():
    """Regression guard (polish item 1): a padded instance name like '  Maine  '
    must be stripped before the terminated_instances.discard call, so the
    set entry (added by terminate_instance under the stripped name) is
    actually removed."""
    inst = _make_instance('Main')
    pool = _FakePool(inst)
    _loop_terminate(pool, inst)
    # Simulate a padded name reaching the helper (e.g. from a caller that
    # passed '  Main  ').
    rearmed = rearm_terminated_instance(pool, '  Main  ', origin='user-message')
    assert rearmed is True
    # The set must be clean — the stripped name 'Main' is gone.
    assert 'Main' not in pool.terminated_instances
    assert pool.is_instance_terminated('Main') is False
