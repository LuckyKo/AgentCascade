"""Tests for the dismiss_agent termination feature.

Covers:
- AgentInstance.terminate() idempotency and state changes
- AgentPool.is_instance_terminated() behavior
- AgentTerminatedError basics
- _check_termination() and _interruptible_sleep() helpers (api_router.py)
- Integration: dismiss_instance() and terminate_instance() setting termination flags

Uses the same mocking pattern as test_agent_pool.py to avoid filesystem/LLM deps.
"""

import time
import threading
from unittest.mock import patch, MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance, AgentState, ACTIVE_STATES
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.api_router import _check_termination, _interruptible_sleep


# ---------------------------------------------------------------------------
# Fixture: build a minimal AgentPool without hitting the filesystem
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_pool():
    """Create an AgentPool with mocked dependencies so it can be instantiated."""
    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector') as mock_telem, \
         patch('agent_cascade.api_router.APIRouter') as mock_router:

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
        return pool


# ---------------------------------------------------------------------------
# Helpers to create minimal AgentInstance objects for tests
# ---------------------------------------------------------------------------

def make_instance(name: str, state: AgentState = AgentState.IDLE):
    """Create a minimal AgentInstance for testing."""
    return AgentInstance(
        instance_name=name,
        agent_class="researcher",
        conversation=[],
        state=state,
        max_turns=None,
        parent_instance=None,
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        compression_summary=None,
        latest_marker_index=-1,
    )


# ===========================================================================
# Unit Tests: AgentInstance.terminate()
# ===========================================================================

class TestTerminateIdempotency:
    """AgentInstance.terminate() can be called multiple times safely."""

    def test_multiple_calls_no_error(self):
        """Calling terminate() multiple times should not raise."""
        inst = make_instance("w1", state=AgentState.RUNNING)
        inst.terminate()
        inst.terminate()
        inst.terminate()
        # No exception — idempotent.

    @pytest.mark.parametrize("initial_state,expected_state,should_transition", [
        (AgentState.RUNNING, AgentState.TERMINATED, True),
        (AgentState.SLEEPING, AgentState.TERMINATED, True),
        (AgentState.COMPLETING, AgentState.TERMINATED, True),
        (AgentState.IDLE, AgentState.IDLE, False),      # not in ACTIVE_STATES
        (AgentState.TERMINATED, AgentState.TERMINATED, False),  # already terminated
    ])
    def test_state_transition_from_all_states(self, initial_state, expected_state, should_transition):
        """Parametrized: terminate() transitions correctly from any starting state.

        ACTIVE_STATES → TERMINATED; IDLE sets flag but no transition; TERMINATED is no-op.
        """
        inst = make_instance("w1", state=initial_state)
        # Track whether _transition was called
        with patch.object(inst, '_transition') as mock_trans:
            inst.terminate()
            if should_transition:
                mock_trans.assert_called_once_with(AgentState.TERMINATED)
            else:
                mock_trans.assert_not_called()

        assert inst.is_terminated is True
        assert inst.state == expected_state

    def test_is_terminated_stays_true_after_multiple_calls(self):
        """is_terminated remains True after repeated terminate() calls."""
        inst = make_instance("w1", state=AgentState.RUNNING)
        inst.terminate()
        assert inst.is_terminated is True
        inst.terminate()
        assert inst.is_terminated is True


class TestTerminateDurableFlag:
    """terminate() sets durable termination signals and clears volatile state."""

    def test_sets_is_terminated_true(self):
        inst = make_instance("w1", state=AgentState.RUNNING)
        assert inst.is_terminated is False
        inst.terminate()
        assert inst.is_terminated is True

    def test_state_becomes_terminated_when_active(self):
        for active_state in ACTIVE_STATES:
            inst = make_instance("w1", state=active_state)
            inst.terminate()
            assert inst.state == AgentState.TERMINATED, \
                f"Expected TERMINATED from {active_state.name}, got {inst.state.name}"

    def test_clears_streaming_responses(self):
        """terminate() clears _streaming_responses to discard partial LLM output."""
        inst = make_instance("w1", state=AgentState.RUNNING)
        # Inject some fake streaming responses.
        with inst._compression_lock:
            inst._streaming_responses.append("partial chunk 1")
            inst._streaming_responses.append("partial chunk 2")
        assert len(inst._streaming_responses) == 2

        inst.terminate()
        assert len(inst._streaming_responses) == 0


# ===========================================================================
# Unit Tests: AgentPool.is_instance_terminated()
# ===========================================================================

