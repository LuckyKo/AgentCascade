"""Regression tests for the skill candidate-eval safety-net activity gate.

The candidate-eval timer (``_candidate_eval_loop``) re-runs the skill decision
gate on an interval. It should only do work when at least one agent is active —
a full re-scan while every agent is IDLE/TERMINATED is pointless. SLEEPING still
counts as active because it's merely waiting on an async tool.

These tests drive the gate logic directly and deterministically (no real 30s
thread wait). They use a lightweight stand-in exposing only the attributes the
loop touches (``instances``, ``skill_manager``, ``_candidate_eval_stop``) so we
can bind the real ``AgentPool`` methods without constructing a full pool.
"""

from types import SimpleNamespace

import pytest

from agent_cascade.agent_instance import ACTIVE_STATES, AgentState
from agent_cascade.pool.core import AgentPool


def _make_pool_stub(states):
    """Build a minimal object with the attributes ``_candidate_eval_loop`` uses.

    ``states`` maps instance_name -> AgentState. The real (unbound) methods are
    bound onto it so we test production code, not a copy.
    """
    stub = SimpleNamespace(
        instances={name: SimpleNamespace(state=state) for name, state in states.items()},
        skill_manager=SimpleNamespace(evaluate_candidates=lambda: None),
    )
    stub.has_active_agent = AgentPool.has_active_agent.__get__(stub)
    stub._candidate_eval_loop = AgentPool._candidate_eval_loop.__get__(stub)
    return stub


# ===========================================================================
# has_active_agent()
# ===========================================================================


class TestHasActiveAgent:
    """Unit tests for the activity gate predicate."""

    def test_false_when_all_idle(self):
        assert _make_pool_stub({'a': AgentState.IDLE}).has_active_agent() is False

    def test_false_when_all_terminated(self):
        assert _make_pool_stub({'a': AgentState.TERMINATED, 'b': AgentState.TERMINATED}).has_active_agent() is False

    def test_false_when_empty(self):
        assert _make_pool_stub({}).has_active_agent() is False

    @pytest.mark.parametrize('state', sorted(ACTIVE_STATES, key=lambda s: s.name))
    def test_true_for_each_active_state(self, state):
        assert _make_pool_stub({'a': state}).has_active_agent() is True

    def test_true_when_one_of_many_is_active(self):
        stub = _make_pool_stub({
            'a': AgentState.IDLE,
            'b': AgentState.TERMINATED,
            'c': AgentState.SLEEPING,
        })
        assert stub.has_active_agent() is True

    def test_sleeping_counts_as_active(self):
        # SLEEPING must be treated as active (waiting on an async tool).
        assert _make_pool_stub({'a': AgentState.SLEEPING}).has_active_agent() is True


# ===========================================================================
# _candidate_eval_loop gate behaviour
# ===========================================================================


class TestCandidateEvalLoopGate:
    """Drive the real loop body once or twice without any real waiting."""

    def test_skips_when_no_agent_active(self):
        calls = {'n': 0}
        stub = _make_pool_stub({'a': AgentState.IDLE, 'b': AgentState.TERMINATED})
        stub.skill_manager.evaluate_candidates = lambda: calls.__setitem__('n', calls['n'] + 1)

        # wait() returns False once (so the body runs a tick), then True to stop.
        waits = iter([False, True])
        stub._candidate_eval_stop = SimpleNamespace(wait=lambda interval: next(waits))

        stub._candidate_eval_loop(0)
        assert calls['n'] == 0, 'evaluate_candidates must not run while no agent is active'

    def test_runs_when_agent_active(self):
        calls = {'n': 0}
        stub = _make_pool_stub({'a': AgentState.RUNNING})
        stub.skill_manager.evaluate_candidates = lambda: calls.__setitem__('n', calls['n'] + 1)

        # wait() returns False once (so the body runs a tick), then True to stop.
        waits = iter([False, True])
        stub._candidate_eval_stop = SimpleNamespace(wait=lambda interval: next(waits))

        stub._candidate_eval_loop(0)
        assert calls['n'] == 1, 'evaluate_candidates must run when an agent is active'

    def test_gate_rechecked_each_tick(self):
        """The gate is re-evaluated on every tick, not just the first."""
        calls = {'n': 0}
        # Instance starts active then flips to IDLE after the first evaluation.
        inst = SimpleNamespace(state=AgentState.RUNNING)
        stub = _make_pool_stub({'a': AgentState.RUNNING})
        stub.instances['a'] = inst

        def evaluate():
            calls['n'] += 1
            if calls['n'] >= 1:
                inst.state = AgentState.IDLE  # deactivate after first tick

        stub.skill_manager.evaluate_candidates = evaluate

        # wait() returns False for the first two ticks, then True to stop.
        waits = iter([False, False, True])
        stub._candidate_eval_stop = SimpleNamespace(wait=lambda interval: next(waits))

        stub._candidate_eval_loop(0)
        assert calls['n'] == 1, 'second tick must be skipped once the agent went idle'
