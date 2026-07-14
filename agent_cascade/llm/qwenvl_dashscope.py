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

import copy
import json
import os
import re
from http import HTTPStatus
from pprint import pformat
from typing import Dict, Iterator, List, Optional

import dashscope

from agent_cascade.llm.base import ModelServiceError, register_llm
from agent_cascade.llm.function_calling import BaseFnCallModel
from agent_cascade.llm.oai import _extract_usage
from agent_cascade.llm.qwen_dashscope import initialize_dashscope
from agent_cascade.llm.schema import ASSISTANT, ContentItem, FunctionCall, Message
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.utils.utils import hash_sha256, save_audio_to_file


@register_llm('qwenvl_dashscope')
class QwenVLChatAtDS(BaseFnCallModel):

    @property
    def support_multimodal_input(self) -> bool:
        return True

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.model = self.model or 'qwen-vl-max'
        initialize_dashscope(cfg)

    def _chat_stream(
        self,
        messages: List[Message],
        delta_stream: bool,
        generate_cfg: dict,
    ) -> Iterator[List[Message]]:
        if delta_stream:
            raise NotImplementedError

        messages = _format_local_files(messages)
        if not self.support_audio_input:
            messages = rm_unsupported_modality(messages)

        messages = [msg.model_dump() for msg in messages]
        if messages[-1]['role'] == ASSISTANT:
            messages[-1]['partial'] = True
        messages = self._conv_agent_cascade_messages_to_oai(messages)
        logger.debug(f'LLM Input: \n{pformat(messages, indent=2)}')
        logger.debug(f'LLM Input generate_cfg: \n{generate_cfg}')
        response = dashscope.MultiModalConversation.call(model=self.model,
                                                         messages=messages,
                                                         result_format='message',
                                                         stream=True,
                                                         **generate_cfg)
        full_content = []
        full_audio = ''  # Only one audio in one response
        full_reasoning_content = ''
        full_tool_calls = []
        res = []
        # Extract usage info from DashScope streaming response (last chunk has it)
        last_usage = {}
        for chunk in response:
            # print(chunk)
            if chunk.status_code == HTTPStatus.OK:
                # Capture usage from each chunk; last chunk typically has the final count
                extracted = _extract_usage(getattr(chunk, 'usage', None))
                if extracted:
                    last_usage = extracted
                if chunk.output.choices:
                    if 'reasoning_content' in chunk.output.choices[0].message and chunk.output.choices[
                            0].message.reasoning_content:
                        full_reasoning_content += chunk.output.choices[0].message.reasoning_content
                    if 'content' in chunk.output.choices[0].message and chunk.output.choices[0].message.content:
                        for item in chunk.output.choices[0].message.content:
                            for k, v in item.items():
                                if k == 'text':
                                    if not v:
                                        continue
                                    if full_content and full_content[-1].text:
                                        full_content[-1].text += v
                                    else:
                                        full_content.append(ContentItem(text=v))
                                elif k == 'image':
                                    full_content.append(ContentItem(image=v))
                                elif k == 'audio':
                                    full_audio += v.get('data')
                    tool_calls = chunk.output.choices[0].message.get('tool_calls', None)
                    if tool_calls:
                        # Track which positions were matched in this chunk to handle parallel tool calls sharing same index
                        _chunk_matched = set()
                        _initial_len = len(full_tool_calls)  # Prevent merging distinct new calls created within this chunk
                        for tc in tool_calls:
                            tc_id = tc.get('id')
                            tc_name = tc['function'].get('name', '')
                            tc_args = tc['function'].get('arguments', '')
                            
                            # Find existing tool call to append to (by ID, then fallback)
                            matched = None
                            matched_idx = -1
                            if tc_id and full_tool_calls:
                                # Match by exact function_id across all entries, skip already-matched positions
                                for idx, existing in enumerate(full_tool_calls):
                                    if existing.extra.get('function_id') == tc_id and idx not in _chunk_matched:
                                        matched = existing
                                        matched_idx = idx
                                        break
                            elif full_tool_calls:
                                # No ID — find first unmatched entry from pre-existing calls (forward walk preserves order)
                                for idx in range(len(full_tool_calls)):
                                    if idx not in _chunk_matched and idx < _initial_len:
                                        matched = full_tool_calls[idx]
                                        matched_idx = idx
                                        break
                                # All pre-existing entries matched this chunk: merge into last entry only
                                # if there were actual pre-existing calls (continuation, not first-chunk creation)
                                if matched is None and _initial_len > 0 and (tc_name or tc_args):
                                    matched = full_tool_calls[-1]
                                    matched_idx = len(full_tool_calls) - 1
                            
                            if matched:
                                if tc_name:
                                    matched.function_call['name'] += tc_name
                                if tc_args:
                                    matched.function_call['arguments'] += tc_args
                                # Mark the matched position so next tool call in this chunk won't reuse it
                                _chunk_matched.add(matched_idx)
                            else:
                                new_idx = len(full_tool_calls)
                                full_tool_calls.append(
                                    Message(role=ASSISTANT,
                                            content='',
                                            function_call=FunctionCall(name=tc_name,
                                                                       arguments=tc_args),
                                            extra={
                                                'model_service_info': json.loads(str(chunk)),
                                                'model': self.model,
                                                'function_id': tc_id or f'call_{new_idx}'
                                            }))
                                # Mark the newly created entry so subsequent deltas in this chunk won't merge into it
                                _chunk_matched.add(new_idx)
                    res = []
                    if full_reasoning_content:
                        msg_extra = {'model_service_info': json.loads(str(chunk)),
                                    'model': self.model}
                        if last_usage:
                            msg_extra['usage'] = last_usage
                        res.append(
                            Message(role=ASSISTANT,
                                    content=[],
                                    reasoning_content=full_reasoning_content,
                                    extra=msg_extra))
                    if full_content:
                        msg_extra = {'model_service_info': json.loads(str(chunk)),
                                    'model': self.model}
                        if last_usage:
                            msg_extra['usage'] = last_usage
                        res.append(
                            Message(role=ASSISTANT,
                                    content=full_content,
                                    reasoning_content='',
                                    extra=msg_extra))
                    if full_tool_calls:
                        res += full_tool_calls
                    yield res
            else:
                raise ModelServiceError(code=chunk.code,
                                        message=chunk.message,
                                        extra={
                                            'model_service_info': json.loads(str(chunk)),
                                            'model': self.model
                                        })
        if full_audio:
            # Only return audio at the end
            res = []
            if full_reasoning_content:
                msg_extra = {'model_service_info': json.loads(str(chunk)),
                            'model': self.model}
                if last_usage:
                    msg_extra['usage'] = last_usage
                res.append(
                    Message(role=ASSISTANT,
                            content=[],
                            reasoning_content=full_reasoning_content,
                            extra=msg_extra))

            if os.getenv('QWEN_AGENT_OMNI_RESPONSE_SAVE_AUDIO', 'false').lower() == 'true':
                work_dir = os.path.join(DEFAULT_WORKSPACE, 'llms')
                os.makedirs(DEFAULT_WORKSPACE, exist_ok=True)
                os.makedirs(work_dir, exist_ok=True)
                file_name = os.path.abspath(os.path.join(work_dir, f'{hash_sha256(full_audio)}.wav'))
                save_audio_to_file(base_64=full_audio, file_name=file_name)
                audio_content = file_name
            else:
                audio_content = f'data:audio/wav;base64,{full_audio}'
            full_content.append(ContentItem(audio=audio_content))
            if full_content:
                msg_extra = {'model_service_info': json.loads(str(chunk)),
                            'model': self.model}
                if last_usage:
                    msg_extra['usage'] = last_usage
                res.append(
                    Message(role=ASSISTANT,
                            content=full_content,
                            reasoning_content='',
                            extra=msg_extra))
            if full_tool_calls:
                res += full_tool_calls
            yield res
        logger.debug(f'LLM Output: \n{pformat([_.model_dump() for _ in res], indent=2)}')

    def _chat_no_stream(
        self,
        messages: List[Message],
        generate_cfg: dict,
    ) -> List[Message]:
        messages = _format_local_files(messages)
        if not self.support_audio_input:
            messages = rm_unsupported_modality(messages)

        messages = [msg.model_dump() for msg in messages]
        if messages[-1]['role'] == ASSISTANT:
            messages[-1]['partial'] = True
        logger.debug(f'LLM Input:\n{pformat(messages, indent=2)}')
        response = dashscope.MultiModalConversation.call(model=self.model,
                                                         messages=messages,
                                                         result_format='message',
                                                         stream=False,
                                                         **generate_cfg)
        if response.status_code == HTTPStatus.OK:
            # Extract usage info from DashScope VL response (includes completion_tokens_details if available)
            resp_usage = _extract_usage(getattr(response, 'usage', None))
            full_content = response.output.choices[0].message.content[0]['text']
            msg_extra = {'model_service_info': response}
            if resp_usage:
                msg_extra['usage'] = resp_usage
            if 'reasoning_content' in response.output.choices[0].message:
                full_reasoning_content = response.output.choices[0].message.reasoning_content
                return [
                    Message(role=ASSISTANT,
                            content=[ContentItem(text=full_content)],
                            reasoning_content=full_reasoning_content,
                            extra=msg_extra)
                ]
            else:
                return [
                    Message(role=ASSISTANT,
                            content=[ContentItem(text=full_content)],
                            extra=msg_extra)
                ]
        else:
            raise ModelServiceError(code=response.code,
                                    message=response.message,
                                    extra={'model_service_info': response})

    def _continue_assistant_response(
        self,
        messages: List[Message],
        generate_cfg: dict,
        stream: bool,
    ) -> Iterator[List[Message]]:
        return self._chat(messages, stream=stream, delta_stream=False, generate_cfg=generate_cfg)


