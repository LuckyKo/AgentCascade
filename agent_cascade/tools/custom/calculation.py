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

import ast
import math
import random
from typing import Dict, Optional, Union

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.base import BaseTool, register_tool

# BUG_0180: the evaluator is a single-expression ``eval``, so a statement block is a compile
# error, not a runtime one. The old handler forwarded the raw SyntaxError and quoted the whole
# expression back — expensive to re-read and silent about the accepted grammar. Echoed input is
# now capped, and statement blocks get a message that names the construct and shows the rewrite.
_MAX_ECHOED_EXPRESSION = 160


def _abbreviate(text: str, limit: int = _MAX_ECHOED_EXPRESSION) -> str:
    """Shorten an expression for an error message, preserving the head."""
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _is_statement_block(expr: str) -> bool:
    """True when ``expr`` parses as statements but not as a single expression.

    ``D = 1; gu = D``, a newline-separated block, ``import os`` and ``for ...: ...`` are all
    valid ``exec``-mode Python, which is exactly what ``eval`` cannot take. Genuinely malformed
    input such as ``1 +`` fails in both modes and keeps the plain syntax error.
    """
    try:
        ast.parse(expr, mode='exec')
    except (SyntaxError, ValueError):
        return False
    try:
        ast.parse(expr, mode='eval')
        return False  # parses as a single expression -> not a statement block
    except (SyntaxError, ValueError):
        return True  # exec-only -> statement block


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
            k: v for k, v in math.__dict__.items() if not k.startswith('__')
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
            # Type coercions
            'int': int,
            'float': float,
            'bool': bool,
            # Sequence helpers
            'len': len,
            'sum': sum,
            # Integer division + remainder in one call
            'divmod': divmod,
            # Explicit truncation (complements floor/ceil) + bare-name aliases
            'trunc': math.trunc,
            'floor': math.floor,
            'ceil': math.ceil,
        })

    def call(self, params: Union[str, dict], **kwargs) -> str:
        params = self._verify_json_format_args(params)
        expression = params['expression']

        # Pre-processing for common mathematical notation
        # 1. Replace ^ with ** for python compatibility
        processed_expr = expression.replace('^', '**')

        try:
            # Restricted eval: only allowlisted math funcs + safe built-ins; __builtins__={} blocks
            # dangerous names like __import__. Attribute access (e.g. (1).__class__) can introspect
            # type objects but is NOT exploitable — no side-effect object is in scope (see tests).
            #
            # BUG_0177: the allowlist must live in the GLOBALS mapping. With it in ``locals``, a
            # name used inside a function body (lambda / comprehension) compiles to LOAD_GLOBAL,
            # which never consults locals — so ``sum([1,2,3])`` worked while
            # ``(lambda: sum([1,2]))()`` raised "name 'sum' is not defined". The same names are
            # exported, so the sandbox surface is unchanged: __builtins__ stays empty and no name
            # outside the allowlist becomes reachable, nested scopes included.
            scope = {'__builtins__': {}}
            scope.update(self.allowed_names)
            result = eval(processed_expr, scope)

            # Format the result to be clean
            if isinstance(result, (int, float)):
                if isinstance(result, float) and result.is_integer():
                    return str(int(result))
                return f"{result:.10g}"
            return str(result)

        except Exception as e:
            # BUG_0180: a statement block (e.g. "D=1; gu=D") is a compile-time SyntaxError, not a
            # runtime one. Detect that case and tell the agent what the tool accepts, showing the
            # lambda rewrite; cap the echoed input so long expressions aren't re-read in full.
            if isinstance(e, SyntaxError) and _is_statement_block(processed_expr):
                return (
                    f"Error: '{_abbreviate(expression)}' is a statement block, but `calculate` "
                    'evaluates a single expression only (no `;`-separated or newline-separated '
                    'statements). Rewrite it as one expression: inline the sub-expressions, or '
                    'wrap them in a lambda, e.g. (lambda D: [D, D * 2])(521.0).'
                )
            return f"Error evaluating expression '{_abbreviate(expression)}': {str(e)}"
