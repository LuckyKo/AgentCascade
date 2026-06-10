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
import hashlib
import json
import os
import random
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from pprint import pformat
from typing import Any, Dict, Iterator, List, Literal, Optional, Tuple, Union

from agent_cascade.llm.schema import ASSISTANT, DEFAULT_SYSTEM_MESSAGE, FUNCTION, SYSTEM, USER, Message
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_MAX_INPUT_TOKENS
from agent_cascade.utils.tokenization_qwen import tokenizer
from agent_cascade.utils.utils import (extract_text_from_message, format_as_multimodal_message, format_as_text_message,
                                    get_message_stats, has_chinese_messages, json_dumps_compact, json_loads,
                                    merge_generate_cfgs, print_traceback)

LLM_REGISTRY = {}


def register_llm(model_type):

    def decorator(cls):
        LLM_REGISTRY[model_type] = cls
        return cls

    return decorator


class ModelServiceError(Exception):

    def __init__(self,
                 exception: Optional[Exception] = None,
                 code: Optional[str] = None,
                 message: Optional[str] = None,
                 extra: Optional[dict] = None):
        if exception is not None:
            super().__init__(exception)
        else:
            super().__init__(f'\nError code: {code}. Error message: {message}')
        self.exception = exception
        self.code = code
        self.message = message
        self.extra = extra


