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
from http import HTTPStatus
from pprint import pformat
from typing import Dict, Iterator, List, Optional

import dashscope

from agent_cascade.llm.base import ModelServiceError, register_llm
from agent_cascade.llm.function_calling import BaseFnCallModel
from agent_cascade.llm.oai import _extract_usage
from agent_cascade.llm.schema import ASSISTANT, FunctionCall, Message
from agent_cascade.log import logger


@register_llm('qwen_dashscope')
class QwenChatAtDS(BaseFnCallModel):

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.model = self.model or 'qwen-max'
        initialize_dashscope(cfg)

    def _chat_stream(
        self,
        messages: List[Message],
        delta_stream: bool,
        generate_cfg: dict,
    ) -> Iterator[List[Message]]:
        messages = [msg.model_dump() for msg in messages]
        if messages[-1]['role'] == ASSISTANT:
            messages[-1]['partial'] = True
        messages = self._conv_agent_cascade_messages_to_oai(messages)
        logger.debug(f'LLM Input: \n{pformat(messages, indent=2)}')
        logger.debug(f'LLM Input generate_cfg: \n{generate_cfg}')
        response = dashscope.Generation.call(
            self.model,
            messages=messages,  # noqa
            result_format='message',
            stream=True,
            **generate_cfg)
        if delta_stream:
            return self._delta_stream_output(response)
        else:
            return self._full_stream_output(response)

    def _chat_no_stream(
        self,
        messages: List[Message],
        generate_cfg: dict,
    ) -> List[Message]:
        messages = [msg.model_dump() for msg in messages]
        if messages[-1]['role'] == ASSISTANT:
            messages[-1]['partial'] = True
        messages = self._conv_agent_cascade_messages_to_oai(messages)
        logger.debug(f'LLM Input: \n{pformat(messages, indent=2)}')
        response = dashscope.Generation.call(
            self.model,
            messages=messages,  # noqa
            result_format='message',
            stream=False,
            **generate_cfg)
        if response.status_code == HTTPStatus.OK:
            # Extract usage info from DashScope response (includes completion_tokens_details if available)
            return [
                Message(role=ASSISTANT,
                        content=response.output.choices[0].message.content,
                        reasoning_content=response.output.choices[0].message.get('reasoning_content', ''),
                        extra={'model_service_info': response, 'usage': _extract_usage(getattr(response, 'usage', None))})
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

    @staticmethod
    def _delta_stream_output(response) -> Iterator[List[Message]]:
        last_usage = {}  # Track usage across chunks; last chunk typically has final count
        for chunk in response:
            if chunk.status_code == HTTPStatus.OK:
                # Extract usage from each chunk (last chunk has the complete count)
                extracted = _extract_usage(getattr(chunk, 'usage', None))
                if extracted:
                    last_usage = extracted
                extra = {'model_service_info': chunk}
                if last_usage:
                    extra['usage'] = last_usage
                yield [
                    Message(role=ASSISTANT,
                            content=chunk.output.choices[0].message.content,
                            reasoning_content=chunk.output.choices[0].message.reasoning_content,
                            extra=extra)
                ]
            else:
                raise ModelServiceError(code=chunk.code, message=chunk.message, extra={'model_service_info': chunk})

    @staticmethod
    def _full_stream_output(response) -> Iterator[List[Message]]:
        full_content = ''
        full_reasoning_content = ''
        full_tool_calls = []
        # Extract usage info from DashScope streaming response (last chunk has it)
        last_usage = {}
        for chunk in response:
            if chunk.status_code == HTTPStatus.OK:
                # Capture usage from each chunk; last chunk typically has the final count
                extracted = _extract_usage(getattr(chunk, 'usage', None))
                if extracted:
                    last_usage = extracted
                if chunk.output.choices[0].message.get('reasoning_content', ''):
                    full_reasoning_content += chunk.output.choices[0].message.reasoning_content
                if chunk.output.choices[0].message.content:
                    full_content += chunk.output.choices[0].message.content
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
                                            'function_id': tc_id or f'call_{new_idx}'
                                        }))
                            # Mark the newly created entry so subsequent deltas in this chunk won't merge into it
                            _chunk_matched.add(new_idx)
                res = []
                if full_reasoning_content:
                    msg_extra = {'model_service_info': json.loads(str(chunk))}
                    if last_usage:
                        msg_extra['usage'] = last_usage
                    res.append(
                        Message(role=ASSISTANT,
                                content='',
                                reasoning_content=full_reasoning_content,
                                extra=msg_extra))
                if full_content:
                    msg_extra = {'model_service_info': json.loads(str(chunk))}
                    if last_usage:
                        msg_extra['usage'] = last_usage
                    res.append(
                        Message(role=ASSISTANT,
                                content=full_content,
                                extra=msg_extra))
                if full_tool_calls:
                    res += full_tool_calls
                yield res
            else:
                raise ModelServiceError(code=chunk.code, message=chunk.message, extra={'model_service_info': chunk})


def initialize_dashscope(cfg: Optional[Dict] = None) -> None:
    cfg = cfg or {}

    api_key = cfg.get('api_key', '')
    base_http_api_url = cfg.get('base_http_api_url', None)
    base_websocket_api_url = cfg.get('base_websocket_api_url', None)

    if not api_key:
        api_key = os.getenv('DASHSCOPE_API_KEY', 'EMPTY')
    if not base_http_api_url:
        base_http_api_url = os.getenv('DASHSCOPE_HTTP_URL', None)
    if not base_websocket_api_url:
        base_websocket_api_url = os.getenv('DASHSCOPE_WEBSOCKET_URL', None)

    api_key = api_key.strip()
    if api_key in ('', 'EMPTY'):
        if dashscope.api_key is None or dashscope.api_key in ('', 'EMPTY'):
            logger.warning(
                'No valid dashscope api_key found in cfg, environment variable `DASHSCOPE_API_KEY` or dashscope.api_key, the model call may raise errors.'
            )
        else:
            logger.info('No dashscope api_key found in cfg, using the dashscope.api_key that has already been set.')
    else:  # valid api_key
        if api_key != dashscope.api_key:
            logger.info('Setting the dashscope api_key.')
            dashscope.api_key = api_key
        # or do nothing since both keys are the same

    if base_http_api_url is not None:
        dashscope.base_http_api_url = base_http_api_url.strip()
    if base_websocket_api_url is not None:
        dashscope.base_websocket_api_url = base_websocket_api_url.strip()
