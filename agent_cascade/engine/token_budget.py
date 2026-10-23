"""Token-budget helpers for the execution engine (Phase 1b split).

``TokenBudgetMixin`` owns the context-window limit and history-token counting
helpers. Method bodies are moved VERBATIM from
:mod:`agent_cascade.engine.core`; the mixin is composed into ``ExecutionEngine``
there so ``ExecutionEngine.<method>`` lookups and instance patch points remain
unchanged.
"""

from __future__ import annotations

from typing import List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.llm.schema import Message
from agent_cascade.log import logger
from agent_cascade.settings import TOKEN_ESTIMATE_CHAR_DIVISOR
from agent_cascade.utils.utils import get_message_stats


class TokenBudgetMixin:
    """Resolve context limits and count message/tool-schema tokens."""

    def _get_max_tokens(self, instance: AgentInstance) -> int:
        """Resolve the effective max_input_tokens from LLM config.

        Delegates to shared helper _resolve_max_tokens from api_integration
        to eliminate code duplication and fix OAI detection read-path bug.
        """
        from agent_cascade.api_integration import _resolve_max_tokens
        return _resolve_max_tokens(self.pool, instance)

    def _get_effective_limit(self, instance: AgentInstance) -> int:
        """Usable context window = max_input_tokens minus the compression reserve.

        This is the denominator that usage_pct is computed against (see
        compression_exec.py). Exposed as a single source of truth so any display of
        "A/B tokens" alongside a percentage uses the SAME B, keeping the ratio and the
        percent consistent. Falls back to the raw max if the reserve would make it <= 0.
        """
        max_tokens = self._get_max_tokens(instance)
        reserve = getattr(self.pool.settings, 'compression_context_reserve_tokens', 0) or 0
        effective = max_tokens - reserve
        return effective if effective > 0 else max_tokens

    def _get_active_functions_for_counting(self, instance: AgentInstance):
        """Resolve the active tool schemas for an instance (for token estimation).

        Mirrors how the LLM call resolves its tools (llm_call.py →
        _get_active_functions_from_template). Fail-soft: returns [] on any error so a
        counting path never crashes because of template resolution.
        """
        try:
            from agent_cascade.engine.helpers import _get_active_functions_from_template
            template = self.pool.get_template(instance.agent_class)
            if not template:
                return []
            return _get_active_functions_from_template(template, instance, pool=self.pool)
        except Exception as e:
            logger.debug(f"Active function resolution for token counting failed (non-fatal): {e}")
            return []

    def _count_history_tokens(self, messages: List[Message], instance: AgentInstance = None, functions=None) -> int:
        """Calculate total tokens in a message list (with caching — Fix #2).

        Uses get_message_stats() for consistent token counting including chat
        template overhead. This aligns with llm/base.py::_count_tokens and ensures
        all code paths report the same estimates that match llama.cpp's actual counts.

        Args:
            messages: The conversation messages to count.
            instance: Optional AgentInstance (for per-instance caching / thread safety).
            functions: Optional active tool schemas. When provided, their serialized
                payload is added to the total — these are always sent to the API as
                ``tools=`` and were previously invisible to this compression-trigger
                count (causing late compression in tool-heavy sessions).
        """
        try:
            inst = instance or getattr(self, '_current_instance', None)  # Prefer explicit param (thread-safe)

            # Cache MESSAGE-ONLY tokens, keyed on conversation length, and add the
            # caller's schema term on every call. Caching the combined total here used
            # to poison schema-aware callers: a message-only call (e.g. a compression
            # warning readout) stored a smaller total under the same key, and the next
            # trigger call with the same message count got it back (BUG_0136).
            msg_tokens = None
            if inst and inst._last_token_count_conversation_length >= 0 and len(
                    messages) == inst._last_token_count_conversation_length:
                msg_tokens = getattr(inst, '_cached_conversation_tokens', None)

            if msg_tokens is None:
                msg_tokens = 0
                for msg in messages:
                    stats = get_message_stats(msg)
                    msg_tokens += stats['tokens']
                if inst:
                    inst._cached_conversation_tokens = msg_tokens
                    inst._last_token_count_conversation_length = len(messages)

            total = msg_tokens

            # Tool schemas are sent on every call but are not part of the message list,
            # so they were being undercounted here. Count them when provided.
            if functions:
                from agent_cascade.utils.utils import estimate_functions_tokens
                total += estimate_functions_tokens(functions)

            if inst:
                # Kept for callers/readouts that ask for the last full total.
                inst._cached_token_count = total

            return total
        except Exception as e:
            logger.debug(f"Token counting failed (using rough estimate): {e}")
            # Fallback: rough estimate (4 chars per token), including
            # reasoning_content
            total_chars = 0
            for m in messages:
                if isinstance(m, list):
                    continue
                content = m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '')
                total_chars += len(str(content or ''))
                # Also count reasoning_content to avoid undercounting
                rc = m.get('reasoning_content') if isinstance(m, dict) else getattr(m, 'reasoning_content', None)
                if rc:
                    if isinstance(rc, list):
                        for item in rc:
                            txt = item.get('text', '') if isinstance(item, dict) else getattr(item, 'text', '')
                            total_chars += len(str(txt or ''))
                    else:
                        total_chars += len(str(rc))
            return max(total_chars // TOKEN_ESTIMATE_CHAR_DIVISOR, 100)