class BaseChatModel(ABC):
    """The base class of LLM"""

    @property
    def support_multimodal_input(self) -> bool:
        # Does the model support multimodal input natively? It affects how we preprocess the input.
        return False

    @property
    def support_multimodal_output(self) -> bool:
        # Does the model generate multimodal outputs beyond texts? It affects how we post-process the output.
        return False

    @property
    def support_audio_input(self) -> bool:
        return False

    def __init__(self, cfg: Optional[Dict] = None):
        import hashlib
        
        cfg = cfg or {}
        self.cfg = cfg
        self.model = cfg.get('model', '').strip()
        generate_cfg = copy.deepcopy(cfg.get('generate_cfg', {}))
        # Support max_input_tokens at the top level of cfg
        if 'max_input_tokens' in cfg and 'max_input_tokens' not in generate_cfg:
            generate_cfg['max_input_tokens'] = cfg['max_input_tokens']
            
        cache_dir = cfg.get('cache_dir', generate_cfg.pop('cache_dir', None))
        self.max_retries = generate_cfg.pop('max_retries', 0)
        self.generate_cfg = generate_cfg
        self.model_type = cfg.get('model_type', '')
        
        # ── Preprocessing Cache (Feature 019: Prompt Reprocessing Optimization) ──
        # Cache preprocessed messages to avoid redundant work when same messages are sent multiple times
        # LRU-style cache with bounded size to prevent memory growth
        self._preprocess_cache: Dict[str, List[Message]] = {}
        self._max_preprocess_cache_size = 50  # Limit cache to 50 entries per model instance
        if 'dashscope' in self.model_type:
            self.generate_cfg['incremental_output'] = True

        self.use_raw_api = os.getenv('QWEN_AGENT_USE_RAW_API', 'false').lower() == 'true'
        if 'use_raw_api' in generate_cfg:
            self.use_raw_api = generate_cfg.pop('use_raw_api')
        elif self.model_type == 'qwen_dashscope':
            # set qwen3-max to `use_raw_api`
            if not self.use_raw_api:
                logger.info('Setting `use_raw_api` to True when using `Qwen3-Max`')
                self.use_raw_api = True

        if cache_dir:
            try:
                import diskcache
            except ImportError:
                print_traceback(is_error=False)
                logger.warning('Caching disabled because diskcache is not installed. Please `pip install diskcache`.')
                cache_dir = None
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            self.cache = diskcache.Cache(directory=cache_dir)
        else:
            self.cache = None

    def _get_message_hash(self, messages: List[Message], functions: Optional[List[Dict]] = None, 
                          generate_cfg: Optional[Dict] = None, lang: str = 'en') -> str:
        """Generate a hash for a list of messages to use as cache key.
        
        Includes message roles, content, function schemas, and relevant config in the hash
        to ensure cache validity when any of these change.
        
        Args:
            messages: List of Message objects to hash.
            functions: Optional list of function schemas (for fncall mode).
            generate_cfg: Optional generation config dict (keys that affect preprocessing).
            lang: Language code ('en' or 'zh') affecting multimodal upload info.
            
        Returns:
            MD5 hex digest string for use as cache key.
        """
        # Create a deterministic string representation of messages
        try:
            msg_repr = str([(m.role, str(m.content)) for m in messages])
        except Exception:
            # Defensive: handle complex multimodal objects with broken __str__
            msg_repr = repr([(m.role, getattr(m, 'content', '<err>')) for m in messages])
        
        # Include functions in hash if present (affects preprocessing)
        fn_repr = str(sorted([f.get('name', '') for f in functions])) if functions else ''
        
        # Include generate_cfg keys that affect preprocessing behavior
        if generate_cfg:
            relevant_keys = ['incremental_output', 'max_input_tokens']
            cfg_items = [(k, v) for k, v in sorted(generate_cfg.items()) if k in relevant_keys]
            gen_repr = str(cfg_items)
        else:
            gen_repr = ''
        
        combined = f"{msg_repr}|||{fn_repr}|||{gen_repr}|||{lang}"
        return hashlib.md5(combined.encode()).hexdigest()
    
    def _clear_preprocess_cache(self):
        """Clear the preprocessing cache.
        
        Called when model state changes (e.g., after compression rebuild) to ensure
        fresh preprocessing on next call.
        """
        self._preprocess_cache.clear()

    def quick_chat(self, prompt: str) -> str:
        *_, responses = self.chat(messages=[Message(role=USER, content=prompt)])
        assert len(responses) == 1
        assert not responses[0].function_call
        assert isinstance(responses[0].content, str)
        return responses[0].content

    def chat(
        self,
        messages: List[Union[Message, Dict]],
        functions: Optional[List[Dict]] = None,
        stream: bool = True,
        delta_stream: bool = False,
        extra_generate_cfg: Optional[Dict] = None,
    ) -> Union[List[Message], List[Dict], Iterator[List[Message]], Iterator[List[Dict]]]:
        """LLM chat interface.

        Args:
            messages: Inputted messages.
            functions: Inputted functions for function calling. OpenAI format supported.
            stream: Whether to use streaming generation.
            delta_stream: Whether to stream the response incrementally.
              (1) When False (recommended): Stream the full response every iteration.
              (2) When True: Stream the chunked response, i.e, delta responses.
            extra_generate_cfg: Extra LLM generation hyper-parameters.

        Returns:
            the generated message list response by llm.
            
        Note: Usage information from LLM API responses is stored in Message.extra['usage']
        dict with keys: prompt_tokens, completion_tokens, total_tokens (when available).
        See Feature 006 for ground-truth token tracking implementation details.
        """

        # Unify the input messages to type List[Message]:
        messages = copy.deepcopy(messages)
        _return_message_type = 'dict'
        new_messages = []
        for msg in messages:
            if isinstance(msg, dict):
                new_messages.append(Message(**msg))
            else:
                new_messages.append(msg)
                _return_message_type = 'message'
        messages = new_messages

        if not messages:
            raise ValueError('Messages can not be empty.')

        # Cache lookup:
        if self.cache is not None:
            cache_key = dict(messages=messages, functions=functions, extra_generate_cfg=extra_generate_cfg)
            cache_key: str = json_dumps_compact(cache_key, sort_keys=True)
            cache_value: str = self.cache.get(cache_key)
            if cache_value:
                cache_value: List[dict] = json.loads(cache_value)
                if _return_message_type == 'message':
                    cache_value: List[Message] = [Message(**m) for m in cache_value]
                if stream:
                    cache_value: Iterator[List[Union[Message, dict]]] = iter([cache_value])
                return cache_value

        if stream and delta_stream:
            logger.warning(
                'Support for `delta_stream=True` is deprecated. '
                'Please use `stream=True and delta_stream=False` or `stream=False` instead. '
                'Using `delta_stream=True` makes it difficult to implement advanced postprocessing and retry mechanisms.'
            )

        generate_cfg = merge_generate_cfgs(base_generate_cfg=self.generate_cfg, new_generate_cfg=extra_generate_cfg)
        # if 'seed' not in generate_cfg:
        #     generate_cfg['seed'] = random.randint(a=0, b=2**30)
        if 'lang' in generate_cfg:
            lang: Literal['en', 'zh'] = generate_cfg.pop('lang')
        else:
            lang: Literal['en', 'zh'] = 'zh' if has_chinese_messages(messages) else 'en'
        if not stream and 'incremental_output' in generate_cfg:
            generate_cfg.pop('incremental_output')

        if DEFAULT_SYSTEM_MESSAGE and messages[0].role != SYSTEM:
            messages = [Message(role=SYSTEM, content=DEFAULT_SYSTEM_MESSAGE)] + messages

        # Not precise. It's hard to estimate tokens related with function calling and multimodal items.
        max_input_tokens = generate_cfg.pop('max_input_tokens', DEFAULT_MAX_INPUT_TOKENS)

        # Feature 006: Extract token count callback BEFORE agent_settings loop
        on_token_count_cb = generate_cfg.pop('_on_token_count', None)

        agent_settings = [
            'disabled_tools', 'max_turns', 'auto_continue', 'auto_rollback_on_loop',
            'read_file_limit', 'mcpServers', 'work_access_folders',
            'grep_char_limit', 'shell_char_limit', 'code_char_limit', 'seed',
            '_on_token_count'  # Safety net: also pop here in case it wasn't extracted above
        ]
        for setting in agent_settings:
            generate_cfg.pop(setting, None)

        if max_input_tokens > 0:
            agent_name = generate_cfg.pop('agent_name', 'Unknown')
            messages = _truncate_input_messages_roughly(
                messages=messages,
                max_tokens=max_input_tokens,
                agent_name=agent_name,
                on_token_count_cb=on_token_count_cb,
            )

        if functions:
            fncall_mode = True
        else:
            fncall_mode = False
        if 'function_choice' in generate_cfg:
            fn_choice = generate_cfg['function_choice']
            valid_fn_choices = [f.get('name', f.get('name_for_model', None)) for f in (functions or [])]
            valid_fn_choices = ['auto', 'none'] + [f for f in valid_fn_choices if f]
            if fn_choice not in valid_fn_choices:
                raise ValueError(f'The value of function_choice must be one of the following: {valid_fn_choices}. '
                                 f'But function_choice="{fn_choice}" is received.')
            if fn_choice == 'none':
                fncall_mode = False

        # Note: the preprocessor's behavior could change if it receives function_choice="none"
        messages = self._preprocess_messages(messages,
                                             lang=lang,
                                             generate_cfg=generate_cfg,
                                             functions=functions,
                                             use_raw_api=self.use_raw_api)
        if not self.support_multimodal_input:
            messages = [format_as_text_message(msg, add_upload_info=False) for msg in messages]

        # Remove AgentCascade specific keys that LLM SDKs (like OpenAI) don't accept
        if 'disabled_tools' in generate_cfg:
            del generate_cfg['disabled_tools']

        if self.use_raw_api:
            logger.debug('`use_raw_api` takes effect.')
            assert stream and (not delta_stream), '`use_raw_api` only support full stream!!!'
            return self.raw_chat(messages=messages, functions=functions, stream=stream, generate_cfg=generate_cfg)

        if not fncall_mode:
            for k in ['parallel_function_calls', 'function_choice', 'thought_in_content']:
                if k in generate_cfg:
                    del generate_cfg[k]

        def _call_model_service():
            if fncall_mode:
                return self._chat_with_functions(
                    messages=messages,
                    functions=functions,
                    stream=stream,
                    delta_stream=delta_stream,
                    generate_cfg=generate_cfg,
                    lang=lang,
                )
            else:
                # TODO: Optimize code structure
                if messages[-1].role == ASSISTANT:
                    assert not delta_stream, 'Continuation mode does not currently support `delta_stream`'
                    return self._continue_assistant_response(messages, generate_cfg=generate_cfg, stream=stream)
                else:
                    return self._chat(
                        messages,
                        stream=stream,
                        delta_stream=delta_stream,
                        generate_cfg=generate_cfg,
                    )

        if stream and delta_stream:
            # No retry for delta streaming
            output = _call_model_service()
        elif stream and (not delta_stream):
            output = retry_model_service_iterator(_call_model_service, max_retries=self.max_retries)
        else:
            output = retry_model_service(_call_model_service, max_retries=self.max_retries)

        if isinstance(output, list):
            assert not stream
            logger.debug(f'LLM Output: \n{pformat([_.model_dump() for _ in output], indent=2)}')
            output = self._postprocess_messages(output, fncall_mode=fncall_mode, generate_cfg=generate_cfg)
            if not self.support_multimodal_output:
                output = _format_as_text_messages(messages=output)
            if self.cache:
                self.cache.set(cache_key, json_dumps_compact(output))
            return self._convert_messages_to_target_type(output, _return_message_type)
        else:
            assert stream
            if delta_stream:
                # Hack: To avoid potential errors during the postprocessing of stop words when delta_stream=True.
                # Man, we should never have implemented the support for `delta_stream=True` in the first place!
                generate_cfg = copy.deepcopy(generate_cfg)  # copy to avoid conflicts with `_call_model_service`
                assert 'skip_stopword_postproc' not in generate_cfg
                generate_cfg['skip_stopword_postproc'] = True
            output = self._postprocess_messages_iterator(output, fncall_mode=fncall_mode, generate_cfg=generate_cfg)

            def _format_and_cache() -> Iterator[List[Message]]:
                o = []
                for o in output:
                    if o:
                        if not self.support_multimodal_output:
                            o = _format_as_text_messages(messages=o)
                        yield o
                if o and (self.cache is not None):
                    self.cache.set(cache_key, json_dumps_compact(o))

            return self._convert_messages_iterator_to_target_type(_format_and_cache(), _return_message_type)

    def _chat(
        self,
        messages: List[Union[Message, Dict]],
        stream: bool,
        delta_stream: bool,
        generate_cfg: dict,
    ) -> Union[List[Message], Iterator[List[Message]]]:
        if stream:
            return self._chat_stream(messages, delta_stream=delta_stream, generate_cfg=generate_cfg)
        else:
            return self._chat_no_stream(messages, generate_cfg=generate_cfg)

    @abstractmethod
    def _chat_with_functions(
        self,
        messages: List[Union[Message, Dict]],
        functions: List[Dict],
        stream: bool,
        delta_stream: bool,
        generate_cfg: dict,
        lang: Literal['en', 'zh'],
    ) -> Union[List[Message], Iterator[List[Message]]]:
        raise NotImplementedError

    def _continue_assistant_response(
        self,
        messages: List[Message],
        generate_cfg: dict,
        stream: bool,
    ) -> Iterator[List[Message]]:
        raise NotImplementedError

    @abstractmethod
    def _chat_stream(
        self,
        messages: List[Message],
        delta_stream: bool,
        generate_cfg: dict,
    ) -> Iterator[List[Message]]:
        raise NotImplementedError

    @abstractmethod
    def _chat_no_stream(
        self,
        messages: List[Message],
        generate_cfg: dict,
    ) -> List[Message]:
        raise NotImplementedError

    def _preprocess_messages(
        self,
        messages: List[Message],
        lang: Literal['en', 'zh'],
        generate_cfg: dict,
        functions: Optional[List[Dict]] = None,
        use_raw_api: bool = False,
    ) -> List[Message]:
        # ── Feature 019: Preprocessing Cache ────────────────────────────────────
        # Check cache first to avoid redundant preprocessing work
        cache_key = self._get_message_hash(messages, functions, generate_cfg, lang)
        if cache_key in self._preprocess_cache:
            return copy.deepcopy(self._preprocess_cache[cache_key])
        
        # Evict oldest entries if cache is full (FIFO-style eviction)
        if len(self._preprocess_cache) >= self._max_preprocess_cache_size:
            # Remove first 20% of entries to make room
            keys_to_remove = list(self._preprocess_cache.keys())[:int(self._max_preprocess_cache_size * 0.2)]
            for key in keys_to_remove:
                self._preprocess_cache.pop(key, None)
        
        # ── Actual Preprocessing ────────────────────────────────────────────────
        add_multimodel_upload_info = False
        if functions or (not self.support_multimodal_input):
            add_multimodel_upload_info = True
        add_audio_upload_info = False
        if functions or (not self.support_audio_input):
            add_audio_upload_info = True
        preprocessed = [
            format_as_multimodal_message(msg,
                                         add_upload_info=True,
                                         add_multimodel_upload_info=add_multimodel_upload_info,
                                         add_audio_upload_info=add_audio_upload_info,
                                         lang=lang) for msg in messages
        ]
        
        # Cache the result (store reference, will be deep-copied on retrieval)
        self._preprocess_cache[cache_key] = preprocessed
        
        return copy.deepcopy(preprocessed)

    def _postprocess_messages(
        self,
        messages: List[Message],
        fncall_mode: bool,
        generate_cfg: dict,
    ) -> List[Message]:
        messages = [
            format_as_multimodal_message(msg,
                                         add_upload_info=False,
                                         add_multimodel_upload_info=False,
                                         add_audio_upload_info=False) for msg in messages
        ]
        if not generate_cfg.get('skip_stopword_postproc', False):
            stop = generate_cfg.get('stop', [])
            messages = _postprocess_stop_words(messages, stop=stop)
        return messages

    def _postprocess_messages_iterator(
        self,
        messages: Iterator[List[Message]],
        fncall_mode: bool,
        generate_cfg: dict,
    ) -> Iterator[List[Message]]:
        pre_msg = []
        for pre_msg in messages:
            yield self._postprocess_messages(pre_msg, fncall_mode=fncall_mode, generate_cfg=generate_cfg)
        # logger.debug(f'LLM Output: \n{pformat([_.model_dump() for _ in pre_msg], indent=2)}')

    def _convert_messages_to_target_type(self, messages: List[Message],
                                         target_type: str) -> Union[List[Message], List[Dict]]:
        if target_type == 'message':
            return [Message(**x) if isinstance(x, dict) else x for x in messages]
        elif target_type == 'dict':
            return [x.model_dump() if not isinstance(x, dict) else x for x in messages]
        else:
            raise NotImplementedError

    def _convert_messages_iterator_to_target_type(
            self, messages_iter: Iterator[List[Message]],
            target_type: str) -> Union[Iterator[List[Message]], Iterator[List[Dict]]]:
        for messages in messages_iter:
            yield self._convert_messages_to_target_type(messages, target_type)

    def raw_chat(
        self,
        messages: List[Union[Message, Dict]],
        functions: Optional[List[Dict]] = None,
        stream: bool = True,
        generate_cfg: Optional[Dict] = None,
    ) -> Union[List[Message], List[Dict], Iterator[List[Message]], Iterator[List[Dict]]]:
        if functions and functions[0].get('type') != 'function':
            functions = [{'type': 'function', 'function': f} for f in functions]
        if functions:
            generate_cfg['tools'] = functions
        if stream:
            return self._chat_stream(messages=messages, delta_stream=False, generate_cfg=generate_cfg)

    @staticmethod
    def _sanitize_fn_args(fn_args):
        """Sanitize function_call arguments to ensure they are a valid JSON string.

        OpenAI spec requires tool_call.arguments to be a JSON string. This method:
        - Serializes dict arguments to JSON
        - Validates string arguments (must parse as a JSON object)
        - Falls back to '{}' for empty/invalid arguments (with a warning)

        Returns a str that is valid JSON representing an object.
        """
        if isinstance(fn_args, dict):
            try:
                return json.dumps(fn_args, ensure_ascii=False)
            except (TypeError, ValueError):
                logger.warning('Tool call arguments dict was not JSON-serializable (%s); defaulting to "{}"', repr(fn_args))
                return '{}'
        if not isinstance(fn_args, str) or not fn_args.strip():
            # Empty args are normal during streaming (name arrives before arguments fill in) — only debug-level
            logger.debug('Tool call arguments were empty/None (type: %s); defaulting to "{}"', type(fn_args).__name__)
            return '{}'
        # Validate the string is parseable JSON and represents an object
        try:
            parsed = json_loads(fn_args)
            if isinstance(parsed, dict):
                return fn_args
            logger.warning('Tool call arguments parsed but were not a JSON object (got %s); defaulting to "{}". Original: %s',
                          type(parsed).__name__, repr(fn_args[:200]))
            return '{}'
        except (ValueError, TypeError):
            logger.warning('Tool call arguments were not valid JSON; defaulting to "{}". Original: %s', repr(fn_args[:200]))
            return '{}'

    @staticmethod
    def _conv_agent_cascade_messages_to_oai(messages: List[Union[Message, Dict]]):
        new_messages = []
        for msg in messages:
            if msg['role'] == ASSISTANT:
                if not new_messages or new_messages[-1]['role'] != ASSISTANT:
                    new_messages.append({'role': ASSISTANT})
                if msg.get('content'):
                    new_messages[-1]['content'] = msg['content']
                if msg.get('reasoning_content'):
                    new_messages[-1]['reasoning_content'] = msg['reasoning_content']
                if msg.get('function_call'):
                    if not new_messages[-1].get('tool_calls'):
                        new_messages[-1]['tool_calls'] = []
                    # Sanitize arguments to ensure valid JSON string (OpenAI spec requirement)
                    fn_args = BaseChatModel._sanitize_fn_args(msg['function_call'].get('arguments', ''))

                    new_messages[-1]['tool_calls'].append({
                        'id': msg.get('extra', {}).get('function_id', '1'),
                        'type': 'function',
                        'function': {
                            'name': msg['function_call']['name'],
                            'arguments': fn_args
                        }
                    })
            elif msg['role'] == FUNCTION:
                # OpenAI spec: tool messages need 'tool_call_id', not 'id'
                tool_call_id = msg.get('extra', {}).get('function_id', '1')
                content = msg.get('content', '')
                # Content might be a list of ContentItems — flatten to string
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if hasattr(item, 'text') and item.text:
                            parts.append(item.text)
                        elif isinstance(item, dict) and item.get('text'):
                            parts.append(item['text'])
                    content = ''.join(parts)
                new_messages.append({
                    'role': 'tool',
                    'tool_call_id': tool_call_id,
                    'content': content or '',
                })
            else:
                new_messages.append(msg)
        return new_messages

    def quick_chat_oai(self, messages: List[dict], tools: Optional[list] = None) -> dict:
        """
        This is a temporary OpenAI-compatible interface that is encapsulated and may change at any time.
        It is mainly used for temporary interfaces and should not be overly dependent.
        - Only supports full streaming
        - The message is in dict format
        - Only supports text LLM
        """

        def _convert_to_agent_cascade_messages(messages):
            new_messages = []
            for msg in messages:
                if msg['role'] in ['system', 'user']:
                    new_messages.append(msg)
                elif msg['role'] == 'tool':
                    new_msg = copy.deepcopy(msg)
                    new_msg['role'] = 'function'
                    new_messages.append(new_msg)
                elif msg['role'] == 'assistant':
                    if msg['content']:
                        new_messages.append({'role': 'assistant', 'content': msg['content']})
                    if msg.get('reasoning_content', ''):
                        new_messages.append({
                            'role': 'assistant',
                            'content': '',
                            'reasoning_content': msg['reasoning_content']
                        })
                    if msg.get('tool_calls'):
                        for tool in msg.get('tool_calls'):
                            new_messages.append({
                                'role': 'assistant',
                                'content': '',
                                'function_call': {
                                    'name': tool['function']['name'],
                                    'arguments': tool['function']['arguments']
                                }
                            })
            return new_messages

        def _convert_to_oai_message(data):
            message = {'role': 'assistant', 'content': '', 'reasoning_content': '', 'tool_calls': []}

            for item in data:
                if item.get('reasoning_content'):
                    message['reasoning_content'] += item['reasoning_content']

                if item.get('content'):
                    message['content'] += item['content']

                if item.get('function_call'):
                    # Sanitize arguments to ensure valid JSON string (OpenAI spec requirement)
                    fc_args = BaseChatModel._sanitize_fn_args(item['function_call'].get('arguments', ''))

                    tool_call = {
                        'id': f"{len(message['tool_calls']) + 1}",
                        'type': 'function',
                        'function': {
                            'name': item['function_call']['name'],
                            'arguments': fc_args
                        }
                    }
                    message['tool_calls'].append(tool_call)
            # Fake token usage
            response = {
                'choices': [{
                    'message': message
                }],
                'usage': {
                    'prompt_tokens': 0,
                    'completion_tokens': 0,
                    'total_tokens': 0
                }
            }
            return response

        if tools:
            functions = [tool['function'] for tool in tools]
        else:
            functions = None
        for rsp in self.chat(
                messages=_convert_to_agent_cascade_messages(messages),
                functions=functions,
                stream=True,
        ):
            yield _convert_to_oai_message(rsp)


