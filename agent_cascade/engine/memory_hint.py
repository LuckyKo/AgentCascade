"""Memory-hint query construction and submit hook.

Extracted VERBATIM from :mod:`agent_cascade.engine.core` (Phase 1a of the
engine-core audit). The mixin keeps the methods available as
``ExecutionEngine.<method>`` through inheritance while giving the hint logic
its own module and test surface.
"""

from __future__ import annotations

from agent_cascade.engine.helpers import _extract_direct_text_content
from agent_cascade.llm.schema import ASSISTANT
from agent_cascade.log import logger

# Justifications carried in the hint query. Deliberately small: the query is
# capped at memory_hint_query_chars (default 1000) and this text competes with
# the assistant's own prose for that budget.
JUSTIFICATION_MAX_CHARS = 240        # per justification
JUSTIFICATION_MAX_ITEMS = 4          # per turn
JUSTIFICATION_BUDGET_DIVISOR = 3     # justifications get at most max_chars // 3
JUSTIFICATION_SECTION_PREFIX = '[tool-justification] '
JUSTIFICATION_EXCLUDE_TOOLS = {'propose_skill'}  # self-describing; would double-weight


class MemoryHintMixin:
    """Best-effort memory-hint submission and query extraction."""

    def _maybe_submit_memory_hint(self, instance, turn_output) -> None:
        """Best-effort memory-hint submit at the Phase-3/Phase-4 boundary.

        Feature: memory_hint (plan §1.2). Submits a hint job for any turn that has
        assistant text, reasoning content, or tool-call justifications — including
        text+tool turns and tool-call-only turns that carry justifications. Turns
        with none of these produce an empty query and are skipped. Delivery is async
        on a daemon worker and rides the existing tool-warning queue; there is NO
        user-message injection here.

        Best-effort: ANY exception is swallowed (never raises, never blocks, never
        mutates the turn budget). Fast path returns before touching locks/manager.
        """
        try:
            manager = getattr(self.pool, 'memory_hint_manager', None)
            if manager is None:
                return
            cfg = getattr(self.pool, 'llm_cfg', None) or {}
            if not cfg.get('memory_hint_enabled', False):
                return

            # Skip sleeping instances (state can change; re-checked at delivery too).
            try:
                from agent_cascade.agent_instance import AgentState
                if getattr(instance, 'state', None) == AgentState.SLEEPING:
                    return
            except Exception:  # noqa: BLE001
                pass

            query = self._extract_memory_hint_query(turn_output, int(cfg.get('memory_hint_query_chars', 1000)))
            if not query:
                return  # no text, reasoning, or justifications → nothing to match.

            manager.submit(instance.instance_name, query, getattr(instance, 'agent_class', ''),
                           turn=getattr(instance, '_current_turn', -1))
        except Exception as e:  # noqa: BLE001 — best-effort, never break the run
            logger.debug('[MEMORY_HINT] submit hook failed for %s: %s',
                         getattr(instance, 'instance_name', '?'), e)

    @staticmethod
    def _tool_justifications(msg) -> list:
        """Pull the `justification` argument out of every tool call on one message.

        NARROW EXCEPTION to the "tool calls never enter the hint query" rule: only the
        agent-authored `justification` string is read, never `name` or the raw argument
        blob. That is what keeps this from re-introducing the `[TOOL CALL: name(args)]`
        leak the shared extract_text_from_message() fallback caused.

        Handles both storage shapes in the same precedence order as
        utils._format_tool_calls_for_text (function_call wins over tool_calls), dict and
        Message-object forms via _msg_field_or_extra, and returns [] for anything
        malformed rather than raising (this feeds a best-effort hook).

        Tools in JUSTIFICATION_EXCLUDE_TOOLS are skipped entirely (their justification
        restates other content the matcher already indexes).
        """
        from agent_cascade.utils.utils import _msg_field_or_extra
        import json

        out = []

        def _harvest(args_raw, tool_name=None):
            if tool_name in JUSTIFICATION_EXCLUDE_TOOLS:
                return
            if not isinstance(args_raw, str) or not args_raw.strip():
                return
            try:
                parsed = json.loads(args_raw)
            except (ValueError, TypeError):
                return                       # malformed JSON → skip, never raise
            if not isinstance(parsed, dict):
                return
            val = parsed.get('justification')
            if not isinstance(val, str):      # missing key OR non-string → skip
                return
            val = ' '.join(val.split())       # collapse newlines/whitespace
            if not val:
                return
            out.append(val[:JUSTIFICATION_MAX_CHARS])

        fc = _msg_field_or_extra(msg, 'function_call')
        if fc is not None:
            if isinstance(fc, dict):
                _harvest(fc.get('arguments'), fc.get('name'))
            else:
                _harvest(getattr(fc, 'arguments', None), getattr(fc, 'name', None))
            return out                       # same priority rule as the shared helper

        tc = _msg_field_or_extra(msg, 'tool_calls')
        if isinstance(tc, list):
            for item in tc:
                fn = _msg_field_or_extra(item, 'function')
                if fn is None:
                    continue
                if isinstance(fn, dict):
                    tool_name = fn.get('name')
                    args = fn.get('arguments')
                else:
                    tool_name = getattr(fn, 'name', None)
                    args = getattr(fn, 'arguments', None)
                _harvest(args, tool_name)
        return out

    @staticmethod
    def _extract_memory_hint_query(turn_output, max_chars: int) -> str:
        """Build the hint query from a turn's assistant text/reasoning.

        Reads each assistant message's ``content`` DIRECTLY (str, or the ``text``
        fields of list parts) plus its ``reasoning_content`` — it deliberately does
        NOT use the shared ``extract_text_from_message()`` helper, whose tool-call
        fallback would leak tool-call descriptions into the query.

        Tool calls are NEVER part of the query, with one narrow exception: the
        agent-authored ``justification`` argument (see :meth:`_tool_justifications`)
        is appended as a tagged section when present. A tool-call-only turn that
        carries at least one justification now produces a non-empty query and will
        submit a hint job — this is the intended recall improvement for mid-edit turns.

        Returns '' when a turn has NEITHER text NOR reasoning NOR justifications.
        Concatenates text + reasoning across all messages, capped at ``max_chars``.
        """
        parts = []
        just_parts = []
        for msg in (turn_output or []):
            role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role != ASSISTANT:
                continue
            # Text content — read DIRECTLY (no tool-call fallback).
            text = _extract_direct_text_content(msg)
            if text:
                parts.append(text)
            # Reasoning content (a pure-thinking turn still counts as "has content").
            rc = msg.get('reasoning_content') if isinstance(msg, dict) else getattr(msg, 'reasoning_content', None)
            from agent_cascade.utils.utils import _reasoning_to_text
            rc_text = _reasoning_to_text(rc)
            if rc_text:
                parts.append(rc_text)

            # ── NEW: tool-call justifications (narrow exception, see helper) ──
            for j in MemoryHintMixin._tool_justifications(msg):
                if j not in just_parts:          # de-dupe within the turn
                    just_parts.append(j)

        prose = ' '.join(p for p in parts if p).strip()
        if not just_parts:
            return prose[:max_chars]             # unchanged fast path

        # Justifications get at most 1/3 of the budget and the remainder goes to the
        # agent's own prose — cosine dilution in the memory matcher is the cost of
        # padding the query, so keep this share small.
        just_budget = min(
            sum(len(j) + 1 for j in just_parts),
            max(0, max_chars // JUSTIFICATION_BUDGET_DIVISOR),
        )
        block = ''
        used = 0
        for j in just_parts[:JUSTIFICATION_MAX_ITEMS]:
            room = just_budget - used - 1        # -1 for the joining space
            if room <= 0:
                break
            chunk = j[:room]
            block += (JUSTIFICATION_SECTION_PREFIX if not block else ' ') + chunk
            used += len(chunk) + 1

        if not block:
            return prose[:max_chars]

        # Reserve space for the block so it is never truncated away by long prose.
        max_prose_len = max(0, max_chars - len(block) - 1)  # -1 for joining space
        query = (prose[:max_prose_len] + ' ' + block).strip()
        return query[:max_chars]
