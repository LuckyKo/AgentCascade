"""BUG_0157 — in-stream provider errors must surface as exceptions.

The provider can answer HTTP 200 (headers flushed) and then fail *during*
generation with an SSE error frame::

    event: error
    data: {"error": {"message": "internal error generation | checkpoint ...",
                     "type": "server_error", "code": 500}}

The OpenAI SDK turns that into a chunk without ``choices``, so before the fix the
stream simply ended and the engine saw an empty completion — which it retried with
no bound (265 requests in 15 s in the incident).

Hermetic: no network; ``_chat_complete_create`` is replaced by a fake stream.
"""

import json
from types import SimpleNamespace

import pytest

from agent_cascade.llm.base import ModelServiceError
from agent_cascade.llm.oai import TextChatAtOAI, _raise_on_stream_error, _stream_error_payload
from agent_cascade.llm.schema import Message


# ──────────────────────────────────────────────────────────────────────────────
# Fake SSE / SDK plumbing
# ──────────────────────────────────────────────────────────────────────────────


class _SSE:

    def __init__(self, data, event=None):
        self.data = data
        self.event = event

    def json(self):
        return json.loads(self.data)


class _Delta:

    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None


class _Chunk:

    def __init__(self, content=None):
        self.model = None
        self.usage = None
        self.choices = [SimpleNamespace(delta=_Delta(content))] if content is not None else []


class _FakeClient:

    def _process_response_data(self, *, data, cast_to, response):  # noqa: N803 - SDK signature
        content = None
        choices = data.get('choices') if isinstance(data, dict) else None
        if choices:
            content = (choices[0].get('delta') or {}).get('content')
        return _Chunk(content)


class _FakeResponse:

    def __init__(self, frames):
        self._frames = frames
        self._closed = False
        self._cast_to = object
        self._client = _FakeClient()
        self.response = object()

    def _iter_events(self):
        for frame in self._frames:
            yield frame

    def close(self):
        self._closed = True


def _stream(frames, delta_stream=False):
    """Run ``TextChatAtOAI._chat_stream`` over a fake SSE stream."""
    model = TextChatAtOAI({'model': 'test-model'})
    response = _FakeResponse(frames)
    model._chat_complete_create = lambda *a, **k: response
    return list(model._chat_stream([Message(role='user', content='hi')], delta_stream, {})), response


def _error_frame(code=500, message='internal error generation | checkpoint recovery owner is stale', event=None):
    payload = {'error': {'message': message, 'type': 'server_error', 'code': code}}
    return _SSE(json.dumps(payload), event=event)


# ──────────────────────────────────────────────────────────────────────────────
# Payload extraction
# ──────────────────────────────────────────────────────────────────────────────


def test_error_envelope_extracts_code_and_message():
    err = _stream_error_payload(_error_frame())
    assert err == {'code': '500', 'message': 'server_error: internal error generation | checkpoint recovery owner is stale'}


def test_numeric_status_code_is_kept():
    sse = _SSE(json.dumps({'error': {'message': 'server overloaded | context cache exhausted', 'code': 429}}))
    assert _stream_error_payload(sse)['code'] == '429'


def test_non_numeric_code_falls_back_to_status():
    sse = _SSE(json.dumps({'error': {'message': 'still loading', 'type': 'server_error', 'status': 503}}))
    assert _stream_error_payload(sse)['code'] == '503'


def test_event_error_with_plain_text_data():
    err = _stream_error_payload(_SSE('model crashed mid-generation', event='error'))
    assert err == {'code': None, 'message': 'model crashed mid-generation'}


def test_normal_chunk_is_not_an_error():
    sse = _SSE(json.dumps({'choices': [{'delta': {'content': 'hello'}}]}))
    assert _stream_error_payload(sse) is None


def test_done_marker_is_not_an_error():
    assert _stream_error_payload(_SSE('[DONE]')) is None


def test_invalid_json_without_error_event_is_not_an_error():
    assert _stream_error_payload(_SSE('not json')) is None


# ──────────────────────────────────────────────────────────────────────────────
# _chat_stream raises instead of returning an empty completion
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize('delta_stream', [False, True])
def test_mid_stream_error_raises_model_service_error(delta_stream):
    with pytest.raises(ModelServiceError) as ei:
        _stream([_error_frame(code=500)], delta_stream=delta_stream)

    err = ei.value
    assert err.code == '500'
    assert 'checkpoint recovery owner is stale' in str(err)


@pytest.mark.parametrize('delta_stream', [False, True])
def test_mid_stream_429_raises_retryable_error(delta_stream, caplog):
    frame = _SSE(
        json.dumps({'error': {
            'message': 'server overloaded | context cache exhausted',
            'code': 429
        }}))
    with caplog.at_level('ERROR', logger='agent_cascade_logger'):
        with pytest.raises(ModelServiceError) as ei:
            _stream([frame], delta_stream=delta_stream)

    assert ei.value.code == '429'
    assert any('Provider error mid-stream: HTTP 429' in r.getMessage() for r in caplog.records)


def test_error_after_partial_still_raises():
    """A failure after the first token must not be mistaken for a short answer."""
    frames = [
        _SSE(json.dumps({'choices': [{'delta': {'content': 'partial'}}]})),
        _error_frame(code=500),
    ]
    with pytest.raises(ModelServiceError):
        _stream(frames)


@pytest.mark.parametrize('delta_stream', [False, True])
def test_clean_stream_is_unaffected(delta_stream):
    frames = [
        _SSE(json.dumps({'choices': [{'delta': {'content': 'hi'}}]})),
        _SSE('[DONE]'),
    ]
    results, response = _stream(frames, delta_stream=delta_stream)

    contents = []
    for item in results:
        for msg in item:
            if isinstance(msg, Message):
                contents.append(msg.content)
    assert 'hi' in contents
    assert response._closed, 'the response must still be closed on the success path'


def test_raise_helper_is_a_noop_for_normal_frames():
    _raise_on_stream_error(_SSE(json.dumps({'choices': [{'delta': {'content': 'hi'}}]})))
