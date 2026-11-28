"""Regression tests for BUG_0434 — a user message stranded in the run-end tail.

Two halves under test:

1. ``RunLoopMixin._run_finalize`` must NOT drain the user-message queue on a normal
   completion (the message is not stale — a follow-up run processes it), but MUST drain
   it on a terminal stop (Stop / dismissal / superseded run).
2. ``api_server.should_retrigger_stranded`` (the pure gate behind the run-end
   ``_retrigger_stranded_message`` hook) only authorizes a follow-up run for an IDLE
   instance with a queued message and no stop/generation in progress.
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.api_server import should_retrigger_stranded
from agent_cascade.engine.core import ExecutionEngine
from agent_cascade.llm.schema import Message


# ============================================================================
# Engine exit-drain gate
# ============================================================================


def make_instance(name: str = 'Root') -> AgentInstance:
    return AgentInstance(
        instance_name=name,
        agent_class='orchestrator',
        conversation=[Message(role='system', content='sys')],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=0,
        parent_instance=None,
    )


def make_engine(instance: AgentInstance):
    pool = MagicMock()
    pool.stopped = False
    pool._run_generation = 1
    pool.is_instance_terminated.return_value = False
    pool.get_instance.return_value = instance
    pool.has_pending.return_value = False
    pool.has_messages.return_value = True
    pool.drain_queue.return_value = ['a message that arrived in the run-end tail']

    engine = ExecutionEngine(pool)
    engine._my_generation = 1
    pool._compression_halted = set()
    return engine, pool


class TestExitDrainGate:

    def test_normal_completion_keeps_tail_message_queued(self):
        """A normal completion must leave the queued message for the follow-up run."""
        inst = make_instance()
        engine, pool = make_engine(inst)

        with patch('agent_cascade.snapshot.maybe_autosave_on_task_end'):
            engine._run_finalize(inst, early_exit=False)

        pool.drain_queue.assert_not_called()
        assert inst.state == AgentState.IDLE

    def test_terminal_stop_still_drains(self):
        """Stop / dismissal / superseded runs must still discard stale queued messages."""
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.is_instance_terminated.return_value = True

        with patch('agent_cascade.snapshot.maybe_autosave_on_task_end'):
            engine._run_finalize(inst, early_exit=False)

        pool.drain_queue.assert_called_once_with(inst.instance_name)

    def test_pool_stop_still_drains(self):
        inst = make_instance()
        engine, pool = make_engine(inst)
        pool.stopped = True

        with patch('agent_cascade.snapshot.maybe_autosave_on_task_end'):
            engine._run_finalize(inst, early_exit=False)

        pool.drain_queue.assert_called_once_with(inst.instance_name)


# ============================================================================
# Pure run-end retrigger decision
# ============================================================================


def _pool(*, stopped: bool = False):
    return SimpleNamespace(stopped=stopped)


def _instance(state=AgentState.IDLE):
    return SimpleNamespace(state=state)


class TestShouldRetriggerStranded:

    def test_idle_with_queued_message(self):
        assert should_retrigger_stranded(
            pool=_pool(), instance=_instance(), generating=False,
            stop_requested=False, has_messages=True) is True

    def test_generating_blocks(self):
        assert should_retrigger_stranded(
            pool=_pool(), instance=_instance(), generating=True,
            stop_requested=False, has_messages=True) is False

    def test_stop_requested_blocks(self):
        """Stop / session switch must never auto-start a follow-up run."""
        assert should_retrigger_stranded(
            pool=_pool(), instance=_instance(), generating=False,
            stop_requested=True, has_messages=True) is False

    def test_pool_stopped_blocks(self):
        assert should_retrigger_stranded(
            pool=_pool(stopped=True), instance=_instance(), generating=False,
            stop_requested=False, has_messages=True) is False

    def test_dismissed_instance_blocks(self):
        assert should_retrigger_stranded(
            pool=_pool(), instance=None, generating=False,
            stop_requested=False, has_messages=True) is False

    def test_no_queued_message_blocks(self):
        assert should_retrigger_stranded(
            pool=_pool(), instance=_instance(), generating=False,
            stop_requested=False, has_messages=False) is False

    def test_non_idle_state_blocks(self):
        """SLEEPING/RUNNING have an owner; engine.run() only accepts IDLE anyway."""
        for state in (AgentState.RUNNING, AgentState.SLEEPING,
                      AgentState.COMPLETING, AgentState.TERMINATED):
            assert should_retrigger_stranded(
                pool=_pool(), instance=_instance(state), generating=False,
                stop_requested=False, has_messages=True) is False, state


# ============================================================================
# Run-end wiring (source guard, same style as TestAfkRunEndWiring)
# ============================================================================


class TestRunEndRetriggerWiring:

    def test_retrigger_runs_before_afk_nudge(self):
        import inspect
        from agent_cascade import api_server

        src = inspect.getsource(api_server)
        retrigger = "_retrigger_stranded_message(target_instance_name or session['session_name'], loop)"
        afk = "_afk_maybe_nudge(target_instance_name or session['session_name'], loop)"
        assert retrigger in src
        assert src.index(retrigger) < src.index(afk), \
            'the stranded-message follow-up run must start BEFORE the AFK hook'

    def test_retrigger_helper_is_nested_in_create_app(self):
        import inspect
        from agent_cascade import api_server

        create_app_src = inspect.getsource(api_server.create_app)
        assert 'def _retrigger_stranded_message(' in create_app_src
        assert 'def should_retrigger_stranded(' not in create_app_src
