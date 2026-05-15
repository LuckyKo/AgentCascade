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

import math
import random
from typing import Dict, Optional, Union

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('calculate')
class Calculate(BaseTool):
    description = TOOL_METADATA['calculate']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'expression': {
                'description': TOOL_METADATA['calculate']['parameters']['expression'],
                'type': 'string',
            }
        },
        'required': ['expression'],
    }

    def __init__(self, cfg: Optional[Dict] = None):
        super().__init__(cfg)
        self.allowed_names = {
            k: v for k, v in math.__dict__.items() if not k.startswith("__")
        }
        self.allowed_names.update({
            'abs': abs,
            'round': round,
            'min': min,
            'max': max,
            'pow': pow,
            'ln': math.log,  # Alias ln to log
            'random': random.random,
            'randint': random.randint,
            'uniform': random.uniform,
        })

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)
        expression = params['expression']
        
        # Pre-processing for common mathematical notation
        # 1. Replace ^ with ** for python compatibility
        processed_expr = expression.replace('^', '**')
        
        try:
            # We use a restricted eval here for safety. 
            # Only math functions and basic built-ins are allowed.
            # __builtins__ is set to empty to disable access to dangerous functions like __import__.
            result = eval(processed_expr, {"__builtins__": {}}, self.allowed_names)
            
            # Format the result to be clean
            if isinstance(result, (int, float)):
                if isinstance(result, float) and result.is_integer():
                    return str(int(result))
                return f"{result:.10g}"
            return str(result)
            
        except Exception as e:
            return f"Error evaluating expression '{expression}': {str(e)}"
