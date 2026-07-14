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

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(__file__, '../../..')))  # noqa

ROOT_RESOURCE = os.path.abspath(os.path.join(__file__, '../../../examples/resource'))  # noqa
from examples.assistant_add_custom_tool import test as assistant_add_custom_tool  # noqa
from examples.assistant_weather_bot import test as assistant_weather_bot  # noqa
from examples.function_calling import test as function_calling  # noqa
from examples.function_calling_in_parallel import test as parallel_function_calling  # noqa
# from examples.gpt_mentions import test as gpt_mentions  # noqa
from examples.group_chat_chess import test as group_chat_chess  # noqa
from examples.group_chat_demo import test as group_chat_demo  # noqa
from examples.llm_riddles import test as llm_riddles  # noqa
from examples.llm_vl_mix_text import test as llm_vl_mix_text  # noqa
from examples.multi_agent_router import test as multi_agent_router  # noqa
from examples.qwen2vl_assistant_tooluse import test as qwen2vl_assistant_tooluse  # noqa
from examples.qwen2vl_assistant_video import test as test_video  # noqa
from examples.react_data_analysis import test as react_data_analysis  # noqa
from examples.visual_storytelling import test as visual_storytelling  # noqa


# ---------------------------------------------------------------------------
# Text-only examples — use local_llm_cfg
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Text-only examples — use local_llm_cfg
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('query', ['draw a dog'])
def test_assistant_add_custom_tool(query, local_llm_cfg):
    assistant_add_custom_tool(query=query, llm_cfg=local_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('query', ['海淀区天气'])
@pytest.mark.parametrize('file', [None, os.path.join(ROOT_RESOURCE, 'poem.pdf')])
def test_assistant_weather_bot(query, file, local_llm_cfg):
    assistant_weather_bot(query=query, file=file, llm_cfg=local_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
def test_function_calling(local_llm_cfg):
    function_calling(llm_cfg=local_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
def test_parallel_function_calling(local_llm_cfg):
    parallel_function_calling(llm_cfg=local_llm_cfg)


# ---------------------------------------------------------------------------
# VL (vision + text) examples — use local_vl_llm_cfg
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
def test_llm_vl_mix_text(local_llm_cfg, local_vl_llm_cfg):
    llm_vl_mix_text(llm_cfg=local_llm_cfg, vl_llm_cfg=local_vl_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('query', [None, '看图说话'])
@pytest.mark.parametrize('image', ['https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg'])
def test_visual_storytelling(query, image, local_llm_cfg, local_vl_llm_cfg):
    visual_storytelling(query=query, image=image, llm_cfg=local_llm_cfg, vl_llm_cfg=local_vl_llm_cfg)


# ---------------------------------------------------------------------------
# ReAct / data analysis — text LLM with code interpreter
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize(
    'query', ['pd.head the file first and then help me draw a line chart to show the changes in stock prices'])
@pytest.mark.parametrize('file', [os.path.join(ROOT_RESOURCE, 'stock_prices.csv')])
def test_react_data_analysis(query, file, local_llm_cfg):
    react_data_analysis(query=query, file=file, llm_cfg=local_llm_cfg)


# ---------------------------------------------------------------------------
# LLM riddles — text-only
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
def test_llm_riddles(local_llm_cfg):
    llm_riddles(llm_cfg=local_llm_cfg)


# ---------------------------------------------------------------------------
# Multi-agent router — uses both text and VL agents
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('query', ['告诉我你现在知道什么了'])
@pytest.mark.parametrize('image', [None, 'https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg'])
@pytest.mark.parametrize('file', [None, os.path.join(ROOT_RESOURCE, 'poem.pdf')])
def test_multi_agent_router(query, image, file, local_llm_cfg, local_vl_llm_cfg):
    multi_agent_router(query=query, image=image, file=file, llm_cfg=local_llm_cfg, vl_llm_cfg=local_vl_llm_cfg)


# ---------------------------------------------------------------------------
# Group chat examples — text LLM
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
@pytest.mark.parametrize('query', ['开始吧'])
def test_group_chat_chess(query, local_llm_cfg):
    group_chat_chess(query=query, llm_cfg=local_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.skip_if_no_local
def test_group_chat_demo(local_llm_cfg):
    # group_chat_demo.test() has no parameters; it uses hardcoded config internally
    group_chat_demo()


# ---------------------------------------------------------------------------
# Qwen2-VL examples — VL LLM
# ---------------------------------------------------------------------------

@pytest.mark.extra_examples
@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
def test_qwen2vl_assistant_tooluse(local_vl_llm_cfg):
    qwen2vl_assistant_tooluse(vl_llm_cfg=local_vl_llm_cfg)


@pytest.mark.extra_examples
@pytest.mark.extra_vl
@pytest.mark.skip_if_no_local
def test_video_understanding(local_vl_llm_cfg):
    test_video(vl_llm_cfg=local_vl_llm_cfg)