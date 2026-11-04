"""BUG_0187 — session-aware KV save.

NInfer retains every finished conversation in its own catalog cell and reports the
cell's ``session_digest`` (plus ``id_slot``) on the final/usage SSE chunk. The
autoloader used to save ``slot 0`` unconditionally, so under concurrent fleet traffic
the caller's state file captured whichever sibling agent dominated that single slot.

These tests lock the AC half of the fix:

1. the digest is captured from the raw SSE payload (the typed SDK chunk drops it),
2. it is stored together with the endpoint that produced it, and
3. ``state_ops.save_instance_state`` forwards it to ``/state/save`` only when it
   belongs to the endpoint being saved — never a stale digest from another model.

Hermetic: no network, fake SSE plumbing mirrors tests/test_oai_stream_error_event.py.
"""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import agent_cascade.state_ops as state_ops
from agent_cascade.engine.helpers import _make_session_digest_callback
from agent_cascade.llm.base import _get_on_session_digest_cb, _set_on_session_digest_cb
from agent_cascade.llm.oai import TextChatAtOAI, _fire_slot_identity_from_raw
from agent_cascade.llm.schema import Message

CAPABLE_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:1234/v1',
    'model': 'test-model',
}
OTHER_EP = {
    'state_save_enabled': True,
    'api_base': 'http://127.0.0.1:9123/v1',
    'model': 'other-model',
}


def _endpoint_tuple(ep):
    return (ep['api_base'], ep['model'])


def _make_instance(*, endpoint=CAPABLE_EP, digest=None, digest_endpoint=None):
    """Stand-in AgentInstance carrying exactly the attributes the save seam reads."""
    inst = MagicMock()
    inst.instance_name = 'Maine'
    inst.agent_class = 'Orchestrator'
    # Real instances use an RLock; the save path re-acquires it.
    inst._state_lock = threading.RLock()
    inst._state_label = None
    inst._last_endpoint_config = dict(endpoint) if endpoint else None
    inst._session_digest = digest
    inst._session_digest_endpoint = digest_endpoint
    return inst


class _Resp:
    def __init__(self, status_code=200, text=''):
        self.status_code = status_code
        self.text = text


def _capture_save_body(inst):
    """Run save_instance_state offline and return the JSON body of the /state/save."""
    with patch.object(state_ops.httpx, 'post', return_value=_Resp(200)) as post, \
            patch.object(state_ops, '_cleanup_old_states'):
        assert state_ops.save_instance_state(inst) is True
    return post.call_args.kwargs['json']


# ── Callback captures digest + owning endpoint ────────────────────────────────


def test_callback_records_digest_and_endpoint():
    inst = _make_instance()
    cb = _make_session_digest_callback(inst)
    cb('abc123', 2)
    assert inst._session_digest == 'abc123'
    assert inst._session_digest_endpoint == _endpoint_tuple(CAPABLE_EP)


def test_callback_ignores_empty_digest():
    inst = _make_instance(digest='previous', digest_endpoint=_endpoint_tuple(CAPABLE_EP))
    cb = _make_session_digest_callback(inst)
    cb('', 0)
    cb(None, None)
    assert inst._session_digest == 'previous'  # untouched


# ── /state/save body carries the digest only when it belongs to that endpoint ──


def test_save_body_includes_digest_for_matching_endpoint():
    inst = _make_instance(digest='deadbeef', digest_endpoint=_endpoint_tuple(CAPABLE_EP))
    body = _capture_save_body(inst)
    assert body == {'label': 'Maine', 'session_digest': 'deadbeef'}


def test_save_body_omits_digest_without_one():
    inst = _make_instance()
    body = _capture_save_body(inst)
    assert body == {'label': 'Maine'}


def test_save_body_drops_digest_from_a_different_endpoint():
    """A digest produced by another model must never be sent to this endpoint."""
    inst = _make_instance(digest='deadbeef', digest_endpoint=_endpoint_tuple(OTHER_EP))
    body = _capture_save_body(inst)
    assert 'session_digest' not in body


