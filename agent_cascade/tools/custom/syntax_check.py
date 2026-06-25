import ast
import json
import os
import re
from pathlib import Path
from typing import Dict, Optional, Union

from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.prompts.dna import TOOL_METADATA


# ── Extension → language mapping ───────────────────────────────────────────────
_EXT_LANG_MAP: Dict[str, str] = {
    # Python
    '.py': 'python', '.pyw': 'python', '.pyi': 'python',
    # JavaScript / TypeScript
    '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript', '.tsx': 'typescript',
    # Web
    '.html': 'html', '.htm': 'html',
    '.css': 'css', '.scss': 'css', '.less': 'css',
    # Data / Config
    '.json': 'json',
    '.yaml': 'yaml', '.yml': 'yaml',
    '.toml': 'toml',
    '.xml': 'xml', '.svg': 'xml', '.xsl': 'xml', '.xslt': 'xml',
    # C-family
    '.c': 'c', '.h': 'c',
    '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp', '.hxx': 'cpp',
    '.cs': 'csharp',
    '.java': 'java',
    # Other
    '.go': 'go',
    '.rs': 'rust',
    '.rb': 'ruby',
    '.php': 'php',
    '.lua': 'lua',
    '.sh': 'bash', '.bash': 'bash',
    '.ps1': 'powershell',
    '.bat': 'batch', '.cmd': 'batch',
    '.sql': 'sql',
    '.r': 'r', '.R': 'r',
    '.kt': 'kotlin', '.kts': 'kotlin',
    '.swift': 'swift',
    '.pl': 'perl', '.pm': 'perl',
    '.scala': 'scala',
}


