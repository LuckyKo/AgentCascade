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

import pytest

from agent_cascade.tools import AmapWeather, CodeInterpreter, ImageGen, Retrieval, WebSearch


# [NOTE] 不带"市"会出错
@pytest.mark.parametrize('params', [json.dumps({'location': '北京市'}), {'location': '杭州市'}])
def test_amap_weather(params):
    tool = AmapWeather()
    tool.call(params)


@pytest.mark.parametrize('params', ["print('hello qwen')", {'code': "print('hello qwen')"}])
def test_code_interpreter(params):
    tool = CodeInterpreter()
    tool.call(params)


def test_image_gen():
    tool = ImageGen()
    tool.call({'prompt': 'a dog'})


def test_retrieval():
    tool = Retrieval()
    tool.call({
        'query': 'Who are the authors of this paper?',
        'files': ['https://qianwen-res.oss-cn-beijing.aliyuncs.com/QWEN_TECHNICAL_REPORT.pdf']
    })


def test_web_search():
    tool = WebSearch()
    tool.call({'query': 'AgentCascade'})