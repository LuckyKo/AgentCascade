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
import logging
import os
import requests
from pprint import pformat
from typing import Dict, Iterator, List, Optional

import openai

from agent_cascade.utils.utils import format_as_text_message

if openai.__version__.startswith('0.'):
    from openai.error import OpenAIError  # noqa
else:
    from openai import OpenAIError

from agent_cascade.llm.base import ModelServiceError, register_llm
from agent_cascade.llm.function_calling import BaseFnCallModel
from agent_cascade.llm.schema import ASSISTANT, FunctionCall, Message
from agent_cascade.log import logger


# Standard OpenAI-compatible inference parameters
ALLOWED_LLM_PARAMS = {
    'temperature', 'top_p', 'top_k', 'n', 'stop', 'max_tokens',
    'presence_penalty', 'frequency_penalty', 'logit_bias', 'user',
    'response_format', 'tools', 'tool_choice', 'parallel_tool_calls',
    'min_p', 'repeat_penalty', 'repetition_penalty', 'extra_body',
    'timeout', 'request_timeout'
}


@register_llm('oai')
class TextChatAtOAI(BaseFnCallModel):

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.model = self.model or 'local-model'
        cfg = cfg or {}

        api_base = cfg.get('api_base')
        api_base = api_base or cfg.get('base_url')
        api_base = api_base or cfg.get('model_server')
        api_base = (api_base or '').strip()

        api_key = cfg.get('api_key')
        api_key = api_key or os.getenv('OPENAI_API_KEY')
        api_key = (api_key or 'EMPTY').strip()

        if openai.__version__.startswith('0.'):
            if api_base:
                openai.api_base = api_base
            if api_key:
                openai.api_key = api_key
            self._complete_create = openai.Completion.create
            self._chat_complete_create = openai.ChatCompletion.create
        else:
            api_kwargs = {}
            if api_base:
                api_kwargs['base_url'] = api_base
            if api_key:
                api_kwargs['api_key'] = api_key

            def _chat_complete_create(*args, **kwargs):
                # OpenAI API v1 does not allow the following args, must pass by extra_body
                extra_params = ['top_k', 'repetition_penalty', 'repeat_penalty', 'repeatPenalty', 'min_p']
                if any((k in kwargs) for k in extra_params):
                    kwargs['extra_body'] = copy.deepcopy(kwargs.get('extra_body', {}))
                    for k in extra_params:
                        if k in kwargs:
                            kwargs['extra_body'][k] = kwargs.pop(k)
                if 'request_timeout' in kwargs:
                    kwargs['timeout'] = kwargs.pop('request_timeout')

                local_api_kwargs = dict(api_kwargs)
                if 'api_base' in kwargs:
                    local_api_kwargs['base_url'] = kwargs.pop('api_base')
                if 'api_key' in kwargs:
                    local_api_kwargs['api_key'] = kwargs.pop('api_key')

                client = openai.OpenAI(**local_api_kwargs)
                return client.chat.completions.create(*args, **kwargs)

            def _complete_create(*args, **kwargs):
                # OpenAI API v1 does not allow the following args, must pass by extra_body
                extra_params = ['top_k', 'repetition_penalty', 'repeat_penalty', 'repeatPenalty', 'min_p']
                if any((k in kwargs) for k in extra_params):
                    kwargs['extra_body'] = copy.deepcopy(kwargs.get('extra_body', {}))
                    for k in extra_params:
                        if k in kwargs:
                            kwargs['extra_body'][k] = kwargs.pop(k)
                if 'request_timeout' in kwargs:
                    kwargs['timeout'] = kwargs.pop('request_timeout')

                local_api_kwargs = dict(api_kwargs)
                if 'api_base' in kwargs:
                    local_api_kwargs['base_url'] = kwargs.pop('api_base')
                if 'api_key' in kwargs:
                    local_api_kwargs['api_key'] = kwargs.pop('api_key')

                client = openai.OpenAI(**local_api_kwargs)
                return client.completions.create(*args, **kwargs)

            self._complete_create = _complete_create
            self._chat_complete_create = _chat_complete_create

        # Attempt to dynamically detect context window size from local model servers (LM Studio, Ollama, etc.)
        if api_base and self.model and 'max_input_tokens' not in self.generate_cfg:
            try:
                models_url = f"{api_base.rstrip('/')}/models"
                headers = {"Authorization": f"Bearer {api_key}"} if api_key != 'EMPTY' else {}
                response = requests.get(models_url, headers=headers, timeout=5)
                if response.status_code == 200:
                    models_data = response.json()
                    data = models_data.get('data', [])
                    target_model = None
                    
                    # 1. Try exact match
                    for m in data:
                        if m.get('id') == self.model:
                            target_model = m
                            break
                    
                    # 2. If no exact match and only one model, assume it's the one
                    if not target_model and len(data) == 1:
                        target_model = data[0]
                        logger.info(f"Using single available model '{target_model.get('id')}' for context detection.")
                    
                    # 3. Special case for LM Studio / whatever_is_on
                    if not target_model and (self.model == 'whatever_is_on' or not data):
                        # Use the first model if it exists and looks plausible
                        if data:
                            target_model = data[0]
                            logger.info(f"Picking first available model '{target_model.get('id')}' for potential context detection.")
                    
                    if target_model:
                        # 4. Extract context length from model object (check direct and nested config)
                        ctx_len = (target_model.get('context_length') or 
                                   target_model.get('max_context_length') or
                                   target_model.get('config', {}).get('context_length') or
                                   target_model.get('config', {}).get('max_context_length'))
                        
                        # 5. If still missing, try querying the specific model endpoint
                        if not ctx_len:
                            try:
                                specific_url = f"{models_url}/{target_model.get('id')}"
                                logger.debug(f"Missing context metadata in list. Trying specific endpoint: {specific_url}")
                                spec_resp = requests.get(specific_url, headers=headers, timeout=3)
                                if spec_resp.status_code == 200:
                                    spec_data = spec_resp.json()
                                    ctx_len = (spec_data.get('context_length') or 
                                               spec_data.get('max_context_length') or
                                               spec_data.get('config', {}).get('context_length') or
                                               spec_data.get('config', {}).get('max_context_length'))
                            except Exception as inner_e:
                                logger.debug(f"Individual model metadata query failed: {inner_e}")
                        
                        if ctx_len:
                            logger.info(f"Dynamically detected context window for {target_model.get('id')}: {ctx_len}")
                            self.generate_cfg['max_input_tokens'] = int(ctx_len)
                        else:
                            logger.info(f"Model {target_model.get('id')} found, but could not detect context length via API.")
                    else:
                        logger.debug(f"Could not identify a target model in {models_url} for context length detection.")
            except Exception as e:
                logger.debug(f"Optional context length detection failed: {e}")

    def _chat_stream(
        self,
        messages: List[Message],
        delta_stream: bool,
        generate_cfg: dict,
    ) -> Iterator[List[Message]]:
        messages = self.convert_messages_to_dicts(messages)
        logger.debug(f'LLM Input generate_cfg: \n{generate_cfg}')
        local_model = generate_cfg.pop('model', self.model)
        log_api_post = generate_cfg.pop('log_api_post', False)
        
        # Strict Allowlist: Only pass parameters that the LLM API actually understands
        generate_cfg = {k: v for k, v in generate_cfg.items() if k in ALLOWED_LLM_PARAMS}
        
        if log_api_post:
            try:
                import json, time
                from pathlib import Path
                from agent_cascade.settings import DEFAULT_WORKSPACE
                debug_dir = Path(DEFAULT_WORKSPACE) / 'logs' / 'debug'
                debug_dir.mkdir(parents=True, exist_ok=True)
                dump_file = debug_dir / f"api_post_{int(time.time()*1000)}.json"
                dump_data = {"model": local_model, "messages": messages, **generate_cfg}
                with open(dump_file, 'w', encoding='utf-8') as f:
                    json.dump(dump_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to dump API POST: {e}")
            
        try:
            response = self._chat_complete_create(model=local_model, messages=messages, stream=True, **generate_cfg)
            if delta_stream:
                for chunk in response:
                    # Update local model info if returned by the server (e.g. LM Studio)
                    if hasattr(chunk, 'model') and chunk.model:
                        self.model = chunk.model
                        
                    if chunk.choices:
                        reasoning = chunk.choices[0].delta.reasoning_content if hasattr(chunk.choices[0].delta, 'reasoning_content') else ''
                        content = chunk.choices[0].delta.content if hasattr(chunk.choices[0].delta, 'content') else ''
                        if reasoning or content:
                            yield [Message(role=ASSISTANT, content=content or '', reasoning_content=reasoning or '')]
            else:
                full_response = ''
                full_reasoning_content = ''
                full_tool_calls = []
                for chunk in response:
                    # Update local model info if returned by the server
                    if hasattr(chunk, 'model') and chunk.model:
                        self.model = chunk.model
                        
                    if chunk.choices:
                        if hasattr(chunk.choices[0].delta,
                                   'reasoning_content') and chunk.choices[0].delta.reasoning_content:
                            full_reasoning_content += chunk.choices[0].delta.reasoning_content
                        if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
                            full_response += chunk.choices[0].delta.content
                        if hasattr(chunk.choices[0].delta, 'tool_calls') and chunk.choices[0].delta.tool_calls:
                            for tc in chunk.choices[0].delta.tool_calls:
                                if full_tool_calls and (not tc.id or
                                                        tc.id == full_tool_calls[-1]['extra']['function_id']):
                                    if tc.function.name:
                                        full_tool_calls[-1].function_call['name'] += tc.function.name
                                    if tc.function.arguments:
                                        full_tool_calls[-1].function_call['arguments'] += tc.function.arguments
                                else:
                                    full_tool_calls.append(
                                        Message(role=ASSISTANT,
                                                content='',
                                                function_call=FunctionCall(name=tc.function.name,
                                                                           arguments=tc.function.arguments),
                                                extra={'function_id': tc.id}))

                        res = []
                        finish_reason = getattr(chunk.choices[0], 'finish_reason', None)
                        extra = {'finish_reason': finish_reason} if finish_reason else {}
                        
                        if full_reasoning_content or full_response:
                            res.append(Message(
                                role=ASSISTANT,
                                content=full_response,
                                reasoning_content=full_reasoning_content,
                                extra=extra
                            ))
                        if full_tool_calls:
                            for tc in full_tool_calls:
                                if not tc.extra:
                                    tc.extra = {}
                                tc.extra.update(extra)
                            res += full_tool_calls
                        yield res
        except OpenAIError as ex:
            raise ModelServiceError(exception=ex)

    def _chat_no_stream(
        self,
        messages: List[Message],
        generate_cfg: dict,
    ) -> List[Message]:
        messages = self.convert_messages_to_dicts(messages)
        local_model = generate_cfg.pop('model', self.model)
        log_api_post = generate_cfg.pop('log_api_post', False)

        # Strict Allowlist: Only pass parameters that the LLM API actually understands
        generate_cfg = {k: v for k, v in generate_cfg.items() if k in ALLOWED_LLM_PARAMS}

        if log_api_post:
            try:
                import json, time
                from pathlib import Path
                from agent_cascade.settings import DEFAULT_WORKSPACE
                debug_dir = Path(DEFAULT_WORKSPACE) / 'logs' / 'debug'
                debug_dir.mkdir(parents=True, exist_ok=True)
                dump_file = debug_dir / f"api_post_{int(time.time()*1000)}.json"
                dump_data = {"model": local_model, "messages": messages, **generate_cfg}
                with open(dump_file, 'w', encoding='utf-8') as f:
                    json.dump(dump_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to dump API POST: {e}")

        try:
            response = self._chat_complete_create(model=local_model, messages=messages, stream=False, **generate_cfg)
            
            # Update local model info if returned by the server
            if hasattr(response, 'model') and response.model:
                self.model = response.model
                
            finish_reason = getattr(response.choices[0], 'finish_reason', None)
            extra = {'finish_reason': finish_reason} if finish_reason else {}
            if hasattr(response.choices[0].message, 'reasoning_content'):
                return [
                    Message(role=ASSISTANT,
                            content=response.choices[0].message.content,
                            reasoning_content=response.choices[0].message.reasoning_content,
                            extra=extra)
                ]
            else:
                return [Message(role=ASSISTANT, content=response.choices[0].message.content, extra=extra)]
        except OpenAIError as ex:
            raise ModelServiceError(exception=ex)

    def convert_messages_to_dicts(self, messages: List[Message]) -> List[dict]:
        # TODO: Change when the VLLM deployed model needs to pass reasoning_complete.
        #  At this time, in order to be compatible with lower versions of vLLM,
        #  and reasoning content is currently not useful
        messages = [format_as_text_message(msg, add_upload_info=False) for msg in messages]
        messages = [msg.model_dump() for msg in messages]
        messages = self._conv_agent_cascade_messages_to_oai(messages)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f'LLM Input: \n{pformat(messages, indent=2)}')
        return messages
