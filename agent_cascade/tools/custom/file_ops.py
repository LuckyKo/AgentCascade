from pathlib import Path
import tempfile
import os
import atexit
import sys
from typing import Optional, Any, Union
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.settings import (
    DEFAULT_WORKSPACE, DEFAULT_TOOL_RESULT_MAX_CHARS, CHARS_PER_TOKEN_ESTIMATE,
)
import json
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.utils.utils import json_loads

# --- Module-level cairosvg DLL state (Windows only) --------------------------- #
_cairosvg_dll_handles: list = []          # handles returned by os.add_dll_directory()
_cairosvg_setup_done: bool = False        # ensures DLL setup runs exactly once


def _cleanup_cairosvg_dll_handles():
    """Close all DLL directory handles registered for cairosvg atexit."""
    for handle in _cairosvg_dll_handles:
        try:
            handle.close()
        except Exception:
            pass
    _cairosvg_dll_handles.clear()

atexit.register(_cleanup_cairosvg_dll_handles)

_gtk_common_paths = [
    os.environ.get("GTK_LIBS", ""),
    os.environ.get("CAIROCFFI_DLL_DIRECTORIES", ""),
    r"C:\Program Files\GTK3-Runtime Win64\bin",
    r"D:\Program Files\GTK3-Runtime Win64\bin",
    r"C:\Program Files (x86)\GTK3-Runtime Win64\bin",
]

# --- read_file constants ----------------------------------------------------- #
DEFAULT_MAX_INPUT_TOKENS = 58000          # Default context window size in tokens
DEFAULT_READ_LINES = 250                  # Default lines to read when no limit specified
MAX_LINE_LIMIT_EXPLICIT = 100000          # Max lines when user explicitly sets a limit
HEX_DUMP_BYTES = 1024                     # Bytes to show in hex view for binary files
CONTEXT_FRACTION = 0.25                   # Fraction of context window reserved for tool output
MIN_TRUNCATED_LINE_CHARS = 200            # Minimum characters to keep when truncating a single line


def _is_binary_file(path: Path) -> bool:
    """Check if a file is binary by reading its first 1 KiB and looking for null bytes."""
    try:
        with open(path, 'rb') as f:
            chunk = f.read(1024)
        # Empty files are not binary
        if not chunk:
            return False
        # Check for null bytes (strong indicator of binary content)
        return b'\x00' in chunk
    except OSError:
        return False


def _format_hex_dump(data: bytes) -> str:
    """Create a hex dump with ASCII column, similar to `hexdump -C`."""
    lines: list[str] = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        # Pad to fixed width (47 chars for 16 bytes: 3*16+15 spaces)
        hex_part = hex_part.ljust(47)
        ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        lines.append(f'{i:08x}  {hex_part}  |{ascii_part}|')
    return '\n'.join(lines)