class TestIsInstanceTerminated:
    """AgentPool.is_instance_terminated() checks both terminated_instances set and instance flag."""

    def test_returns_true_when_in_terminated_instances_set(self, agent_pool):
        """If name is in terminated_instances, return True even without an instance object."""
        agent_pool.terminated_instances.add("gone")
        assert agent_pool.is_instance_terminated("gone") is True

    def test_returns_true_when_instance_is_terminated_flag_set(self, agent_pool):
        """If instance exists with is_terminated=True, return True."""
        inst = make_instance("w1", state=AgentState.IDLE)
        inst.terminate()  # sets is_terminated=True
        agent_pool.instances["w1"] = inst
        assert agent_pool.is_instance_terminated("w1") is True

    def test_returns_false_when_neither_condition(self, agent_pool):
        """If not in terminated_instances and instance.is_terminated=False, return False."""
        inst = make_instance("alive", state=AgentState.RUNNING)
        agent_pool.instances["alive"] = inst
        assert agent_pool.is_instance_terminated("alive") is False

    def test_returns_false_for_nonexistent_instance(self, agent_pool):
        """Unknown instance name returns False."""
        assert agent_pool.is_instance_terminated("nobody") is False

    def test_strips_whitespace_from_instance_name(self, agent_pool):
        """is_instance_terminated() strips whitespace from the name."""
        agent_pool.terminated_instances.add("trimmed")
        assert agent_pool.is_instance_terminated(" trimmed ") is True


# ===========================================================================
# Unit Tests: AgentTerminatedError
# ===========================================================================

class TestAgentTerminatedError:
    """Basic behavior of the AgentTerminatedError exception type."""

    def test_can_raise_and_catch(self):
        name = "victim"
        with pytest.raises(AgentTerminatedError) as exc_info:
            raise AgentTerminatedError(name)
        err = exc_info.value
        assert err.instance_name == name

    def test_error_message_is_meaningful(self):
        err = AgentTerminatedError("worker-42")
        msg = str(err)
        assert "worker-42" in msg
        # Should mention termination/dismissal.
        assert "terminated" in msg.lower() or "dismissed" in msg.lower()


# ===========================================================================
# Unit Tests: api_router.py helpers
# ===========================================================================

class TestCheckTermination:
    """_check_termination(pool, instance_name) helper."""

    def test_returns_false_when_pool_is_none(self):
        assert _check_termination(None, "any") is False

    def test_returns_false_when_instance_name_empty(self, agent_pool):
        assert _check_termination(agent_pool, "") is False
        assert _check_termination(agent_pool, None) is False  # type: ignore[arg-type]

    def test_returns_true_when_instance_terminated(self, agent_pool):
        agent_pool.terminated_instances.add("dead")
        assert _check_termination(agent_pool, "dead") is True

    def test_returns_false_when_instance_not_terminated(self, agent_pool):
        inst = make_instance("alive", state=AgentState.RUNNING)
        agent_pool.instances["alive"] = inst
        assert _check_termination(agent_pool, "alive") is False


class TestInterruptibleSleep:
    """_interruptible_sleep() wakes promptly on termination."""

    def test_sleeps_full_duration_when_no_pool(self):
        """Test that _interruptible_sleep completes normally when no pool is provided.

        Without a pool, _check_termination always returns False, so the full sleep
        duration elapses without interruption. This verifies basic timing behavior,
        not the termination-checking logic itself.
        """
        start = time.monotonic()
        _interruptible_sleep(0.25, pool=None, instance_name="nobody")
        elapsed = time.monotonic() - start
        assert 0.2 <= elapsed <= 0.5

    def test_raises_agent_terminated_error_when_terminated(self, agent_pool):
        # Pre-terminate instance so _interruptible_sleep detects it immediately.
        agent_pool.terminated_instances.add("victim")

        with pytest.raises(AgentTerminatedError) as exc_info:
            _interruptible_sleep(10.0, pool=agent_pool, instance_name="victim", interval=0.1)

        assert exc_info.value.instance_name == "victim"

    def test_wakes_promptly_on_termination(self):
        """Use a threading.Event to deterministically trigger termination mid-sleep."""
        terminate_event = threading.Event()

        class MockPool:
            """Minimal pool mock with controllable termination state."""
            def __init__(self):
                self._terminated = False

            def is_instance_terminated(self, name: str) -> bool:
                return self._terminated

        pool = MockPool()

        # Thread that flips the termination flag when signaled.
        def trigger_termination():
            terminate_event.wait()  # Block until test signals us.
            pool._terminated = True

        trigger_thread = threading.Thread(target=trigger_termination, daemon=True)
        trigger_thread.start()

        # Run _interruptible_sleep in a separate thread so we can control timing.
        result: dict = {}

        def do_sleep():
            try:
                _interruptible_sleep(5.0, pool=pool, instance_name="test", interval=0.1)
                result["status"] = "completed"
            except AgentTerminatedError as e:
                result["status"] = "terminated"
                result["error"] = e

        sleep_thread = threading.Thread(target=do_sleep)
        sleep_thread.start()

        # Let the sleep loop start, then trigger termination.
        time.sleep(0.25)  # Enough for at least one iteration to enter sleep().
        terminate_event.set()

        sleep_thread.join(timeout=2.0)
        assert not sleep_thread.is_alive(), "Sleep thread did not exit promptly"
        assert result.get("status") == "terminated", \
            f"Expected AgentTerminatedError, got status={result.get('status')}"


