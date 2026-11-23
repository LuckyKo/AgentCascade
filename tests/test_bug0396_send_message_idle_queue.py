"""BUG_0396 regression: send_message to an IDLE agent must QUEUE, not reject.

A message sent to an IDLE agent is enqueued (delivered on that agent's next
explicit start) instead of being rejected; the agent must NOT be auto-started.
TERMINATED agents are gone and must still be rejected (their queue is popped at
teardown, so a queued message would be lost).

Self-contained: uses a minimal fake pool so the test runs without AgentPool
startup. See also tests/integration/test_send_message_manual.py (same behavior
against that file's FakeAgentPool) and tests/tools/test_send_message.py.
"""

import json
import threading
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import AgentState
from agent_cascade.tools.custom.send_message import SendMessage


def _make_mock_agent(name, state):
    agent = MagicMock()
    agent.name = name
    agent.state = state
    return agent


class _FakePool:
    """Minimal pool exposing only what send_message touches."""

    def __init__(self):
        self._pool_lock = threading.RLock()
        self._queue_lock = threading.Lock()
        self.message_queues = {}
        self.instances = {}

    def enqueue_message(self, instance_name, text):
        # No state check on purpose — matches the real enqueue_message contract.
        with self._queue_lock:
            self.message_queues.setdefault(instance_name, []).append(text)

    def get_queue_messages(self, instance_name):
        """Non-destructive peek (so the agent is never 'delivered'/'started')."""
        with self._queue_lock:
            return list(self.message_queues.get(instance_name, []))


@pytest.fixture
def pool():
    p = _FakePool()
    with p._pool_lock:
        p.instances['agentA'] = _make_mock_agent('agentA', AgentState.RUNNING)
    return p


def _set_sender(name):
    from agent_cascade.operation_manager.path_security import _thread_locals
    original = getattr(_thread_locals, 'instance_name', None)
    _thread_locals.instance_name = name
    return original


def _restore_sender(original):
    from agent_cascade.operation_manager.path_security import _thread_locals
    if original is not None:
        _thread_locals.instance_name = original
    else:
        delattr(_thread_locals, 'instance_name')


def test_send_to_idle_agent_queues_not_starts(pool):
    """IDLE target -> queued, feedback notes queued-not-delivered, agent stays IDLE."""
    from agent_cascade.agent_instance import AgentState as _AS

    with pool._pool_lock:
        pool.instances['idleB'] = _make_mock_agent('idleB', _AS.IDLE)

    tool = SendMessage(agent_pool=pool)
    original = _set_sender('agentA')
    try:
        result = tool.call(json.dumps({
            'destination': 'idleB',
            'message': 'please review when awake',
        }))

        # 1) feedback notes the idle, queued-not-delivered state.
        assert 'idle agent' in result.lower() and 'not delivered' in result.lower(), result
        assert 'ERROR' not in result, f"Expected queuing (no error), got: {result}"

        # 2) message IS queued (peek, not drained).
        msgs = pool.get_queue_messages('idleB')
        assert msgs == ['[MESSAGE from agentA]: please review when awake'], msgs

        # 3) the target is STILL IDLE — send_message did not start it.
        assert pool.instances['idleB'].state == _AS.IDLE
    finally:
        _restore_sender(original)


def test_send_to_terminated_agent_rejected(pool):
    """TERMINATED target -> rejected; nothing is queued."""
    from agent_cascade.agent_instance import AgentState as _AS

    with pool._pool_lock:
        pool.instances['deadB'] = _make_mock_agent('deadB', _AS.TERMINATED)

    tool = SendMessage(agent_pool=pool)
    original = _set_sender('agentA')
    try:
        result = tool.call(json.dumps({'destination': 'deadB', 'message': 'hi'}))

        assert 'ERROR' in result and 'TERMINATED' in result, result
        assert pool.get_queue_messages('deadB') == [], 'Nothing must be queued to a terminated agent'
    finally:
        _restore_sender(original)
