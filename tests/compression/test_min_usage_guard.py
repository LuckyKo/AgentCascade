"""Tests for the agent-triggered min-context-usage guard in compress_context().

The guard refuses AGENT-triggered compression when context usage is below a
threshold (default 50%). It must NOT affect /compress (trigger='user') or forced
(force=True) paths, and it must fail-open on any computation error.

Modeled on TestCompressContextConsolidationTrigger in test_memory_consolidation.py:
uses the MockAgentPool harness from conftest.py plus a patched invoke_compression_agent.
"""
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.compression.core import compress_context
from agent_cascade.llm.schema import SYSTEM, USER, Message
from tests.conftest import MockAgentPool

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


class _SettingsStub:
    """Minimal settings object for the guard (MockAgentPool has no settings)."""

    def __init__(self, reserve_tokens: int = 3000, min_usage_pct: float = 50.0):
        self.compression_context_reserve_tokens = reserve_tokens
        self.compression_min_usage_pct = min_usage_pct


def _build_history(num_pairs: int) -> List[Message]:
    """Build [SYSTEM] + num_pairs user/assistant pairs (no markers).

    No compression marker → active_set = history[2:], so there is always content
    to compress. Token count scales with num_pairs, letting tests control usage%.
    """
    history: List[Message] = [_make_msg(SYSTEM, 'You are a test agent')]
    for i in range(num_pairs):
        # Longish text so each message carries real tokens.
        body = f"User question number {i} about the weather and planning " * 3
        history.append(_make_msg(USER, body))
        history.append(_make_msg('assistant', f"Assistant reply number {i} with details " * 3))
    return history


def _make_pool(num_pairs: int, reserve_tokens: int = 3000, min_usage_pct: float = 50.0) -> MockAgentPool:
    pool = MockAgentPool(_build_history(num_pairs))
    pool.settings = _SettingsStub(reserve_tokens=reserve_tokens, min_usage_pct=min_usage_pct)
    # No api_router → step 3b uses get_agent('Compressor') (returns None on the mock),
    # so available_for_messages stays None and no discard-count capping happens.
    return pool


def _run(pool: MockAgentPool, max_tokens: int, **kwargs):
    """Run compress_context with _resolve_max_tokens pinned to a deterministic value."""
    kwargs.setdefault('agent_pool', pool)
    kwargs.setdefault('target_agent_name', 'TestAgent')
    kwargs.setdefault('fraction', 0.5)
    kwargs.setdefault('mode', 'auto')

    with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=max_tokens), \
         patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke:
        mock_invoke.return_value = ('Fresh compression summary', '')
        result = compress_context(**kwargs)
    return result, mock_invoke


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestMinUsageGuard:
    """Agent-triggered compression is refused below the min-usage threshold."""

    def test_agent_low_usage_refused(self):
        """(a) agent trigger + low usage (<50%) → refused, no LLM call, no pool mutation."""
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)
        conv_before = list(pool.get_conversation('TestAgent'))

        # max_tokens huge → usage well below 50%.
        result, mock_invoke = _run(pool, max_tokens=1_000_000)

        assert result.success is False
        assert 'min-usage guard' in (result.error or '')
        # Pure computation before refusal: no LLM call.
        mock_invoke.assert_not_called()
        # No pool mutation.
        assert pool.get_conversation('TestAgent') == conv_before

    def test_agent_high_usage_proceeds(self):
        """(b) agent trigger + high usage (>50%) → not refused by this guard."""
        pool = _make_pool(num_pairs=20, reserve_tokens=3000, min_usage_pct=50.0)

        # max_tokens small → usage well above 50% (guard passes).
        result, mock_invoke = _run(pool, max_tokens=2000)

        assert 'min-usage guard' not in (result.error or '')

    def test_user_trigger_low_usage_proceeds(self):
        """(c) trigger='user' + low usage → guard bypassed (not refused by it)."""
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        result, mock_invoke = _run(pool, max_tokens=1_000_000, trigger='user')

        assert 'min-usage guard' not in (result.error or '')

    def test_force_low_usage_proceeds(self):
        """(d) force=True + low usage → guard bypassed."""
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        result, mock_invoke = _run(pool, max_tokens=1_000_000, force=True)

        assert 'min-usage guard' not in (result.error or '')

    def test_dry_run_agent_low_usage_proceeds(self):
        """(e) dry_run=True agent + low usage → guard bypassed."""
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        result, mock_invoke = _run(pool, max_tokens=1_000_000, dry_run=True)

        assert 'min-usage guard' not in (result.error or '')


