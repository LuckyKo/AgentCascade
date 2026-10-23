# Copyright 2023 The Qwen team, Alibaba Group. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Provider encrypted-reasoning blob support (OpenAI o-series / OpenRouter / xAI).

Deterministic, mock-based tests (no cloud key). Covers the full seam chain:

  1. capture  — non-stream + stream response -> Message.reasoning_blocks
  2. echo     — convert_messages_to_dicts emits the provider 'reasoning' key when opted in,
                and NOT when the capability flag is off (strict no-op).
  3. round-trip — turn 2 succeeds when the blob is echoed, 400s when stripped (the hard
                regression guard against the silent "starts from scratch" failure mode).
  4. serialization — the two plain-object whitelists (state_builder, logger) preserve the
                blob, and format_as_multimodal_message carries it through the request build.
  5. compression — the opaque blob is NOT folded into summaries; kept messages retain it.

A real e2e against o3-mini is gated on OPENAI_API_KEY (documented prerequisite; never blocks
the suite — no cloud keys are configured in this repo).
"""

import os

import pytest

from agent_cascade.llm.base import ModelServiceError
from agent_cascade.llm.oai import TextChatAtOAI
from agent_cascade.llm.schema import ASSISTANT, Message
from agent_cascade.api_integration_pkg.state_builder import serialize_message
from agent_cascade.compression.agent_invoker import _format_messages_for_summary

BLOB = 'gAAAAABdeterministic_test_blob'
BLK_ID = 'rs_test_1'


def _oai_cfg(api_base, **extra):
    """Endpoint config that skips context-window detection (no /models GET needed)."""
    cfg = {'api_base': api_base, 'model': 'test-model', 'generate_cfg': {'max_input_tokens': 8192}}
    cfg.update(extra)
    return cfg


# ---------------------------------------------------------------------------
# 1. Capture
# ---------------------------------------------------------------------------


def test_capture_non_stream():
    """_chat_no_stream reads msg.reasoning.{id,encrypted_content} and attaches it to the
    content-bearing Message (not to per-tool-call messages)."""
    llm = TextChatAtOAI(_oai_cfg('http://127.0.0.1:9/v1'))

    class _Msg:
        role = ASSISTANT
        content = 'answer-1'
        reasoning_content = None
        tool_calls = None
        reasoning = {'id': BLK_ID, 'encrypted_content': BLOB}

    class _Choice:
        finish_reason = 'stop'
        message = _Msg()

    class _Resp:
        model = 'test-model'
        usage = None
        choices = [_Choice()]

    llm._chat_complete_create = lambda **kwargs: _Resp()

    result = llm.chat(messages=[Message('user', 'q')], stream=False)
    assert result[-1].reasoning_blocks is not None
    assert result[-1].reasoning_blocks[0].encrypted_content == BLOB
    assert result[-1].reasoning_blocks[0].id == BLK_ID


def test_capture_stream():
    """_chat_stream accumulates the completed reasoning item (arrives on its own chunk) and
    attaches it to the final content-bearing Message."""

    class _SSE:
        data = 'c'

        def json(self):
            return {}

    # Two chunks: text delta, then the completed reasoning item on a separate chunk.
    chunk_deltas = [
        {'content': 'answer-1'},
        {'reasoning': {'id': BLK_ID, 'encrypted_content': BLOB}},
    ]
    remaining = list(chunk_deltas)

    class _Client:
        def _process_response_data(self, data, cast_to=None, response=None):
            delta_spec = remaining.pop(0)

            class _Delta:
                pass

            d = _Delta()
            for k, v in delta_spec.items():
                setattr(d, k, v)

            class _Choice:
                pass

            c = _Choice()
            c.delta = d
            c.finish_reason = None

            class _Chunk:
                pass

            ch = _Chunk()
            ch.choices = [c]
            ch.model = None
            ch.usage = None
            return ch

    class _Resp:
        _client = _Client()
        _cast_to = None
        response = None

        def _iter_events(self):
            for _ in range(len(chunk_deltas)):
                yield _SSE()

        def close(self):
            pass

    llm = TextChatAtOAI(_oai_cfg('http://127.0.0.1:9/v1'))
    llm._chat_complete_create = lambda **kw: _Resp()

    chunks = list(llm.chat(messages=[Message('user', 'q')], stream=True, delta_stream=False))
    # Flatten every yielded message; the blob must be on the final content-bearing one.
    blobbed = [m for res in chunks for m in res if getattr(m, 'reasoning_blocks', None)]
    assert blobbed, 'stream capture did not attach reasoning_blocks'
    assert blobbed[0].reasoning_blocks[0].encrypted_content == BLOB
    assert blobbed[0].reasoning_blocks[0].id == BLK_ID


# ---------------------------------------------------------------------------
# 2. Echo (request build) — gated on the capability flag
# ---------------------------------------------------------------------------


def _two_turn_messages():
    return [
        Message('user', 'q1'),
        Message(ASSISTANT, 'answer-1',
                reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}]),
        Message('user', 'q2'),
    ]


def test_echo_present_when_enabled():
    """With supports_encrypted_reasoning=True the serialized assistant message carries the
    provider 'reasoning' echo key (id + encrypted_content)."""
    llm = TextChatAtOAI(_oai_cfg('http://127.0.0.1:9/v1', supports_encrypted_reasoning=True))
    out = llm.convert_messages_to_dicts(_two_turn_messages())
    assistants = [m for m in out if m['role'] == ASSISTANT]
    assert assistants
    assert assistants[0]['reasoning'] == {'id': BLK_ID, 'encrypted_content': BLOB}


def test_echo_absent_when_disabled():
    """Default (flag off) is a strict no-op: the provider 'reasoning' key is never emitted."""
    llm = TextChatAtOAI(_oai_cfg('http://127.0.0.1:9/v1'))
    out = llm.convert_messages_to_dicts(_two_turn_messages())
    for m in out:
        assert 'reasoning' not in m


def test_clobber_guard_tool_call_preserves_blob():
    """A blobbed assistant message merged with a following tool-call message (which carries NO
    blob) must NOT have its echoed 'reasoning' key clobbered.

    Exercises the `if incoming_blocks:` guard in base.py._conv_agent_cascade_messages_to_oai:
    consecutive assistant messages merge into one wire message; the tool-call message has no
    reasoning_blocks, so it must leave the prior blob intact (mirrors the reasoning_content guard).
    """
    from agent_cascade.llm.schema import FunctionCall

    llm = TextChatAtOAI(_oai_cfg('http://127.0.0.1:9/v1', supports_encrypted_reasoning=True))
    msgs = [
        Message('user', 'weather?'),
        Message(ASSISTANT, 'SF is sunny',
                reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}]),
        # Tool-call turn: empty content, no reasoning_blocks — must not clobber the prior blob.
        Message(ASSISTANT, '', function_call=FunctionCall('get_weather', '{"city":"SF"}')),
    ]
    out = llm.convert_messages_to_dicts(msgs)
    assistants = [m for m in out if m['role'] == ASSISTANT]
    assert len(assistants) == 1, 'consecutive assistant messages must merge into one wire message'
    assert assistants[0]['reasoning'] == {'id': BLK_ID, 'encrypted_content': BLOB}, 'blob clobbered'
    assert assistants[0].get('tool_calls'), 'tool call must still be present alongside the blob'


def _plain_obj(**overrides):
    """A non-Pydantic, non-dict message object (exercises the generic-object fallback paths)."""
    class _Plain:
        role = ASSISTANT
        content = 'answer-1'
        reasoning_blocks = [{'id': BLK_ID, 'encrypted_content': BLOB}]

    inst = _Plain()
    for k, v in overrides.items():
        setattr(inst, k, v)
    return inst


# ---------------------------------------------------------------------------
# 3. Round-trip through the echo-required mock (the hard regression guard)
# ---------------------------------------------------------------------------


def test_round_trip_echo_required(echo_server):
    """Turn 1 captures the blob; turn 2 succeeds ONLY because AC echoes it back verbatim.

    The echo-required mock 400s if the assistant message on the wire lacks a complete
    reasoning block — turning the silent "model restarts reasoning" failure into a loud one.
    """
    llm = TextChatAtOAI(_oai_cfg(echo_server, supports_encrypted_reasoning=True))

    r1 = llm.chat(messages=[Message('user', 'q1')], stream=False)
    assert r1[-1].reasoning_blocks[0].encrypted_content == BLOB  # captured from turn 1

    # Carry the assistant turn forward verbatim; turn 2 must succeed.
    r2 = llm.chat(messages=[Message('user', 'q1'), r1[-1], Message('user', 'q2')], stream=False)
    assert r2[-1].content == 'answer-2'


def test_round_trip_fails_when_blob_stripped(echo_server):
    """If the blob is stripped before turn 2, the provider rejects the request (400).

    This is the assertion that catches the silent-regression failure mode: without the echo
    the request is rejected rather than quietly degrading quality.
    """
    llm = TextChatAtOAI(_oai_cfg(echo_server, supports_encrypted_reasoning=True))

    r1 = llm.chat(messages=[Message('user', 'q1')], stream=False)
    assert r1[-1].reasoning_blocks  # captured

    # Rebuild the assistant turn WITHOUT the blob (simulates a dropped field).
    stripped = Message(ASSISTANT, 'answer-1')
    with pytest.raises(ModelServiceError):
        llm.chat(messages=[Message('user', 'q1'), stripped, Message('user', 'q2')], stream=False)


# ---------------------------------------------------------------------------
# 4. Serialization seams (the silent-drop points)
# ---------------------------------------------------------------------------


def test_serialize_message_plain_object_preserves_blob():
    """state_builder.serialize_message plain-object fallback must keep reasoning_blocks."""
    d = serialize_message(_plain_obj(), for_ui=False, use_cache=False)
    assert d.get('reasoning_blocks') == [{'id': BLK_ID, 'encrypted_content': BLOB}]


def test_logger_generic_object_preserves_blob(tmp_path):
    """AgentInstanceLogger._format_message generic-object fallback must keep reasoning_blocks
    (parity with state_builder)."""
    from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger

    logger = AgentInstanceLogger('test', 'encblobs', log_dir=str(tmp_path), log_path=str(tmp_path / 'encblobs.jsonl'))
    d = logger._format_message(_plain_obj())
    assert d.get('reasoning_blocks') == [{'id': BLK_ID, 'encrypted_content': BLOB}]


def test_model_dump_round_trip():
    """A real Message round-trips through model_dump()/reconstruction (model_dump path)."""
    m = Message(ASSISTANT, 'answer-1', reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}])
    dumped = m.model_dump()
    assert dumped['reasoning_blocks'] == [{'id': BLK_ID, 'encrypted_content': BLOB}]
    restored = Message(**dumped)
    assert restored.reasoning_blocks[0].encrypted_content == BLOB


def test_format_as_text_message_preserves_blob():
    """format_as_multimodal_message rebuilds a Message; it must carry reasoning_blocks through
    or the entire echo path is dead."""
    from agent_cascade.utils.utils import format_as_text_message

    m = Message(ASSISTANT, 'answer-1', reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}])
    formatted = format_as_text_message(m, add_upload_info=False)
    assert formatted.reasoning_blocks[0].encrypted_content == BLOB


# ---------------------------------------------------------------------------
# 5. Compression boundary (accept chain-restart; never fold the opaque blob)
# ---------------------------------------------------------------------------


def test_compression_does_not_fold_blob():
    """_format_messages_for_summary must NOT fold the opaque encrypted blob into the summary."""
    msg = Message(ASSISTANT, 'visible answer',
                  reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}])
    summary = _format_messages_for_summary([msg])
    assert BLOB not in summary  # opaque blob is not summarizable text
    assert 'visible answer' in summary  # normal content is still folded


def test_compression_kept_tail_rebuild_preserves_blob():
    """Drive the real kept-tail rebuild (compression/helpers.py:rebuild_working_set): the pool's
    kept conversation is deep-copied into the caller's list, so a blobbed kept message retains
    reasoning_blocks and the echo still works for the surviving tail. Summarized-portion blobs are
    folded into summary text by _format_messages_for_summary and never carried as a field, so they
    are gone by design — the chain restarts at the boundary (documented, acceptable)."""
    from agent_cascade.compression.helpers import rebuild_working_set

    # The pool holds the kept tail (post-slice); one message carries a blob.
    kept = [Message(ASSISTANT, 'answer-1',
                    reasoning_blocks=[{'id': BLK_ID, 'encrypted_content': BLOB}])]

    class _FakePool:
        def get_conversation(self, name):
            return kept

        def get_instance(self, name):
            return None

    # The caller's stale working set is cleared and re-populated from the pool.
    caller_msgs = [Message('user', 'stale pre-compression turn')]
    rebuild_working_set(caller_msgs, _FakePool(), 'agent')

    assert len(caller_msgs) == 1, 'caller set must be rebuilt from the pool, not appended'
    assert caller_msgs[0].reasoning_blocks[0].encrypted_content == BLOB


# ---------------------------------------------------------------------------
# Real e2e (gated on a cloud key; never blocks the suite)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.getenv('OPENAI_API_KEY'),
                    reason='requires OPENAI_API_KEY + an o-series model (no cloud key configured)')
def test_real_o3_mini_two_turn():
    """Optional real-provider check: a 2-turn o3-mini conversation with the echo enabled must
    not error. Requires OPENAI_API_KEY and network access."""
    from agent_cascade.llm import get_chat_model

    llm = get_chat_model({'model_type': 'oai', 'model': 'o3-mini', 'reasoning_effort': 'low',
                         'supports_encrypted_reasoning': True})
    r1 = llm.chat(messages=[Message('user', 'What is 15 + 27? Think briefly.')], stream=False)
    r2 = llm.chat(messages=[Message('user', 'What is 15 + 27? Think briefly.'), r1[-1],
                            Message('user', 'Now say the result again.')], stream=False)
    assert isinstance(r2[-1]['content'], str)