def test_save_body_drops_non_string_digest():
    """Guards against a MagicMock/auto-attribute leaking into the JSON body."""
    inst = _make_instance(digest=object(), digest_endpoint=_endpoint_tuple(CAPABLE_EP))
    body = _capture_save_body(inst)
    assert 'session_digest' not in body


# ── Raw SSE payload is the only place the digest survives ─────────────────────


class _SSE:
    def __init__(self, data):
        self.data = data
        self.event = None

    def json(self):
        return json.loads(self.data)


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None


class _Chunk:
    def __init__(self, content=None, usage=None):
        self.model = 'test-model'
        self.usage = usage
        self.choices = [SimpleNamespace(delta=_Delta(content))] if content is not None else []


class _FakeClient:
    def _process_response_data(self, *, data, cast_to, response):  # noqa: N803 - SDK signature
        content = None
        choices = data.get('choices') if isinstance(data, dict) else None
        if choices:
            content = (choices[0].get('delta') or {}).get('content')
        usage = None
        if isinstance(data, dict) and data.get('usage'):
            u = data['usage']
            usage = SimpleNamespace(prompt_tokens=u.get('prompt_tokens'),
                                    completion_tokens=u.get('completion_tokens'),
                                    total_tokens=u.get('total_tokens'),
                                    completion_tokens_details=None,
                                    output_tokens_details=None)
        return _Chunk(content, usage)


class _FakeResponse:
    def __init__(self, frames):
        self._frames = frames
        self._cast_to = object
        self._client = _FakeClient()
        self.response = object()

    def _iter_events(self):
        yield from self._frames

    def close(self):
        pass


def _frame(payload):
    return _SSE(json.dumps(payload))


def test_raw_chunk_digest_fires_callback():
    seen = []
    _set_on_session_digest_cb(lambda digest, slot: seen.append((digest, slot)))
    try:
        _fire_slot_identity_from_raw({'session_digest': 'feedface', 'id_slot': 3})
        _fire_slot_identity_from_raw({'choices': []})
        _fire_slot_identity_from_raw(None)
    finally:
        _set_on_session_digest_cb(None)
    assert seen == [('feedface', 3)]


def test_chat_stream_reports_digest_from_final_usage_chunk():
    """End-to-end through the streaming loop: the digest on the usage chunk survives."""
    frames = [
        _frame({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]}),
        _frame({'choices': [{'delta': {'content': 'hi'}, 'finish_reason': None}]}),
        _frame({
            'choices': [{'delta': {}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12},
            'id_slot': 2,
            'session_digest': 'cafebabe',
        }),
        _SSE('[DONE]'),
    ]
    seen = []
    _set_on_session_digest_cb(lambda digest, slot: seen.append((digest, slot)))
    try:
        model = TextChatAtOAI({'model': 'test-model'})
        response = _FakeResponse(frames)
        model._chat_complete_create = lambda *a, **k: response
        list(model._chat_stream([Message(role='user', content='hi')], False, {}))
    finally:
        _set_on_session_digest_cb(None)
    assert seen == [('cafebabe', 2)]


def test_chat_installs_digest_callback_and_keeps_it_out_of_api_kwargs():
    """``chat()`` must extract ``_on_session_digest`` before the backend sees it.

    This is the wiring seam: llm_call registers the callback in ``extra_generate_cfg``,
    base.chat moves it to the thread-local, and the OpenAI allowlist must never forward
    it as an API parameter.
    """
    observed = {}

    def fake_chat_stream(messages, delta_stream, generate_cfg):
        observed['installed'] = _get_on_session_digest_cb()
        observed['leaked'] = '_on_session_digest' in generate_cfg
        return iter([])

    cb = lambda digest, slot=None: None  # noqa: E731 - identity only
    model = TextChatAtOAI({'model': 'test-model', 'max_input_tokens': 0})
    model._chat_stream = fake_chat_stream
    try:
        list(model.chat([Message(role='user', content='hi')], stream=True,
                        delta_stream=False,
                        extra_generate_cfg={'_on_session_digest': cb}))
    finally:
        _set_on_session_digest_cb(None)

    assert observed['installed'] is cb
    assert observed['leaked'] is False
