import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, Union

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.tools.custom import cpp_lex

# ── Extension → language mapping ───────────────────────────────────────────────
_EXT_LANG_MAP: Dict[str, str] = {
    # Python
    '.py': 'python',
    '.pyw': 'python',
    '.pyi': 'python',
    # JavaScript / TypeScript
    '.js': 'javascript',
    '.mjs': 'javascript',
    '.cjs': 'javascript',
    '.jsx': 'javascript',
    '.ts': 'typescript',
    '.tsx': 'typescript',
    # Web
    '.html': 'html',
    '.htm': 'html',
    '.css': 'css',
    '.scss': 'css',
    '.less': 'css',
    # Data / Config
    '.json': 'json',
    '.yaml': 'yaml',
    '.yml': 'yaml',
    '.toml': 'toml',
    '.xml': 'xml',
    '.svg': 'xml',
    '.xsl': 'xml',
    '.xslt': 'xml',
    # C-family
    '.c': 'c',
    '.h': 'c',
    '.cpp': 'cpp',
    '.cc': 'cpp',
    '.cxx': 'cpp',
    '.hpp': 'cpp',
    '.hxx': 'cpp',
    '.cs': 'csharp',
    '.java': 'java',
    # Other
    '.go': 'go',
    '.rs': 'rust',
    '.rb': 'ruby',
    '.php': 'php',
    '.lua': 'lua',
    '.sh': 'bash',
    '.bash': 'bash',
    '.ps1': 'powershell',
    '.bat': 'batch',
    '.cmd': 'batch',
    '.sql': 'sql',
    '.r': 'r',
    '.R': 'r',
    '.kt': 'kotlin',
    '.kts': 'kotlin',
    '.swift': 'swift',
    '.pl': 'perl',
    '.pm': 'perl',
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
        line_info = f" at line {e.lineno}" if e.lineno else ''
        offset_info = f", column {e.offset}" if e.offset else ''
        text_info = f"\n{e.text.rstrip()}" if e.text else ''
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
        return 'Error: PyYAML is not installed. Cannot validate YAML.'
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
                'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track',
                'wbr'
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
                errors.append(f"Line {line}: Found closing </{tag}> but expected </{expected}>")
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
        return 'HTML Issues:\n' + '\n'.join(errors[:20])  # Cap at 20 issues
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
        return 'CSS Issues:\n' + '\n'.join(errors[:20])
    return 'Valid'


def _balance_scan(view: str) -> list[str]:
    """Scan a code-only view (strings/comments already neutralized) for
    unbalanced brackets/braces/parens. Returns a list of human-readable
    findings (an empty list means the file is balanced).

    This is the single shared bracket-stacking routine used by both
    ``_check_c_family`` and ``_check_cpp`` (DRY). Line numbers are recovered by
    counting newlines, which the neutralized views preserve.
    """
    errors = []
    stack = []
    match_map = {')': '(', ']': '[', '}': '{'}
    open_chars = set('({[')
    line_no = 1
    for ch in view:
        if ch == '\n':
            line_no += 1
            continue
        if ch in match_map:
            expected = match_map[ch]
            if not stack:
                errors.append(f"Line {line_no}: Unexpected '{ch}' "
                              f"with no matching '{expected}'")
            elif stack[-1][0] != expected:
                opener, open_line = stack[-1]
                errors.append(f"Line {line_no}: '{ch}' does not match "
                              f"'{opener}' opened at line {open_line}")
                stack.pop()
            else:
                stack.pop()
        elif ch in open_chars:
            stack.append((ch, line_no))
    for opener, open_line in stack:
        close_char = {'{': '}', '(': ')', '[': ']'}[opener]
        errors.append(f"Line {open_line}: Unclosed '{opener}' (expected '{close_char}')")
    return errors


def _c_family_neutralize(content: str) -> str:
    """Build a length-preserving code-only view of C-family source.

    String literals (double/single/backtick), ``//`` and ``/* */`` comments, and
    regex-literal bodies are blanked to spaces (newlines preserved) so a plain
    bracket scan (_balance_scan) sees only real code. This is the C-family
    analogue of ``cpp_lex.neutralize``; it keeps the regex/template branches
    that are real in JavaScript/TypeScript/PHP/Perl/R. A quote only closes a
    literal opened by the SAME quote type (D-4: a ``'`` never terminates a
    ``"..."`` string).

    DRY seam (D-6): the comment/string-blanking core overlaps with
    ``cpp_lex.neutralize``. If you fix a comment/string edge case there, apply
    the same fix here (and vice-versa).
    """
    out_lines = []
    in_string = False
    string_char = ''
    in_block_comment = False
    for line in content.splitlines():
        chars = []
        n = len(line)
        i = 0
        prev_ch = ''
        while i < n:
            ch = line[i]
            # block comment body
            if in_block_comment:
                if prev_ch == '*' and ch == '/':
                    in_block_comment = False
                chars.append(' ')
                prev_ch = ch
                i += 1
                continue
            # comment start
            if ch == '/' and not in_string and i + 1 < n:
                nxt = line[i + 1]
                if nxt == '/':
                    chars.extend(' ' * (n - i))  # rest of line is a line comment
                    break
                if nxt == '*':
                    in_block_comment = True
                    chars.append(' ')
                    prev_ch = ch
                    i += 1
                    continue
            # string start / content / close
            if ch in ('"', "'", '`'):
                # Inside a backtick template literal, ' and " are plain
                # characters (only an unescaped ` can terminate the template).
                if in_string and string_char == '`' and ch != '`':
                    chars.append(' ')
                    prev_ch = ch
                    i += 1
                    continue
                if not in_string:
                    in_string = True
                    string_char = ch
                    chars.append(' ')
                    prev_ch = ch
                    i += 1
                    continue
                # D-4: a quote only closes a literal opened by the SAME quote type.
                if ch != string_char:
                    chars.append(' ')  # foreign quote is string content
                    prev_ch = ch
                    i += 1
                    continue
                backslash_count = 0
                j = i - 1
                while j >= 0 and line[j] == '\\':
                    backslash_count += 1
                    j -= 1
                if backslash_count % 2 == 0:
                    in_string = False
                chars.append(' ')
                prev_ch = ch
                i += 1
                continue
            # regex literal /pattern/flags
            if ch == '/' and not in_string:
                before = ''
                j = i - 1
                while j >= 0 and line[j] in ' \t':
                    j -= 1
                if j >= 0:
                    before = line[j]
                if (before in ('(', ')', '[', ']', '=', '!', '&', '|', '^', ',', ';', ':', '?', '~')
                        or i == 0 or j < 0):
                    j = i + 1
                    while j < n:
                        if line[j] == '\\':
                            j += 2
                            continue
                        if line[j] == '[':
                            k = j + 1
                            while k < n:
                                if line[k] == '\\':
                                    k += 2
                                    continue
                                if line[k] == ']':
                                    break
                                k += 1
                            j = k + 1
                            continue
                        if line[j] == '/':
                            j += 1
                            while j < n and (line[j].isalpha() or line[j] == '_'):
                                j += 1
                            break
                        j += 1
                    chars.append(' ' * (j - i))  # blank the whole regex body
                    i = j
                    continue
            # code character
            chars.append(ch)
            prev_ch = ch
            i += 1
        out_lines.append(''.join(chars))
    return '\n'.join(out_lines)


def _check_c_family(content: str, _path: str) -> str:
    """Check C-family languages for balanced braces, parens, and brackets.

    Strings/comments/regex-literals are neutralized first (_c_family_neutralize),
    then the shared _balance_scan reports any unbalanced delimiters. Serves the
    13 non-C/C++ C-family languages (JS/TS/C#/Java/Go/Rust/Kotlin/Swift/Scala/
    PHP/Lua/Perl/R) — regex and template-literal branches are kept because they
    are real in those languages (D-6).
    """
    view = _c_family_neutralize(content)
    errors = _balance_scan(view)
    if errors:
        return 'Syntax Issues:\n' + '\n'.join(errors[:20])
    return 'Valid'


# ── C/C++ checker (BUG_0193) ─────────────────────────────────────────────────

# Genuine grammar-error markers (report as findings) vs. include/environment
# failures (do NOT report as a syntax finding — fall back to the heuristic).
_FATAL_PATTERNS = (
    r"expected ';'", r'expected unqualified-id', r'unbalanced',
    r'expected expression', r"expected '}'", r'mismatched close delimiter',
    r'C2059', r'C2143', r'C1003', r'C1009', r'C1075',
    r'C1513', r'C1514', r'C2447', r'C2449',
)
_NONFATAL_PATTERNS = (
    r'C1083', r'cannot open include file', r'fatal error: .*not found',
    r'LNK1181', r'no such file or directory',
)


def _classify_compiler_output(output: str) -> str:
    """Classify compiler diagnostics as 'fatal' (genuine grammar errors, report
    them) or 'nonfatal' (include/environment failures — fall back to the
    heuristic). A fatal grammar marker trumps an include failure."""
    if any(re.search(p, output) for p in _FATAL_PATTERNS):
        return 'fatal'
    return 'nonfatal'


def _first_include_error(output: str) -> str:
    """Return the first include/environment failure line (for the advisory
    label); falls back to the first non-empty line."""
    for line in output.splitlines():
        if re.search(r'C1083|cannot open include file|fatal error: .*not found', line, re.IGNORECASE):
            return line.strip()
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return 'include'


def _run_compiler(compiler, path: str):
    """Run the syntax-only compiler gate. Returns ``(exit_code, output)`` or
    ``None`` on timeout / OS error (the caller treats ``None`` as "no usable
    compiler" and falls back to the heuristic)."""
    exe, extra = compiler
    base = os.path.basename(exe).lower()
    is_c = path.lower().endswith('.c')
    if base == 'cl.exe':
        cmd = [exe, '/Zs', '/nologo', '/c'] + list(extra) + [path]
    else:
        cmd = [exe, '-fsyntax-only', '-x', 'c' if is_c else 'c++'] + list(extra) + [path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    output = (proc.stderr or '') + (proc.stdout or '')
    return (proc.returncode, output)


def _run_node_check(node: str, content: str):
    """Run ``node --check`` over the given JS *content* via a temporary ``.mjs``
    file. Writing to ``.mjs`` forces ES-module parsing, which is a superset of
    CommonJS syntax — so a CJS file (``require``/``module.exports``) and an ESM
    file (``import``/``export``) both parse cleanly, avoiding the
    "Cannot use import statement outside a module" false positive that checking
    the original ``.js`` path (with package.json module-type inference) would
    produce. Returns ``(exit_code, output)`` or ``None`` on timeout / OS error
    (the caller treats ``None`` as "node unusable" and falls back to the
    heuristic)."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mjs', delete=False,
                                         encoding='utf-8') as tmp:
            tmp.write(content)
            tmp_path = tmp.name
        proc = subprocess.run([node, '--check', tmp_path],
                              capture_output=True, text=True, timeout=20)
        output = (proc.stderr or '') + (proc.stdout or '')
        return (proc.returncode, output)
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _check_cpp(content: str, path: str) -> str:
    """Check C/C++ syntax.

    Runs the shared bracket scan over a ``cpp_lex.neutralize``d view. If a C++
    compiler is discoverable, a 20 s syntax-only gate is attempted: a clean
    exit is authoritative "Valid", fatal grammar diagnostics are reported, and
    include/environment failures fall back to the labelled heuristic so a
    missing include is never reported as a syntax finding. When no compiler is
    available (the default on most hosts) the labelled heuristic is returned.

    Returns a string, or a ``(result, advisory_label)`` tuple for the heuristic
    paths (the wrapper appends the label after the OK/ERROR prefix).
    """
    view = cpp_lex.neutralize(content)
    findings = _balance_scan(view)
    base = 'Syntax Issues:\n' + '\n'.join(findings[:20]) if findings else 'Valid'
    compiler = cpp_lex.find_compiler()
    if compiler:
        result = _run_compiler(compiler, path)
        if result is not None:
            exit_code, output = result
            if exit_code == 0:
                return 'Valid'  # authoritative, no advisory label
            if _classify_compiler_output(output) == 'fatal':
                lines = [l for l in output.splitlines() if l.strip()][:20]
                return 'Compiler Issues:\n' + '\n'.join(lines)
            first = _first_include_error(output)
            label = f'heuristic, advisory — compiler present but includes unresolved: {first}'
            return (base, label)
    label = 'heuristic, advisory — no C++ compiler found'
    return (base, label)


def _check_javascript(content: str, path: str) -> str:
    """Check JavaScript syntax.

    Prefers the authoritative ``node --check`` parse gate when node is on PATH
    and the file is plain JS (``.js``/``.mjs``/``.cjs``): a clean exit is
    "Valid", a non-zero exit reports node's diagnostic. The content is checked
    via a temporary ``.mjs`` file (see _run_node_check) so both CJS and ESM
    syntax parse cleanly. Falls back to the shared C-family bracket heuristic
    (advisory) when node is unavailable, or for ``.jsx`` whose JSX syntax
    ``node --check`` cannot parse.

    Returns a string for the node-authoritative paths, or a
    ``(result, advisory_label)`` tuple for the heuristic-fallback path.
    """
    base = _check_c_family(content, path)
    ext = os.path.splitext(path)[1].lower()
    node = shutil.which('node')
    if node and ext in ('.js', '.mjs', '.cjs'):
        result = _run_node_check(node, content)
        if result is not None:
            exit_code, output = result
            if exit_code == 0:
                return 'Valid'  # authoritative
            lines = [l for l in output.splitlines() if l.strip()][:20]
            return 'Syntax Issues:\n' + '\n'.join(lines)
        # node present + plain JS, but the gate was unusable (timeout / OS error)
        label = 'heuristic, advisory — node --check unavailable, fell back to bracket scan'
        return (base, label)
    if not node:
        label = 'heuristic, advisory — node not available, fell back to bracket scan'
    else:
        label = ('heuristic, advisory — JSX not parseable by node --check, '
                 'fell back to bracket scan')
    return (base, label)


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
        return 'Shell Issues:\n' + '\n'.join(errors)
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
    'c': _check_cpp,
    'cpp': _check_cpp,
    'csharp': _check_c_family,
    'java': _check_c_family,
    'javascript': _check_javascript,
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
            },
            'force_as': {
                'type': 'string',
                'description': TOOL_METADATA['syntax_check']['parameters']['force_as']
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
            force_as = params.get('force_as')          # optional override of extension detection
        except Exception as e:
            return f"Invalid parameters: {str(e)}"

        if not rel_path.strip():
            return 'ERROR: No file path provided.'

        # Resolve absolute path with validation
        from agent_cascade.utils.tool_path_resolver import resolve_tool_path
        try:
            abs_path = resolve_tool_path(rel_path, mode='ro', agent_pool=self.agent_pool)
        except ValueError as e:
            return f"ERROR: {str(e)}"

        if not abs_path.exists():
            return f"ERROR: File not found at '{rel_path}'"

        if not abs_path.is_file():
            return f"ERROR: '{rel_path}' is not a file."

        # Check file size (10MB limit)
        if abs_path.stat().st_size > 10 * 1024 * 1024:
            return f"ERROR: File '{rel_path}' exceeds 10MB limit. Skipping syntax check."

        # Resolve language: force_as overrides extension detection (its whole point is to
        # check files whose extension is unknown/absent). Single source of truth: _CHECKER_MAP.
        if force_as:
            lang = str(force_as).strip().lower()
            if lang not in _CHECKER_MAP:
                return (f"ERROR: force_as='{force_as}' is not a supported language. "
                        f"Supported languages: {', '.join(sorted(_CHECKER_MAP.keys()))}")
        else:
            lang = _detect_language(str(abs_path))
            if not lang:
                ext = abs_path.suffix
                return (f"ERROR: Unsupported file type '{ext}'. "
                        f"Supported extensions: {', '.join(sorted(_EXT_LANG_MAP.keys()))}")

        # Read file content
        try:
            content = abs_path.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            return f"ERROR reading file: {str(e)}"

        if not content.strip():
            return f'OK: {rel_path} syntax valid ({lang}, 0 lines)'

        # Find and run the checker
        checker = _CHECKER_MAP.get(lang)
        if not checker:
            return (f"Error: No syntax checker available for language '{lang}'. "
                    f"Supported languages: {', '.join(sorted(set(_CHECKER_MAP.keys())))}")

        try:
            result = checker(content, str(abs_path))
            line_count = len(content.splitlines())
            if isinstance(result, tuple):
                base, advisory = result
            else:
                base, advisory = result, None
            if base == 'Valid':
                out = f"OK: {rel_path} syntax valid ({lang}, {line_count} lines)"
            else:
                out = f"ERROR: {rel_path} syntax error ({lang}) — {base}"
            if advisory:
                out += f" ({advisory})"
            return out
        except Exception as e:
            return f"Error during syntax check: {str(e)}"