# ===========================================================================
# Integration Tests: dismiss_instance() and terminate_instance()
# ===========================================================================

class TestDismissInstanceSetsTerminationFlag:
    """dismiss_instance() sets the durable termination flag on the instance."""

    def test_dismiss_active_instance_sets_is_terminated(self, agent_pool):
        """Dismissing an active (RUNNING) instance should set is_terminated=True."""
        inst = make_instance("busy", state=AgentState.RUNNING)
        agent_pool.instances["busy"] = inst

        agent_pool.dismiss_instance("busy")

        # Instance may be removed from pool, but its flag persists.
        assert inst.is_terminated is True

    def test_dismiss_idle_instance_sets_is_terminated(self, agent_pool):
        """Dismissing an IDLE instance should also set is_terminated=True."""
        inst = make_instance("idle_worker", state=AgentState.IDLE)
        agent_pool.instances["idle_worker"] = inst

        agent_pool.dismiss_instance("idle_worker")

        assert inst.is_terminated is True

    def test_dismiss_removes_instance_from_pool(self, agent_pool):
        """dismiss_instance() removes the instance from pool.instances."""
        inst = make_instance("gone", state=AgentState.IDLE)
        agent_pool.instances["gone"] = inst

        agent_pool.dismiss_instance("gone")

        assert "gone" not in agent_pool.instances


class TestTerminateInstanceCallsInstanceTerminate:
    """terminate_instance() correctly chains into inst.terminate()."""

    def test_terminate_instance_sets_is_terminated(self, agent_pool):
        """terminate_instance() should call inst.terminate(), setting is_terminated=True."""
        inst = make_instance("target", state=AgentState.RUNNING)
        agent_pool.instances["target"] = inst

        agent_pool.terminate_instance("target")

        assert inst.is_terminated is True
        assert inst.state == AgentState.TERMINATED

    def test_terminate_instance_adds_to_terminated_set(self, agent_pool):
        """terminate_instance() adds the name to terminated_instances."""
        inst = make_instance("target2", state=AgentState.RUNNING)
        agent_pool.instances["target2"] = inst

        agent_pool.terminate_instance("target2")

        assert "target2" in agent_pool.terminated_instances

    def test_terminate_instance_clears_streaming_responses(self, agent_pool):
        """Via inst.terminate(), streaming responses should be cleared."""
        inst = make_instance("target3", state=AgentState.RUNNING)
        with inst._compression_lock:
            inst._streaming_responses.append("leak")
        agent_pool.instances["target3"] = inst

        agent_pool.terminate_instance("target3")

        assert len(inst._streaming_responses) == 0

    def test_terminate_instance_set_global_stopped_false_does_not_affect_pool(self, agent_pool):
        """Bug5 Fix: terminate_instance with set_global_stopped=False must not set pool.stopped."""
        inst = make_instance("target4", state=AgentState.RUNNING)
        agent_pool.instances["target4"] = inst

        assert agent_pool.stopped is False
        agent_pool.terminate_instance("target4", set_global_stopped=False)
        # Instance should be terminated but pool-wide stopped flag must remain False.
        assert inst.is_terminated is True
        assert agent_pool.stopped is False, \
            "set_global_stopped=False should not affect pool.stopped"

    def test_terminate_instance_set_global_stopped_true_sets_pool_stopped(self, agent_pool):
        """With set_global_stopped=True, pool.stopped becomes True."""
        inst = make_instance("target5", state=AgentState.RUNNING)
        agent_pool.instances["target5"] = inst

        assert agent_pool.stopped is False
        agent_pool.terminate_instance("target5", set_global_stopped=True)
        assert agent_pool.stopped is True