def _detect_language(file_path: str) -> str:
    """Detect language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_LANG_MAP.get(ext, '')


# ── Individual language checkers ───────────────────────────────────────────────

def _check_python(content: str, path: str) -> str:
    """Check Python syntax using the built-in compile()."""
    try:
        compile(content, path, 'exec')
        return 'Valid'
    except SyntaxError as e:
        line_info = f" at line {e.lineno}" if e.lineno else ""
        offset_info = f", column {e.offset}" if e.offset else ""
        text_info = f"\n{e.text.rstrip()}" if e.text else ""
        return f"Syntax Error: {e.msg}{line_info}{offset_info}{text_info}"


def _check_json(content: str, _path: str) -> str:
    """Check JSON syntax using the built-in json module."""
    try:
        json.loads(content)
        return 'Valid'
    except json.JSONDecodeError as e:
        return f"JSON Error: {e.msg} at line {e.lineno}, column {e.colno}"


def _check_yaml(content: str, _path: str) -> str:
    """Check YAML syntax using PyYAML if available."""
    try:
        import yaml
    except ImportError:
        return "Error: PyYAML is not installed. Cannot validate YAML."
    try:
        yaml.safe_load(content)
        return 'Valid'
    except yaml.YAMLError as e:
        error_str = str(e).splitlines()[0] if str(e).splitlines() else str(e)
        return f"YAML Error: {error_str}"


def _check_toml(content: str, _path: str) -> str:
    """Check TOML syntax using tomllib (3.11+) or tomli."""
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return "Error: No TOML parser available (requires Python 3.11+ or 'tomli' package)."
    try:
        tomllib.loads(content)
        return 'Valid'
    except Exception as e:
        error_msg = str(e).splitlines()[0] if str(e).splitlines() else str(e)
        return f"TOML Error: {error_msg}"


def _check_xml(content: str, _path: str) -> str:
    """Check XML syntax using the built-in xml.etree."""
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring(content)
        return 'Valid'
    except ET.ParseError as e:
        return f"XML Error: {e}"


def _check_html(content: str, _path: str) -> str:
    """Check HTML for basic well-formedness using the built-in html.parser."""
    from html.parser import HTMLParser

    errors = []

    class StrictHTMLParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self._tag_stack = []
            self._void_elements = {
                'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
                'link', 'meta', 'param', 'source', 'track', 'wbr'
            }

        def handle_starttag(self, tag, attrs):
            if tag.lower() not in self._void_elements:
                self._tag_stack.append((tag.lower(), self.getpos()))

        def handle_endtag(self, tag):
            # Void elements should never have end tags (self-closing like <img />)
            if tag.lower() in self._void_elements:
                return
            if self._tag_stack and self._tag_stack[-1][0] == tag.lower():
                self._tag_stack.pop()
            elif self._tag_stack:
                expected = self._tag_stack[-1][0]
                line, col = self.getpos()
                errors.append(
                    f"Line {line}: Found closing </{tag}> but expected </{expected}>"
                )
            else:
                # Report unexpected closing tag with no matching open tag
                line, col = self.getpos()
                errors.append(f"Line {line}: Unexpected closing </{tag}> with no open tag")

    try:
        parser = StrictHTMLParser()
        parser.feed(content)
        # Check for unclosed tags
        for tag, (line, col) in parser._tag_stack:
            errors.append(f"Line {line}: Unclosed <{tag}> tag")
    except Exception as e:
        return f"HTML Parse Error: {e}"

    if errors:
        return "HTML Issues:\n" + "\n".join(errors[:20])  # Cap at 20 issues
    return 'Valid'


def _check_css(content: str, _path: str) -> str:
    """Check CSS for basic syntax issues (balanced braces, common typos)."""
    errors = []

    # Strip multi-line comments (/* ... */)
    cleaned = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    # Strip strings (double and single quoted, handling escaped quotes)
    cleaned = re.sub(r'"(?:[^"\\]|\\.)*"', '""', cleaned)
    cleaned = re.sub(r"'(?:[^'\\]|\\.)*'", "''", cleaned)

    brace_depth = 0
    lines = cleaned.splitlines()

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        for ch in stripped:
            if ch == '{':
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth < 0:
                    errors.append(f"Line {i}: Unexpected closing brace '}}'")
                    brace_depth = 0

    if brace_depth > 0:
        errors.append(f"Error: {brace_depth} unclosed brace(s) '{{' in file")
    elif brace_depth < 0:
        errors.append(f"Error: {abs(brace_depth)} extra closing brace(s) '}}' in file")

    if errors:
        return "CSS Issues:\n" + "\n".join(errors[:20])
    return 'Valid'


def _check_c_family(content: str, _path: str) -> str:
    """Check C-family languages for balanced braces, parens, and brackets."""
    errors = []
    stack = []
    match_map = {')': '(', ']': '[', '}': '{'}
    open_chars = set('({[')
    close_chars = set(')}]')

    in_string = False
    string_char = ''
    in_line_comment = False
    in_block_comment = False
    prev_ch = ''

    lines = content.splitlines()
    for line_no, line in enumerate(lines, 1):
        in_line_comment = False
        for col, ch in enumerate(line):
            # Handle block comment state
            if in_block_comment:
                if prev_ch == '*' and ch == '/':
                    in_block_comment = False
                    prev_ch = ''
                    continue
                prev_ch = ch
                continue

            # Handle string state
            if in_string:
                if ch == string_char:
                    # Count preceding backslashes to determine if escaped
                    backslash_count = 0
                    for i in range(col - 1, -1, -1):
                        if line[i] == '\\':
                            backslash_count += 1
                        else:
                            break
                    if backslash_count % 2 == 0:
                        in_string = False
                prev_ch = ch
                continue

            # Check for comment start
            if ch == '/' and col + 1 < len(line):
                next_ch = line[col + 1]
                if next_ch == '/':
                    in_line_comment = True
                    break
                elif next_ch == '*':
                    in_block_comment = True
                    prev_ch = ch
                    continue

            # Check for string start
            if ch in ('"', "'", '`'):
                in_string = True
                string_char = ch
                prev_ch = ch
                continue

            if ch in open_chars:
                stack.append((ch, line_no))
            elif ch in close_chars:
                expected = match_map[ch]
                if not stack:
                    errors.append(f"Line {line_no}: Unexpected '{ch}' with no matching '{expected}'")
                elif stack[-1][0] != expected:
                    opener, open_line = stack[-1]
                    errors.append(
                        f"Line {line_no}: '{ch}' does not match '{opener}' opened at line {open_line}"
                    )
                    stack.pop()
                else:
                    stack.pop()

            prev_ch = ch

    for opener, line_no in stack:
        close = {'{': '}', '(': ')', '[': ']'}[opener]
        errors.append(f"Line {line_no}: Unclosed '{opener}' (expected '{close}')")

    if errors:
        return "Syntax Issues:\n" + "\n".join(errors[:20])
    return 'Valid'


def _check_bash(content: str, _path: str) -> str:
    """Check bash/shell scripts for common syntax issues."""
    errors = []
    lines = content.splitlines()

    # Track unclosed constructs
    if_count = 0
    do_count = 0
    case_count = 0

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Skip comments and empty lines
        if not stripped or stripped.startswith('#'):
            continue

        # Track if/fi
        if re.search(r'\bif\b', stripped):
            if_count += 1
        if re.search(r'\bfi\b', stripped):
            if_count -= 1

        # Track do/done
        if re.search(r'\bdo\b', stripped):
            do_count += 1
        if re.search(r'\bdone\b', stripped):
            do_count -= 1

        # Track case/esac
        if re.search(r'\bcase\b', stripped):
            case_count += 1
        if re.search(r'\besac\b', stripped):
            case_count -= 1

    if if_count > 0:
        errors.append(f"Error: {if_count} unclosed 'if' statement(s) (missing 'fi')")
    if do_count > 0:
        errors.append(f"Error: {do_count} unclosed 'do' block(s) (missing 'done')")
    if case_count > 0:
        errors.append(f"Error: {case_count} unclosed 'case' statement(s) (missing 'esac')")

    if errors:
        return "Shell Issues:\n" + "\n".join(errors)
    return 'Valid'


# ── Language → checker dispatch ────────────────────────────────────────────────
_CHECKER_MAP = {
    'python': _check_python,
    'json': _check_json,
    'yaml': _check_yaml,
    'toml': _check_toml,
    'xml': _check_xml,
    'html': _check_html,
    'css': _check_css,
    'bash': _check_bash,
    # C-family languages all use bracket/brace matching
    'c': _check_c_family,
    'cpp': _check_c_family,
    'csharp': _check_c_family,
    'java': _check_c_family,
    'javascript': _check_c_family,
    'typescript': _check_c_family,
    'go': _check_c_family,
    'rust': _check_c_family,
    'kotlin': _check_c_family,
    'swift': _check_c_family,
    'scala': _check_c_family,
    'php': _check_c_family,
    'lua': _check_c_family,
    'perl': _check_c_family,
    'r': _check_c_family,
}


@register_tool('syntax_check', allow_overwrite=True)
class SyntaxCheck(BaseTool):
    """Checks a file for syntax errors without executing it.

    Auto-detects the language from the file extension and applies the
    appropriate syntax checker. Returns 'Valid' or a detailed error message.
    """

    name = 'syntax_check'
    description = TOOL_METADATA['syntax_check']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['syntax_check']['parameters']['path']
            }
        },
        'required': ['path'],
    }

    def __init__(self, agent_pool=None, **kwargs):
        super().__init__(**kwargs)
        self.agent_pool = agent_pool

    def call(self, params: Union[str, dict], **kwargs) -> str:
        try:
            params = self._verify_json_format_args(params)
            rel_path = params.get('path', '')
        except Exception as e:
            return f"Invalid parameters: {str(e)}"

        if not rel_path.strip():
            return "Error: No file path provided."

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
            return f"Error: File not found at '{rel_path}'"

        if not abs_path.is_file():
            return f"Error: '{rel_path}' is not a file."

        # Check file size (10MB limit)
        if abs_path.stat().st_size > 10 * 1024 * 1024:
            return f"Error: File '{rel_path}' exceeds 10MB limit. Skipping syntax check."

        # Detect language
        lang = _detect_language(str(abs_path))
        if not lang:
            ext = abs_path.suffix
            return (
                f"Error: Unsupported file type '{ext}'. "
                f"Supported extensions: {', '.join(sorted(_EXT_LANG_MAP.keys()))}"
            )

        # Read file content
        try:
            content = abs_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return f"Error reading file: {str(e)}"

        if not content.strip():
            return 'Valid'  # Empty files are syntactically valid

        # Find and run the checker
        checker = _CHECKER_MAP.get(lang)
        if not checker:
            return (
                f"Error: No syntax checker available for language '{lang}'. "
                f"Supported languages: {', '.join(sorted(set(_CHECKER_MAP.keys())))}"
            )

        try:
            result = checker(content, str(abs_path))
            if result == 'Valid':
                return f"Valid ({lang})"
            return f"[{lang}] {result}"
        except Exception as e:
            return f"Error during syntax check: {str(e)}"