def _format_as_text_messages(messages: List[Message]) -> List[Message]:
    for msg in messages:
        if isinstance(msg.content, list):
            for item in msg.content:
                assert item.type == 'text'
        else:
            assert isinstance(msg.content, str)
    messages = [format_as_text_message(msg, add_upload_info=False) for msg in messages]
    return messages


def _postprocess_stop_words(messages: List[Message], stop: List[str]) -> List[Message]:
    messages = copy.deepcopy(messages)
    if not messages:
        return messages

    # Make sure it stops before stop words.
    trunc_messages = []
    for msg in messages:
        truncated = False
        trunc_content = []
        for i, item in enumerate(msg.content):
            item_type, item_text = item.get_type_and_value()
            if item_type == 'text':
                truncated, item.text = _truncate_at_stop_word(text=item_text, stop=stop)
            trunc_content.append(item)
            if truncated:
                break
        msg.content = trunc_content
        trunc_messages.append(msg)
        if truncated:
            break
    messages = trunc_messages

    # It may ends with partial stopword 'Observation' when the full stopword is 'Observation:'.
    # The following post-processing step removes partial stop words.
    partial_stop = []
    for s in stop:
        s = tokenizer.tokenize(s)[:-1]
        if s:
            s = tokenizer.convert_tokens_to_string(s)
            partial_stop.append(s)
    partial_stop = sorted(set(partial_stop))
    if messages:
        last_msg = messages[-1].content
        for i in range(len(last_msg) - 1, -1, -1):
            item_type, item_text = last_msg[i].get_type_and_value()
            if item_type == 'text':
                for s in partial_stop:
                    if item_text.endswith(s):
                        last_msg[i].text = item_text[:-len(s)]
                break

    return messages


