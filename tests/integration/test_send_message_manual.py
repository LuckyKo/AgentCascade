"""Manual integration test for the send_message tool.

This script starts a minimal AgentPool with two mock agents and exercises:
1. Agent A → Agent B messaging (queue entry verified)
2. Agent A → user messaging (WebSocket event format verified when WS is available)

Run standalone:
    python tests/integration/test_send_message_manual.py

No LLM or full server required — uses mocked dependencies only.
"""

import asyncio
import json
import os
import sys
import threading
from unittest.mock import MagicMock

# Ensure project root and config dir are on the path
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def make_mock_agent(name, state=None):
    """Create a lightweight mock agent instance."""
    from agent_cascade.agent_instance import AgentState
    agent = MagicMock()
    agent.name = name
    agent.state = state or AgentState.RUNNING
    return agent


class FakeAgentPool:
    """Minimal pool implementation with only what send_message needs.

    Avoids full AgentPool startup (thread/logging issues in standalone mode).
    Provides: _pool_lock, instances dict, enqueue_message/drain_queue.
    """

    def __init__(self):
        self._pool_lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self.message_queues = {}
        self.instances = {}

    def enqueue_message(self, instance_name: str, text: str):
        with self._queue_lock:
            self.message_queues.setdefault(instance_name, []).append(text)

    def drain_queue(self, instance_name: str):
        with self._queue_lock:
            return self.message_queues.pop(instance_name, [])


def setup_agent_pool():
    """Build a FakeAgentPool with two RUNNING agents."""
    from agent_cascade.agent_instance import AgentState

    pool = FakeAgentPool()

    with pool._pool_lock:
        pool.instances['agentA'] = make_mock_agent('agentA', AgentState.RUNNING)
        pool.instances['agentB'] = make_mock_agent('agentB', AgentState.RUNNING)

    return pool


def test_agent_to_agent(pool):
    """Agent A sends message to Agent B → verify queue entry with sender tag."""
    print("\n[TEST] Agent-to-Agent Messaging")
    print("-" * 40)

    from agent_cascade.tools.custom.send_message import SendMessage

    tool = SendMessage(agent_pool=pool)

    # Simulate that agentA is the current sender via thread-local
    from agent_cascade.operation_manager.path_security import _thread_locals
    original_name = getattr(_thread_locals, 'instance_name', None)
    _thread_locals.instance_name = 'agentA'

    try:
        params = json.dumps({
            'destination': 'agentB',
            'message': 'Integration test message from agentA.'
        })
        result = tool.call(params)

        print(f"  send_message result: {result}")

        # Verify success response
        assert "sent successfully" in result.lower(), f"Expected success, got: {result}"

        # Drain and verify queued message
        msgs = pool.drain_queue('agentB')
        assert len(msgs) == 1, f"Expected 1 message in queue, got {len(msgs)}"

        expected_tag = "[MESSAGE from agentA]: Integration test message from agentA."
        assert msgs[0] == expected_tag, f"Unexpected message format: {msgs[0]}"

        print(f"  ✓ Message queued correctly: {msgs[0]}")
        return True
    finally:
        # Restore original thread-local
        if original_name is not None:
            _thread_locals.instance_name = original_name
        else:
            delattr(_thread_locals, 'instance_name')


def test_agent_to_user(pool):
    """Agent sends message to user → verify WebSocket event format."""
    print("\n[TEST] Agent-to-User Messaging (WebSocket)")
    print("-" * 40)

    from agent_cascade.tools.custom.send_message import SendMessage

    # Set up a fake WebSocket queue and running loop
    ws_queue = asyncio.Queue(maxsize=10)
    ws_loop = asyncio.new_event_loop()

    def run_loop():
        asyncio.set_event_loop(ws_loop)
        ws_loop.run_forever()

    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()

    pool._ws_send_queue = ws_queue
    pool._ws_loop = ws_loop

    tool = SendMessage(agent_pool=pool)

    # Set sender identity
    from agent_cascade.operation_manager.path_security import _thread_locals
    original_name = getattr(_thread_locals, 'instance_name', None)
    _thread_locals.instance_name = 'agentA'

    try:
        params = json.dumps({
            'destination': 'user',
            'message': 'Build completed successfully!'
        })
        result = tool.call(params)

        print(f"  send_message result: {result}")
        assert "sent successfully" in result.lower(), f"Expected success, got: {result}"

        # Read event from queue
        def get_with_timeout():
            return asyncio.wait_for(ws_queue.get(), timeout=2.0)

        future = asyncio.run_coroutine_threadsafe(get_with_timeout(), ws_loop)
        event = future.result(timeout=3.0)

        print(f"  WebSocket event: {event}")

        assert event['type'] == 'agent_message_to_user', f"Wrong event type: {event.get('type')}"
        assert event['sender'] == 'agentA', f"Wrong sender: {event.get('sender')}"
        assert event['message'] == 'Build completed successfully!', f"Wrong message: {event.get('message')}"
        assert 'timestamp' in event, "Missing timestamp in event"

        print("  ✓ WebSocket event format verified")
        return True
    finally:
        if original_name is not None:
            _thread_locals.instance_name = original_name
        else:
            delattr(_thread_locals, 'instance_name')

        ws_loop.call_soon_threadsafe(ws_loop.stop)
        loop_thread.join(timeout=2.0)


def main():
    print("=" * 50)
    print("send_message Integration Test Suite")
    print("=" * 50)

    pool = None
    passed = 0
    failed = 0

    try:
        pool = setup_agent_pool()
        print("\n✓ FakeAgentPool created with agents: agentA, agentB")

        # Test 1: Agent-to-Agent
        try:
            if test_agent_to_agent(pool):
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

        # Test 2: Agent-to-User
        try:
            if test_agent_to_user(pool):
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ✗ FAILED: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    finally:
        pool = None

    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
