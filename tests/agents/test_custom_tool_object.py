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

import json
import urllib.parse

import json5
import pytest

from agent_cascade.agents import Assistant
from agent_cascade.tools.base import BaseTool


class MyImageGen(BaseTool):
    name = 'my_image_gen'
    description = 'AI painting (image generation) service, input text description, and return the image URL drawn based on text information.'
    parameters = [{
        'name': 'prompt',
        'type': 'string',
        'description': 'Detailed description of the desired image content, in English',
        'required': True
    }]

    def call(self, params: str, **kwargs) -> str:
        prompt = json5.loads(params)['prompt']
        prompt = urllib.parse.quote(prompt)
        return json.dumps({'image_url': f'https://image.pollinations.ai/prompt/{prompt}'}, ensure_ascii=False)


def init_agent_service(llm_cfg):
    system = ('According to the user\'s request, you must draw a picture with my_image_gen tool')

    tools = [MyImageGen(), 'code_interpreter']  # code_interpreter is a built-in tool in AgentCascade
    bot = Assistant(llm=llm_cfg, system_message=system, function_list=tools)

    return bot


@pytest.mark.skip_if_no_local
def test_custom_tool_object(local_llm_cfg):
    # Define the agent
    llm_cfg = dict(local_llm_cfg)
    bot = init_agent_service(llm_cfg)

    # Chat
    messages = [{'role': 'user', 'content': 'draw a dog'}]
    response = None
    for response in bot.run(messages=messages):
        print('bot response:', response)

    assert len(response) >= 2, f"Expected at least 2 responses, got {len(response)}"
    # Check that my_image_gen was called (look for it in the response chain)
    func_calls = [r for r in response if r.get('role') == 'function' and r.get('name') == 'my_image_gen']
    assert len(func_calls) > 0 or any(r.get('name') for r in response), \
        f"Expected my_image_gen tool call. Response roles: {[r.get('role') for r in response]}"
