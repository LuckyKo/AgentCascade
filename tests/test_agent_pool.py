"""Integration tests for AgentPool message queue, dismissal, and state management.

These tests exercise AgentPool with real (but minimal) dependencies — no LLM needed.
We patch OperationManager / TelemetryCollector to avoid disk I/O side-effects.
"""

import threading
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixture: build a minimal AgentPool without hitting the filesystem
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_pool():
    """Create an AgentPool with mocked dependencies so it can be instantiated."""
    # OperationManager is imported inside __init__, TelemetryCollector and APIRouter at module level
    with patch('agent_cascade.operation_manager.OperationManager') as mock_op_mgr, \
         patch('agent_cascade.telemetry.TelemetryCollector') as mock_telem, \
         patch('agent_cascade.api_router.APIRouter') as mock_router:

        # Set up OperationManager mock
        op_mgr = MagicMock()
        op_mgr.base_dir = MagicMock()
        op_mgr.base_dir.__str__ = lambda self: '/tmp/test_workspace'
        op_mgr.extra_work_folders_ro = []
        op_mgr.extra_work_folders_rw = []
        mock_op_mgr.return_value = op_mgr

        # Set up APIRouter mock
        router = MagicMock()
        router.get_effective_concurrency.return_value = 3
        mock_router.return_value = router

        from agent_cascade.agent_pool import AgentPool
        pool = AgentPool(
            llm_cfg={'max_parallel_agents': 2},
            agents_dir='/tmp/fake_agents',
            workspace_dir='/tmp/test_workspace',
        )
        # Configure pool settings (idle timeout, etc.) — applied after construction
        if hasattr(pool, 'settings'):  # TODO: Remove hasattr guard once old pool files are fully removed
            pool.settings.idle_timeout_seconds = 60.0
            pool.settings.idle_check_interval = 30.0
        # Start then immediately stop idle checker (exercises startup path for regression testing)
        pool.start()
        pool._idle.stop()
        return pool


# ===========================================================================
# Message queue: enqueue / drain / dedup
# ===========================================================================

class TestMessageQueue:
    """Test per-agent message queue enqueue/drain/dedup logic."""

    def test_enqueue_and_drain(self, agent_pool):
        agent_pool.enqueue_message("worker1", "task A")
        agent_pool.enqueue_message("worker1", "task B")
        msgs = agent_pool.drain_queue("worker1")
        assert msgs == ["task A", "task B"]

    def test_drain_empty(self, agent_pool):
        msgs = agent_pool.drain_queue("nobody")
        assert msgs == []

    def test_drain_consumes_messages(self, agent_pool):
        """After drain, the queue should be empty."""
        agent_pool.enqueue_message("w1", "msg1")
        agent_pool.drain_queue("w1")
        msgs = agent_pool.drain_queue("w1")
        assert msgs == []

    def test_has_messages_true(self, agent_pool):
        agent_pool.enqueue_message("w1", "hello")
        assert agent_pool.has_messages("w1") is True

    def test_has_messages_false(self, agent_pool):
        assert agent_pool.has_messages("nobody") is False

    def test_multiple_agents_isolated_queues(self, agent_pool):
        """Different agents have separate queues."""
        agent_pool.enqueue_message("a", "msg_a")
        agent_pool.enqueue_message("b", "msg_b")
        assert agent_pool.drain_queue("a") == ["msg_a"]
        assert agent_pool.drain_queue("b") == ["msg_b"]


# ===========================================================================
# Agent dismissal
# ===========================================================================