@register_tool('read_file', allow_overwrite=True)
class ReadFile(BaseTool):
    """Reads and returns the content of a specified file.

    Handles text files natively with streaming line-by-line reading. For binary
    files, displays a hex dump of the first N bytes with ASCII representation.
    """

    name = 'read_file'
    description = TOOL_METADATA['read_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['read_file']['parameters']['path']
            },
            'start_line': {
                'type': 'integer',
                'description': TOOL_METADATA['read_file']['parameters']['start_line'],
                'default': 1
            },
            'limit': {
                'type': 'integer',
                'description': TOOL_METADATA['read_file']['parameters']['limit']
            }
        },
        'required': ['path'],
    }

    def __init__(self, cfg: Optional[dict] = None, **kwargs: Any) -> None:
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    # ------------------------------------------------------------------ #
    #  Helper: resolve path using operation_manager (aligns with other tools)
    # ------------------------------------------------------------------ #
    def _resolve_path(self, path: str) -> Path:
        """Resolve *path* using the same mechanism as all other file-op tools.
        
        Delegates to ``operation_manager._resolve_path`` when available; falls back
        to a minimal resolution against ``DEFAULT_WORKSPACE`` otherwise.
        """
        if self.agent_pool is not None and hasattr(self.agent_pool, 'operation_manager'):
            return self.agent_pool.operation_manager._resolve_path(path, mode="ro")

        # Fallback: resolve against DEFAULT_WORKSPACE with commonpath check
        base_dir = Path(DEFAULT_WORKSPACE)
        if Path(path).is_absolute():
            resolved = Path(path).resolve()
        else:
            resolved = (base_dir / path).resolve()
        base_resolved = base_dir.resolve()
        try:
            common = os.path.commonpath([resolved, base_resolved])
        except ValueError:
            raise ValueError(f"Path '{path}' is outside the allowed directory")
        if common != str(base_resolved):
            raise ValueError(f"Path '{path}' is outside the allowed directory")
        return resolved

    # ------------------------------------------------------------------ #
    #  Helper: safely extract max_input_tokens from a config dict         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_max_tokens(cfg: dict) -> Optional[int]:
        """Extract max_input_tokens from a config dict, checking both top-level
        and generate_cfg sub-dict."""
        val = cfg.get('max_input_tokens') or (cfg.get('generate_cfg', {}) or {}).get('max_input_tokens')
        return int(val) if val else None

    # ------------------------------------------------------------------ #
    #  Helper: determine effective max_input_tokens from config hierarchy
    # ------------------------------------------------------------------ #
    def _get_max_input_tokens(self, kwargs: dict) -> int:
        """Return the effective context window size in tokens.
        
        Priority: agent_obj.llm > agent_pool.llm_cfg > DEFAULT_MAX_INPUT_TOKENS
        """
        tokens = DEFAULT_MAX_INPUT_TOKENS

        # Check agent_pool configuration
        if self.agent_pool is not None:
            pool_max = self._extract_max_tokens(getattr(self.agent_pool, 'llm_cfg', {}))
            if pool_max:
                tokens = pool_max

        # Check agent_obj override (highest priority)
        agent_obj = kwargs.get('agent_obj')
        if agent_obj is not None:
            llm = getattr(agent_obj, 'llm', None)
            gen_cfg = getattr(llm, 'generate_cfg', {}) if llm else {}
            agent_max = self._extract_max_tokens(gen_cfg)
            if agent_max and agent_max != DEFAULT_MAX_INPUT_TOKENS:
                tokens = agent_max

        return tokens

    # ------------------------------------------------------------------ #
    #  Helper: determine line limit and whether this is a "wild read"     #
    # ------------------------------------------------------------------ #
    def _determine_limits(self, limit: Optional[int]) -> tuple[int, bool]:
        """Return (line_limit, is_wild_read). Wild reads have no explicit limit
        set by the caller and get capped more aggressively on character budget.
        
        Priority: explicit limit > -1 (unlimited) > default."""
        if limit == -1:
            return MAX_LINE_LIMIT_EXPLICIT, False
        elif limit is not None:
            return min(int(limit), MAX_LINE_LIMIT_EXPLICIT), False
        else:
            return DEFAULT_READ_LINES, True  # wild read — char budget will be capped lower

    # ------------------------------------------------------------------ #
    #  Helper: calculate character budget for the read                    #
    # ------------------------------------------------------------------ #
    def _calculate_char_limit(self, kwargs: dict, is_wild_read: bool, wild_limit: int) -> int:
        """Calculate the character limit based on context window and token estimates."""
        max_input_tokens = self._get_max_input_tokens(kwargs)
        char_limit = int(max_input_tokens * CONTEXT_FRACTION * CHARS_PER_TOKEN_ESTIMATE)
        char_limit = max(500, char_limit)  # floor at 500 chars
        if is_wild_read:
            char_limit = min(char_limit, wild_limit)
        return char_limit

    # ------------------------------------------------------------------ #
    #  Helper: read text file with streaming line-by-line iteration       #
    # ------------------------------------------------------------------ #
    def _read_text_file(
        self, path: str, resolved: Path, start_line: int, limit: int, char_limit: int
    ) -> str:
        """Read a text file using streaming line-by-line iteration.
        
        Returns formatted content string ready for the user.
        """
        total_lines = 0
        lines_read: list[str] = []
        current_chars = 0
        hit_line_limit = False   # Truncated because we hit the line count limit
        hit_char_limit = False   # Truncated because we hit the character budget

        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            for line_num, raw_line in enumerate(f, 1):
                if line_num < start_line:
                    continue
                if len(lines_read) >= limit:
                    # We've read enough lines — peek ahead to see if there's more
                    extra = f.readline()
                    if extra:
                        hit_line_limit = True
                        # Count remaining lines for accurate total (+1 for the peeked line)
                        total_lines = line_num + 1 + sum(1 for _ in f)
                    else:
                        total_lines = len(lines_read)  # exactly limit lines, EOF
                    break

                stripped = raw_line.rstrip('\n\r')
                formatted = f"{line_num}: {stripped}\n"

                if current_chars + len(formatted) > char_limit:
                    # First line itself is huge — include a truncated portion
                    if not lines_read:
                        cut = min(len(formatted), max(char_limit, MIN_TRUNCATED_LINE_CHARS))
                        lines_read.append(formatted[:cut] + " ... [LINE TRUNCATED]\n")
                    hit_char_limit = True
                    # Count remaining lines for accurate total (+1 for current line)
                    total_lines = line_num + sum(1 for _ in f)
                    break

                lines_read.append(formatted)
                current_chars += len(formatted)
                total_lines = line_num

        # Since empty files are caught below, actual_end is always valid here
        actual_end = start_line + len(lines_read) - 1
        content = ''.join(lines_read)

        if not lines_read:
            return f"File content ({path}) — empty file."

        # Build header with total line count info
        header = f"File content ({path}), lines {start_line} to {actual_end} of {total_lines}:"

        truncated_msg = ""
        if hit_line_limit or hit_char_limit:
            header += " [TRUNCATED]"
            remaining = max(0, total_lines - actual_end)
            next_chunk = min(limit, remaining) if limit > 0 else remaining
            truncated_msg = (
                f"\n\n[PAGINATION NOTE: This file is large. Use read_file with "
                f"start_line={actual_end + 1} to read the next {next_chunk} lines.]"
            )

        return f"{header}\n```\n{content}```{truncated_msg}"

    # ------------------------------------------------------------------ #
    #  Helper: read binary file and return hex dump view
    # ------------------------------------------------------------------ #
    def _read_binary_file(self, path: str, resolved: Path) -> str:
        """Read a binary file and return a hex dump of the first HEX_DUMP_BYTES bytes."""
        try:
            file_size = resolved.stat().st_size
        except OSError:
            file_size = 0

        with open(resolved, 'rb') as f:
            data = f.read(HEX_DUMP_BYTES)

        if not data:
            return f"File content ({path}) — empty binary file."

        hex_view = _format_hex_dump(data)
        size_str = f"{file_size:,}" if file_size > 0 else "unknown"
        
        return (
            f"Binary file ({path}), {size_str} bytes.\n"
            f"Hex dump of first {len(data)} bytes:\n```\n{hex_view}\n```"
        )

    # ------------------------------------------------------------------ #
    #  Helper: resolve negative/zero start_line against total lines       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_start_line(start_line: int, total_lines: int) -> int:
        """Convert a possibly-negative start_line to a valid 1-based line number.
        
        Mirrors ReIndent's negative-index handling (see operation_manager.py ~1927):
        - Positive: 1 = first line, 2 = second, etc. Clamped to [1, total_lines].
        - Zero or negative: converted like Python list indexing (-1 = last, -3 = third-to-last).
          If the result is <= 0 (e.g., start_line=-100 on a 5-line file), clamped to 1.
        """
        if start_line <= 0:
            # 0 → last line; negative counts from end (-1=last, -2=second-to-last)
            offset_from_end = min(max(1, -start_line), total_lines)
            resolved = total_lines - offset_from_end + 1
            return max(1, resolved)
        return min(start_line, total_lines)

    # ------------------------------------------------------------------ #
    #  Main call()                                                        #
    # ------------------------------------------------------------------ #
    def call(self, params: Union[str, dict], **kwargs: Any) -> str:
        params = self._verify_json_format_args(params)
        path = params.get('path')
        if not path:
            return "ERROR: Missing 'path' parameter. Please provide a file path."

        # Validate start_line type (Fix #7)
        raw_start = params.get('start_line', 1)
        try:
            raw_start = int(raw_start)
        except (TypeError, ValueError):
            return f"ERROR: 'start_line' must be an integer, got: {raw_start!r}"

        limit = params.get('limit')

        # Get the character limit from agent/tool options or settings
        wild_limit = DEFAULT_TOOL_RESULT_MAX_CHARS
        if self.agent_pool is not None:
            wild_limit = getattr(self.agent_pool, 'llm_cfg', {}).get(
                'tool_result_max_chars', wild_limit
            )

        # Determine line limit and whether this is a "wild read" (no explicit limit)
        limit, is_wild_read = self._determine_limits(limit)

        try:
            # Resolve path using the same mechanism as all other file-op tools
            resolved = self._resolve_path(path)

            if not resolved.exists():
                return f"File not found: {path}"

            # Check it's actually a file (not a directory or special file)
            if not resolved.is_file():
                return f"Not a regular file: {path}"

            # Determine character budget for this read
            char_limit = self._calculate_char_limit(kwargs, is_wild_read, wild_limit)

            # Check for binary content
            if _is_binary_file(resolved):
                return self._read_binary_file(path, resolved)

            # Resolve negative/zero start_line against total file length
            if raw_start <= 0:
                with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                    total = sum(1 for _ in f)
                start_line = self._resolve_start_line(raw_start, total)
            else:
                # Clamp positive start_line to a reasonable max (we'll refine after reading)
                start_line = raw_start

            return self._read_text_file(
                path=path, resolved=resolved, start_line=start_line,
                limit=limit, char_limit=char_limit,
            )

        except ValueError as e:
            # Path resolution errors (outside allowed directories)
            return f"Path error for '{path}': {e}"
        except PermissionError as e:
            return f"Permission denied reading '{path}': {e}"
        except OSError as e:
            # Catches FileNotFoundError, IOError, etc. — merged handler (Fix #6)
            return f"OS error reading file '{path}': {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


