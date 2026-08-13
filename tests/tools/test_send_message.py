"""Unit tests for the send_message tool.

Tests agent-to-agent messaging, agent-to-user messaging, error handling,
and sender identity — all without requiring a full server or LLM.
"""

import json
import threading
from unittest.mock import patch, MagicMock, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Helpers: build a minimal AgentPool + mock agents for send_message tests
# ---------------------------------------------------------------------------

def _make_mock_agent(name, state=None):
    """Create a lightweight mock agent instance with required attributes."""
    from agent_cascade.agent_instance import AgentState
    agent = MagicMock()
    agent.name = name
    if state is not None:
        agent.state = state
    else:
        agent.state = AgentState.RUNNING  # default active state
    return agent


@pytest.fixture
def agent_pool_with_agents():
    """Create an AgentPool with two mock RUNNING agents ('agentA', 'agentB')."""
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
        pool.start()
        pool._idle.stop()

        # Insert mock agents into the pool's instances dict (under lock)
        from agent_cascade.agent_instance import AgentState
        agent_a = _make_mock_agent('agentA', AgentState.RUNNING)
        agent_b = _make_mock_agent('agentB', AgentState.RUNNING)

        with pool._pool_lock:
            pool.instances['agentA'] = agent_a
            pool.instances['agentB'] = agent_b

        yield pool


@pytest.fixture
def send_message_tool(agent_pool_with_agents):
    """Create a SendMessage tool bound to the test pool."""
    from agent_cascade.tools.custom.send_message import SendMessage
    return SendMessage(agent_pool=agent_pool_with_agents)


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------

class TestSendMessageInputValidation:
    """Test that invalid inputs are rejected with clear error messages."""

    def test_no_agent_pool_returns_error(self):
        """When agent_pool is None, call() returns an error."""
        from agent_cascade.tools.custom.send_message import SendMessage
        tool = SendMessage(agent_pool=None)
        params = json.dumps({'destination': 'user', 'message': 'hello'})
        result = tool.call(params)
        assert "No agent pool available" in result

    def test_empty_destination_returns_error(self, send_message_tool):
        """Empty destination string returns a clear error."""
        params = json.dumps({'destination': '', 'message': 'hello'})
        result = send_message_tool.call(params)
        assert "Failed" in result and "Destination cannot be empty" in result

    def test_whitespace_only_destination_returns_error(self, send_message_tool):
        """Whitespace-only destination is treated as empty."""
        params = json.dumps({'destination': '   ', 'message': 'hello'})
        result = send_message_tool.call(params)
        assert "Failed" in result and "Destination cannot be empty" in result

    def test_empty_message_returns_error(self, send_message_tool):
        """Empty message content returns a clear error."""
        params = json.dumps({'destination': 'user', 'message': ''})
        result = send_message_tool.call(params)
        assert "Failed" in result and "Message content cannot be empty" in result

    def test_whitespace_only_message_returns_error(self, send_message_tool):
        """Whitespace-only message is treated as empty."""
        params = json.dumps({'destination': 'user', 'message': '   \t\n  '})
        result = send_message_tool.call(params)
        assert "Failed" in result and "Message content cannot be empty" in result


# ---------------------------------------------------------------------------
# Agent-to-agent messaging tests
# ---------------------------------------------------------------------------

class TestSendMessageAgentToAgent:
    """Test agent-to-agent message queuing with sender tagging."""

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='agentA')
    def test_basic_agent_to_agent_queues_message(self, mock_get_name, send_message_tool, agent_pool_with_agents):
        """Agent A sends to Agent B → message is queued with sender tag."""
        params = json.dumps({
            'destination': 'agentB',
            'message': 'Hey agentB, please check this out.'
        })
        result = send_message_tool.call(params)

        assert "sent successfully" in result.lower() and "'agentB'" in result

        # Verify the queued message has the sender tag
        msgs = agent_pool_with_agents.drain_queue('agentB')
        assert len(msgs) == 1
        assert '[MESSAGE from agentA]: Hey agentB, please check this out.' == msgs[0]

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='agentA')
    def test_sender_identity_is_correct(self, mock_get_name, send_message_tool, agent_pool_with_agents):
        """Verify sender is obtained via _get_current_instance_name()."""
        params = json.dumps({
            'destination': 'agentB',
            'message': 'test'
        })
        send_message_tool.call(params)

        mock_get_name.assert_called()
        msgs = agent_pool_with_agents.drain_queue('agentB')
        assert msgs[0].startswith('[MESSAGE from agentA]:')

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='agentA')
    def test_self_message_is_rejected(self, mock_get_name, send_message_tool, agent_pool_with_agents):
        """Agent cannot send a message to itself."""
        params = json.dumps({
            'destination': 'agentA',
            'message': 'hello me'
        })
        result = send_message_tool.call(params)

        assert "Failed" in result and "yourself" in result.lower()
        # No message should be queued
        assert agent_pool_with_agents.drain_queue('agentA') == []

    def test_invalid_destination_no_such_agent(self, send_message_tool):
        """Sending to a non-existent agent returns an error."""
        params = json.dumps({
            'destination': 'nonexistent',
            'message': 'hello'
        })
        result = send_message_tool.call(params)

        assert "Failed" in result and "'nonexistent'" in result and "exists" in result.lower()

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='agentA')
    def test_inactive_agent_destination_rejected(self, mock_get_name, send_message_tool, agent_pool_with_agents):
        """Sending to an inactive agent (IDLE/TERMINATED) returns an error."""
        from agent_cascade.agent_instance import AgentState

        # Set agentB to IDLE
        with agent_pool_with_agents._pool_lock:
            agent_pool_with_agents.instances['agentB'].state = AgentState.IDLE

        params = json.dumps({
            'destination': 'agentB',
            'message': 'hello'
        })
        result = send_message_tool.call(params)

        assert "Failed" in result and "IDLE" in result


