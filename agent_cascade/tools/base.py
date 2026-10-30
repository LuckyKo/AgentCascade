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
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union

from agent_cascade.llm.schema import ContentItem
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.utils.utils import (has_chinese_chars, is_http_url, json_loads, logger, print_traceback,
                                       sanitize_chrome_file_path, save_url_to_local_work_dir)

TOOL_REGISTRY = {}


def _comparable_path(path: str) -> str:
    """Absolute, case-folded, symlink-resolved form of ``path`` for containment tests."""
    try:
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def _is_inside_work_dir(path: str, work_dir: str) -> bool:
    """True when a local file already lives under ``work_dir`` (BUG_0179).

    ``BaseToolWithFileAccess.call`` stages every file/image referenced anywhere in the
    conversation into ``work_dir`` so the tool can read it. When the file is *already*
    under ``work_dir`` the staging is a no-op at best; at worst it is destructive:
    ``save_url_to_local_work_dir`` deletes an existing destination before copying, so a
    same-named file in the workspace root was silently replaced by the staged copy (and
    every staged file got a fresh mtime).

    The visible case: ``CodeInterpreter``'s work_dir is the workspace root, so each
    ``code_interpreter`` call copied *every* image from earlier ``media_gen`` results
    (``<workspace>/logs/media/images/img_*.jpg``) into ``<workspace>/`` — files that
    never move back out, because nothing cleans the workspace root.

    Remote references (http/https/data:) have no local path to compare and are always
    staged, exactly as before.
    """
    path = path if isinstance(path, str) else str(path)
    if is_http_url(path) or path.lower().startswith('data:'):
        return False
    try:
        local = sanitize_chrome_file_path(path)
        if not os.path.isfile(local):
            return False
        root = _comparable_path(work_dir)
        target = _comparable_path(local)
        return os.path.commonpath([root, target]) == root
    except (OSError, ValueError):
        # ValueError: different drives on Windows (no common path); OSError: unreadable
        # path. Neither is provably inside, so fall back to the old staging behaviour.
        return False


class ToolServiceError(Exception):

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


def register_tool(name, allow_overwrite=False):

    def decorator(cls):
        if name in TOOL_REGISTRY:
            if allow_overwrite:
                logger.warning(f'Tool `{name}` already exists! Overwriting with class {cls}.')
            else:
                raise ValueError(f'Tool `{name}` already exists! Please ensure that the tool name is unique.')
        if cls.name and (cls.name != name):
            raise ValueError(f'{cls.__name__}.name="{cls.name}" conflicts with @register_tool(name="{name}").')
        cls.name = name
        TOOL_REGISTRY[name] = cls

        return cls

    return decorator


def is_tool_schema(obj: dict) -> bool:
    """
    Check if obj is a valid JSON schema describing a tool compatible with OpenAI's tool calling.
    Example valid schema:
    {
      "name": "get_current_weather",
      "description": "Get the current weather in a given location",
      "parameters": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "The city and state, e.g. San Francisco, CA"
          },
          "unit": {
            "type": "string",
            "enum": ["celsius", "fahrenheit"]
          }
        },
        "required": ["location"]
      }
    }
    """
    import jsonschema
    try:
        assert set(obj.keys()) == {'name', 'description', 'parameters'}
        assert isinstance(obj['name'], str)
        assert obj['name'].strip()
        assert isinstance(obj['description'], str)
        assert isinstance(obj['parameters'], dict)

        assert set(obj['parameters'].keys()) == {'type', 'properties', 'required'}
        assert obj['parameters']['type'] == 'object'
        assert isinstance(obj['parameters']['properties'], dict)
        assert isinstance(obj['parameters']['required'], list)
        assert set(obj['parameters']['required']).issubset(set(obj['parameters']['properties'].keys()))
    except AssertionError:
        return False
    try:
        jsonschema.validate(instance={}, schema=obj['parameters'])
    except jsonschema.exceptions.SchemaError:
        return False
    except jsonschema.exceptions.ValidationError:
        pass
    return True