@register_tool('view_image', allow_overwrite=True)
class ViewImage(BaseTool):
    """View an image file from the workspace."""

    name = 'view_image'
    description = TOOL_METADATA['view_image']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['view_image']['parameters']['path']
            }
        },
        'required': ['path'],
    }

    def __init__(self, cfg: Optional[dict] = None, **kwargs: Any) -> None:
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')

    # ------------------------------------------------------------------ #
    #  SVG → PNG conversion helpers                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _setup_cairosvg_dll_dirs():
        """On Windows, register DLL directories so cairosvg's native libs can load.
        
        Runs exactly once (guarded by the module-level _cairosvg_setup_done flag).
        All os.add_dll_directory() handles are stored and closed atexit.
        """
        global _cairosvg_setup_done
        if _cairosvg_setup_done or sys.platform != "win32":
            return

        for p in _gtk_common_paths:
            if not isinstance(p, str):
                continue
            p = p.strip()
            if not p or not os.path.isdir(p):
                continue
            try:
                handle = os.add_dll_directory(p)
                _cairosvg_dll_handles.append(handle)
            except OSError:
                pass  # already registered

        _cairosvg_setup_done = True

    @staticmethod
    def _convert_svg_to_png(svg_path: Path) -> Path:
        """
        Convert an SVG file to PNG using cairosvg.

        Returns a Path pointing to the temp PNG file. Cleaned up by the caller
        after serving.
        """
        # Ensure DLL dirs are registered (Windows-only, no-op on Linux/macOS)
        ViewImage._setup_cairosvg_dll_dirs()

        try:
            import cairosvg
        except ImportError:
            raise ImportError(
                "cairosvg is required for SVG viewing. Install it with: pip install cairosvg"
            )
        except OSError as exc:
            # Cairosvg may fail on Windows if GTK3 runtime is missing
            raise OSError(
                f"cairosvg native library error: {exc}. "
                "On Windows you may need GTK3 runtime. "
                "Install from: https://github.com/tschoonj/GTK3-Runtime-for-Windows/releases "
                "or set the GTK_LIBS environment variable."
            )

        # Read SVG, convert to PNG bytes
        svg_bytes = svg_path.read_bytes()
        png_data = cairosvg.svg2png(bytestring=svg_bytes)

        # Write to a temp file so the existing image-serving pipeline can use it
        tmp_fd, tmp_png_path = tempfile.mkstemp(suffix='.png', prefix='svg_view_')
        os.close(tmp_fd)
        with open(tmp_png_path, "wb") as f:
            f.write(png_data)

        return Path(tmp_png_path)

    # ------------------------------------------------------------------ #
    #  Main call()                                                        #
    # ------------------------------------------------------------------ #

    def call(self, params: str, **kwargs):
        from agent_cascade.llm.schema import ContentItem
        params = self._verify_json_format_args(params)
        path = params['path']

        temp_png: Path | None = None  # track temp file for cleanup
        try:
            if hasattr(self, 'agent_pool') and self.agent_pool:
                resolved = self.agent_pool.operation_manager._resolve_path(path, mode="ro")
            else:
                # Fallback if no agent_pool
                base_dir = Path(DEFAULT_WORKSPACE)
                if Path(path).is_absolute():
                    resolved = Path(path).resolve()
                else:
                    resolved = (base_dir / path).resolve()
                base_resolved = base_dir.resolve()
                try:
                    common = os.path.commonpath([resolved, base_resolved])
                except ValueError:
                    return f"Path '{path}' is outside the allowed directory"
                if common != str(base_resolved):
                    return f"Path '{path}' is outside the allowed directory"

            if not resolved.exists():
                return f"Image not found: {path}"

            # SVG files need conversion to PNG (PIL/Pillow can't read SVG natively)
            if resolved.suffix.lower() == '.svg':
                temp_png = self._convert_svg_to_png(resolved)
                file_url = temp_png.as_uri()
            else:
                file_url = resolved.as_uri()

            return [
                ContentItem(image=file_url),
                ContentItem(text=f"Viewing image: {path}")
            ]
        except (ValueError, TypeError) as e:
            # SVG parse errors from cairosvg come through as ValueError/TypeError
            return f"SVG parse error in '{path}': {e}"
        except Exception as e:
            return f"Error viewing image: {str(e)}"
        finally:
            # Clean up the temp PNG file after serving (best-effort)
            if temp_png and os.path.exists(temp_png):
                try:
                    os.remove(temp_png)
                except OSError:
                    pass  # non-critical cleanup failure


