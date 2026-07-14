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

"""Integration tests against a local LLM server (LM Studio / Ollama).

These tests use the conftest fixtures ``local_llm_cfg`` and ``local_vl_llm_cfg``
which auto-detect a running local server at session start.  If no server is
found, all tests are skipped with a clear message instead of failing with auth
errors against DashScope / OpenAI.

Mark your test functions with @pytest.mark.skip_if_no_local to opt into this
behaviour even when you don't use the fixtures directly.
"""

import pytest

from agent_cascade.llm import get_chat_model
from agent_cascade.llm.schema import Message


# ---------------------------------------------------------------------------
# Basic chat — non-streaming
# ---------------------------------------------------------------------------

@pytest.mark.skip_if_no_local
def test_local_llm_basic(local_llm_cfg):
    """Send a simple message and verify we get text back."""
    llm = get_chat_model(local_llm_cfg)
    assert llm.max_retries >= 0

    messages = [Message('user', 'Say hello in one word')]
    response = llm.chat(messages=messages, stream=False)

    assert len(response) > 0
    content = response[-1]['content']
    assert isinstance(content, str) and len(content.strip()) > 0


# ---------------------------------------------------------------------------
# Streaming modes (mirror test_oai.py parameterisation)
# ---------------------------------------------------------------------------

@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [True, False])
def test_local_llm_streaming(local_llm_cfg, stream, delta_stream):
    """Test all streaming/delta combinations against the local endpoint."""
    if not stream and delta_stream:
        pytest.skip('Skipping this combination')

    llm = get_chat_model(local_llm_cfg)
    messages = [Message('user', 'Reply with exactly "OK"')]
    response = llm.chat(messages=messages, stream=stream, delta_stream=delta_stream)

    if stream:
        response = list(response)[-1]

    assert isinstance(response[-1]['content'], str)


# ---------------------------------------------------------------------------
# Vision model test (if VL fixture resolves)
# ---------------------------------------------------------------------------

@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
def test_local_vl_llm_basic(local_vl_llm_cfg):
    """Send a vision-capable chat request and verify response."""
    llm = get_chat_model(local_vl_llm_cfg)
    assert llm.max_retries >= 0

    messages = [Message('user', 'What color is the sky? Answer in one word.')]
    response = llm.chat(messages=messages, stream=False)

    assert len(response) > 0
    content = response[-1]['content']
    assert isinstance(content, str) and len(content.strip()) > 0


# ---------------------------------------------------------------------------
# Model availability check
# ---------------------------------------------------------------------------

@pytest.mark.skip_if_no_local
def test_models_available(local_llm_models):
    """Verify the detected server actually returned model IDs."""
    assert len(local_llm_models) > 0, "Server reported zero models"


# ---------------------------------------------------------------------------
# Retry config fixture
# ---------------------------------------------------------------------------

@pytest.mark.skip_if_no_local
def test_retry_cfg(local_llm_cfg_with_retry):
    """Ensure retry-aware config is properly shaped."""
    llm = get_chat_model(local_llm_cfg_with_retry)
    assert llm.max_retries == 2, "Retry fixture should set max_retries=2"