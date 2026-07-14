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

"""Continue-generation tests against a local LLM server."""

import pytest

from agent_cascade.llm import get_chat_model
from agent_cascade.llm.schema import Message


@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('stream', [True, False])
@pytest.mark.parametrize('delta_stream', [False])
def test_continue(local_llm_cfg, stream, delta_stream):
    """Test that the model can continue from a partial assistant response."""
    if not stream and delta_stream:
        pytest.skip('Skipping this combination')

    llm = get_chat_model(local_llm_cfg)
    messages = [
        Message('user', 'what is 1+1?'),
        Message('assistant', '```python\nprint(1+1)\n```\n```output\n2\n```\n')
    ]

    response = llm.chat(messages=messages, stream=stream, delta_stream=delta_stream)
    if stream:
        response = list(response)[-1]
    assert isinstance(response[-1]['content'], str)
    assert response[-1].function_call is None
    print(response)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])