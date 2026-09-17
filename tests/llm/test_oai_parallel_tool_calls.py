"""Unit tests for ``oai.TextChatAtOAI._chat_stream`` parallel tool-call handling.

Regression (Bug B): the no-ID "merge into last" fallback used to fuse two DISTINCT parallel
tool calls into one when a model omitted tool-call IDs — collapsing two real calls into a single
function_call message (one bubble instead of two). The fix makes that fallback merge only for a
true continuation (empty name); a delta with a non-empty name always starts a NEW entry.

These tests drive the REAL ``_chat_stream`` (non-delta branch) with mocked SSE chunks — no live
server needed — and assert on the yielded message list:
  * two distinct parallel calls WITH ids -> two separate function_call messages (id path);
  * two distinct parallel calls WITHOUT ids -> NOT fused into one (the Bug B fix).

Follows the mocking conventions in test_oai.py (mock _chat_complete_create; patch watch_stream to
pass chunks through so the real matching logic runs unmodified).
"""

from unittest.mock import patch

from agent_cascade.llm.oai import TextChatAtOAI
from agent_cascade.llm.schema import Message


# ---------------------------------------------------------------------------
# Mock SSE plumbing
# ---------------------------------------------------------------------------


class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, id=None, function=None):
        self.id = id
        self.function = function


class _Delta:
    def __init__(self, content=None, tool_calls=None):
        # reasoning_content intentionally absent (hasattr False) to keep the path simple.
        self.content = content
        if tool_calls is not None:
            self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta, finish_reason=None):
        self.delta = delta
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, choices, model=None, usage=None):
        self.choices = choices
        if model is not None:
            self.model = model
        if usage is not None:
            self.usage = usage


class _SSE:
    """Mimics an httpx SSE event: .data and .json()."""

    def __init__(self, data, payload):
        self.data = data
        self._payload = payload

    def json(self):
        return self._payload


class _Resp:
    """Mimics the OpenAI streaming response object consumed by _chat_stream."""

    def __init__(self, chunks):
        # Pre-build SSE events (data + parsed json) from raw chunk objects. Each _Chunk holds a
        # single choice whose delta carries the tool_calls; mirror that shape into a nested dict
        # payload (the real client would parse JSON here).
        self._events = []
        for c in chunks:
            ch = c.choices[0]
            delta = {}
            if getattr(ch.delta, 'content', None) is not None:
                delta['content'] = ch.delta.content
            if getattr(ch.delta, 'tool_calls', None) is not None:
                delta['tool_calls'] = [{
                    'id': tc.id,
                    'function': {
                        'name': tc.function.name,
                        'arguments': tc.function.arguments,
                    },
                } for tc in ch.delta.tool_calls]
            self._events.append(_SSE('chunk', {'choices': [{'delta': delta, 'finish_reason': ch.finish_reason}]}))
        self._client = type('_Client', (), {
            '_process_response_data': staticmethod(lambda data, cast_to=None, response=None: _from_dict(data))
        })()
        self._cast_to = None
        self.response = None

    def _iter_events(self):
        # watch_stream is patched to pass these through unchanged.
        return iter(self._events)

    def close(self):
        # _chat_stream's finally block calls response.close(); no-op for the mock.
        pass


def _from_dict(payload):
    """Rebuild a _Chunk from the SSE payload dict (mirrors what the real client would do)."""
    choice = payload['choices'][0]
    d = choice.get('delta', {})
    tool_calls = None
    if d.get('tool_calls') is not None:
        tool_calls = [_TC(id=tc.get('id'), function=_Fn(name=(tc.get('function') or {}).get('name'),
                                                        arguments=(tc.get('function') or {}).get('arguments')))
                      for tc in d['tool_calls']]
    delta = _Delta(content=d.get('content'), tool_calls=tool_calls)
    return _Chunk(choices=[_Choice(delta, finish_reason=choice.get('finish_reason'))])


def _make_client():
    return TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})


def _run_stream(llm, chunks):
    """Drive the REAL _chat_stream with mocked SSE; return the final yielded message list."""
    llm._chat_complete_create = lambda **kwargs: _Resp(chunks)
    # Pass chunks through unchanged so the real tool-call matching logic runs unmodified.
    with patch('agent_cascade.utils.streaming.watch_stream', side_effect=lambda it, *a, **k: iter(it)):
        frames = list(llm.chat(messages=[Message('user', 'do two things')], stream=True, delta_stream=False))
    return frames[-1] if frames else []