class BaseTool(ABC):
    name: str = ''
    description: str = ''
    parameters: Union[List[dict], dict] = []

    def __init__(self, cfg: Optional[dict] = None):
        self.cfg = cfg or {}
        if not self.name:
            raise ValueError(
                f'You must set {self.__class__.__name__}.name, either by @register_tool(name=...) or explicitly setting {self.__class__.__name__}.name'
            )
        # Skip strict validation - allow flexible parameter definitions
        # Validation still happens at tool call time via jsonschema

    @abstractmethod
    def call(self, params: Union[str, dict], **kwargs) -> Union[str, list, dict, List[ContentItem]]:
        """The interface for calling tools.

        Each tool needs to implement this function, which is the workflow of the tool.

        Args:
            params: The parameters of func_call.
            kwargs: Additional parameters for calling tools.

        Returns:
            The result returned by the tool, implemented in the subclass.
        """
        raise NotImplementedError

    def dms_bound(self, tool_args, instance) -> Optional[float]:
        """This tool's effective holder-DMS bound in seconds, or None if it has none.

        Called by the single dispatch boundary (ToolDispatcher.execute_tool) BEFORE a
        synchronous tool call, to decide how long the holder-side dead-man's-switch should
        shield this holder before treating it as a genuine wedge (plan t292).

        Tools with a real, config-driven finite timeout override this to return that bound
        (e.g. shell_cmd -> MAX_SHELL_TIMEOUT, code_interpreter -> its wall-clock budget +
        IOPub silence tail). Tools with no finite bound return None; the boundary then falls
        back to the settings cap (holder_dms_max_bound_s) so an unbounded marker can never
        shield a holder forever.

        Args:
            tool_args: The resolved tool arguments (dict, or the raw string the LLM sent).
            instance: The calling AgentInstance (may be None in unusual paths).

        Returns:
            Bound in seconds, or None to defer to the settings cap.
        """
        return None

    def _get_agent_name(self, kwargs: dict) -> str:
        """Resolve runtime agent instance name from tool call kwargs or instance attribute."""
        return (kwargs.get('agent_instance_name') or kwargs.get('agent_name') or getattr(self, 'agent_name', None) or
                'unknown')

    def _verify_json_format_args(self, params: Union[str, dict], strict_json: bool = False) -> dict:
        """Verify the parameters of the function call.

        Parses JSON arguments and validates against the tool's parameter schema.
        Thinking block tags are handled by json_loads at parse time (pre-parse strip),
        so no per-value sanitization is needed — tags never legitimately appear inside
        parsed JSON values per investigation.
        """
        if isinstance(params, str):
            try:
                if strict_json:
                    params_json = json.loads(params)
                else:
                    params_json = json_loads(params)
                # Verify result is a dict (json_loads raises ValueError on parse failure, but could return non-dict JSON)
                if not isinstance(params_json, dict):
                    raise ValueError('Parameters must be formatted as a valid JSON! Got non-dict result')
            except (json.decoder.JSONDecodeError, ValueError):
                raise ValueError('Parameters must be formatted as a valid JSON!')
        else:
            params_json: dict = params

        if isinstance(self.parameters, list):
            for param in self.parameters:
                if 'required' in param and param['required']:
                    if param['name'] not in params_json:
                        raise ValueError('Parameters %s is required!' % param['name'])
        elif isinstance(self.parameters, dict):
            import jsonschema
            try:
                jsonschema.validate(instance=params_json, schema=self.parameters)
            except jsonschema.ValidationError as e:
                # BUG_0024 (and BUG_0022's sibling class): schemas without
                # additionalProperties:false silently accept unknown keys and the tool then
                # runs with defaults — a silent wrong-behaviour. Surface the offending key
                # names in the message so the caller can self-correct on retry.
                if e.validator == 'additionalProperties':
                    bad = [p for p in params_json if p not in self.parameters.get('properties', {})]
                    valid = sorted(self.parameters.get('properties', {}))
                    raise ValueError(
                        f"unknown parameter(s) for tool '{self.name}': {sorted(bad)}. "
                        f"Valid parameters: {valid}"
                    ) from e
                # BUG_0022/BUG_0024: required-missing (e.g. an empty {} after a failed JSON
                # parse, or a dropped 'path'/'command') used to escape as a raw
                # jsonschema.ValidationError crash line. Convert it to the same friendly
                # ValueError shape so tools can return it as a normal tool result.
                if e.validator == 'required':
                    missing = [p for p in self.parameters.get('required', []) if p not in params_json]
                    # BUG_0077: make the error self-diagnosing. A model-side tool call
                    # that omits or renames a required key used to fail with only the
                    # missing key, which forced the model to guess ("param ordering").
                    # Echo the keys that WERE received, the schema's own required list,
                    # and the full property list (so optional params like 'justification'
                    # are visible) — all read from the tool's own schema, generically.
                    received = list(params_json.keys())
                    required = list(self.parameters.get('required', []))
                    known = list(self.parameters.get('properties', {}))
                    msg = (
                        f"missing required parameter(s) for tool '{self.name}': {sorted(missing)}"
                        f"  | received keys: {received}"
                        f"  | required: {required}"
                        f"  | known parameters: {known}"
                    )
                    if len(missing) == 1:
                        msg += (f"  | If you meant to send the value under another name, "
                                f"re-issue with the exact key '{missing[0]}'.")
                    elif missing:
                        msg += (f"  | If you meant to send the value under another name, "
                                f"re-issue with the exact keys {sorted(missing)}.")
                    raise ValueError(msg) from e
                raise
        else:
            raise ValueError(f"Invalid parameters schema for tool '{self.name}': must be list or dict")
        return params_json

    @property
    def function(self) -> dict:  # Bad naming. It should be `function_info`.
        return {
            # 'name_for_human': self.name_for_human,
            'name': self.name,
            'description': self.description,
            'parameters': self.parameters,
            # 'args_format': self.args_format
        }

    @property
    def name_for_human(self) -> str:
        return self.cfg.get('name_for_human', self.name)

    @property
    def args_format(self) -> str:
        fmt = self.cfg.get('args_format')
        if fmt is None:
            if has_chinese_chars([self.name_for_human, self.name, self.description, self.parameters]):
                fmt = '此工具的输入应为JSON对象。'
            else:
                fmt = 'Format the arguments as a JSON object.'
        return fmt

    @property
    def file_access(self) -> bool:
        return False


class BaseToolWithFileAccess(BaseTool, ABC):

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        assert self.name
        default_work_dir = os.path.join(DEFAULT_WORKSPACE, 'tools', self.name)
        self.work_dir: str = self.cfg.get('work_dir', default_work_dir)

    @property
    def file_access(self) -> bool:
        return True

    def call(self, params: Union[str, dict], files: List[str] = None, **kwargs) -> str:
        # Copy remote files to the working directory:
        if files:
            os.makedirs(self.work_dir, exist_ok=True)
            for file in files:
                try:
                    # Already readable at its own path — copying it to work_dir only
                    # duplicates bytes (and can clobber a same-named file there). BUG_0179.
                    if _is_inside_work_dir(file, self.work_dir):
                        continue
                    save_url_to_local_work_dir(file, self.work_dir)
                except Exception:
                    print_traceback()

        # Then do something with the files:
        # ...
