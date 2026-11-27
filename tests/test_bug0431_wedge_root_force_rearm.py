"""BUG_0431: a user-message resume must force-re-arm a DMS-wedge-terminated ROOT whose
``_in_blocking_tool`` marker is permanently STALE.

Context: a DMS wedge kill (idle_manager ROOT pass) records a recoverable reason but abandons the
root's engine.run() thread un-joined (BUG_0244). If that parked thread never runs its
dispatch-boundary ``finally`` (which sets ``_in_blocking_tool=None``), the marker stays non-None
forever. The BUG_0308 quiescence gate then DEFERS the re-arm permanently, so a resumed root stays
TERMINATED and the whole session is dead (no in-process recovery short of a full restart).

Fix: an explicit user-message resume has already bumped ``pool._run_generation``
(run_agent_unified), so any parked thread is generation-superseded and cannot touch the live run's
state — that is the durable protection. ``rearm_terminated_instance(..., force=True)`` therefore
skips the quiescence gate and clears the stale marker. The reason gate is UNCHANGED: a reason-less
termination (Stop / dismissal / teardown) still stays dead.

Self-contained — no LLM or API server required.
"""

import logging
import threading
import time

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.pool.idle_manager import _build_dms_wedge_reason, is_dms_wedge_reason
from agent_cascade.utils.termination_recovery import rearm_terminated_instance

STALE_MARKER = ('call_agent', 123.0, 300.0)  # (tool, since_ts, bound) — a parked, never-unwound tool


# ──────────────────────────────────────────────
# Pool / instance doubles (same surface test_bug0308 / test_bug0205 use for the re-arm helper)
# ──────────────────────────────────────────────


class _FakePool:
    """Minimal pool surface ``rearm_terminated_instance`` needs: get_instance, terminated_instances,
    _pool_lock, is_instance_terminated."""

    def __init__(self, instance):
        self.instances = {instance.instance_name: instance}
        self.terminated_instances = set()
        self._pool_lock = threading.RLock()

    def get_instance(self, instance_name: str):
        return self.instances.get(instance_name.strip())

    def is_instance_terminated(self, instance_name: str) -> bool:
        name = instance_name.strip()
        with self._pool_lock:
            inst = self.instances.get(name)
            return (name in self.terminated_instances) or (inst is not None and inst.is_terminated)


def _make_root(name='Main'):
    return AgentInstance(
        instance_name=name, agent_class='orchestrator', conversation=[],
        created_at=0.0, last_activity=0.0, latest_marker_index=-1,
        parent_instance=None,
    )


def _terminate_wedge(inst, pool, stale_marker=True):
    """Put a root into the DMS-wedge-terminated posture: TERMINATED + flag + pool set + wedge reason
    + (by default) a stale, never-unwound ``_in_blocking_tool`` marker."""
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = _build_dms_wedge_reason(inst.instance_name, 'call_agent', 300.0)
    pool.terminated_instances.add(inst.instance_name)
    if stale_marker:
        inst._in_blocking_tool = STALE_MARKER


def _terminate_loop(inst, pool, stale_marker=True):
    """Loop-detector termination (NOT a wedge reason), with an optional stale marker."""
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = (
        'exhausted loop-recovery budget (4 rollbacks, max_auto_rollbacks=3); pattern: repeat')
    pool.terminated_instances.add(inst.instance_name)
    if stale_marker:
        inst._in_blocking_tool = STALE_MARKER


def _terminate_reasonless(inst, pool):
    """A reason-less termination (user Stop / dismissal / teardown) — must stay dead."""
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = None
    pool.terminated_instances.add(inst.instance_name)


# ══════════════════════════════════════════════
# 1. The headline fix: force re-arms a wedge root with a stale marker
# ══════════════════════════════════════════════


def test_force_rearm_revives_wedge_root_with_stale_marker():
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_wedge(inst, pool)

    # Precondition: wedge reason, still parked (stale marker), TERMINATED.
    assert is_dms_wedge_reason(inst._terminated_reason)
    assert inst._in_blocking_tool == STALE_MARKER
    assert inst.state == AgentState.TERMINATED

    assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True

    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert inst._in_blocking_tool is None          # stale marker discarded
    assert 'Main' not in pool.terminated_instances
    assert pool.is_instance_terminated('Main') is False
    assert inst._terminated_reason is None          # one-shot marker consumed


# ══════════════════════════════════════════════
# 2. The default path is unchanged: force=False still defers on a stale marker
# ══════════════════════════════════════════════


def test_default_force_defers_wedge_with_stale_marker(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_wedge(inst, pool)

    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False  # force defaults False

    # Nothing mutated: stays TERMINATED, marker and reason intact (a later message retries).
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert inst._in_blocking_tool == STALE_MARKER
    assert 'Main' in pool.terminated_instances
    assert is_dms_wedge_reason(inst._terminated_reason)

    # The DEFER refusal is greppable.
    assert any('wedge re-arm DEFERRED' in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 3. force does NOT lift the reason gate: a reason-less termination stays dead
# ══════════════════════════════════════════════


def test_force_does_not_revive_reason_less_termination(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_reasonless(inst, pool)

    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is False

    assert inst.state == AgentState.TERMINATED       # 'TERMINATED stays dead' contract preserved
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances

    # The reason-gate refusal is greppable.
    assert any('re-arm REFUSED (reason gate)' in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 4. Loop-detector reasons are NOT quiescence-gated: force re-arm works with a stale marker
# ══════════════════════════════════════════════


def test_force_rearm_works_for_loop_reason_with_stale_marker():
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_loop(inst, pool)

    # force=True on a loop reason (stale marker present) — not wedge-gated, so it re-arms.
    assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert inst._in_blocking_tool is None            # cleared by force
    assert 'Main' not in pool.terminated_instances


def test_default_force_rearms_loop_reason_with_stale_marker():
    # BUG_0205 baseline: a loop reason re-arms even without force and even with a stale marker,
    # because loop reasons skip the wedge quiescence gate entirely.
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_loop(inst, pool)

    assert rearm_terminated_instance(pool, 'Main', origin='user-message') is True
    assert inst.state == AgentState.IDLE
    assert inst._in_blocking_tool == STALE_MARKER    # non-force leaves the marker as-is


# ══════════════════════════════════════════════
# 5. Idempotency: a second force re-arm on an already-IDLE instance is a no-op
# ══════════════════════════════════════════════


def test_second_force_rearm_on_idle_is_noop(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_wedge(inst, pool)

    assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True
    # Now IDLE with the reason consumed — a second call must not re-transition.
    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is False
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    # The healthy IDLE no-op must NOT emit a REFUSED WARNING (the reason-gate log guard works:
    # a healthy instance has no reason and is not terminated, so the guarded log stays silent).
    assert not any('REFUSED' in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
