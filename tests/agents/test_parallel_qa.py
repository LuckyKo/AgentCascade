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

from agent_cascade.agents.doc_qa import ParallelDocQA


@pytest.mark.skip_if_no_local
def test_parallel_qa(local_llm_cfg):
    llm_cfg = dict(local_llm_cfg)
    agent = ParallelDocQA(llm=llm_cfg)
    messages = [{
        'role':
            'user',
        'content': [{
            'text': 'FastAPI适合IO密集任务吗'
        }, {
            'file': 'https://www.runoob.com/fastapi/fastapi-tutorial.html'
        }, {
            'file': 'https://www.runoob.com/fastapi/fastapi-install.html'
        }]
    }]
    *_, last = agent.run(messages)

    assert len(last[-1]['content']) > 0
