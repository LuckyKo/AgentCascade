"""BUG_0203 (F1/F3) regression tests.

F1: a ``[SYSTEM ERROR…`` assistant message is skipped when
     harvesting a summary, and the failure is classified non-retryable (so a broken
     LLM does not cause retry-forever re-sending the same payload).
F3: compression round-exhaustion → safe deterministic truncation fallback (bounded
     context, no ``ContextWindowExceeded``, no system error to the caller).
"""
import pytest
from unittest.mock import MagicMock

from agent_cascade.compression.agent_invoker import _execute_compressor_and_extract_summary
from agent_cascade.compression.core import truncate_context_for_fallback
from agent_cascade.llm.schema import Message
from agent_cascade.prompts.dna import COMPRESSION_END_MARKER
from tests.conftest import MockAgentPool


# ──────────────────────────────────────────────────────────────────────────────
# F1 — [SYSTEM ERROR / RETRYING] assistant messages are skipped during harvest
# ──────────────────────────────────────────────────────────────────────────────


class TestF1_SystemErrorAssistanceMessageSkipped:
    """BUG_0203 (F1): engine error strings committed as assistant messages are
    skipped during summary harvest and classified non-retryable."""

    def _make_comp_instance(self, assistant_content: str) -> MagicMock:
        """Build a mock compressor instance whose conversation ends with the given
        assistant message (simulating the engine committing an error string)."""
        comp_instance = MagicMock()
        # _compression_lock must be usable as a context manager.
        comp_instance._compression_lock = MagicMock()
        comp_instance._compression_lock.__enter__ = MagicMock(return_value=comp_instance._compression_lock)
        comp_instance._compression_lock.__exit__ = MagicMock(return_value=False)
        comp_instance.conversation = [
            Message(role='system', content='You are a compressor.'),
            Message(role='user', content='Summarize this conversation.'),
            Message(role='assistant', content=assistant_content),
        ]
        return comp_instance

    def _mock_engine(self) -> MagicMock:
        """Build a mock engine whose run() yields nothing (empty generator)."""
        engine = MagicMock()
        engine.run.return_value = iter([])
        engine._telemetry.return_value = None
        engine.reacquire_after_slot_yield = MagicMock()
        engine.save_before_slot_yield = MagicMock()
        return engine

    def _mock_pool(self) -> MockAgentPool:
        """Build a MockAgentPool with the minimal attributes the harvest path needs."""
        pool = MockAgentPool()
        pool.stopped = False
        pool.get_instance = MagicMock(return_value=None)  # no caller instance → no slot yield
        return pool

    def test_system_error_message_skipped_and_non_retryable(self):
        """A [SYSTEM ERROR… assistant message is skipped; the failure is non-retryable."""
        comp_instance = self._make_comp_instance('[SYSTEM ERROR: Empty LLM response]')
        engine = self._mock_engine()
        pool = self._mock_pool()

        with pytest.raises(RuntimeError) as exc_info:
            _execute_compressor_and_extract_summary(
                agent_pool=pool,
                engine=engine,
                comp_instance=comp_instance,
                comp_state_key='Compressor_test',
                caller_name='TestAgent',
                timeout_label='Compression',
            )

        err_msg = str(exc_info.value)
        # The error must NOT contain the retryable substrings (which would cause
        # retry-forever re-sending the same payload).
        assert 'missing end marker' not in err_msg.lower()
        assert 'empty summary' not in err_msg.lower()
        # The error MUST contain the F1-specific non-retryable message.
        assert 'LLM produced no output' in err_msg
        assert 'not retryable' in err_msg

    def test_valid_summary_still_harvested(self):
        """A valid assistant summary (with end marker) is still harvested correctly."""
        content = f"This is a valid summary of the conversation.\n{COMPRESSION_END_MARKER}"
        comp_instance = self._make_comp_instance(content)
        engine = self._mock_engine()
        pool = self._mock_pool()

        summary, caption = _execute_compressor_and_extract_summary(
            agent_pool=pool,
            engine=engine,
            comp_instance=comp_instance,
            comp_state_key='Compressor_test',
            caller_name='TestAgent',
            timeout_label='Compression',
        )
        assert 'valid summary' in summary.lower()
        assert caption == ''


# ──────────────────────────────────────────────────────────────────────────────
# F3 — round-exhaustion → safe deterministic truncation fallback
# ──────────────────────────────────────────────────────────────────────────────