def _truncate_at_stop_word(text: str, stop: List[str]):
    truncated = False
    for s in stop:
        k = text.find(s)
        if k >= 0:
            truncated = True
            text = text[:k]
    return truncated, text


def _truncate_input_messages_roughly(messages: List[Message], max_tokens: int, agent_name: str = 'Unknown', on_token_count_cb=None) -> List[Message]:
    if len([m for m in messages if m.role == SYSTEM]) >= 2:
        raise ModelServiceError(
            code='400',
            message='The input messages must contain no more than one system message. '
            ' And the system message, if exists, must be the first message.',
        )
    if not messages:
        return messages

    turns = []
    for m in messages:
        if m.role == SYSTEM:
            continue
        elif m.role == USER:
            turns.append([m])
        else:
            if turns:
                turns[-1].append(m)
            else:
                raise ModelServiceError(
                    code='400',
                    message='The input messages (excluding the system message) must start with a user message.',
                )

    def _count_tokens(msg: Message) -> int:
        return get_message_stats(msg)['tokens']

    def _truncate_message(msg: Message, max_tokens: int, keep_both_sides: bool = False):
        if isinstance(msg.content, str):
            content = tokenizer.truncate(msg.content, max_token=max_tokens, keep_both_sides=keep_both_sides)
        else:
            text = []
            for item in msg.content:
                if not item.text:
                    return None
                text.append(item.text)
            text = '\n'.join(text)
            content = tokenizer.truncate(text, max_token=max_tokens, keep_both_sides=keep_both_sides)
        return Message(role=msg.role, content=content)

    def _truncate_turn(indexed_messages1: list, message_tokens1: dict, exceedance: int, is_last_turn: bool):
        # ******* rm this turn *******
        all_tokens = 0
        for msg_idx, msg in indexed_messages1:
            all_tokens += message_tokens1[msg_idx]
        logger.debug(f'exceedance start: {exceedance}, all tokens of this turn {all_tokens}')
        if all_tokens <= exceedance:
            # remove all turn
            return [], (exceedance - all_tokens)

        # ******* trunk this turn *******
        if len(indexed_messages1) == 1:
            assert is_last_turn
            # very long user
            idx, msg = indexed_messages1[0]
            msg = _truncate_message(msg=msg, max_tokens=message_tokens1[idx] - exceedance, keep_both_sides=True)
            return [msg], 0

        indexed_messages1 = copy.deepcopy(indexed_messages1)
        message_tokens1 = copy.deepcopy(message_tokens1)

        # split this turn by step
        messages_per_step = []  # [ [ (idx, msg), (idx, msg) ], [], ... ]
        for msg_idx, msg in indexed_messages1:
            if msg.role == USER:
                if messages_per_step and messages_per_step[-1][-1][1].role == USER:
                    messages_per_step[-1].append([msg_idx, msg])
                else:
                    messages_per_step.append([[msg_idx, msg]])
            elif msg.role == ASSISTANT:
                if messages_per_step and messages_per_step[-1][-1][1].role == ASSISTANT:
                    messages_per_step[-1].append([msg_idx, msg])
                else:
                    messages_per_step.append([[msg_idx, msg]])
            elif msg.role == FUNCTION:
                messages_per_step[-1].append([msg_idx, msg])

        last_step_idx = messages_per_step[-1][0][0]

        # step1: minimized function result
        logger.debug(f'exceedance step1 **minimized function result**: {exceedance}')
        for i, (msg_idx, msg) in enumerate(indexed_messages1):
            if exceedance <= 0 or msg.role != FUNCTION or (is_last_turn and msg_idx >= last_step_idx):
                continue

            fn_msg_tokens = message_tokens1[msg_idx]

            if fn_msg_tokens > exceedance:  # enough save room, can be truncated
                msg = _truncate_message(msg=msg, max_tokens=fn_msg_tokens - exceedance, keep_both_sides=True)
                indexed_messages1[i][1] = msg
                message_tokens1[msg_idx] = fn_msg_tokens - exceedance
                exceedance = 0  # force to set to 0 to avoid _truncate_message corner cases since we know there is no exceedance
                break
            else:
                msg.content = 'omit'
                message_tokens1[msg_idx] = 0
                exceedance -= fn_msg_tokens
        if exceedance <= 0:
            return [x[1] for x in indexed_messages1], 0

        # step2: rm middle step
        logger.debug(f'exceedance step2 **rm middle step**: {exceedance}')
        # keep_messages =
        keep_idx = 0
        for i, step in enumerate(messages_per_step):
            if i == 0 or i == (len(messages_per_step) - 1):
                continue
            step_tokens = sum([message_tokens1[x[0]] for x in step])
            if step_tokens >= exceedance:
                exceedance = 0
                keep_idx = messages_per_step[i + 1][0][0]
                break
            else:
                exceedance -= step_tokens
                keep_idx = messages_per_step[i + 1][0][0]

        if exceedance <= 0:
            res = [x[1] for x in messages_per_step[0]] + [x[1] for x in indexed_messages1 if x[0] >= keep_idx]
            return res, 0

        # step3: trunk FUNCTION of last step
        logger.debug(f'exceedance step3 **trunk FUNCTION of last step**: {exceedance}')
        messages_to_keep = []
        for msg_idx, msg in messages_per_step[-1]:
            if msg.role != FUNCTION:
                messages_to_keep.append([msg_idx, msg])
                continue

            fn_msg_tokens = message_tokens1[msg_idx]

            if fn_msg_tokens > exceedance:  # enough save room, can be truncated
                msg = _truncate_message(msg=msg, max_tokens=fn_msg_tokens - exceedance, keep_both_sides=True)
                exceedance = 0  # force to set to 0 to avoid _truncate_message corner cases since we know there is no exceedance
            else:
                msg.content = 'omit'
                message_tokens1[msg_idx] = 0
                exceedance -= fn_msg_tokens
            messages_to_keep.append([msg_idx, msg])

        messages_to_keep = messages_per_step[0] + messages_to_keep
        if exceedance <= 0:
            return [x[1] for x in messages_to_keep], 0

        # step4: trunk content of user/assistant
        logger.debug(f'exceedance step4 **trunk content of user/assistant**: {exceedance}')
        for i, (msg_idx, msg) in enumerate(messages_to_keep):
            fn_msg_tokens = message_tokens1[msg_idx]

            if fn_msg_tokens > exceedance:  # enough save room, can be truncated
                msg = _truncate_message(msg=msg, max_tokens=fn_msg_tokens - exceedance, keep_both_sides=True)
                messages_to_keep[i][1] = msg
                exceedance = 0  # force to set to 0 to avoid _truncate_message corner cases since we know there is no exceedance
                break
            else:
                msg.content = 'omit'
                exceedance -= fn_msg_tokens

        return [x[1] for x in messages_to_keep], 0

    available_token = max_tokens
    message_tokens = defaultdict(int)
    last_user_idx = None
    indexed_messages_per_user = defaultdict(list)  # user_msg_idx -> [(msg_idx, msg)...]
    new_messages = []
    for msg_idx, msg in enumerate(messages):
        if msg.role == SYSTEM:
            new_messages.append(msg)
            available_token = max_tokens - _count_tokens(msg=msg)
            continue
        message_tokens[msg_idx] = _count_tokens(msg=msg)
        if msg.role == USER:
            last_user_idx = msg_idx
        indexed_messages_per_user[last_user_idx].append([msg_idx, msg])

    all_tokens = sum([x for x in message_tokens.values()])
    # Log stats with agent name to avoid confusion in multi-agent logs
    logger.info(f'Agent [{agent_name}] - ALL tokens: {all_tokens}, Available tokens: {available_token}')
    
    # Feature 006: Invoke token count callback if registered (for compression tracking)
    if on_token_count_cb is not None and callable(on_token_count_cb):
        try:
            on_token_count_cb(all_tokens, available_token, max_tokens)
        except Exception as e:
            logger.debug(f"Token count callback failed for agent {agent_name}: {e}")
    
    if all_tokens <= available_token:
        return messages
    if available_token <= 0:
        raise ModelServiceError(
            code='400',
            message=f'The input system has exceed the maximum input context length ({max_tokens} tokens)',
        )

    exceedance = all_tokens - available_token  # make exceedance <= 0 -> ok
    for it, (user_msg_idx, indexed_messages) in enumerate(indexed_messages_per_user.items()):
        logger.debug(f'user_msg_idx: {user_msg_idx}, exceedance: {exceedance}')
        if exceedance <= 0:
            new_messages += [x[1] for x in indexed_messages]
            continue
        else:
            is_last_turn = (it == len(indexed_messages_per_user) - 1)
            new_turn, exceedance = _truncate_turn(indexed_messages1=indexed_messages,
                                                  message_tokens1=message_tokens,
                                                  exceedance=exceedance,
                                                  is_last_turn=is_last_turn)
            if new_turn:
                new_messages += new_turn

    return new_messages


