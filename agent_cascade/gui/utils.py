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
import os
from typing import Dict, List

from agent_cascade.llm.schema import ASSISTANT, CONTENT, FUNCTION, NAME, REASONING_CONTENT, ROLE, SYSTEM, USER

THINK_OPEN = '''
<details open>
  <summary>Thinking ...</summary>

{thought}

</details>
'''

THINK_CLOSED = '''
<details>
  <summary>Thinking ...</summary>

{thought}

</details>
'''

TOOL_CALL_OPEN = '''
<details open>
  <summary>🛠️ Calling tool: <b>{tool_name}</b></summary>
<div class="tool-call-body">{tool_input}</div>
</details>
'''

TOOL_CALL_CLOSED = '''
<details>
  <summary>🛠️ Calling tool: <b>{tool_name}</b></summary>
<div class="tool-call-body">{tool_input}</div>
</details>
'''

TOOL_OUTPUT = '''
<details>
  <summary>✅ Tool Execution Results</summary>
<div class="tool-output-body">{tool_output}</div>
</details>
'''


def get_avatar_image(name: str = 'user') -> str:
    if name == 'user':
        return os.path.join(os.path.dirname(__file__), 'assets/user.jpeg')

    return os.path.join(os.path.dirname(__file__), 'assets/logo.jpeg')


def convert_history_to_chatbot(messages):
    if not messages:
        return None
    chatbot_history = [[None, None]]
    for message in messages:
        if message.keys() != {'role', 'content'}:
            raise ValueError('Each message must be a dict containing only "role" and "content".')
        if message['role'] == USER:
            chatbot_history[-1][0] = message['content']
        elif message['role'] == ASSISTANT:
            chatbot_history[-1][1] = message['content']
            chatbot_history.append([None, None])
        else:
            raise ValueError(f'Message role must be {USER} or {ASSISTANT}.')
    return chatbot_history


def convert_fncall_to_text(messages: List[Dict]) -> List[Dict]:
    new_messages = []
    last_was_function = False

    for i, msg in enumerate(messages):
        role, content, reasoning_content, name = msg[ROLE], msg[CONTENT], msg.get(REASONING_CONTENT,
                                                                                  ''), msg.get(NAME, None)

        # Handle content as list or string
        if isinstance(content, list):
            # Extract text content from list of content items
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if 'text' in item:
                        text_parts.append(item['text'])
                    elif 'image' in item:
                        b64 = item.get('image', '')
                        # if b64 and not b64.startswith('data:image/'):
                        #     # 默认按png处理
                        #     data_url = f'data:image/png;base64,{b64}'
                        # else:
                        data_url = b64
                        text_parts.append(f'<img src="{data_url}" style="max-width:100%;height:auto;" />')
                    elif 'audio' in item:
                        text_parts.append(f"[Audio: {item.get('audio', '')}]")
                elif isinstance(item, str):
                    text_parts.append(item)
            # print(len(text_parts))
            content = ' '.join(text_parts)
        else:
            content = content or ''

        content = content.lstrip('\n').rstrip().replace('```', '')

        # if role is system or user, just append the message
        if role in (SYSTEM, USER):
            new_messages.append({ROLE: role, CONTENT: content, NAME: name})

        # if role is assistant, append the message and add function call details
        elif role == ASSISTANT:
            if reasoning_content:
                thought = reasoning_content
                # Keep open if it's the last message OR if the content is still essentially empty (streaming)
                is_active = (msg == messages[-1]) or not content.strip()
                t_tmpl = THINK_OPEN if is_active else THINK_CLOSED
                content = t_tmpl.format(thought=thought) + content

            if '<think>' in content:
                ti = content.find('<think>')
                te = content.find('</think>')
                is_thinking_active = False
                if te == -1:
                    te = len(content)
                    is_thinking_active = True
                
                thought = content[ti + len('<think>'):te]
                t_tmpl = THINK_OPEN if is_thinking_active else THINK_CLOSED
                
                if thought.strip():
                    _content = content[:ti] + t_tmpl.format(thought=thought)
                else:
                    _content = content[:ti]
                if te < len(content):
                    _content += content[te:]
                content = _content.strip('\n')

            fn_call = msg.get(f'{FUNCTION}_call', {})
            if fn_call:
                f_name = fn_call['name']
                # Try to parse and format gracefully
                f_args_raw = fn_call['arguments']
                if len(f_args_raw) > 10000: # Skip pretty-printing for very large payloads
                    f_args = f_args_raw
                else:
                    try:
                        f_args = json.dumps(json.loads(f_args_raw), indent=2, ensure_ascii=False)
                    except:
                        f_args = f_args_raw
                
                # Keep open if it's the last message OR if the next message isn't a FUNCTION result yet
                next_msg = messages[i + 1] if i + 1 < len(messages) else None
                is_active_tool = (next_msg is None or next_msg.get(ROLE) != FUNCTION)
                
                tc_tmpl = TOOL_CALL_OPEN if is_active_tool else TOOL_CALL_CLOSED
                content += "\n" + tc_tmpl.format(tool_name=f_name, tool_input=f_args)
            
            # If the previous message was a function result, start a new bubble
            # even if the role is the same.
            if len(new_messages) > 0 and new_messages[-1][ROLE] == ASSISTANT and new_messages[-1][NAME] == name and not last_was_function:
                new_messages[-1][CONTENT] += "\n" + content
            else:
                new_messages.append({ROLE: role, CONTENT: content, NAME: name})
            last_was_function = False

        # if role is function, append the message and add function result and exit details
        elif role == FUNCTION:
            assert new_messages[-1][ROLE] == ASSISTANT
            new_messages[-1][CONTENT] += TOOL_OUTPUT.format(tool_output=content)
            last_was_function = True

        # if role is not system, user, assistant or function, raise TypeError
        else:
            raise TypeError

    return new_messages
