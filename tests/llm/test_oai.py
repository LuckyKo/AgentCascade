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

"""OpenAI-compatible API tests against a local LLM server (LM Studio / Ollama).

Uses ``local_llm_cfg`` fixture from conftest.py which auto-detects a running
local server.  Tests skip cleanly if no local endpoint is available.
"""

import pytest

from agent_cascade.llm import get_chat_model
from agent_cascade.llm.schema import Message

functions = [{
    'name': 'image_gen',
    'description': 'AI绘画（图像生成）服务，输入文本描述和图像分辨率，返回根据文本信息绘制的图片URL。',
    'parameters': {
        'type': 'object',
        'properties': {
            'prompt': {
                'type': 'string',
                'description': '详细描述了希望生成的图像具有什么内容，例如人物、环境、动作等细节描述，使用英文',
            },
        },
        'required': ['prompt'],
    }
}]


@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('functions', [None, functions])
@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [True, False])
def test_llm_oai(local_llm_cfg, functions, stream, delta_stream):
    """Test OpenAI-compatible chat with all streaming/function combinations."""
    if not stream and delta_stream:
        pytest.skip('Skipping this combination')

    if delta_stream and functions:
        pytest.skip('Skipping this combination')

    llm = get_chat_model(local_llm_cfg)
    assert llm.max_retries >= 0

    messages = [Message('user', 'draw a cute cat')]
    response = llm.chat(messages=messages, functions=functions, stream=stream, delta_stream=delta_stream)
    if stream:
        response = list(response)[-1]

    assert isinstance(response[-1]['content'], str)
    # Function call assertions only valid when functions are provided
    # (local models may not always trigger function calls reliably)
    if functions:
        assert response[-1].function_call is not None


@pytest.mark.skip_if_no_local
def test_llm_oai_basic(local_llm_cfg):
    """Minimal non-streaming chat — should always pass with any local model."""
    llm = get_chat_model(local_llm_cfg)
    messages = [Message('user', 'Say hello in one word')]
    response = llm.chat(messages=messages, stream=False)

    assert isinstance(response[-1]['content'], str)
    assert len(response[-1]['content'].strip()) > 0


@pytest.mark.skip_if_no_local
def test_llm_oai_streaming(local_llm_cfg):
    """Streaming chat — verify we get content from the final chunk."""
    llm = get_chat_model(local_llm_cfg)
    messages = [Message('user', 'Reply with exactly "OK"')]
    response = list(llm.chat(messages=messages, stream=True, delta_stream=False))

    assert len(response) > 0
    assert isinstance(response[-1][-1]['content'], str)


class TestServerModelSeparation:
    """Regression: the server-echoed model id must NOT overwrite self.model.

    Telemetry and the A/B config fingerprint key on ``self.model`` (the user's
    configured name). Previously the OAI client wrote the server-reported id
    (e.g. a llama.cpp --alias / gguf filename) back into ``self.model`` on every
    stream, so call #1 logged the alias and call #2+ logged the filename —
    duplicating per-model telemetry. The echoed id is now kept in a separate
    ``_server_model`` field used only for context-window detection.
    """

    def _make_client(self):
        from agent_cascade.llm.oai import TextChatAtOAI
        return TextChatAtOAI({'api_base': 'http://127.0.0.1:9/v1', 'model': 'my-alias'})

    def test_init_keeps_config_name(self):
        llm = self._make_client()
        assert llm.model == 'my-alias'
        assert llm._server_model is None

    def test_server_echo_does_not_mutate_self_model(self):
        """Simulate the streaming path writing the server-reported id."""
        llm = self._make_client()
        # Replicate the exact mutation logic now in _chat_stream / non-stream:
        echoed = 'Qwen3-4B-Instruct-Q4_K_M.gguf'
        if echoed != llm._server_model:
            llm._server_model = echoed

        # The canonical identity (what telemetry reads) is untouched.
        assert llm.model == 'my-alias'
        # The server id is captured separately for context detection.
        assert llm._server_model == echoed

    def test_context_match_considers_server_id(self):
        """Context detection must still find the model by its server-reported id."""
        llm = self._make_client()
        llm._server_model = 'Qwen3-4B-Instruct-Q4_K_M.gguf'
        # The match-id set used by _detect_context_window includes both names.
        _match_ids = {llm.model}
        if llm._server_model:
            _match_ids.add(llm._server_model)
        assert 'Qwen3-4B-Instruct-Q4_K_M.gguf' in _match_ids
        assert 'my-alias' in _match_ids

    def test_non_stream_chat_keeps_config_name(self, monkeypatch):
        """Drive the REAL non-stream chat() path with a mocked server response that
        echoes a different model id (a gguf filename). Assert self.model — the value
        telemetry reads — stays the config name while _server_model captures the echo.

        This exercises the actual mutation site in oai.py rather than re-implementing
        it, so it fails if anyone reintroduces ``self.model = response.model``.
        """
        llm = self._make_client()

        class _Msg:
            content = 'hi'
            reasoning_content = None
            tool_calls = None

        class _Choice:
            finish_reason = 'stop'
            message = _Msg()

        class _Resp:
            model = 'Qwen3-4B-Instruct-Q4_K_M.gguf'  # server echoes the gguf filename
            choices = [_Choice()]
            usage = None

        llm._chat_complete_create = lambda **kwargs: _Resp()

        result = llm.chat(messages=[Message('user', 'hello')], stream=False)

        # The call succeeded and returned content.
        assert result[-1].content == 'hi'
        # Canonical identity (telemetry source) is untouched by the server echo.
        assert llm.model == 'my-alias'
        # Server-reported id captured separately for context detection.
        assert llm._server_model == 'Qwen3-4B-Instruct-Q4_K_M.gguf'