def retry_model_service(
    fn,
    max_retries: int = 10,
) -> Any:
    """Retry a function"""

    num_retries, delay = 0, 1.0
    while True:
        try:
            return fn()

        except ModelServiceError as e:
            num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)


def retry_model_service_iterator(
    it_fn,
    max_retries: int = 10,
) -> Iterator:
    """Retry an iterator"""

    num_retries, delay = 0, 1.0
    while True:
        try:
            for rsp in it_fn():
                yield rsp
            break

        except ModelServiceError as e:
            num_retries, delay = _raise_or_delay(e, num_retries, delay, max_retries)


def _raise_or_delay(
    e: ModelServiceError,
    num_retries: int,
    delay: float,
    max_retries: int = 10,
    max_delay: float = 300.0,
    exponential_base: float = 2.0,
) -> Tuple[int, float]:
    """Retry with exponential backoff"""

    # Note: All raises use `from None` to break Python's implicit exception chaining.
    # Without this, when _raise_or_delay is called from within retry_model_service_iterator's
    # except block, a bare `raise e` would set e.__context__ = e (self-referencing chain)
    # because we're still inside the active handler context for that same exception.

    if max_retries <= 0:  # no retry
        raise e from None

    # Bad request, e.g., incorrect config or input
    if e.code == '400':
        raise e from None

    # If harmful input or output detected, let it fail
    if e.code == 'DataInspectionFailed':
        raise e from None
    if 'inappropriate content' in str(e):
        raise e from None

    # Retry is meaningless if the input is too long
    if 'maximum context length' in str(e):
        raise e from None

    logger.warning('ModelServiceError - ' + str(e).strip('\n'))

    if num_retries >= max_retries:
        raise ModelServiceError(exception=Exception(f'Maximum number of retries ({max_retries}) exceeded.')) from None

    num_retries += 1
    jitter = 1.0 + random.random()
    delay = min(delay * exponential_base, max_delay) * jitter
    time.sleep(delay)
    return num_retries, delay


def _rm_think(text: str) -> str:
    if '</think>' in text:
        return text.split('</think>')[-1].lstrip('\n')
    return text
