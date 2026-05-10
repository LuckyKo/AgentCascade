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
from typing import Dict, List, Optional, Union

from agent_cascade.llm import get_chat_model
from agent_cascade.llm.schema import USER, ContentItem, Message
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('image_gen', allow_overwrite=True)
class ImageGen(BaseTool):
    description = TOOL_METADATA['image_gen']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'prompt': {
                'description': TOOL_METADATA['image_gen']['parameters']['prompt'],
                'type': 'string',
            }
        },
        'required': ['prompt'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        llm_cfg = self.cfg.get('llm_cfg', {})
        if not llm_cfg:
            raise ValueError('llm_cfg is required!')
        self.llm = get_chat_model(llm_cfg)
        self.size = self.cfg.get('size', '1024*1024')

    def call(self, params: Union[str, dict], **kwargs) -> List[ContentItem]:
        if isinstance(params, str):
            params = json.loads(params)

        messages = [Message(role=USER, content=[ContentItem(text=params['prompt'])])]
        kwargs.pop('messages')

        *_, last = self.llm.chat(messages=messages)
        return last[-1]['content']
