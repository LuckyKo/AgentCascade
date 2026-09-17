"""Unit tests for the id-aware streaming dedup fingerprint in ``state_builder``.

Regression: two PARALLEL tool calls to the same tool with equal/partial args used to
collapse into one under the old 4-tuple fingerprint (content, reasoning, str(function_call),
name) because content/reasoning/name were all empty and only str(function_call) discriminated.
The second call was silently dropped -> ``num_streaming`` undercounted -> the UI froze after
the first tool bubble.

The fix makes the fingerprint a UNIFORM 5-tuple ``(content, reasoning, func_call, name, id_part)``
in BOTH the committed-tail seed loop and the streaming append loop, where ``id_part`` is
``extra.function_id`` (falling back to the loop index when a model omits it) for tool-call
messages and ``None`` otherwise. These tests pin that behavior.

Self-contained: no network, no live API — lightweight fakes mirroring
test_state_builder_tail_cut.py conventions.
"""

import threading
from unittest.mock import MagicMock

from agent_cascade.agent_instance import AgentState
from agent_cascade.api_integration_pkg import state_builder as sb


# ---------------------------------------------------------------------------
# Fakes (mirrors test_state_builder_tail_cut.py)
# ---------------------------------------------------------------------------


def _msg(role, content='', **kw):
    """Build a plain-dict message (serialize_message handles dicts natively)."""
    m = {'role': role, 'content': content}
    m.update(kw)
    return m


def _tool_msg(name, args, function_id=None):
    """A tool-call message: empty content/reasoning/name + one function_call."""
    m = _msg('assistant', '', function_call={'name': name, 'arguments': args})
    if function_id is not None:
        m['extra'] = {'function_id': function_id}
    return m


def _make_inst(conversation, streaming_responses=None, name='Maine'):
    inst = MagicMock()
    inst.instance_name = name
    inst.agent_class = 'coder'
    inst.parent_instance = None
    inst.state = AgentState.RUNNING  # real enum member: _serialize_instance reads state.name
    inst.conversation = list(conversation)
    inst._streaming_responses = list(streaming_responses or [])
    inst._state_lock = threading.RLock()
    inst._compression_lock = threading.RLock()
    return inst


def _make_pool(inst):
    pool = MagicMock()
    pool.is_instance_halted.return_value = False
    pool.has_messages.return_value = False
    pool.get_queue_messages.return_value = []
    pool.slice_history_for_llm.side_effect = lambda msgs: list(msgs)
    return pool


def _serialize(inst, pool, streaming=True):
    """Drive ``_serialize_instance`` with the flag forced ON (tail-cut path)."""
    saved = sb.STREAM_DELTA_ENABLED
    try:
        sb.STREAM_DELTA_ENABLED = True
        return sb._serialize_instance(
            inst,
            pool,
            include_messages=True,
            streaming=streaming,
            streaming_responses=inst._streaming_responses,
        )
    finally:
        sb.STREAM_DELTA_ENABLED = saved


# ---------------------------------------------------------------------------
# 1. PRIMARY BUG: two parallel same-tool calls must BOTH survive
# ---------------------------------------------------------------------------


