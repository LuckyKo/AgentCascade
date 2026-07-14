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

"""Memory module tests against a local LLM server."""

import os
import shutil
from pathlib import Path

import json5
import pytest

from agent_cascade.llm.schema import ContentItem, Message
from agent_cascade.memory import Memory


@pytest.mark.skip_if_no_local
def test_memory(local_llm_cfg):
    """Test memory retrieval with a PDF file."""
    if os.path.exists('workspace'):
        shutil.rmtree('workspace')

    llm_cfg = dict(local_llm_cfg)
    mem = Memory(llm=llm_cfg)
    messages = [
        Message('user', [
            ContentItem(text='how to flip images'),
            ContentItem(file=str(Path(__file__).resolve().parent.parent.parent / 'examples/resource/doc.pdf'))
        ])
    ]
    *_, last = mem.run(messages, max_ref_token=4000, parser_page_size=500)
    print(last)
    assert isinstance(last[-1].content, str)
    assert len(last[-1].content) > 0

    res = json5.loads(last[-1].content)
    assert isinstance(res, list)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])