"""BUG_0432: a user-message resume must force-re-arm a TERMINATED ROOT whose
``_terminated_reason`` is None (no recoverable reason).

Context: a slot-holding ROOT killed by the DMS HOLDER pass (idle_manager) gets NO
recoverable reason — that pass never writes one, and the reason-writing ROOT pass
skips any name it already collected. So ``_terminated_reason`` stays None, and the
BUG_0308/0205 reason gate refused the re-arm, leaving the resumed root TERMINATED
forever in-process (the run_loop preflight then rejects the IDLE->RUNNING entry).
Only a full process restart could recover it.

Fix: ``rearm_terminated_instance(..., force=True)`` — the explicit user-message
resume, the function's only production caller — now lifts the reason gate too (in
addition to the BUG_0431 quiescence gate), reviving a TERMINATED ROOT regardless of
``_terminated_reason``. The "TERMINATED stays dead" contract is preserved for every
NON-user path (Stop / dismissal / teardown / loop-detector), because they never pass
``force=True``; the default (force=False) still refuses a reason-less termination.

Self-contained — no LLM or API server required.
"""

import logging
import threading

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.pool.idle_manager import _build_dms_wedge_reason
from agent_cascade.utils.termination_recovery import rearm_terminated_instance

STALE_MARKER = ('call_agent', 123.0, 300.0)  # (tool, since_ts, bound) — a parked, never-unwound tool


# ──────────────────────────────────────────────
# Pool / instance doubles (same surface test_bug0431 / test_bug0308 / test_bug0205 use)
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


def _terminate_reasonless(inst, pool, stale_marker=True):
    """A reason-less TERMINATED root (e.g. a DMS holder-pass kill that wrote no reason),
    with an optional stale, never-unwound ``_in_blocking_tool`` marker."""
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = None
    pool.terminated_instances.add(inst.instance_name)
    if stale_marker:
        inst._in_blocking_tool = STALE_MARKER


# ══════════════════════════════════════════════
# 1. Headline: force re-arms a reason-less TERMINATED ROOT
# ══════════════════════════════════════════════


def test_force_rearm_revives_reason_less_root(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_reasonless(inst, pool)

    # Precondition: reason-less TERMINATED root, parked marker, in the terminated set.
    assert inst._terminated_reason is None
    assert inst._in_blocking_tool == STALE_MARKER
    assert inst.state == AgentState.TERMINATED
    assert 'Main' in pool.terminated_instances

    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True

    # Revived: state, flag, pool set, marker, and the (absent) reason are all clean.
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert 'Main' not in pool.terminated_instances
    assert inst._in_blocking_tool is None          # stale marker discarded by force
    assert inst._terminated_reason is None

    # The greppable success line is present; the REFUSED refusal is NOT (reason gate lifted).
    assert any('-> IDLE for new run' in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]
    assert not any('REFUSED' in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 2. Stay-dead contract: force=False (non-user default) still refuses a reason-less root
# ══════════════════════════════════════════════


def test_default_force_stays_dead_for_reason_less_root(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_reasonless(inst, pool)

    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message') is False  # force defaults False

    # Nothing mutated — the "TERMINATED stays dead" contract holds for non-forced callers.
    assert inst.state == AgentState.TERMINATED
    assert inst.is_terminated is True
    assert 'Main' in pool.terminated_instances

    # The reason-gate refusal is greppable on the force=False path.
    assert any('re-arm REFUSED (reason gate)' in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records), [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 3. Regression (BUG_0431): the reason-SET case still force-re-arms
# ══════════════════════════════════════════════


def test_force_rearm_still_works_for_dms_wedge_reason():
    inst = _make_root('Main')
    pool = _FakePool(inst)
    inst.state = AgentState.TERMINATED
    inst.is_terminated = True
    inst._terminated_reason = _build_dms_wedge_reason(inst.instance_name, 'call_agent', 300.0)
    pool.terminated_instances.add(inst.instance_name)
    inst._in_blocking_tool = STALE_MARKER

    assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert inst._in_blocking_tool is None          # cleared by force (BUG_0431)
    assert inst._terminated_reason is None          # one-shot marker consumed
    assert 'Main' not in pool.terminated_instances


# ══════════════════════════════════════════════
# 4. The REFUSED (reason gate) log is scoped to force=False only
# ══════════════════════════════════════════════


def test_refused_reason_gate_log_only_on_force_false(caplog):
    # force=True reason-less: no REFUSED log (gate lifted, revives).
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_reasonless(inst, pool)
    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True
        assert not any('re-arm REFUSED (reason gate)' in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    # force=False reason-less: REFUSED log fires (still refuses).
    caplog.clear()
    inst2 = _make_root('Main')
    pool2 = _FakePool(inst2)
    _terminate_reasonless(inst2, pool2)
    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool2, 'Main', origin='user-message', force=False) is False
        assert any('re-arm REFUSED (reason gate)' in r.getMessage() and r.levelno == logging.WARNING
                   for r in caplog.records), [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 5. Idempotency: a second force re-arm on an already-IDLE instance is a no-op
# ══════════════════════════════════════════════


def test_second_force_rearm_on_idle_is_noop(caplog):
    inst = _make_root('Main')
    pool = _FakePool(inst)
    _terminate_reasonless(inst, pool)

    assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is True
    # Now IDLE — a second forced call must not re-transition or log a refusal.
    with caplog.at_level(logging.WARNING):
        assert rearm_terminated_instance(pool, 'Main', origin='user-message', force=True) is False
    assert inst.state == AgentState.IDLE
    assert inst.is_terminated is False
    assert not any('REFUSED' in r.getMessage() for r in caplog.records), \
        [r.getMessage() for r in caplog.records]


# ══════════════════════════════════════════════
# 6. Child guard: force does not revive a terminated CHILD (root-only scope preserved)
# ══════════════════════════════════════════════


def test_force_does_not_revive_child():
    parent = _make_root('Orch')
    child = AgentInstance(
        instance_name='Child', agent_class='coder', conversation=[],
        created_at=0.0, last_activity=0.0, latest_marker_index=-1,
        parent_instance=parent,
    )
    pool = _FakePool(child)
    pool.instances[parent.instance_name] = parent  # get_instance(child) needs only child, but keep both
    child.state = AgentState.TERMINATED
    child.is_terminated = True
    child._terminated_reason = None
    pool.terminated_instances.add('Child')

    # Children are owned by the child_runner dismissal path; force never re-arms them here.
    assert rearm_terminated_instance(pool, 'Child', origin='user-message', force=True) is False
    assert child.state == AgentState.TERMINATED
    assert child.is_terminated is True


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
