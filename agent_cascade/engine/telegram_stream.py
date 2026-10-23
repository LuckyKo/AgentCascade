"""Telegram stream-time push helpers (Phase 1d split).

The three ``_tg_*`` helpers are moved VERBATIM from
:mod:`agent_cascade.engine.core`. ``core`` re-imports them because bare-name
call sites remain in the engine loop.
"""

from __future__ import annotations

from typing import Any, List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.helpers import _extract_direct_text_content
from agent_cascade.llm.schema import ASSISTANT, Message
from agent_cascade.log import logger


# ── TG-STREAM: stream-time condition-based Telegram pushes ────────────────────────
# Replaces the reverted end-of-run history-extraction approach: pushes fire at the
# moment their condition is met in the engine loop, operating ONLY on the turn's
# committed output (`turn_output` / `response` locals) — no conversation lookback.
# All helpers are best-effort (never raise into the run loop) and root-gated
# (parent_instance is None — sub-agents must not push to the phone).

def _tg_stream_text(messages: List[Any]) -> str:
    """Return the text of the LAST assistant message in `messages` that has real text.

    Walks reversed; skips non-assistant roles and empty/tool-call-only messages.
    Reads content directly (str, or the ``text`` fields of list parts) — no
    tool-call fallback, so a function_call turn never yields push text. Returns ''
    when no assistant-with-text exists.
    """
    for msg in reversed(messages or []):
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')
        if role != ASSISTANT:
            continue
        text = _extract_direct_text_content(msg)
        if text:
            return text
    return ''


def _tg_stream_push(instance: AgentInstance, turn_output: List[Message], pool: Any) -> None:
    """TG-STREAM F1: push the FIRST text output of a run, at commit time.

    Called from _process_response immediately after turn_output is committed to the
    conversation. Fires once per run (_tg_first_pushed), only for root instances, and
    only when the committed turn actually contains assistant text — a tool-call-only
    first turn does NOT consume the flag (the next text turn still pushes).

    notify_user is non-blocking/thread-safe and returns True only when scheduled onto
    a live bridge loop; the flag is set ONLY on that success so a down-bridge attempt
    retries on the next text output. Best-effort: any exception is swallowed.
    """
    try:
        if getattr(instance, 'parent_instance', None) is not None:
            return  # sub-agent — never push to the phone
        if getattr(instance, '_tg_first_pushed', False):
            return  # already delivered this run's first text output
        text = _tg_stream_text(turn_output)
        if not text:
            return  # no assistant text in this turn (e.g. tool-call-only) — wait for one
        sup = getattr(pool, 'telegram_supervisor', None)
        if sup is None or not hasattr(sup, 'notify_user'):
            return  # bridge not attached — silent no-op
        inst_name = instance.instance_name
        gen = getattr(pool, '_run_generation', None)
        ok = sup.notify_user(text, instance_name=inst_name, run_generation=gen)
        if ok:
            instance._tg_first_pushed = True
            instance._tg_first_pushed_text = text  # recorded for the F2 pre-phase dup guard
            logger.debug('[TG-PUSH] first-output push delivered for %s gen=%s (%d chars)',
                         inst_name, gen, len(text))
        else:
            # notify_user False = bridge not live (silent no-op by contract) — flag stays
            # False so the next text output retries; WARNING per plan §F3.
            logger.warning('[TG-PUSH] first-output push failed for %s (bridge not live?) gen=%s',
                           inst_name, gen)
    except Exception as e:  # noqa: BLE001 — best-effort, never break the run
        logger.debug('[TG-PUSH] first-output push error for %s: %s',
                     getattr(instance, 'instance_name', '?'), e)


def _tg_push_final(instance: AgentInstance, response: List[Message], pool: Any, phase: str) -> None:
    """TG-STREAM F2: push the final answer at stream time.

    Called from Phase 5 on genuine completion, with `response` = this run's committed
    output so far (no history lookback). ``phase`` is 'pre' when the natural end happens
    before an auto-skill reflection and 'post' after one — keyed on
    instance._auto_skill_proposed at the call site.

    Dedup via _tg_final_pushed_phase: the 'pre' and 'post' phases are INDEPENDENT — a
    successful 'pre' push does NOT suppress a later 'post' (the reflection's own final
    answer is still delivered); each phase only suppresses further attempts of its own
    kind (a second 'pre' attempt is skipped once either phase has pushed, which matters
    if another genuine-completion point occurs before the extension fires). A failed
    attempt leaves the marker unset so the next attempt retries. Best-effort: never raises.
    """
    try:
        if getattr(instance, 'parent_instance', None) is not None:
            return  # sub-agent — never push to the phone
        pushed_phase = getattr(instance, '_tg_final_pushed_phase', None)
        if phase == 'pre' and pushed_phase in ('pre', 'post'):
            return
        if phase == 'post' and pushed_phase == 'post':
            return
        text = _tg_stream_text(response)
        if not text:
            return  # no assistant text committed yet — nothing to push
        inst_name = instance.instance_name
        gen = getattr(pool, '_run_generation', None)
        if phase == 'pre' and getattr(instance, '_tg_first_pushed', False) \
                and text == getattr(instance, '_tg_first_pushed_text', None):
            # The run's first text output WAS its final answer — F1 already delivered this exact
            # text at commit time. Pushing it again would be a verbatim duplicate on Telegram.
            # Consume the pre-phase marker so no later retry re-pushes it either.
            instance._tg_final_pushed_phase = 'pre'
            logger.debug('[TG-PUSH] final(pre) suppressed for %s gen=%s: identical to first-output push (%d chars)',
                         inst_name, gen, len(text))
            return
        sup = getattr(pool, 'telegram_supervisor', None)
        if sup is None or not hasattr(sup, 'notify_user'):
            return  # bridge not attached — silent no-op
        ok = sup.notify_user(text, instance_name=inst_name, run_generation=gen)
        if ok:
            instance._tg_final_pushed_phase = phase
            logger.debug('[TG-PUSH] final(%s) push delivered for %s gen=%s (%d chars)',
                         phase, inst_name, gen, len(text))
        else:
            # notify_user False = bridge not live (silent no-op by contract) — marker stays
            # unset so the next completion path retries; WARNING per plan §F3.
            logger.warning('[TG-PUSH] final(%s) push failed for %s (bridge not live?) gen=%s',
                           phase, inst_name, gen)
    except Exception as e:  # noqa: BLE001 — best-effort, never break the run
        logger.debug('[TG-PUSH] final(%s) push error for %s: %s',
                     phase, getattr(instance, 'instance_name', '?'), e)