class TestDismissal:
    """Test that dismissing agents cleans up state correctly."""

    def test_dismiss_inactive_agent_clears_conversation(self, agent_pool):
        """Dismissing an inactive agent should clear its conversation."""
        from agent_cascade.agent_instance import AgentInstance
        from agent_cascade.llm.schema import Message
        import time
        inst = AgentInstance(
            instance_name="ghost",
            agent_class="researcher",
            conversation=[Message(role="user", content="hi")],
            max_turns=None,
            parent_instance=None,
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            compression_summary=None,
            latest_marker_index=-1,
        )
        agent_pool.instances["ghost"] = inst
        agent_pool.dismiss_instance("ghost")
        assert "ghost" not in agent_pool.instances or \
                len(agent_pool.get_conversation("ghost")) == 0

    def test_dismiss_active_agent_sets_stop_flag(self, agent_pool):
        """Dismissing an active agent should set the stopped flag."""
        from agent_cascade.agent_instance import AgentInstance, AgentState
        import time
        inst = AgentInstance(
            instance_name="busy_agent",
            agent_class="researcher",
            conversation=[],
            state=AgentState.RUNNING,
            max_turns=None,
            parent_instance=None,
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            compression_summary=None,
            latest_marker_index=-1,
        )
        agent_pool.instances["busy_agent"] = inst
        assert not agent_pool.stopped
        agent_pool.dismiss_instance("busy_agent")
        assert agent_pool.stopped is True

    def test_dismiss_fires_callbacks(self, agent_pool):
        """Dismiss callbacks should be fired via _fire_on_dismissed."""
        received = []
        agent_pool.on_dismissed(lambda name, log: received.append(name))
        agent_pool._fire_on_dismissed("worker1", "/tmp/log.jsonl")
        assert "worker1" in received

    def test_dismiss_callback_error_is_caught(self, agent_pool):
        """A callback that raises should not prevent other callbacks from running."""
        results = []
        agent_pool.on_dismissed(lambda n, l: results.append("good"))
        agent_pool.on_dismissed(lambda n, l: (_ for _ in ()).throw(RuntimeError("boom")))
        agent_pool.on_dismissed(lambda n, l: results.append("also_good"))
        agent_pool._fire_on_dismissed("w1")
        assert results == ["good", "also_good"]

    def test_terminate_instance_sets_stop_when_active(self, agent_pool):
        """Terminating an active instance should set the stopped flag."""
        from agent_cascade.agent_instance import AgentInstance, AgentState
        import time
        inst = AgentInstance(
            instance_name="term_agent",
            agent_class="researcher",
            conversation=[],
            state=AgentState.RUNNING,
            max_turns=None,
            parent_instance=None,
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            compression_summary=None,
            latest_marker_index=-1,
        )
        agent_pool.instances["term_agent"] = inst
        agent_pool.terminate_instance("term_agent", set_global_stopped=True)  # Bug5: explicitly request global stop
        assert agent_pool.stopped is True
        assert "term_agent" in agent_pool.terminated_instances

    def test_terminate_instance_no_stop_when_inactive(self, agent_pool):
        """Terminating an inactive instance only marks it, doesn't stop."""
        agent_pool.terminate_instance("inactive_agent")
        assert agent_pool.stopped is False
        assert "inactive_agent" in agent_pool.terminated_instances


# ===========================================================================
# Halt / resume lifecycle
# ===========================================================================

class TestHaltLifecycle:
    """Test per-instance halt/resume for forced compression."""

    def test_halt_and_resume(self, agent_pool):
        agent_pool.halt_instance("w1")
        assert agent_pool.is_halted("w1") is True
        agent_pool.resume_instance("w1")
        assert agent_pool.is_halted("w1") is False

    def test_halt_all_except_one(self, agent_pool):
        """halt_all_instances halts all instances except the one specified."""
        from agent_cascade.agent_instance import AgentInstance
        import time
        # Create actual instances so halt_all_instances can find them
        for name in ("a", "b"):
            inst = AgentInstance(
                instance_name=name,
                agent_class="researcher",
                conversation=[],
                max_turns=None,
                parent_instance=None,
                created_at=time.monotonic(),
                last_activity=time.monotonic(),
                compression_summary=None,
                latest_marker_index=-1,
            )
            agent_pool.instances[name] = inst
        # "c" is excluded — just needs to not be in self.instances
        agent_pool.active_stack_append("c", 0)
        agent_pool.halt_all_instances(except_instance="c")
        assert agent_pool.is_halted("a") is True
        assert agent_pool.is_halted("b") is True
        assert agent_pool.is_halted("c") is False

    def test_resume_all_only_compression_halted(self, agent_pool):
        """resume_all_instances should only clear compression-halted instances."""
        # Manually halt "a" (not via compression)
        agent_pool.halt_instance("a")
        # Halt "b" via compression path
        from agent_cascade.agent_instance import AgentInstance
        import time
        for name in ("b",):
            inst = AgentInstance(
                instance_name=name,
                agent_class="researcher",
                conversation=[],
                max_turns=None,
                parent_instance=None,
                created_at=time.monotonic(),
                last_activity=time.monotonic(),
                compression_summary=None,
                latest_marker_index=-1,
            )
            agent_pool.instances[name] = inst
        agent_pool.halt_all_instances(except_instance="c_nonexistent")
        # Now resume all — only "b" should be resumed (was compression-halted)
        agent_pool.resume_all_instances()
        assert agent_pool.is_halted("a") is True   # still halted (manual)
        assert agent_pool.is_halted("b") is False  # resumed

    def test_state_lock_protection(self, agent_pool):
        """State mutations should be lock-guarded via _state_lock."""
        import threading
        assert isinstance(agent_pool._execution._state_lock, type(threading.RLock()))


