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
    'timeout', 'request_timeout', 'api_base', 'api_key'
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

        self.api_base = api_base
        self.api_key = api_key

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
        self.dynamic_model = not cfg.get('model') or cfg.get('model') == 'whatever_is_on'
        self.original_model = self.model
        
        if api_base and self.model and 'max_input_tokens' not in self.generate_cfg:
            self._detect_context_window(api_base, api_key)

    def _detect_context_window(self, api_base: str, api_key: str):
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
                # BUT ONLY if it's a plausible chat model and we are in dynamic mode.
                if not target_model and len(data) == 1:
                    m_id = data[0].get('id', '').lower()
                    # Skip auto-selection for models that are clearly for other tasks (TTS, Whisper, etc.)
                    non_chat_keywords = ['whisper', 'tts-', '-tts', 'embedding', 'rerank']
                    is_plausible_chat = not any(k in m_id for k in non_chat_keywords)
                    
                    if is_plausible_chat:
                        if self.dynamic_model:
                            target_model = data[0]
                            new_model_id = target_model.get('id')
                            logger.info(f"Auto-selected single available model '{new_model_id}' for calls and context detection.")
                            self.model = new_model_id
                            self.original_model = new_model_id
                        else:
                            # User provided a model name, but it wasn't in the list. 
                            # We can use the single available model for CONTEXT detection, but we keep the user's name for calls.
                            target_model = data[0]
                            logger.info(f"Using single available model '{target_model.get('id')}' for context length detection fallback.")
                
                # 3. Special case for LM Studio / whatever_is_on
                if not target_model and (self.model == 'whatever_is_on' or not data):
                    # Use the first model if it exists and looks plausible
                    if data:
                        m_id = data[0].get('id', '').lower()
                        non_chat_keywords = ['whisper', 'tts-', '-tts', 'embedding', 'rerank']
                        if not any(k in m_id for k in non_chat_keywords):
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
                        detected_len = int(ctx_len)
                        # Only auto-update if not manually set by user, or if we are upgrading from a default/small value
                        current_val = self.generate_cfg.get('max_input_tokens')
                        if not current_val or current_val == 58000 or current_val == 4096:
                            logger.info(f"Dynamically detected context window for {target_model.get('id')}: {detected_len}")
                            self.generate_cfg['max_input_tokens'] = detected_len
                        else:
                            logger.debug(f"Detected context window {detected_len} for {target_model.get('id')}, but keeping user-defined limit of {current_val}.")
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
        request_model = local_model
        if self.dynamic_model and local_model == self.model:
            # If the user didn't specify a model, and we are using our internal state,
            # send the generic 'original_model' name to allow the server to use whatever is loaded.
            request_model = self.original_model
            
        log_api_post = generate_cfg.pop('log_api_post', False)

        # Update local infrastructure state if changed in UI
        new_base = generate_cfg.get('api_base')
        new_key = generate_cfg.get('api_key')
        if (new_base and new_base != self.api_base) or (new_key and new_key != self.api_key):
            self.api_base = new_base or self.api_base
            self.api_key = new_key or self.api_key
            logger.info(f"LLM infrastructure changed. Re-detecting context for: {self.api_base}")
            self._detect_context_window(self.api_base, self.api_key)
        
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
                dump_data = {"model": request_model, "messages": messages, **generate_cfg}
                with open(dump_file, 'w', encoding='utf-8') as f:
                    json.dump(dump_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to dump API POST: {e}")
            
        try:
            response = self._chat_complete_create(model=request_model, messages=messages, stream=True, **generate_cfg)
            try:
                if delta_stream:
                    for chunk in response:
                        # Update local model info if returned by the server (e.g. LM Studio)
                        if hasattr(chunk, 'model') and chunk.model:
                            if chunk.model != self.model:
                                self.model = chunk.model
                                if self.dynamic_model and self.api_base:
                                    self._detect_context_window(self.api_base, self.api_key)
                            
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
                            if chunk.model != self.model:
                                self.model = chunk.model
                                if self.dynamic_model and self.api_base:
                                    self._detect_context_window(self.api_base, self.api_key)
                            
                        if chunk.choices:
                            if hasattr(chunk.choices[0].delta,
                                       'reasoning_content') and chunk.choices[0].delta.reasoning_content:
                                full_reasoning_content += chunk.choices[0].delta.reasoning_content
                            if hasattr(chunk.choices[0].delta, 'content') and chunk.choices[0].delta.content:
                                full_response += chunk.choices[0].delta.content
                            if hasattr(chunk.choices[0].delta, 'tool_calls') and chunk.choices[0].delta.tool_calls:
                                for tc in chunk.choices[0].delta.tool_calls:
                                    tc_index = getattr(tc, 'index', None)
                                    tc_id = getattr(tc, 'id', None)
                                    tc_func = getattr(tc, 'function', None)
                                    tc_name = getattr(tc_func, 'name', None) or '' if tc_func else ''
                                    tc_args = getattr(tc_func, 'arguments', None) or '' if tc_func else ''
                                    
                                    # Find existing tool call to append to (by ID or index)
                                    matched = None
                                    if tc_id:
                                        # New tool call with an ID
                                        for existing in full_tool_calls:
                                            if existing.extra.get('function_id') == tc_id:
                                                matched = existing
                                                break
                                    elif tc_index is not None:
                                        # Continuation chunk — match by index
                                        for existing in full_tool_calls:
                                            if existing.extra.get('tool_index') == tc_index:
                                                matched = existing
                                                break
                                    elif full_tool_calls:
                                        # No ID or index — append to last
                                        matched = full_tool_calls[-1]
                                    
                                    if matched:
                                        if tc_name:
                                            matched.function_call.name = (matched.function_call.name or '') + tc_name
                                        if tc_args:
                                            matched.function_call.arguments = (matched.function_call.arguments or '') + tc_args
                                    else:
                                        full_tool_calls.append(
                                            Message(role=ASSISTANT,
                                                    content='',
                                                    function_call=FunctionCall(name=tc_name,
                                                                               arguments=tc_args),
                                                    extra={'function_id': tc_id or f'call_{len(full_tool_calls)}',
                                                           'tool_index': tc_index if tc_index is not None else len(full_tool_calls)}))
    
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
            finally:
                # Bug #37 Fix: Ensure the HTTP streaming connection is closed even if interrupted (e.g. user clicks stop)
                try:
                    response.close()
                except Exception as e:
                    logger.warning(f"Failed to close streaming response for TextChatAtOAI: {e}")
        except OpenAIError as ex:
            raise ModelServiceError(exception=ex)

    def _chat_no_stream(
        self,
        messages: List[Message],
        generate_cfg: dict,
    ) -> List[Message]:
        messages = self.convert_messages_to_dicts(messages)
        local_model = generate_cfg.pop('model', self.model)
        request_model = local_model
        if self.dynamic_model and local_model == self.model:
            request_model = self.original_model

        log_api_post = generate_cfg.pop('log_api_post', False)

        # Update local infrastructure state if changed in UI
        new_base = generate_cfg.get('api_base')
        new_key = generate_cfg.get('api_key')
        if (new_base and new_base != self.api_base) or (new_key and new_key != self.api_key):
            self.api_base = new_base or self.api_base
            self.api_key = new_key or self.api_key
            logger.info(f"LLM infrastructure changed. Re-detecting context for: {self.api_base}")
            self._detect_context_window(self.api_base, self.api_key)

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
                dump_data = {"model": request_model, "messages": messages, **generate_cfg}
                with open(dump_file, 'w', encoding='utf-8') as f:
                    json.dump(dump_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.error(f"Failed to dump API POST: {e}")

        try:
            response = self._chat_complete_create(model=request_model, messages=messages, stream=False, **generate_cfg)

            # Update local model info if returned by the server
            if hasattr(response, 'model') and response.model:
                if response.model != self.model:
                    self.model = response.model
                    if self.dynamic_model and self.api_base:
                        self._detect_context_window(self.api_base, self.api_key)

            finish_reason = getattr(response.choices[0], 'finish_reason', None)
            extra = {'finish_reason': finish_reason} if finish_reason else {}

            msg = response.choices[0].message
            result = []

            # Handle tool_calls (native function calling mode)
            if hasattr(msg, 'tool_calls') and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = getattr(tc, 'id', None) or f'call_{len(result)}'
                    tc_func = getattr(tc, 'function', None)
                    tc_name = getattr(tc_func, 'name', '') if tc_func else ''
                    tc_args = getattr(tc_func, 'arguments', '') if tc_func else ''
                    result.append(
                        Message(role=ASSISTANT,
                                content='',
                                function_call=FunctionCall(name=tc_name,
                                                           arguments=tc_args),
                                extra={'function_id': tc_id})
                    )
                # Also include content/reasoning if present alongside tool_calls
                reasoning = getattr(msg, 'reasoning_content', None) or ''
                content = msg.content or ''
                if content or reasoning:
                    result.insert(0, Message(role=ASSISTANT,
                                            content=content,
                                            reasoning_content=reasoning,
                                            extra=extra))
            else:
                # No tool_calls — standard text response
                reasoning = getattr(msg, 'reasoning_content', None) or ''
                result.append(Message(role=ASSISTANT,
                                      content=msg.content or '',
                                      reasoning_content=reasoning,
                                      extra=extra))
            return result
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
