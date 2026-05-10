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

import traceback
from typing import Dict, Optional, Union

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('python_compiler')
class PythonCompiler(BaseTool):
    """Checks Python code for syntax errors without executing it."""

    name = 'python_compiler'
    description = TOOL_METADATA['python_compiler']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'code': {
                'type': 'string',
                'description': TOOL_METADATA['python_compiler']['parameters']['code']
            }
        },
        'required': ['code'],
    }

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self._verify_json_format_args(params)
            code = params.get('code', '')
        except Exception as e:
            return f"Invalid parameters: {str(e)}"

        if not code.strip():
            return "Error: Empty code provided."

        try:
            # compile() checks for syntax errors without executing the code.
            # Mode 'exec' is used for a module/block of code.
            compile(code, '<string>', 'exec')
            return 'Valid'
        except SyntaxError as e:
            return f"Syntax Error: {e.msg} at line {e.lineno}, offset {e.offset}\n{e.text if e.text else ''}"
        except Exception as e:
            return f"Error: {str(e)}\n{traceback.format_exc()}"