class TestF3_TruncationFallback:
    """BUG_0203 (F3): truncate_context_for_fallback drops the oldest active messages
    to fit the target fraction, without an LLM call, keeping context bounded."""

    def _make_pool_with_history(self, n_messages: int = 10) -> MockAgentPool:
        """Build a MockAgentPool with a conversation of n_messages (plus system + U0)."""
        history = [
            Message(role='system', content='System prompt.'),
            Message(role='user', content='Initial user message (U0).'),
        ]
        for i in range(n_messages):
            role = 'user' if i % 2 == 0 else 'assistant'
            history.append(Message(role=role, content=f'Message {i}'))
        return MockAgentPool(history=history)

    def test_truncation_drops_oldest_and_inserts_marker(self):
        """truncate_context_for_fallback drops the oldest active messages and inserts
        a mechanical marker, keeping the context bounded."""
        pool = self._make_pool_with_history(n_messages=10)
        result = truncate_context_for_fallback(agent_pool=pool, target_agent_name='TestAgent')

        assert result.success, f"truncation failed: {result.error}"
        assert result.messages_discarded > 0
        assert result.marker_message is not None

        # The pool conversation must contain the truncation marker.
        conv = pool.get_conversation('TestAgent')
        marker_contents = [
            m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '')
            for m in conv
        ]
        assert any('truncated' in c.lower() and 'no summary' in c.lower() for c in marker_contents), (
            f"expected a truncation marker in the conversation, got: {marker_contents}")

    def test_truncation_no_active_messages_fails_gracefully(self):
        """If there are no active messages, truncation fails gracefully (no raise)."""
        pool = MockAgentPool(history=[Message(role='system', content='System prompt.')])
        result = truncate_context_for_fallback(agent_pool=pool, target_agent_name='TestAgent')
        assert not result.success
        assert result.error is not None

    def test_truncation_no_conversation_fails_gracefully(self):
        """If there is no conversation, truncation fails gracefully (no raise)."""
        pool = MockAgentPool()
        result = truncate_context_for_fallback(agent_pool=pool, target_agent_name='TestAgent')
        assert not result.success
        assert result.error is not None

    def test_truncate_context_for_fallback_deterministic(self):
        """truncate_context_for_fallback is a pure deterministic reduction:
        it drops the oldest active messages, inserts a marker, and returns a
        successful CompressResult — no LLM call, no raise, bounded context."""
        pool = self._make_pool_with_history(n_messages=10)
        original_len = len(pool.get_conversation('TestAgent'))

        result = truncate_context_for_fallback(agent_pool=pool, target_agent_name='TestAgent')
        assert result.success, f"truncation failed: {result.error}"
        assert result.messages_discarded > 0

        new_len = len(pool.get_conversation('TestAgent'))
        assert new_len < original_len, (
            f"context not bounded: {new_len} >= {original_len}")


# ──────────────────────────────────────────────────────────────────────────────
# F2b — endpoint cursor advanced between compression retry rounds
# ──────────────────────────────────────────────────────────────────────────────


class TestF2b_EndpointCursorAdvanced:
    """BUG_0203 (F2b): the compressor's endpoint cursor is advanced between
    retry rounds so each round tries a different endpoint."""

    def test_compressor_endpoint_is_advanced_between_rounds(self):
        """advance_instance_endpoint IS called for the compressor between rounds."""
        from unittest.mock import MagicMock, patch
        from agent_cascade.compression.agent_invoker import invoke_compression_agent

        pool = MagicMock()
        pool.stopped = False
        pool.api_router = MagicMock()
        pool.api_router.advance_instance_endpoint = MagicMock()
        # Make the compression agent fail with a retryable error so the loop continues.
        pool.invoke_compression_agent = MagicMock(
            side_effect=RuntimeError('missing end marker'))
        # Minimal attributes the function needs.
        pool.get_instance = MagicMock(return_value=None)
        pool.instance_state = {}
        pool.active_stack_remove = MagicMock()
        pool._execution = MagicMock()
        pool._execution._state_lock = MagicMock()

        with patch('agent_cascade.compression.agent_invoker.invoke_compression_agent',
                   side_effect=RuntimeError('missing end marker')):
            try:
                invoke_compression_agent(
                    agent_pool=pool,
                    target_messages=[Message(role='user', content='test')],
                    caller_name='TestAgent',
                )
            except RuntimeError:
                pass

        # The endpoint cursor must have been advanced at least once.
        pool.api_router.advance_instance_endpoint.assert_called()