# ---------------------------------------------------------------------------
# 1. Distinct parallel calls WITH ids -> two separate function_call messages
# ---------------------------------------------------------------------------


def test_parallel_distinct_calls_with_ids_two_messages():
    """Two distinct parallel tool calls (distinct ids) -> two separate function_call messages."""
    llm = _make_client()
    chunks = [
        # First chunk: both calls open with their ids + names.
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id='id_A', function=_Fn(name='search', arguments='{"q":"a"}')),
            _TC(id='id_B', function=_Fn(name='write_file', arguments='{"p":"/x"}')),
        ]))]),
        # Second chunk: continuation args for both (same ids) — must NOT merge across calls.
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id='id_A', function=_Fn(arguments=' more')),
            _TC(id='id_B', function=_Fn(arguments=' more2')),
        ]))]),
    ]

    msgs = _run_stream(llm, chunks)
    tool_msgs = [m for m in msgs if m.function_call is not None]
    assert len(tool_msgs) == 2, f"expected 2 function_call messages, got {len(tool_msgs)}: " + \
        ', '.join(str(m.function_call) for m in tool_msgs)
    # Distinct ids preserved on the two calls.
    ids = sorted(m.extra.get('function_id') for m in tool_msgs)
    assert ids == ['id_A', 'id_B'], f"expected distinct ids, got {ids}"


# ---------------------------------------------------------------------------
# 2. Distinct parallel calls WITHOUT ids -> NOT fused into one (Bug B fix)
# ---------------------------------------------------------------------------


def test_parallel_distinct_calls_no_ids_not_fused():
    """Two distinct parallel tool calls with NO ids -> two entries, not fused into one.

    This is the Bug B regression: before the fix, the no-ID fallback merged the second call's
    deltas into the first entry (full_tool_calls[-1]), fusing two real calls into one message.
    """
    llm = _make_client()
    chunks = [
        # First chunk opens call A (no id).
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id=None, function=_Fn(name='search', arguments='{"q":"a"}')),
        ]))]),
        # Second chunk opens call B (no id, NON-EMPTY name) -> must be a NEW entry, not merged.
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id=None, function=_Fn(name='write_file', arguments='{"p":"/x"}')),
        ]))]),
    ]

    msgs = _run_stream(llm, chunks)
    tool_msgs = [m for m in msgs if m.function_call is not None]
    assert len(tool_msgs) == 2, f"BUG B: two distinct no-id calls fused into one. got {len(tool_msgs)}: " + \
        ', '.join(str(m.function_call) for m in tool_msgs)
    # The two calls keep their distinct names (not concatenated).
    names = sorted(m.function_call.name for m in tool_msgs)
    assert names == ['search', 'write_file'], f"expected distinct names, got {names}"


# ---------------------------------------------------------------------------
# 3. Continuation WITH empty name still merges into the last entry (Grok compat preserved)
# ---------------------------------------------------------------------------


def test_no_id_continuation_still_merges():
    """A no-ID continuation delta (empty name, args only) still merges into the last entry.

    Preserves the Grok same-index behavior: a bare-args continuation after an established call
    appends to that call rather than starting a new one.
    """
    llm = _make_client()
    chunks = [
        # Open call A (no id).
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id=None, function=_Fn(name='search', arguments='{"q":"a"}')),
        ]))]),
        # Continuation: empty name, more args -> must MERGE into the same entry.
        _Chunk(choices=[_Choice(_Delta(tool_calls=[
            _TC(id=None, function=_Fn(arguments=' more')),
        ]))]),
    ]

    msgs = _run_stream(llm, chunks)
    tool_msgs = [m for m in msgs if m.function_call is not None]
    assert len(tool_msgs) == 1, f"continuation should merge into one entry, got {len(tool_msgs)}"
    # Args concatenated, name intact.
    assert tool_msgs[0].function_call.name == 'search'
    assert tool_msgs[0].function_call.arguments == '{"q":"a"} more'
