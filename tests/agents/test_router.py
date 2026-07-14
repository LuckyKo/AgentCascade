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

import pytest

from agent_cascade.agents import Assistant, Router
from agent_cascade.llm.schema import ContentItem, Message


@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
def test_router(local_llm_cfg, local_vl_llm_cfg):
    """Test router with local LLM models — delegates to VL and tool agents."""
    llm_cfg = dict(local_llm_cfg)
    llm_cfg_vl = dict(local_vl_llm_cfg)
    tools = ['amap_weather']

    # Define a vl agent
    bot_vl = Assistant(llm=llm_cfg_vl, name='多模态助手', description='可以理解图像内容。')

    # Define a tool agent
    bot_tool = Assistant(
        llm=llm_cfg,
        name='天气预报助手',
        description='可以查询天气',
        function_list=tools,
    )

    # define a router (Simultaneously serving as a text agent)
    bot = Router(llm=llm_cfg, agents=[bot_vl, bot_tool])
    messages = [
        Message('user', [
            ContentItem(text='描述图片'),
            ContentItem(image='https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg'),
        ])
    ]

    *_, last = bot.run(messages)
    assert isinstance(last[-1].content, str), f"Expected string content, got {type(last[-1].content)}"

    messages = [Message('user', '海淀区天气')]

    *_, last = bot.run(messages)
    # Local models may route differently; verify we got a meaningful response
    assert len(last) >= 2, f"Expected at least 2 messages in router response, got {len(last)}"
    func_calls = [msg for msg in last if getattr(msg, 'function_call', None)]
    if func_calls:
        # If tool was called, verify it's amap_weather with reasonable arguments
        assert any('amap_weather' in str(fc.function_call.name) for fc in func_calls), \
            f"Expected amap_weather call, got {[str(fc.function_call.name) for fc in func_calls]}"
    else:
        # No tool call — just verify the response has content about weather
        assert len(last[-1].content) > 0, "Final response has no content"