# ===========================================================================
# Conversation snapshots and rollback
# ===========================================================================

class TestSnapshots:
    """Test capture_snapshots / rollback_to_snapshots."""

    def test_capture_and_rollback(self, agent_pool):
        from agent_cascade.agent_instance import AgentInstance
        from agent_cascade.llm.schema import Message
        import time
        # Create an actual instance with a conversation
        inst = AgentInstance(
            instance_name="w1",
            agent_class="researcher",
            conversation=[
                Message(role="system", content="sys"),
                Message(role="user", content="hi"),
                Message(role="assistant", content="ok"),
            ],
            max_turns=None,
            parent_instance=None,
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            compression_summary=None,
            latest_marker_index=-1,
        )
        agent_pool.instances["w1"] = inst

        snaps = agent_pool.capture_snapshots()
        assert snaps["w1"] == 3

        # Add more messages
        with inst._compression_lock:
            inst.conversation.append(Message(role="user", content="more"))
            inst.conversation.append(Message(role="assistant", content="done"))

        # Rollback
        agent_pool.rollback_to_snapshots(snaps)
        assert len(agent_pool.get_conversation("w1")) == 3


# ===========================================================================
# Thread-safety of state mutations
# ===========================================================================

class TestThreadSafety:
    """Test that concurrent state mutations don't corrupt AgentPool."""

    def test_concurrent_enqueue_drain(self, agent_pool):
        """Multiple threads enqueueing and draining the same queue should not crash."""
        errors = []

        def enqueuer(n):
            try:
                for i in range(50):
                    agent_pool.enqueue_message("w1", f"msg-{n}-{i}")
            except Exception as e:
                errors.append(str(e))

        def drainer():
            try:
                for _ in range(25):
                    agent_pool.drain_queue("w1")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=enqueuer, args=(i,)) for i in range(4)]
        threads += [threading.Thread(target=drainer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent queue errors: {errors}"

    def test_concurrent_halt_resume(self, agent_pool):
        """Concurrent halt/resume on the same instance should not crash."""
        errors = []

        def halter():
            try:
                for _ in range(50):
                    agent_pool.halt_instance("w1")
            except Exception as e:
                errors.append(str(e))

        def resumer():
            try:
                for _ in range(50):
                    agent_pool.resume_instance("w1")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=halter), threading.Thread(target=resumer)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent halt/resume errors: {errors}"


# ===========================================================================
# last_tool_args initialization
# ===========================================================================

class TestLastToolArgs:
    """Test that last_tool_args is properly initialized and accessible."""

    def test_last_tool_args_initialized(self, agent_pool):
        assert isinstance(agent_pool.last_tool_args, dict)

    def test_last_tool_args_can_be_written(self, agent_pool):
        """The streaming path writes to this; verify it's writable."""
        agent_pool.last_tool_args["scope1"] = {"tool1": {"arg": "val"}}
        assert agent_pool.last_tool_args["scope1"]["tool1"]["arg"] == "val"