@register_tool('write_file', allow_overwrite=True)
class WriteFile(BaseTool):
    """Writes content to a specified file in the local filesystem."""

    name = 'write_file'
    description = TOOL_METADATA['write_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['write_file']['parameters']['path']
            },
            'content': {
                'type': 'string',
                'description': TOOL_METADATA['write_file']['parameters']['content']
            },
            'justification': {
                'type': 'string',
                'description': TOOL_METADATA['write_file']['parameters']['justification']
            }
        },
        'required': ['path', 'content'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        import re
        from agent_cascade.utils.utils import extract_code

        # --- Robust Fallback for Non-JSON Input ---
        # Handles the case where the model emits "path\n```code```" instead of JSON
        if isinstance(params, str) and not params.strip().startswith('{'):
            match = re.search(r'^(?:path:?\s*)?([^\n`]+)\s*?\n*?```[^\n]*\n(.*?)\n?```', params.strip(), re.DOTALL | re.IGNORECASE)
            if match:
                path = match.group(1).strip()
                content = match.group(2)
                return self.agent_pool.operation_manager.write_file(
                    path=path,
                    content=content,
                    agent_name=self.agent_name,
                )

        # --- Standard JSON Path ---
        params_json = self._verify_json_format_args(params)
        path = params_json.get('path')
        content = params_json.get('content', '')

        # Only strip markdown wrappers if content looks like it was JSON-embedded
        # (i.e., starts with ``` — this is a legacy fallback for when XML extraction
        # didn't happen and the model put a code block inside the JSON string)
        if isinstance(content, str) and content.strip().startswith('```'):
            content = extract_code(content)

        return self.agent_pool.operation_manager.write_file(
            path=path,
            content=content,
            agent_name=self.agent_name,
        )


@register_tool('edit_file', allow_overwrite=True)
class EditFile(BaseTool):
    """Replaces text within a file."""

    name = 'edit_file'
    description = TOOL_METADATA['edit_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['edit_file']['parameters']['path']
            },
            'old_content': {
                'type': 'string',
                'description': TOOL_METADATA['edit_file']['parameters']['old_content']
            },
            'new_content': {
                'type': 'string',
                'description': TOOL_METADATA['edit_file']['parameters']['new_content']
            },
            'match_mode': {
                'type': 'string',
                'enum': ['exact', 'heuristic', 'heuristic_agnostic', 'delete_and_insert'],
                'default': 'exact',
                'description': TOOL_METADATA['edit_file']['parameters']['match_mode']
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to edit this file'
            }
        },
        'required': ['path', 'old_content'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        from agent_cascade.utils.utils import extract_code
        
        # Normalize legacy parameter names to current schema
        try:
            if isinstance(params, str):
                p = json_loads(params)
                if 'old_string' in p and 'old_content' not in p:
                    p['old_content'] = p['old_string']
                if 'new_string' in p and 'new_content' not in p:
                    p['new_content'] = p['new_string']
                params = json.dumps(p)
            elif isinstance(params, dict):
                if 'old_string' in params and 'old_content' not in params:
                    params['old_content'] = params['old_string']
                if 'new_string' in params and 'new_content' not in params:
                    params['new_content'] = params['new_string']
        except (json.JSONDecodeError, TypeError, KeyError, ValueError):
            pass

        params_json = self._verify_json_format_args(params)
        path = params_json.get('path')
        old_content = params_json.get('old_content')
        new_content = params_json.get('new_content')
        match_mode = params_json.get('match_mode', 'exact')
        
        # Handle cases where model uses XML tags with old names
        if not old_content and params_json.get('old_string'):
            old_content = params_json.get('old_string')
        if not new_content and params_json.get('new_string'):
            new_content = params_json.get('new_string')

        # Only strip markdown wrappers as a legacy fallback (when content was
        # JSON-embedded instead of XML-extracted)
        if new_content and isinstance(new_content, str) and new_content.strip().startswith('```'):
            new_content = extract_code(new_content)

        if not path:
            return "ERROR: Missing 'path'."
        if not old_content:
            return "ERROR: Missing 'old_content'. Please provide the exact text you want to replace."
        # For delete_and_insert mode, empty new_content means delete-only
        if match_mode == 'delete_and_insert':
            if new_content is None:
                new_content = ''
        elif new_content is None:
            return "ERROR: Missing 'new_content'. Please provide the text you want to replace old_content with."

        return self.agent_pool.operation_manager.edit_file(
            path=path,
            agent_name=self.agent_name,
            old_content=old_content,
            new_content=new_content,
            match_mode=match_mode,
        )


@register_tool('list_dir', allow_overwrite=True)
class ListDir(BaseTool):
    """Lists the names of files and subdirectories directly within a specified directory path."""

    name = 'list_dir'
    description = TOOL_METADATA['list_dir']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['path']
            },
            'recursive': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['list_dir']['parameters']['recursive']
            },
            'max_depth': {
                'type': 'integer',
                'default': -1,
                'description': TOOL_METADATA['list_dir']['parameters']['max_depth']
            },
            'include': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['include']
            },
            'exclude': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['exclude']
            },
            'sort_by': {
                'type': 'string',
                'enum': ['name', 'size', 'date', 'type'],
                'default': 'name',
                'description': TOOL_METADATA['list_dir']['parameters']['sort_by']
            },
            'show_summary': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['list_dir']['parameters']['show_summary']
            },
            'max_entries': {
                'type': 'integer',
                'default': 500,
                'description': TOOL_METADATA['list_dir']['parameters']['max_entries']
            }
        },
        'required': ['path'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        path = params.get('path', '.')
        recursive = params.get('recursive', False)
        max_depth = params.get('max_depth', -1)
        include = params.get('include')  # None if not provided
        exclude = params.get('exclude')  # None if not provided
        sort_by = params.get('sort_by', 'name')
        show_summary = params.get('show_summary', False)
        max_entries = params.get('max_entries', 500)
        return self.agent_pool.operation_manager.list_directory(
            path, recursive=recursive, max_depth=max_depth,
            include=include, exclude=exclude, sort_by=sort_by,
            show_summary=show_summary, max_entries=max_entries
        )


@register_tool('grep', allow_overwrite=True)
class Grep(BaseTool):
    """Search for text patterns in files."""

    name = 'grep'
    description = TOOL_METADATA['grep']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'pattern': {
                'type': 'string',
                'description': TOOL_METADATA['grep']['parameters']['pattern']
            },
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['grep']['parameters']['path']
            },
            'include': {
                'type': 'string',
                'description': TOOL_METADATA['grep']['parameters']['include']
            },
            'exclude': {
                'type': 'string',
                'description': TOOL_METADATA['grep']['parameters']['exclude']
            },
            'ignore_vcs': {
                'type': 'boolean',
                'description': TOOL_METADATA['grep']['parameters']['ignore_vcs']
            },
            'context': {
                'type': 'integer',
                'description': TOOL_METADATA['grep']['parameters']['context']
            },
            'smart_case': {
                'type': 'boolean',
                'description': TOOL_METADATA['grep']['parameters']['smart_case']
            }
        },
        'required': ['pattern'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        pattern = params['pattern']
        path = params.get('path', '.')
        include = params.get('include', '*')
        exclude = params.get('exclude', '')
        ignore_vcs = params.get('ignore_vcs', True)
        context = params.get('context', 0)
        smart_case = params.get('smart_case', True)

        # Get the truncation limit from agent/tool options
        char_limit = 2000
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('grep_char_limit', char_limit)
        elif self.cfg.get('grep_char_limit'):
            char_limit = self.cfg.get('grep_char_limit')

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        spill_file_path = kwargs.get('spill_file_path')  # Pre-computed by orchestrator
        return self.agent_pool.operation_manager.grep(
            pattern, path, include, 
            char_limit=int(char_limit), 
            agent_name=agent_name,
            exclude=exclude,
            ignore_vcs=bool(ignore_vcs),
            context=int(context),
            smart_case=bool(smart_case),
            spill_file_path=spill_file_path
        )


@register_tool('delete_file', allow_overwrite=True)
class DeleteFile(BaseTool):
    """Delete a file — creates a timestamped backup before deletion (requires user approval)."""

    name = 'delete_file'
    description = TOOL_METADATA['delete_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['delete_file']['parameters']['path']
            }
        },
        'required': ['path'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        path = params['path']
        return self.agent_pool.operation_manager.delete_file(path, self.agent_name)


@register_tool('copy_file', allow_overwrite=True)
class CopyFile(BaseTool):
    """Copy a file or directory — creates timestamped backup before overwriting existing destination."""

    name = 'copy_file'
    description = TOOL_METADATA['copy_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'source': {
                'type': 'string',
                'description': TOOL_METADATA['copy_file']['parameters']['source']
            },
            'destination': {
                'type': 'string',
                'description': TOOL_METADATA['copy_file']['parameters']['destination']
            }
        },
        'required': ['source', 'destination'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        source = params['source']
        destination = params['destination']
        return self.agent_pool.operation_manager.copy_file(source, destination, self.agent_name)


class MoveFile(BaseTool):
    """Move a file or directory — creates timestamped backup before overwriting existing destination (requires user approval)."""

    name = 'move_file'
    description = (
        'Move a file or directory to a new location. If the destination already exists, '
        'a timestamped backup is created before overwriting. Requires user approval for any files not owned '
        'by the current agent. Moving files you created in this session is auto-approved.'
    )
    parameters = {
        'type': 'object',
        'properties': {
            'source': {
                'type': 'string',
                'description': "Path to the source file/directory, absolute or relative to workspace root"
            },
            'destination': {
                'type': 'string',
                'description': "Path to the destination, absolute or relative to workspace root"
            }
        },
        'required': ['source', 'destination'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        params = self._verify_json_format_args(params)
        source = params['source']
        destination = params['destination']
        return self.agent_pool.operation_manager.move_file(source, destination, self.agent_name)


@register_tool('re_indent', allow_overwrite=True)
class ReIndent(BaseTool):
    """Re-indents a block of code in a file."""

    name = 're_indent'
    description = TOOL_METADATA['re_indent']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['re_indent']['parameters']['path']
            },
            'lines': {
                'type': 'string',
                'description': TOOL_METADATA['re_indent']['parameters']['lines']
            },
            'indent': {
                'type': 'integer',
                'description': TOOL_METADATA['re_indent']['parameters']['indent']
            },
            'indent_type': {
                'type': 'string',
                'enum': ['space', 'tab'],
                'description': TOOL_METADATA['re_indent']['parameters']['indent_type']
            },
            'mode': {
                'type': 'string',
                'enum': ['shift', 'min', 'flat', 'convert'],
                'default': 'min',
                'description': TOOL_METADATA['re_indent']['parameters']['mode']
            }
        },
        'required': ['path', 'lines', 'indent', 'indent_type'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        params_json = self._verify_json_format_args(params)
        path = params_json.get('path')
        lines = params_json.get('lines')
        indent = params_json.get('indent')
        type_ = params_json.get('indent_type')
        mode = params_json.get('mode', 'min')

        if not path:
            return "ERROR: Missing 'path'."
        if lines is None:
            return "ERROR: Missing 'lines' (1-based line range like '1:10')."
        if indent is None:
            return "ERROR: Missing 'indent' (integer value)."
        if not type_:
            return "ERROR: Missing 'indent_type' ('space' or 'tab')."

        # FIX 7: Explicit validation for type_ and mode
        if type_ not in ('space', 'tab'):
            return "ERROR: 'indent_type' must be 'space' or 'tab'."
        VALID_MODES = ('shift', 'min', 'flat', 'convert')
        if mode not in VALID_MODES:
            return f"ERROR: 'mode' must be one of {VALID_MODES}. Got '{mode}'."

        return self.agent_pool.operation_manager.re_indent(
            path=path,
            agent_name=self.agent_name,
            lines=lines,
            indent=indent,
            indent_type=type_,  # FIX 6: Changed from type= to indent_type=
            mode=mode,
        )

