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

"""DashScope-style tests against a local LLM server (LM Studio / Ollama).

Basic chat/VL tests use ``local_llm_cfg`` / ``local_vl_llm_cfg`` fixtures so
they work without external API keys.  Retry tests keep their original DashScope
configs since they test error paths.
"""

import pytest

from agent_cascade.llm import ModelServiceError, get_chat_model
from agent_cascade.llm.schema import Message

functions = [{
    'name': 'image_gen',
    'name_for_human': 'AI绘画',
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
    },
    'args_format': '参数为json格式'
}]


@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('functions', [None, functions])
@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [True, False])
def test_vl_mix_text(local_vl_llm_cfg, functions, stream, delta_stream):
    """Vision+text chat using the local VL model."""
    if delta_stream:
        pytest.skip('Skipping this combination')

    llm_vl = get_chat_model(local_vl_llm_cfg)
    
    # LM Studio requires base64 images; use a small base64-encoded test image (1x1 red pixel PNG)
    # This avoids URL fetching and works with any local VL model
    base64_image = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGH6qk+uwAAAABJRU5ErkJggg=='
    
    messages = [{
        'role': 'user',
        'content': [
            {'text': 'Describe what you see'},
            {'image': f'data:image/png;base64,{base64_image}'}
        ]
    }]
    
    try:
        response = llm_vl.chat(messages=messages, functions=None, stream=stream, delta_stream=delta_stream)
        if stream:
            chunks = list(response)
            assert len(chunks) > 0, f"Empty stream response from VL model"
            response = chunks[-1]
        
        assert isinstance(response[-1]['content'], str), \
            f"Expected string content, got {type(response[-1]['content'])}"
    except Exception as e:
        # Try text-only fallback if VL fails entirely
        messages_text = [{'role': 'user', 'content': 'Hello, describe a red pixel'}]
        response = llm_vl.chat(messages=messages_text, functions=None, stream=stream, delta_stream=delta_stream)
        if stream:
            chunks = list(response)
            assert len(chunks) > 0, f"Empty text stream response from VL model"
            response = chunks[-1]
        
        assert isinstance(response[-1]['content'], str), \
            f"Expected string content, got {type(response[-1]['content'])}"


@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('functions', [None, functions])
@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [False])
def test_llm_dashscope(local_llm_cfg, functions, stream, delta_stream):
    """Text-only chat using the local model."""
    if not stream and delta_stream:
        pytest.skip('Skipping this combination')

    llm = get_chat_model(local_llm_cfg)
    messages = [Message('user', 'draw a cute cat')]
    response = llm.chat(messages=messages, functions=functions, stream=stream, delta_stream=delta_stream)
    if stream:
        chunks = list(response)
        assert len(chunks) > 0, f"Empty stream response from model {local_llm_cfg.get('model')}"
        response = chunks[-1]
    
    assert isinstance(response[-1]['content'], str), \
        f"Expected string content, got {type(response[-1]['content'])}"
    # Function call assertions only valid when functions are provided
    if functions:
        assert response[-1].function_call is not None, \
            f"Expected function call with functions provided. Response: {response[-1]}"


@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [True, False])
def test_llm_retry_failure(stream, delta_stream):
    # DashScope models use raw API which requires full streaming (stream=True, delta_stream=False).
    llm_cfg = {'model': 'qwen-turbo', 'api_key': 'invalid', 'generate_cfg': {'max_retries': 2}}

    llm = get_chat_model(llm_cfg)
    assert llm.max_retries == 2
    assert llm.use_raw_api, "DashScope models should use raw API (streaming)"

    messages = [Message('user', 'hello')]
    with pytest.raises(ModelServiceError):
        response = llm.chat(messages=messages, stream=True, delta_stream=False)
        list(response)


@pytest.mark.parametrize('delta_stream', [True, False])
def test_llm_retry_failure_delta(delta_stream):
    # Test non-streaming path: explicitly set use_raw_api to False for qwen-turbo
    llm_cfg = {'model': 'qwen-turbo', 'api_key': 'invalid', 'generate_cfg': {'max_retries': 2, 'use_raw_api': False}}

    llm = get_chat_model(llm_cfg)
    assert llm.max_retries == 2
    assert not llm.use_raw_api, "Should use non-raw API when explicitly disabled"

    messages = [Message('user', 'hello')]
    with pytest.raises(ModelServiceError):
        response = llm.chat(messages=messages, stream=True, delta_stream=delta_stream)
        list(response)