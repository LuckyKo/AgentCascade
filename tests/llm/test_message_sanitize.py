"""Regression tests for BUG_0415: tool-result double-emission + orphaned function_id.

Two layers are covered:

1. ``sanitize_tool_messages`` — the durable pre-send guard (drop orphaned result,
   collapse duplicate result, regroup split parallel results) and its hard no-op on a
   clean sequence.
2. The "Invalid JSON arguments" path — after the stop-bleed, a single tool_call yields
   exactly ONE result carrying the real ``function_id``; the ``"1"`` placeholder is gone
   (replaced by the clearly-invalid ``ORPHAN_TOOL_CALL_ID`` sentinel that the guard drops).

The "invalid JSON" / orphan assertions are written discriminatingly: each asserts the
bad artifact is *present in the raw input* and *absent after the guard*, so the test
fails if the guard is removed or degenerated into a no-op (see .agent_lessons/
prove-a-guard-test-can-fail-before-calling-it-coverage.md).
"""

from agent_cascade.agents.message_sanitize import (
    ORPHAN_TOOL_CALL_ID,
    sanitize_tool_messages,
)
from agent_cascade.llm.base import BaseChatModel
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, Message


# --------------------------------------------------------------------------- #
# Part 1 sentinel is not the plausible placeholder
# --------------------------------------------------------------------------- #

def test_orphan_sentinel_is_not_the_plausible_one():
    # The whole point of BUG_0415: the old fallback was the literal "1", which could
    # masquerade as a real call id. The sentinel must be a clearly-invalid marker.
    assert ORPHAN_TOOL_CALL_ID != '1'
    assert ORPHAN_TOOL_CALL_ID  # non-empty so a call/result pair still self-matches


# --------------------------------------------------------------------------- #
# Guard unit tests (operate on OpenAI-format dicts, i.e. post-conversion shape)
# --------------------------------------------------------------------------- #

def _tc(tid):
    return {'id': tid, 'type': 'function', 'function': {'name': 'edit_file', 'arguments': '{}'}}


def _tool(tid, content):
    return {'role': 'tool', 'tool_call_id': tid, 'content': content}


def _assistant_toolcall(*ids):
    return {'role': 'assistant', 'content': '', 'tool_calls': [_tc(i) for i in ids]}


def test_guard_drops_orphaned_tool_call_id():
    raw = [
        _assistant_toolcall('call_x'),
        _tool('1', 'Error: Invalid JSON arguments for tool edit_file.'),  # orphan
        _tool('call_x', 'ERROR: Pattern not found'),                      # real
    ]
    # Discriminating precondition: the orphan exists in the input.
    assert any(m.get('tool_call_id') == '1' for m in raw)

    out = sanitize_tool_messages(raw)

    assert not any(m.get('tool_call_id') == '1' for m in out), 'orphan must be removed'
    assert [m for m in out if m['role'] == 'tool'] == [_tool('call_x', 'ERROR: Pattern not found')]
    # Every surviving tool result still references a declared preceding call.
    ids = {m['tool_call_id'] for m in out if m['role'] == 'tool'}
    assert ids == {'call_x'}


def test_guard_collapses_duplicate_to_last():
    raw = [
        _assistant_toolcall('call_x'),
        _tool('call_x', 'first / stale response'),
        _tool('call_x', 'canonical last response'),
    ]
    out = sanitize_tool_messages(raw)
    results = [m for m in out if m['role'] == 'tool']
    assert len(results) == 1
    # Documented choice: keep the LAST (canonical) response.
    assert results[0]['content'] == 'canonical last response'


def test_guard_is_byte_identical_no_op_on_valid_sequence():
    clean = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'go'},
        _assistant_toolcall('call_x'),
        _tool('call_x', 'ok'),
        {'role': 'assistant', 'content': 'done'},
    ]
    out = sanitize_tool_messages(clean)
    assert out == clean
    # Same object references, not just equal values -> happy path untouched.
    assert all(a is b for a, b in zip(clean, out))
    # A fresh list is always returned (input never aliased/mutated).
    assert out is not clean


def test_guard_regroups_split_parallel_calls():
    raw = [
        _assistant_toolcall('A', 'B'),
        _tool('A', 'a'),
        {'role': 'user', 'content': 'interjected'},
        _tool('B', 'b'),
    ]
    out = sanitize_tool_messages(raw)
    # Results for both parallel calls sit immediately after the owning assistant msg,
    # before the interleaved user message.
    assert out[0]['role'] == 'assistant'
    assert out[1]['tool_call_id'] == 'A'
    assert out[2]['tool_call_id'] == 'B'
    assert out[3]['role'] == 'user'