# DashScope Qwen-VL requires the following format for local files:
#   Linux & Mac: file:///home/images/test.png
#   Windows: file://D:/images/abc.png
def _format_local_files(messages: List[Message]) -> List[Message]:
    messages = copy.deepcopy(messages)
    for msg in messages:
        if isinstance(msg.content, list):
            for item in msg.content:
                if item.image:
                    item.image = _conv_fname(item.image)
                if item.audio:
                    item.audio = _conv_fname(item.audio)
                if item.video:
                    if isinstance(item.video, str):
                        item.video = _conv_fname(item.video)
                    else:
                        assert isinstance(item.video, list)
                        new_url = []
                        for fname in item.video:
                            new_url.append(_conv_fname(fname))
                        item.video = new_url
    return messages


def _conv_fname(fname: str) -> str:
    ori_fname = fname
    if not fname.startswith((
            'http://',
            'https://',
            'file://',
            'data:',  # base64 such as f"data:image/jpg;base64,{image_base64}"
    )):
        if fname.startswith('~'):
            fname = os.path.expanduser(fname)
        fname = os.path.abspath(fname)
        if os.path.isfile(fname):
            if re.match(r'^[A-Za-z]:\\', fname):
                fname = fname.replace('\\', '/')
            fname = 'file://' + fname
            return fname

    return ori_fname


def rm_unsupported_modality(messages: List[Message]) -> List[Message]:
    messages = copy.deepcopy(messages)
    new_messages = []
    for msg in messages:
        if isinstance(msg.content, list):
            new_content = []
            for item in msg.content:
                if item.audio:
                    continue
                else:
                    new_content.append(item)
            msg.content = new_content
        new_messages.append(msg)

    return new_messages
