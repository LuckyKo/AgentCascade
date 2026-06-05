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

import os
import traceback
from typing import Dict, Optional, Union

from agent_cascade.settings import DEFAULT_READ_FILE_MAX_LINES
from agent_cascade.tools.base import BaseToolWithFileAccess, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


@register_tool('python_compiler')
class PythonCompiler(BaseToolWithFileAccess):
    """Checks Python code for syntax errors without executing it. If the code parameter is a path to a .py file, reads and validates that file instead."""

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

    @staticmethod
    def _is_path_allowed(abs_path: str, allowed_prefixes: list) -> bool:
        """Check if a path is within an allowed directory using proper containment check.
        
        Uses os.path.commonpath() instead of .startswith() to prevent sibling-directory escape.
        E.g., /workspace_extra would pass .startswith('/workspace') but fails commonpath check.
        """
        for prefix in allowed_prefixes:
            try:
                common = os.path.commonpath([abs_path, prefix])
                if os.path.normpath(common).lower() == os.path.normpath(prefix).lower():
                    return True
            except ValueError:
                # Different drive letters on Windows (e.g., C:\ vs D:\)
                continue
        return False

    @staticmethod
    def _get_workspace():
        """Get workspace directory at runtime, matching start_api_server.py logic."""
        ws = os.environ.get('QWEN_AGENT_DEFAULT_WORKSPACE')
        if ws and os.path.isdir(ws):
            return os.path.realpath(ws)
        # Fallback: check for sibling AgentWorkspace (matching start_api_server.py)
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sibling_ws = os.path.join(project_root, 'AgentWorkspace')
        if os.path.isdir(sibling_ws):
            normalized = os.path.realpath(sibling_ws)
            os.environ['QWEN_AGENT_DEFAULT_WORKSPACE'] = normalized
            return normalized
        # Final fallback: use local workspace subdirectory (matching start_api_server.py)
        local_ws = os.path.join(project_root, 'workspace')
        normalized = os.path.realpath(local_ws)
        os.environ['QWEN_AGENT_DEFAULT_WORKSPACE'] = normalized
        return normalized

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self._verify_json_format_args(params)
            code = params.get('code', '')
        except Exception as e:
            return f"Invalid parameters: {str(e)}"

        if not code.strip():
            return "Error: Empty code provided."

        # Resolve workspace directory once at the top (matches start_api_server.py pattern)
        workspace_dir = self._get_workspace()

        # Check if code parameter is a path to a .py file
        stripped_code = code.strip()
        file_path = None
        
        if stripped_code.endswith('.py'):
            # Handle absolute paths correctly
            if os.path.isabs(stripped_code):
                potential_path = os.path.realpath(stripped_code)
            else:
                potential_path = os.path.realpath(os.path.join(workspace_dir, stripped_code))
            
            # Check if file exists
            if not os.path.isfile(potential_path):
                return f"Error: File not found: {stripped_code}"
            
            # Security: verify path is within allowed directories (uses commonpath, not startswith)
            allowed_prefixes = [workspace_dir]
            if not self._is_path_allowed(potential_path, allowed_prefixes):
                return f"Error: File '{stripped_code}' is outside the workspace directory."
            
            file_path = potential_path

        # If it's a valid file path, read the file contents with line limit
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = []
                    for _ in range(DEFAULT_READ_FILE_MAX_LINES + 1):
                        line = f.readline()
                        if not line:  # EOF reached — no more content
                            break
                        lines.append(line)

                # Check if we hit the limit
                if len(lines) > DEFAULT_READ_FILE_MAX_LINES:
                    return f"Error: File '{stripped_code}' exceeds {DEFAULT_READ_FILE_MAX_LINES} line limit."
                
                code = ''.join(lines)
            except Exception as e:
                return f"Error: Could not read file {file_path}: {str(e)}"

        try:
            # compile() checks for syntax errors without executing the code.
            # Mode 'exec' is used for a module/block of code.
            compile(code, '<string>', 'exec')
            return f'File valid: {os.path.relpath(file_path, workspace_dir)}' if file_path else 'Valid'
        except SyntaxError as e:
            if file_path:
                rel_path = os.path.relpath(file_path, workspace_dir)
                return f"Syntax Error in {rel_path}: {e.msg} at line {e.lineno}, offset {e.offset}\n{e.text.rstrip() if e.text else ''}"
            return f"Syntax Error: {e.msg} at line {e.lineno}, offset {e.offset}\n{e.text.rstrip() if e.text else ''}"
        except Exception as e:
            return f"Error: {str(e)}\n{traceback.format_exc()}"
