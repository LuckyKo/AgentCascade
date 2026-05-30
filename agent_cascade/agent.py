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
import random
import traceback
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from agent_cascade.llm import get_chat_model
from agent_cascade.llm.base import BaseChatModel
from agent_cascade.llm.schema import CONTENT, DEFAULT_SYSTEM_MESSAGE, ROLE, SYSTEM, ContentItem, Message
from agent_cascade.log import logger
from agent_cascade.tools import TOOL_REGISTRY, BaseTool, MCPManager
from agent_cascade.tools.base import ToolServiceError
from agent_cascade.tools.simple_doc_parser import DocParserError
from agent_cascade.utils.utils import has_chinese_messages, merge_generate_cfgs
from agent_cascade.utils.thinking_block import strip_thinking_blocks


class Agent(ABC):
    """A base class for Agent.

    An agent can receive messages and provide response by LLM or Tools.
    Different agents have distinct workflows for processing messages and generating responses in the `_run` method.
    """

    def __init__(self,
                 function_list: Optional[List[Union[str, Dict, BaseTool]]] = None,
                 llm: Optional[Union[dict, BaseChatModel]] = None,
                 system_message: Optional[str] = DEFAULT_SYSTEM_MESSAGE,
                 name: Optional[str] = None,
                 description: Optional[str] = None,
                 **kwargs):
        """Initialization the agent.

        Args:
            function_list: One list of tool name, tool configuration or Tool object,
              such as 'code_interpreter', {'name': 'code_interpreter', 'timeout': 10}, or CodeInterpreter().
            llm: The LLM model configuration or LLM model object.
              Set the configuration as {'model': '', 'api_key': '', 'model_server': ''}.
            system_message: The specified system message for LLM chat.
            name: The name of this agent.
            description: The description of this agent, which will be used for multi_agent.
        """
        if isinstance(llm, dict):
            self.llm = get_chat_model(llm)
        else:
            self.llm = llm
        self.extra_generate_cfg: dict = {}

        self.function_map = {}
        if function_list:
            for tool in function_list:
                self._init_tool(tool)

        self.system_message = system_message
        self.name = name
        self.description = description
        # Per-agent arg reuse cache: maps arg_name -> last known value.
        # Shared across all tool calls for this agent instance (legacy path).
        self._prev_tool_args: Dict[str, Any] = {}

    def run_nonstream(self, messages: List[Union[Dict, Message]], **kwargs) -> Union[List[Message], List[Dict]]:
        """Same as self.run, but with stream=False,
        meaning it returns the complete response directly
        instead of streaming the response incrementally."""
        *_, last_responses = self.run(messages, **kwargs)
        return last_responses

    def run(self, messages: List[Union[Dict, Message]],
            **kwargs) -> Union[Iterator[List[Message]], Iterator[List[Dict]]]:
        """Return one response generator based on the received messages.

        This method performs a uniform type conversion for the inputted messages,
        and calls the _run method to generate a reply.

        Args:
            messages: A list of messages.

        Yields:
            The response generator.
        """
        messages = copy.deepcopy(messages)
        _return_message_type = 'dict'
        new_messages = []
        # Only return dict when all input messages are dict
        if not messages:
            _return_message_type = 'message'
        for msg in messages:
            if isinstance(msg, dict):
                new_messages.append(Message(**msg))
            else:
                new_messages.append(msg)
                _return_message_type = 'message'

        if 'lang' not in kwargs:
            if has_chinese_messages(new_messages):
                kwargs['lang'] = 'zh'
            else:
                kwargs['lang'] = 'en'

        # Stabilize seed for the entire run to prevent context reprocessing across tool turns
        if kwargs.get('seed') is None:
            kwargs['seed'] = random.randint(0, 2**30)

        if self.system_message:
            if not new_messages or new_messages[0][ROLE] != SYSTEM:
                # Add the system instruction to the agent
                new_messages.insert(0, Message(role=SYSTEM, content=self.system_message))
            else:
                # Already got system message in new_messages
                if isinstance(new_messages[0][CONTENT], str):
                    new_messages[0][CONTENT] = self.system_message + '\n\n' + new_messages[0][CONTENT]
                else:
                    assert isinstance(new_messages[0][CONTENT], list)
                    assert new_messages[0][CONTENT][0].text
                    new_messages[0][CONTENT] = [ContentItem(text=self.system_message + '\n\n')
                                               ] + new_messages[0][CONTENT]  # noqa

        for rsp in self._run(messages=new_messages, **kwargs):
            # Handle both Message objects and dicts (type depends on input message types)
            for i in range(len(rsp)):
                if isinstance(rsp[i], dict):
                    if not rsp[i].get('name') and self.name:
                        rsp[i]['name'] = self.name
                elif hasattr(rsp[i], 'name'):  # Defensive guard against unexpected message types
                    if not rsp[i].name and self.name:
                        rsp[i].name = self.name
            if _return_message_type == 'message':
                yield [Message(**x) if isinstance(x, dict) else x for x in rsp]
            else:
                yield [x.model_dump() if not isinstance(x, dict) else x for x in rsp]

    @abstractmethod
    def _run(self, messages: List[Message], lang: str = 'en', **kwargs) -> Iterator[List[Message]]:
        """Return one response generator based on the received messages.

        The workflow for an agent to generate a reply.
        Each agent subclass needs to implement this method.

        Args:
            messages: A list of messages.
            lang: Language, which will be used to select the language of the prompt
              during the agent's execution process.

        Yields:
            The response generator.
        """
        raise NotImplementedError

    def _call_llm(
        self,
        messages: List[Message],
        functions: Optional[List[Dict]] = None,
        stream: bool = True,
        extra_generate_cfg: Optional[dict] = None,
    ) -> Iterator[List[Message]]:
        """The interface of calling LLM for the agent.

        We prepend the system_message of this agent to the messages, and call LLM.

        Args:
            messages: A list of messages.
            functions: The list of functions provided to LLM.
            stream: LLM streaming output or non-streaming output.
              For consistency, we default to using streaming output across all agents.

        Yields:
            The response generator of LLM.
        """
        return self.llm.chat(messages=messages,
                             functions=functions,
                             stream=stream,
                             extra_generate_cfg=merge_generate_cfgs(
                                 base_generate_cfg={**self.extra_generate_cfg, 'agent_name': self.name},
                                 new_generate_cfg=extra_generate_cfg,
                             ))

    def _get_active_functions(self) -> list:
        """Return function schemas for tools not disabled by the current config.
        
        This is the single source of truth for tool filtering. All agent
        subclasses should call this instead of manually reading disabled_tools.
        """
        disabled_map = getattr(self.llm, 'generate_cfg', {}).get('disabled_tools', {})
        
        # Check both display name and slugified name for robustness
        disabled = set(disabled_map.get(self.name, []))
        if self.name:
            slug = self.name.lower().replace(' ', '_')
            if slug in disabled_map:
                disabled.update(disabled_map[slug])
        
        # Also check agent_type if available
        agent_type = getattr(self, 'agent_type', None)
        if agent_type and agent_type in disabled_map:
            disabled.update(disabled_map[agent_type])
            
        return [func.function for name, func in self.function_map.items() if name not in disabled]

    def _get_disabled_tool_names(self) -> set:
        """Return the set of currently disabled tool names for this agent."""
        disabled_map = getattr(self.llm, 'generate_cfg', {}).get('disabled_tools', {})
        
        # Check both display name and slugified name for robustness
        disabled = set(disabled_map.get(self.name, []))
        if self.name:
            slug = self.name.lower().replace(' ', '_')
            if slug in disabled_map:
                disabled.update(disabled_map[slug])

        # Also check agent_type if available
        agent_type = getattr(self, 'agent_type', None)
        if agent_type and agent_type in disabled_map:
            disabled.update(disabled_map[agent_type])
            
        return disabled

    def _resolve_tool_args(self, tool_args: Union[str, dict]) -> Any:
        """Resolve __USE_PREV_ARG__ placeholders for legacy agent path.

        Parses JSON strings, replaces placeholder values with cached previous
        arg values by name (global cache — not scoped per tool), and updates
        the cache with the resolved args. Unresolvable placeholders pass
        through silently. Malformed JSON is passed through unchanged so the
        tool fails with a clear error.

        Args:
            tool_args: Raw arguments (JSON string or dict).

        Returns:
            Resolved argument dict, or the original value if parsing failed.
        """
        # Parse JSON string if needed
        if isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except json.JSONDecodeError:
                return tool_args  # Pass through so tool fails with clear error
            if not isinstance(parsed, dict):
                return parsed
        elif isinstance(tool_args, dict):
            parsed = tool_args
        else:
            return tool_args

        # Scan for placeholders and resolve from cache
        resolved = copy.deepcopy(parsed)
        for key, val in resolved.items():
            if isinstance(val, str) and val.strip() == "__USE_PREV_ARG__":
                cached_val = self._prev_tool_args.get(key)
                if cached_val is not None:
                    resolved[key] = copy.deepcopy(cached_val)
                # else: leave placeholder as-is — tool will receive it

        # Update cache with all resolved arg values (most recent wins)
        for key, val in resolved.items():
            self._prev_tool_args[key] = copy.deepcopy(val)

        return resolved

    def _call_tool(self, tool_name: str, tool_args: Union[str, dict] = '{}', **kwargs) -> Union[str, List[ContentItem]]:
        """The interface of calling tools for the agent.

        Args:
            tool_name: The name of one tool.
            tool_args: Model generated or user given tool parameters.

        Returns:
            The output of tools.
        """
        if tool_name not in self.function_map:
            return f'Tool {tool_name} does not exists.'

        # Resolve __USE_PREV_ARG__ placeholders (legacy agent path)
        resolved_args = self._resolve_tool_args(tool_args)

        tool = self.function_map[tool_name]
        try:
            # Pass the agent itself as agent_obj so tools (like compress_context) 
            # can sync back to its base system_message for persistence across turns.
            if 'agent_obj' not in kwargs:
                kwargs['agent_obj'] = self
            tool_result = tool.call(resolved_args, **kwargs)
        except (ToolServiceError, DocParserError) as ex:
            error_message = str(ex)
            logger.warning(f'Tool `{tool_name}` reported a service error:\n{error_message}')
            return error_message
        except Exception as ex:
            # Special Case: Allow LoopDetectedError to propagate to the orchestrator/API server
            # for surgical rollback and retry logic to trigger.
            if type(ex).__name__ == 'LoopDetectedError':
                raise ex
                
            exception_type = type(ex).__name__
            exception_message = str(ex)
            traceback_info = ''.join(traceback.format_tb(ex.__traceback__))
            error_message = f'An error occurred when calling tool `{tool_name}`:\n' \
                            f'{exception_type}: {exception_message}\n' \
                            f'Traceback:\n{traceback_info}'
            logger.warning(error_message)
            return error_message

        if isinstance(tool_result, str):
            return tool_result
        elif isinstance(tool_result, list) and all(isinstance(item, ContentItem) for item in tool_result):
            return tool_result  # multimodal tool results
        else:
            return json.dumps(tool_result, ensure_ascii=False, indent=4)

    def _init_tool(self, tool: Union[str, Dict, BaseTool]):
        if isinstance(tool, BaseTool):
            tool_name = tool.name
            if tool_name in self.function_map:
                logger.warning(f'Repeatedly adding tool {tool_name}, will use the newest tool in function list')
            self.function_map[tool_name] = tool
        elif isinstance(tool, dict) and 'mcpServers' in tool:
            tools = MCPManager().initConfig(tool)
            for tool in tools:
                tool_name = tool.name
                if tool_name in self.function_map:
                    logger.warning(f'Repeatedly adding tool {tool_name}, will use the newest tool in function list')
                self.function_map[tool_name] = tool
        else:
            if isinstance(tool, dict):
                tool_name = tool['name']
                tool_cfg = tool
            else:
                tool_name = tool
                tool_cfg = None
            if tool_name not in TOOL_REGISTRY:
                raise ValueError(f'Tool {tool_name} is not registered.')

            if tool_name in self.function_map:
                logger.warning(f'Repeatedly adding tool {tool_name}, will use the newest tool in function list')
            self.function_map[tool_name] = TOOL_REGISTRY[tool_name](tool_cfg)

    def _detect_tool(self, message: Message) -> Tuple[bool, str, str, str]:
        """A built-in tool call detection for func_call format message.

        Args:
            message: one message generated by LLM (can be Message or dict).

        Returns:
            Need to call tool or not, tool name, tool args, text replies.
        """
        func_name = None
        func_args = None

        # Handle both Message objects and dicts
        if isinstance(message, dict):
            func_call = message.get('function_call')
            text = message.get('content', '') or ''
        else:
            func_call = getattr(message, 'function_call', None)
            text = getattr(message, 'content', '') or ''

        if func_call:
            if isinstance(func_call, dict):
                func_name = func_call.get('name')
                func_args = func_call.get('arguments')
            else:
                func_name = func_call.name
                func_args = func_call.arguments

        return (func_name is not None), func_name, func_args, text


# The most basic form of an agent is just a LLM, not augmented with any tool or workflow.
class BasicAgent(Agent):

    def _run(self, messages: List[Message], lang: str = 'en', **kwargs) -> Iterator[List[Message]]:
        extra_generate_cfg = {'lang': lang}
        if kwargs.get('seed') is not None:
            extra_generate_cfg['seed'] = kwargs['seed']
        return self._call_llm(messages, extra_generate_cfg=extra_generate_cfg)
