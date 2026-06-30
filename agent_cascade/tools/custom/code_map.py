import re
import os
import ast
from typing import List, Dict, Any, Optional
from pathlib import Path
from agent_cascade.tools.base import BaseTool
from agent_cascade.prompts.dna import TOOL_METADATA

# Attempt to import Pygments for better cross-language tokenization
try:
    from pygments import lexers, token
    HAS_PYGMENTS = True
except ImportError:
    HAS_PYGMENTS = False


class CodeMap(BaseTool):
    """Tool to quickly map large code files - with line numbers of functions, classes, and variables."""

    name = 'code_map'
    description = TOOL_METADATA['code_map']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['code_map']['parameters']['path']
            },
            'force_as': {
                'type': 'string',
                'description': TOOL_METADATA['code_map']['parameters']['force_as']
            }
        },
        'required': ['path'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        rel_path = params['path']
        force_as = params.get('force_as', '').lower()

        # Resolve absolute path with validation (same pattern as file_ops.py)
        if self.agent_pool and hasattr(self.agent_pool, 'operation_manager') and self.agent_pool.operation_manager:
            try:
                abs_path = self.agent_pool.operation_manager._resolve_path(rel_path, mode="ro")
            except ValueError as e:
                return f"Error: {str(e)}"
        else:
            # Fallback if no agent_pool (same pattern as read_file)
            from agent_cascade.settings import DEFAULT_WORKSPACE
            base_dir = Path(DEFAULT_WORKSPACE)
            if Path(rel_path).is_absolute():
                abs_path = Path(rel_path).resolve()
            else:
                abs_path = (base_dir / rel_path).resolve()
            if not str(abs_path).startswith(str(base_dir.resolve())):
                return f"Path '{rel_path}' is outside the allowed directory"

        if not abs_path.exists():
            return f"Error: File not found at {rel_path}"

        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            return f"Error reading file: {str(e)}"

        # Determine language
        ext = abs_path.suffix.lower().lstrip('.')
        lang = force_as or self._detect_lang(ext)

        if lang == 'python':
            return self._map_python(content)
        else:
            return self._map_generic(content, lang)

    def _detect_lang(self, ext: str) -> str:
        mapping = {
            'py': 'python',
            'js': 'javascript',
            'jsx': 'javascript',
            'ts': 'typescript',
            'tsx': 'typescript',
            'cpp': 'cpp',
            'hpp': 'cpp',
            'cc': 'cpp',
            'cxx': 'cpp',
            'c': 'c',
            'h': 'c',
            'java': 'java',
            'cs': 'csharp',
            'go': 'go',
            'rs': 'rust',
            'php': 'php',
            'rb': 'ruby',
            'sh': 'shell',
            'ps1': 'powershell',
            'sql': 'sql',
            'md': 'markdown',
            'html': 'html',
            'htm': 'html',
            'css': 'css',
            'scss': 'css',
            'less': 'css',
        }
        return mapping.get(ext, 'text')

    def _map_python(self, content: str) -> str:
        try:
            tree = ast.parse(content)
            result = ["# Python Code Map\n"]
            
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    result.append(f"L{node.lineno}: class {node.name}")
                    # Map methods inside class
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            result.append(f"  L{item.lineno}: def {item.name}")
                        elif isinstance(item, ast.AsyncFunctionDef):
                            result.append(f"  L{item.lineno}: async def {item.name}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Check if it's a top-level function (parent is Module)
                    # Note: ast.walk doesn't maintain hierarchy easily, 
                    # so we just check if it's in the top-level body of the module.
                    if any(node == top for top in tree.body):
                        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        result.append(f"L{node.lineno}: {prefix} {node.name}")

            if len(result) == 1:
                return "No classes or functions found in Python file."
            return "\n".join(result)
        except SyntaxError as e:
            return f"Syntax Error parsing Python file: {e.msg} (Line {e.lineno})"
        except Exception as e:
            return self._map_generic(content, 'python') # Fallback

    def _map_generic(self, content: str, lang: str) -> str:
        result = [f"# {lang.capitalize()} Code Map (Heuristic)\n"]
        lines = content.splitlines()

        # Regex patterns for common languages
        patterns = {
            'javascript': [
                (r'^\s*class\s+(\w+)', 'class'),
                (r'^\s*(?:async\s+)?function\s+(\w+)', 'func'),
                (r'^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[\w$]+)\s*=>', 'arrow'),
            ],
            'typescript': [
                (r'^\s*(?:export\s+)?class\s+(\w+)', 'class'),
                (r'^\s*(?:export\s+)?interface\s+(\w+)', 'interface'),
                (r'^\s*(?:export\s+)?type\s+(\w+)', 'type'),
                (r'^\s*(?:async\s+)?function\s+(\w+)', 'func'),
            ],
            'java': [
                (r'^\s*(?:public|protected|private)?\s*(?:static\s+)?(?:class|interface|enum)\s+(\w+)', 'class'),
                (r'^\s*(?:public|protected|private)?\s*(?:static\s+)?[\w<>[\]]+\s+(\w+)\s*\(', 'method'),
            ],
            'cpp': [
                (r'^\s*(?:class|struct|enum|namespace)\s+(\w+)', 'class'),
                (r'^\s*(?:[\w:*&<>]+)\s+(\w+)\s*\([^)]*\)\s*(?:const|override|final)?\s*(?:[:{]|$)', 'func'),
            ],
            'csharp': [
                (r'^\s*(?:public|protected|private|internal|partial)?\s*(?:static|abstract|sealed|partial)?\s*(?:class|interface|struct|enum|record)\s+(\w+)', 'class'),
                (r'^\s*(?:public|protected|private|internal)?\s*(?:static|virtual|override|async|abstract|extern)?\s*(?:[\w<>[\]]+|void)\s+(\w+)\s*\(', 'method'),
            ],
            'go': [
                (r'^\s*type\s+(\w+)\s+(?:struct|interface)', 'type'),
                (r'^\s*func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(', 'func'),
            ],
            'rust': [
                (r'^\s*(?:pub(?:\([^)]+\))?\s+)?(?:struct|enum|trait|type)\s+(\w+)', 'type'),
                (r'^\s*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+(\w+)', 'func'),
                (r'^\s*impl(?:\s+[\w:<>, ]+)?\s+for\s+(\w+)', 'impl'),
            ],
            'html': [
                (r'<\w+[^>]*\bid=["\']([^"\']+)["\']', 'id'),
                (r'<\w+[^>]*\bclass=["\']([^"\']+)["\']', 'class'),
                (r'<h([1-6])[^>]*>(.*?)<\/h\1>', 'heading'),
            ],
            'css': [
                (r'^@media\s+([^\{]+)', 'media'),
                (r'^([.#\w][\w\.-]+)\s*\{?', 'rule'),
            ]
        }

        active_patterns = patterns.get(lang, patterns.get('javascript')) # Use JS as fallback for many C-like

        # Simple line-by-line regex matching
        # Note: This is naive and will match inside strings/comments unless we use Pygments
        for i, line in enumerate(lines, 1):
            for pattern, p_type in active_patterns:
                match = re.search(pattern, line)
                if match:
                    name = match.group(1)
                    if p_type == 'class':
                        result.append(f"L{i}: class {name}")
                    elif p_type == 'interface':
                        result.append(f"L{i}: interface {name}")
                    elif p_type in ('func', 'method', 'arrow'):
                        result.append(f"L{i}: function {name}")
                    elif p_type == 'type':
                        result.append(f"L{i}: type {name}")
                    elif p_type == 'impl':
                        result.append(f"L{i}: impl for {name}")
                    elif p_type == 'id':
                        result.append(f"L{i}: element with id: {name}")
                    elif p_type == 'heading':
                        # Use group(1) for level and group(2) for text
                        h_text = match.group(2).strip()
                        result.append(f"L{i}: <h{name}> {h_text}")
                    elif p_type == 'media':
                        result.append(f"L{i}: @media {name}")
                    elif p_type == 'rule':
                        result.append(f"L{i}: css rule: {name}")

        if len(result) == 1:
            return f"No recognizable structures found for {lang}."
        return "\n".join(result)