class TestMinUsageGuardFailOpen:
    """Any computation error must fail open (proceed), never block compression."""

    def test_resolve_max_tokens_raises_fails_open(self):
        """(f) _resolve_max_tokens raises → usage uncomputable → guard fails open."""
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        kwargs = dict(agent_pool=pool, target_agent_name='TestAgent', fraction=0.5, mode='auto')
        with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens',
                   side_effect=RuntimeError('boom')), \
             patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke:
            mock_invoke.return_value = ('Fresh compression summary', '')
            result = compress_context(**kwargs)

        assert 'min-usage guard' not in (result.error or '')

    def test_no_instance_fails_open(self):
        """No instance in the pool → usage uncomputable → guard fails open."""
        # Build a pool with history, then drop the instance so instances.get() → None.
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)
        pool.instances.clear()

        kwargs = dict(agent_pool=pool, target_agent_name='TestAgent', fraction=0.5, mode='auto')
        with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=1_000_000), \
             patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke:
            mock_invoke.return_value = ('Fresh compression summary', '')
            result = compress_context(**kwargs)

        assert 'min-usage guard' not in (result.error or '')


class TestMinUsageGuardToolSchemaTokens:
    """The guard's numerator must include tool-schema tokens, matching the engine.

    Pins the token-counting parity fix: a conversation whose MESSAGE-only usage is
    below the threshold but whose message+tool-schema usage is above it must be
    REFUSED — proving the guard counts tool schemas (not just messages).
    """

    def _make_pool_with_tools(self, num_pairs, reserve_tokens=3000, min_usage_pct=50.0):
        """Build a pool whose instance exposes agent_class and whose pool resolves a template."""
        pool = _make_pool(num_pairs=num_pairs, reserve_tokens=reserve_tokens, min_usage_pct=min_usage_pct)
        # Give the mock instance an agent_class and let the pool resolve a (dummy) template,
        # so the guard's tool-schema counting path actually executes.
        pool.instances['TestAgent'].agent_class = 'coder'
        pool.get_template = lambda name: object()  # truthy → proceed to function resolution
        return pool

    def test_tool_schema_tokens_push_over_threshold_proceeds(self):
        """Message-only < 50% but message+tools > 50% → guard PROCEEDS (not refused).

        This is the pin: WITHOUT tool-schema counting, message-only usage stays below the
        threshold and the guard would REFUSE. WITH the fix, the added tool tokens push usage
        over 50%, so compression is allowed — proving the guard counts tool schemas.
        """
        # Small conversation → message tokens are tiny. With max_tokens=10_000 and a reserve
        # of 3000, effective_limit = 7000; message-only usage is well under 50%.
        pool = self._make_pool_with_tools(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        # Tool schemas contribute enough tokens to cross the 50% line (4000 of 7000 ≈ 57%).
        # NOTE: these two helpers are imported at module level in compression.core, so patch
        # core's own bindings (not the source modules) — that is where _estimate_usage_pct looks them up.
        with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=10_000), \
             patch('agent_cascade.compression.core._get_active_functions_from_template',
                   return_value=[{'name': 'f'}] * 50), \
             patch('agent_cascade.compression.core.estimate_functions_tokens', return_value=4000), \
             patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke:
            mock_invoke.return_value = ('Fresh compression summary', '')
            result = compress_context(agent_pool=pool, target_agent_name='TestAgent', fraction=0.5, mode='auto')

        # NOT refused by the min-usage guard — tool tokens lifted usage above 50%.
        assert 'min-usage guard' not in (result.error or '')

    def test_message_only_below_threshold_without_tools_refused(self):
        """Same conversation but NO tool tokens → usage stays < 50% → REFUSED.

        Contrast case: confirms the proceed-above is caused by the tool-schema tokens, not
        the messages. No get_template on the mock pool → AttributeError caught → message-only
        count, which with max_tokens=10_000 is far below 50%.
        """
        pool = _make_pool(num_pairs=2, reserve_tokens=3000, min_usage_pct=50.0)

        with patch('agent_cascade.api_integration_pkg.tokens._resolve_max_tokens', return_value=10_000), \
             patch('agent_cascade.compression.core.invoke_compression_agent') as mock_invoke:
            mock_invoke.return_value = ('Fresh compression summary', '')
            result = compress_context(agent_pool=pool, target_agent_name='TestAgent', fraction=0.5, mode='auto')

        # Refused because message-only usage is far below 50% (no tool tokens added).
        assert result.success is False
        assert 'min-usage guard' in (result.error or '')