# ---------------------------------------------------------------------------
# Agent-to-user messaging tests
# ---------------------------------------------------------------------------

class TestSendMessageAgentToUser:
    """Test agent-to-user WebSocket notification path."""

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='worker1')
    def test_send_to_user_with_no_websocket_degrades_gracefully(self, mock_get_name, send_message_tool):
        """When pool has no WebSocket setup, returns warning but doesn't crash."""
        params = json.dumps({
            'destination': 'user',
            'message': 'Task complete!'
        })
        result = send_message_tool.call(params)

        # Should degrade gracefully with a warning
        assert "Warning" in result or "sent" in result.lower()

    @patch('agent_cascade.tools.custom.send_message._get_current_instance_name', return_value='worker1')
    def test_send_to_user_event_format(self, mock_get_name, send_message_tool, agent_pool_with_agents):
        """Verify the WebSocket event has correct format when WS is available."""
        import asyncio

        # Set up a fake WebSocket queue and running loop on the pool.
        # run_coroutine_threadsafe requires an *running* event loop in another thread.
        ws_queue = asyncio.Queue(maxsize=10)
        ws_loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(ws_loop)
            ws_loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        agent_pool_with_agents._ws_send_queue = ws_queue
        agent_pool_with_agents._ws_loop = ws_loop

        params = json.dumps({
            'destination': 'user',
            'message': 'Build finished successfully.'
        })
        result = send_message_tool.call(params)

        assert "sent successfully" in result.lower()

        # Read the event from the queue using run_coroutine_threadsafe since loop is running in another thread
        def get_with_timeout():
            return asyncio.wait_for(ws_queue.get(), timeout=2.0)

        try:
            future = asyncio.run_coroutine_threadsafe(get_with_timeout(), ws_loop)
            event = future.result(timeout=3.0)
        except (asyncio.TimeoutError, TimeoutError):
            pytest.fail("Expected an event to be queued for WebSocket")

        assert event['type'] == 'agent_message_to_user'
        assert event['sender'] == 'worker1'
        assert event['message'] == 'Build finished successfully.'
        assert 'timestamp' in event

        ws_loop.call_soon_threadsafe(ws_loop.stop)
        loop_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Edge cases and robustness tests
# ---------------------------------------------------------------------------

class TestSendMessageEdgeCases:
    """Test edge cases like missing params, malformed JSON, etc."""

    def test_missing_destination_param(self, send_message_tool):
        """Missing destination key in JSON → jsonschema validation error."""
        import jsonschema
        params = json.dumps({'message': 'hello'})
        with pytest.raises(jsonschema.ValidationError) as exc_info:
            send_message_tool.call(params)
        assert "'destination' is a required property" in str(exc_info.value)

    def test_missing_message_param(self, send_message_tool):
        """Missing message key in JSON → jsonschema validation error."""
        import jsonschema
        params = json.dumps({'destination': 'user'})
        with pytest.raises(jsonschema.ValidationError) as exc_info:
            send_message_tool.call(params)
        assert "'message' is a required property" in str(exc_info.value)

    def test_malformed_json_args_handled(self, send_message_tool):
        """Malformed JSON input should raise or return an error (BaseTool behavior)."""
        # BaseTool._verify_json_format_args raises ValueError on bad JSON
        with pytest.raises((ValueError, Exception)):
            send_message_tool.call("not json at all {{{")
