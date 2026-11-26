"""Pre-send message-sequence integrity guard for tool/function messages (BUG_0415).

A single ``tool_calls[].id`` must be answered by exactly ONE ``tool``/``function``
message, and every ``tool`` message must reference a ``tool_calls[].id`` that appeared
in a *preceding* assistant message. A violation of either rule is a hard OpenAI 400.
Historically a failed tool call could emit two results for one call, one carrying the
placeholder id ``"1"`` that matched no preceding call. That corruption is currently
masked by the live send path but is a landmine for resume/restore/log-replay and for
stricter endpoints.

This module is the durable guard: run :func:`sanitize_tool_messages` on the message
list immediately before every LLM send (``BaseChatModel.chat`` -- the single entry
point that both the execution engine and the legacy agents funnel through; the lower
transport converter ``_conv_agent_cascade_messages_to_oai`` is deliberately NOT guarded
because it is also shared by token-counting and direct-conversion callers/tests that
must see messages unchanged). It:

- drops any ``tool``/``function`` result whose id matches no preceding assistant call;
- collapses duplicate results for one *uniquely-declared* id, keeping the LAST
  (canonical) response;
- reorders so each unique-id result sits immediately after the assistant call that
  owns it;
- leaves results for a *reused* id (one declared by more than one assistant message --
  a mock-fixture artifact, never a real session) strictly in place: no collapse, no
  reorder, so legitimate historical results are not dropped;
- is a no-op on a clean, valid sequence (same object references -> happy path unchanged);
- is fail-safe: an anomaly drops the offending message and logs a warning, and a
  catastrophic failure returns the input unchanged -- it must never raise into the
  request path (a broken sanitizer must not turn a recoverable state into a crash).

Works on both ``agent_cascade.llm.schema.Message`` objects and OpenAI-format dicts, so
it is usable at the ``chat`` boundary (Message list) and testable directly (dict list).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Fallback id used ONLY when a tool-call id is genuinely unavailable. It is a clearly
# invalid, non-plausible marker -- never a value that could pass for a real call id
# (the old code used the literal ``"1"``). The send-time guard treats a result carrying
# this id as orphaned and drops it. Using the SAME sentinel on both the call and the
# result side keeps a fully-id-less pair self-consistent instead of forcing a hard 400.
ORPHAN_TOOL_CALL_ID = '__ac_no_function_id__'

_ASSISTANT = 'assistant'
# The send layer uses OpenAI's 'tool' role; 'function' is the legacy/AC spelling. Both
# are treated as a tool result so the guard is robust regardless of conversion stage.
_TOOL_ROLES = ('tool', 'function')


def _role(msg):
    return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', None)


def _extra(msg):
    extra = msg.get('extra') if isinstance(msg, dict) else getattr(msg, 'extra', None)
    return extra if isinstance(extra, dict) else {}


def _result_id(msg):
    """Id a tool result responds to, tolerating every shape (or None when absent)."""
    if isinstance(msg, dict):
        rid = msg.get('tool_call_id') or msg.get('id') or _extra(msg).get('function_id')
    else:
        rid = getattr(msg, 'tool_call_id', None) or _extra(msg).get('function_id')
    return rid or None


def _assistant_call_ids(msg):
    """Ids declared by an assistant message (openai 'tool_calls' or AC 'function_call')."""
    if _role(msg) != _ASSISTANT:
        return []
    ids = []
    tool_calls = msg.get('tool_calls') if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict) and tc.get('id'):
                ids.append(tc['id'])
    function_call = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
    if function_call:
        fid = _extra(msg).get('function_id')
        if fid:
            ids.append(fid)
    return ids


def sanitize_tool_messages(messages):
    """Return a tool-result-invariant message list.

    On the normal path a fresh list is returned (input never mutated). On the two
    degenerate paths the input object is returned verbatim: empty/falsy input, and the
    fail-safe exception path. A clean, valid sequence yields an equal list with
    identical object references, so the happy path is unchanged.
    """
    try:
        return _sanitize_tool_messages_impl(messages)
    except Exception as exc:  # noqa: BLE001 - never break the request path
        logger.warning(
            'sanitize_tool_messages raised unexpectedly; passing messages through '
            'unchanged (sequence may 400): %s', exc)
        return messages


def _sanitize_tool_messages_impl(messages):
    if not messages:
        return messages

    # An id declared by MORE THAN ONE assistant message is a "reused" id -- a mock
    # fixture artifact (real sessions use globally-unique call ids). Reused ids are
    # NOT the double-emission pattern, so their results are left strictly in place
    # (no collapse, no reorder); touching them drops legitimate historical results
    # and starves compression (regression in test_t5_deep_nesting_under_contention).
    declared_count = {}
    for msg in messages:
        if _role(msg) == _ASSISTANT:
            for cid in _assistant_call_ids(msg):
                declared_count[cid] = declared_count.get(cid, 0) + 1
    reused_ids = {rid for rid, n in declared_count.items() if n > 1}
    unique_ids = {rid for rid, n in declared_count.items() if n == 1}

    # Pass 1+2: ORDER-AWARE forward scan. We only admit a result whose owning call-id
    # was already declared by an assistant message appearing *before* it; a result
    # that precedes its owner is an orphan (would be a 400) and is dropped. For a
    # duplicated UNIQUE id we keep the LAST occurrence (the canonical, usually the
    # real result) and drop the earlier ones -- a documented, deliberate choice.
    # Reused-id results are never collapsed.
    seen_call_ids = set()
    drop_idx = set()
    occurrences = {}
    for i, msg in enumerate(messages):
        role = _role(msg)
        if role == _ASSISTANT:
            seen_call_ids.update(_assistant_call_ids(msg))
            continue
        if role not in _TOOL_ROLES:
            continue
        rid = _result_id(msg)
        if not rid or rid not in seen_call_ids:
            drop_idx.add(i)
            logger.warning(
                '[BUG_0415] Dropping orphaned tool result (index=%d, id=%r): no matching '
                'preceding tool_call id.', i, rid)
        elif rid in reused_ids:
            continue  # mock artifact: keep in place, never collapse
        else:
            occurrences.setdefault(rid, []).append(i)

    for rid, idxs in occurrences.items():
        if len(idxs) > 1:
            for i in idxs[:-1]:  # keep the last response per tool_call
                drop_idx.add(i)
                logger.warning(
                    '[BUG_0415] Dropping duplicate tool result (index=%d, id=%r); keeping '
                    'the last response for this tool_call.', i, rid)

    work = [m for i, m in enumerate(messages) if i not in drop_idx]

    # Pass 3: ordering. Attach each surviving UNIQUE-id result immediately after the
    # assistant message that owns it, so parallel/split results are grouped before the
    # next assistant/user message. Reused-id (and id-less) results stay in their
    # original position. On a clean sequence every result already sits right after
    # its owner, so this pass is an identity (same object references, same order).
    by_id = {}
    for m in work:
        if _role(m) in _TOOL_ROLES:
            rid = _result_id(m)
            if rid and rid in unique_ids:
                by_id[rid] = m

    out = []
    emitted = set()
    for m in work:
        role = _role(m)
        if role in _TOOL_ROLES:
            rid = _result_id(m)
            # Only UNIQUE-id results take part in the emit-at-owner logic; reused and
            # id-less results are kept strictly in place.
            if rid and rid in unique_ids:
                if rid in emitted:
                    continue
                emitted.add(rid)
            out.append(m)
        elif role == _ASSISTANT and _assistant_call_ids(m):
            out.append(m)
            for cid in _assistant_call_ids(m):
                if cid in unique_ids and cid in by_id and cid not in emitted:
                    emitted.add(cid)
                    out.append(by_id[cid])
        else:
            out.append(m)
    return out