# --------------------------------------------------------------------------- #
# End-to-end through the real send conversion chokepoint
# --------------------------------------------------------------------------- #

def test_invalid_json_path_yields_one_result_with_real_id():
    """Field symptom: one edit_file call produced a bogus id='1' result AND a real one.

    After the stop-bleed (result #1 carries the sentinel, not '1') + the send guard, the
    sequence collapses to exactly ONE result bearing the real function_id.
    """
    real = 'call_56cd_deadbeef'
    messages = [
        Message(role=ASSISTANT, content='', function_call={'name': 'edit_file', 'arguments': '{}'},
                extra={'function_id': real}),
        # Synthetic validation-failure result that lost the real id -> sentinel (not '1').
        Message(role=FUNCTION, name='edit_file',
                content="Error: Invalid JSON arguments for tool 'edit_file'.",
                extra={'function_id': ORPHAN_TOOL_CALL_ID}),
        # The real execution result with the correct id.
        Message(role=FUNCTION, name='edit_file',
                content='ERROR: Pattern not found (exact)',
                extra={'function_id': real}),
    ]

    out = sanitize_tool_messages(messages)
    results = [m for m in out if m.role == FUNCTION]

    assert len(results) == 1
    assert results[0].extra['function_id'] == real
    assert results[0].content == 'ERROR: Pattern not found (exact)'
    # No plausible '1' and no sentinel survive to the wire.
    assert not any(m.extra.get('function_id') in ('1', ORPHAN_TOOL_CALL_ID) for m in results)


def test_missing_function_id_converts_to_sentinel_not_one():
    """Stop-bleed at the transport converter: an id-less function result converts to the
    clearly-invalid sentinel, never the plausible '1'."""
    messages = [
        Message(role=ASSISTANT, content='', function_call={'name': 'shell_cmd', 'arguments': '{}'},
                extra={}),
        Message(role=FUNCTION, name='shell_cmd', content='result', extra={}),
    ]
    converted = BaseChatModel._conv_agent_cascade_messages_to_oai(messages)
    tool_msgs = [m for m in converted if m.get('role') == 'tool']
    assert len(tool_msgs) == 1
    assert tool_msgs[0]['tool_call_id'] == ORPHAN_TOOL_CALL_ID
    assert tool_msgs[0]['tool_call_id'] != '1'


def test_guard_drops_result_appearing_before_its_owner():
    """Order-aware contract: a tool result that precedes its owning assistant call is an
    orphan (matches no PRECEDING call) and must be dropped, even though the id appears
    later in the list."""
    raw = [_tool('X', 'result before owner'), _assistant_toolcall('X')]
    # Discriminating precondition: the result with id X is present in the input.
    assert any(m.get('tool_call_id') == 'X' for m in raw)

    out = sanitize_tool_messages(raw)
    assert not any(m.get('role') == 'tool' for m in out), 'result before its owner must be dropped'
    assert [m['role'] for m in out] == ['assistant']


def test_guard_fail_safe_returns_input_unchanged_on_anomaly():
    """A catastrophic anomaly in the impl must NOT raise into the request path; the
    guard returns the input object verbatim. (BUG_0415 fail-safe requirement.)"""

    class _Boom:
        def __bool__(self):
            return True  # not treated as empty, so the impl proceeds

        def __iter__(self):
            raise RuntimeError('boom')  # breaks any forward scan

    boom = _Boom()
    out = sanitize_tool_messages(boom)  # must not raise
    assert out is boom  # input object returned unchanged


def test_guard_empty_input_is_identity():
    """Empty/falsy input returns the input object verbatim (no rebuild)."""
    empty = []
    assert sanitize_tool_messages(empty) is empty
    assert sanitize_tool_messages(None) is None


def test_guard_parallel_calls_no_op_identity():
    """One assistant message declaring two parallel calls, both answered in order, is a
    valid sequence -> byte/object identical no-op (same object references)."""
    clean = [_assistant_toolcall('A', 'B'), _tool('A', 'a'), _tool('B', 'b')]
    out = sanitize_tool_messages(clean)
    assert out == clean
    assert all(a is b for a, b in zip(clean, out))
    assert out is not clean
