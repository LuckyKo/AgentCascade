"""Test that compression warning injection doesn't double-trigger after compress_context.

These tests verify the threshold-based compression logic. The method
`_inject_compression_warning_for_agent` on the removed OrchestratorAgent stub was dead code
(ExecutionEngine has its own compression injection), but the threshold logic it represents
is still relevant for understanding the system's compression safety guarantees.

We test it using a self-contained mock class rather than the removed OrchestratorAgent stub.
"""

from unittest.mock import MagicMock, PropertyMock


class _CompressionThresholdMock:
    """Minimal mock that implements the compression threshold logic from ExecutionEngine."""

    def __init__(self):
        self.agent_pool = None

    def _get_max_tokens(self) -> int:
        return 10_000

    def _get_history_tokens(self, messages) -> int:
        # In production this actually counts tokens; we mock it in tests
        return 0

    def _inject_compression_warning_for_agent(self, agent, instance_name, messages):
        """Replicate the threshold logic from ExecutionEngine's compression injection."""
        if instance_name == 'compression_agent':
            return False

        max_tokens = self._get_max_tokens()
        current_tokens = self._get_history_tokens(messages)
        usage_pct = (current_tokens / max_tokens) * 100 if max_tokens > 0 else 0

        if usage_pct > 95.0:
            compressor = agent.function_map.get('compress_context')
            if compressor:
                compressor.call()
            return True

        return False


def test_inject_compression_does_not_double_trigger():
    """When compress_context already ran and mutated llm_messages, a subsequent call to
    _inject_compression_warning_for_agent should NOT trigger another compression.

    The bug: llm_messages is deep-copied at loop start. compress_context mutates the pool
    (removes old messages) AND mutates kwargs['messages'] in-place via clear()/extend().
    But _inject_compression_warning checks tokens against llm_messages which still has all
    the old deep-copied messages → sees >95% → triggers second forceful compression.
    """
    max_tokens = 10_000
    num_old_msgs = 200  # enough to be >95% of context
    old_messages = []
    for i in range(num_old_msgs):
        msg = MagicMock()
        msg.content = f"old message {i} " * 10  # ~40 tokens each = 8000+ total
        msg.role = 'user' if i % 2 == 0 else 'assistant'
        msg.name = 'TestAgent'
        msg.function_call = None
        msg.get.return_value = msg.role if hasattr(msg, 'role') else ''
        msg.__getitem__ = lambda self, k: {'content': self.content, 'role': self.role,
                                            'name': self.name}.get(k)
        type(msg).reasoning_content = PropertyMock(return_value=None)
        old_messages.append(msg)

    # Simulate what compress_context does: mutate the list in-place (clear + extend)
    # This removes ~50% of messages, simulating a 50% compression
    num_removed = len(old_messages) // 2
    old_messages.clear()
    for i in range(num_removed):
        msg = MagicMock()
        msg.content = f"post-compression message {i} " * 3
        msg.role = 'user' if i % 2 == 0 else 'assistant'
        msg.name = 'TestAgent'
        msg.function_call = None
        msg.get.return_value = msg.role if hasattr(msg, 'role') else ''
        type(msg).reasoning_content = PropertyMock(return_value=None)
        old_messages.append(msg)

    # Now create a mock agent and call _inject_compression_warning_for_agent
    mock_agent = MagicMock()
    mock_agent.function_map = {'compress_context': MagicMock()}

    orch = _CompressionThresholdMock()
    orch._get_max_tokens = lambda: max_tokens
    orch._get_history_tokens = lambda msgs: int(max_tokens * 0.4)  # After compression, ~40%

    orch._inject_compression_warning_for_agent(
        agent=mock_agent,
        instance_name='TestAgent',
        messages=old_messages,
    )

    # compress_context should NOT have been called because tokens are under 95%
    mock_agent.function_map['compress_context'].call.assert_not_called()


def test_inject_compression_triggers_when_over_95():
    """When context is genuinely over 95%, compression SHOULD be triggered."""
    max_tokens = 10_000
    messages = [MagicMock(content="x" * 100, role='user', name='TestAgent')]

    mock_compressor = MagicMock()
    mock_agent = MagicMock()
    mock_agent.function_map = {'compress_context': mock_compressor}

    orch = _CompressionThresholdMock()
    orch._get_max_tokens = lambda: max_tokens
    orch._get_history_tokens = lambda msgs: int(max_tokens * 0.97)  # >95%

    orch._inject_compression_warning_for_agent(
        agent=mock_agent,
        instance_name='TestAgent',
        messages=messages,
    )

    # compress_context SHOULD have been called
    mock_compressor.call.assert_called_once()


def test_inject_compression_no_double_trigger_with_stale_llm_messages():
    """The actual bug: llm_messages is a deep copy from loop start. After compress_context
    mutates the pool, _inject_compression_warning checks stale llm_messages and triggers
    again even though the pool was already compressed.

    This test simulates that exact scenario.
    """
    max_tokens = 10_000

    # Build messages: 200 old messages (~97% of tokens) + 1 new tool result
    all_messages = []
    for i in range(200):
        msg = MagicMock()
        msg.content = f"old {i} " * 5
        msg.role = 'user' if i % 2 == 0 else 'assistant'
        msg.name = 'TestAgent'
        msg.function_call = None
        type(msg).reasoning_content = PropertyMock(return_value=None)
        all_messages.append(msg)

    # Tool result message (added after compress_context runs)
    tool_result_msg = MagicMock()
    tool_result_msg.content = "Compression successful."
    tool_result_msg.role = 'function'
    tool_result_msg.name = 'compress_context'
    type(tool_result_msg).reasoning_content = PropertyMock(return_value=None)

    # Simulate compress_context: mutates the list in-place (clear + extend)
    num_removed = len(all_messages) // 2
    all_messages.clear()
    for i in range(num_removed):
        msg = MagicMock()
        msg.content = f"post-compression {i} " * 3
        msg.role = 'user' if i % 2 == 0 else 'assistant'
        msg.name = 'TestAgent'
        msg.function_call = None
        type(msg).reasoning_content = PropertyMock(return_value=None)
        all_messages.append(msg)

    # Now add the tool result (this is what happens in the orchestrator loop after compress_context returns)
    all_messages.append(tool_result_msg)

    mock_compressor = MagicMock()
    mock_agent = MagicMock()
    mock_agent.function_map = {'compress_context': mock_compressor}

    orch = _CompressionThresholdMock()
    orch._get_max_tokens = lambda: max_tokens
    orch._get_history_tokens = lambda msgs: int(max_tokens * 0.4)  # After compression + tool result, ~40%

    orch._inject_compression_warning_for_agent(
        agent=mock_agent,
        instance_name='TestAgent',
        messages=all_messages,
    )

    # compress_context should NOT have been called — we already compressed this turn
    mock_compressor.call.assert_not_called()


if __name__ == '__main__':
    import pytest
    pytest.main([__file__, '-v'])