def test_parallel_same_tool_identical_args_both_survive():
    """Two identical same-tool calls (no ids) -> both appended, num_streaming == 2.

    This is the exact reported freeze: old code dropped the second because its fingerprint
    collided with the first's.
    """
    committed = [_msg('user', 'q')]
    responses = [
        _tool_msg('search', '{"query":"a"}'),
        _tool_msg('search', '{"query":"a"}'),  # identical args, no function_id
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    tool_bubbles = [m for m in result['messages'] if m.get('function_call')]
    assert len(tool_bubbles) == 2, f"expected 2 tool bubbles, got {len(tool_bubbles)}"
    # history_count == committed + num_streaming (both streaming calls counted)
    assert result['history_count'] == len(committed) + 2


def test_parallel_same_tool_identical_args_with_ids_both_survive():
    """Two identical same-tool calls WITH distinct ids -> both survive via id_part."""
    committed = [_msg('user', 'q')]
    responses = [
        _tool_msg('search', '{"query":"a"}', function_id='id_1'),
        _tool_msg('search', '{"query":"a"}', function_id='id_2'),
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    tool_bubbles = [m for m in result['messages'] if m.get('function_call')]
    assert len(tool_bubbles) == 2
    assert result['history_count'] == len(committed) + 2


def test_parallel_same_tool_partial_args_both_survive():
    """Two same-tool calls with DIFFERENT (partial) args -> both survive.

    Regression guard: distinct-arg parallel calls must keep working under the new 5-tuple.
    """
    committed = [_msg('user', 'q')]
    responses = [
        _tool_msg('search', '{"query":"a"}'),
        _tool_msg('search', '{"query":"b"}'),
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    tool_bubbles = [m for m in result['messages'] if m.get('function_call')]
    assert len(tool_bubbles) == 2
    assert result['history_count'] == len(committed) + 2


def test_parallel_different_tools_both_survive():
    """Two DIFFERENT tools -> both survive (sanity: distinct names never collided)."""
    committed = [_msg('user', 'q')]
    responses = [
        _tool_msg('search', '{}'),
        _tool_msg('write_file', '{}'),
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    tool_bubbles = [m for m in result['messages'] if m.get('function_call')]
    assert len(tool_bubbles) == 2


# ---------------------------------------------------------------------------
# 2. Text-only stale-prefix dedup still works (must NOT be weakened by the fix)
# ---------------------------------------------------------------------------


def test_text_partial_still_dedups_against_committed():
    """A text partial identical to the last committed assistant is still deduped."""
    committed = [_msg('user', 'q'), _msg('assistant', 'DONE')]
    responses = [_msg('assistant', 'DONE')]  # same fingerprint as committed[1]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    # tail = [committed assistant "DONE"]; identical partial deduped (num_streaming == 0)
    assert len(result['messages']) == 1
    assert result['history_count'] == 2


def test_text_partial_stale_prefix_not_appended():
    """A stale PREFIX of the last committed assistant is not appended as a duplicate."""
    committed = [_msg('user', 'q'), _msg('assistant', 'The full answer.')]
    responses = [_msg('assistant', 'The ful')]  # stale prefix of committed final
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    # The stale partial is suppressed by the _is_stale_prefix_of_serialized guard.
    text_msgs = [m for m in result['messages'] if (m.get('content') or '')]
    assert len(text_msgs) == 1
    assert result['history_count'] == 2


# ---------------------------------------------------------------------------
# 3. Non-tool messages still dedup under the 5-tuple (id_part=None path)
# ---------------------------------------------------------------------------


def test_identical_text_messages_dedup_under_5tuple():
    """Two identical non-tool streaming messages -> only one appended (id_part is None)."""
    committed = [_msg('user', 'q')]
    responses = [
        _msg('assistant', 'hello'),
        _msg('assistant', 'hello'),  # duplicate, no function_call
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    # Count only assistant text messages (exclude the committed user message).
    text_msgs = [m for m in result['messages'] if m.get('role') == 'assistant' and (m.get('content') or '')]
    assert len(text_msgs) == 1, f"expected 1 assistant text msg (deduped), got {len(text_msgs)}"


def test_distinct_text_messages_both_survive():
    """Two DIFFERENT non-tool streaming messages -> both appended."""
    committed = [_msg('user', 'q')]
    responses = [
        _msg('assistant', 'first'),
        _msg('assistant', 'second'),
    ]
    inst = _make_inst(committed, streaming_responses=responses)
    result = _serialize(inst, _make_pool(inst), streaming=True)

    # Count only assistant text messages (exclude the committed user message).
    text_msgs = [m for m in result['messages'] if m.get('role') == 'assistant' and (m.get('content') or '')]
    assert len(text_msgs) == 2, f"expected 2 assistant text msgs, got {len(text_msgs)}"
