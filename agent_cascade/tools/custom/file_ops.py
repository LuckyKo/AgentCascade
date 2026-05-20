from pathlib import Path
import tempfile
import os
import atexit
import sys
from agent_cascade.tools.base import BaseTool
from agent_cascade.settings import DEFAULT_WORKSPACE, DEFAULT_READ_FILE_MAX_LINES, DEFAULT_TOOL_RESULT_MAX_CHARS
from agent_cascade.prompts.dna import TOOL_METADATA

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


class ReadFile(BaseTool):
    """Reads and returns the content of a specified file. Handles text, images, and PDF files."""

    name = 'read_file'
    description = TOOL_METADATA['read_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'absolute_path': {
                'type': 'string',
                'description': TOOL_METADATA['read_file']['parameters']['absolute_path']
            },
            'offset': {
                'type': 'integer',
                'description': TOOL_METADATA['read_file']['parameters']['offset'],
                'default': 0
            },
            'limit': {
                'type': 'integer',
                'description': TOOL_METADATA['read_file']['parameters']['limit']
            },
            'full_read': {
                'type': 'boolean',
                'description': TOOL_METADATA['read_file']['parameters']['full_read'],
                'default': False
            }
        },
        'required': ['absolute_path'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        from agent_cascade.utils.utils import json_loads
        import json
        # Mapping for backward compatibility
        try:
            if isinstance(params, str):
                p = json_loads(params)
                if 'path' in p and 'absolute_path' not in p:
                    p['absolute_path'] = p['path']
                if 'start_line' in p and 'offset' not in p:
                    p['offset'] = p['start_line'] - 1
                params = json.dumps(p)
            elif isinstance(params, dict):
                if 'path' in params and 'absolute_path' not in params:
                    params['absolute_path'] = params['path']
                if 'start_line' in params and 'offset' not in params:
                    params['offset'] = params['start_line'] - 1
        except:
            pass

        params = self._verify_json_format_args(params)
        path = params.get('absolute_path')
        offset = params.get('offset', 0)
        start_line = params.get('start_line', offset + 1)
        limit = params.get('limit')
        full_read = params.get('full_read', False)

        # Get the character limit from agent/tool options or settings
        # Default "soft" limit for wild reads (if no limit/full_read provided)
        wild_limit = DEFAULT_TOOL_RESULT_MAX_CHARS
        if hasattr(self, 'agent_pool') and self.agent_pool:
            wild_limit = getattr(self.agent_pool, 'llm_cfg', {}).get('tool_result_max_chars', wild_limit)

        # Line-based limit is now secondary to character-based volume
        if not full_read:
            if limit is None:
                # If no limit provided, we read until we hit the character threshold
                limit = 1000000 
                is_wild_read_candidate = True
            else:
                is_wild_read_candidate = False
                if limit > 100000:
                    limit = 100000
        else:
            limit = 1000000  # Effectively "full" read
            is_wild_read_candidate = False

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
                if not str(resolved).startswith(str(base_dir.resolve())):
                     return f"Path '{path}' is outside the allowed directory"

            if not resolved.exists():
                return f"File not found: {path}"

            with open(resolved, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            total_lines = len(lines)
            start_idx = max(0, start_line - 1)
            
            # --- Simple per-tool chunk limit ---
            # Cap a single read to ~25% of the context window (in chars).
            # The orchestrator's _truncate_tool_result() handles the 95% context guard.
            max_input_tokens = 58000
            if hasattr(self, 'agent_pool') and self.agent_pool:
                llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
                pool_max = llm_cfg.get('max_input_tokens') or llm_cfg.get('generate_cfg', {}).get('max_input_tokens')
                if pool_max:
                    max_input_tokens = int(pool_max)
            agent_obj = kwargs.get('agent_obj')
            if agent_obj and hasattr(agent_obj, 'llm') and hasattr(agent_obj.llm, 'generate_cfg'):
                agent_max = agent_obj.llm.generate_cfg.get('max_input_tokens')
                if agent_max and agent_max != 58000:
                    max_input_tokens = int(agent_max)
            
            # 25% of context * ~2.5 chars/token (Hard Context Safety Limit)
            char_limit = int(max_input_tokens * 0.25 * 2.5)
            char_limit = max(500, char_limit)  # floor at 500 chars

            # If it's a candidate for wild read (no limit provided), 
            # we cap it at wild_limit to avoid being flagged by the orchestrator
            if is_wild_read_candidate:
                char_limit = min(char_limit, wild_limit)

            end_idx = min(total_lines, start_idx + limit)
            
            # Build content iteratively, respecting the chunk char limit
            content_lines = []
            current_chars = 0
            actual_end_idx = start_idx
            
            for i in range(start_idx, end_idx):
                line_text = f"{i+1}: {lines[i]}"
                if current_chars + len(line_text) > char_limit:
                    if current_chars == 0:
                        # First line is itself huge — include a truncated portion
                        cut = min(len(line_text), max(char_limit, 200))
                        content_lines.append(line_text[:cut] + " ... [LINE TRUNCATED]\n")
                        actual_end_idx = i + 1
                    break
                
                content_lines.append(line_text)
                current_chars += len(line_text)
                actual_end_idx = i + 1

            content = "".join(content_lines)
            header = f"File content ({path}), lines {start_idx+1} to {actual_end_idx} of {total_lines}:"
            
            if actual_end_idx < total_lines:
                header += " [TRUNCATED]"

            msg = f"{header}\n```\n{content}\n```"
            
            if actual_end_idx < total_lines:
                msg += (
                    f"\n\n[PAGINATION NOTE: This file is large. Use read_file with "
                    f"start_line={actual_end_idx+1} to read the next "
                    f"{min(limit, total_lines - actual_end_idx)} lines.]"
                )

            return msg
        except Exception as e:
            return f"Error reading file: {str(e)}"


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

    def __init__(self, cfg=None, **kwargs):
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
                if not str(resolved).startswith(str(base_dir.resolve())):
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


class WriteFile(BaseTool):
    """Writes content to a specified file in the local filesystem."""

    name = 'write_file'
    description = TOOL_METADATA['write_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'file_path': {
                'type': 'string',
                'description': "Path to the file, relative to the workspace root (e.g., 'src/main.py', 'output/result.txt')."
            },
            'content': {
                'type': 'string',
                'description': TOOL_METADATA['write_file']['parameters']['content']
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to create this file'
            }
        },
        'required': ['file_path', 'content'],
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
        import json
        from agent_cascade.utils.utils import extract_code, json_loads

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

        # Mapping for backward compatibility
        try:
            if isinstance(params, str):
                p = json_loads(params)
                if 'path' in p and 'file_path' not in p:
                    p['file_path'] = p['path']
                params = json.dumps(p)
            elif isinstance(params, dict):
                if 'path' in params and 'file_path' not in params:
                    params['file_path'] = params['path']
        except:
            pass

        # --- Standard JSON Path ---
        params_json = self._verify_json_format_args(params)
        path = params_json.get('file_path')
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


class EditFile(BaseTool):
    """Replaces text within a file."""

    name = 'edit_file'
    description = TOOL_METADATA['edit_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'file_path': {
                'type': 'string',
                'description': "Path to the file, relative to the workspace root (e.g., 'src/main.py')."
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
                'enum': ['exact', 'heuristic'],
                'default': 'exact',
                'description': TOOL_METADATA['edit_file']['parameters']['match_mode']
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to edit this file'
            }
        },
        'required': ['file_path', 'old_content', 'new_content'],
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    def call(self, params: str, **kwargs) -> str:
        import json
        from agent_cascade.utils.utils import extract_code, json_loads
        
        # Mapping for backward compatibility
        try:
            if isinstance(params, str):
                p = json_loads(params)
                if 'path' in p and 'file_path' not in p:
                    p['file_path'] = p['path']
                if 'old_string' in p and 'old_content' not in p:
                    p['old_content'] = p['old_string']
                if 'new_string' in p and 'new_content' not in p:
                    p['new_content'] = p['new_string']
                params = json.dumps(p)
            elif isinstance(params, dict):
                if 'path' in params and 'file_path' not in params:
                    params['file_path'] = params['path']
                if 'old_string' in params and 'old_content' not in params:
                    params['old_content'] = params['old_string']
                if 'new_string' in params and 'new_content' not in params:
                    params['new_content'] = params['new_string']
        except:
            pass

        params_json = self._verify_json_format_args(params)
        path = params_json.get('file_path')
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
            return "ERROR: Missing 'file_path'."
        if not old_content:
            return "ERROR: Missing 'old_content'. Please provide the exact text you want to replace."
        if new_content is None:
            return "ERROR: Missing 'new_content'. Please provide the text you want to replace old_content with."

        return self.agent_pool.operation_manager.edit_file(
            path=path,
            agent_name=self.agent_name,
            old_content=old_content,
            new_content=new_content,
            match_mode=match_mode,
        )


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
        return self.agent_pool.operation_manager.list_directory(path)


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

        # Get the truncation limit from agent/tool options
        char_limit = 2000
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('grep_char_limit', char_limit)
        elif self.cfg.get('grep_char_limit'):
            char_limit = self.cfg.get('grep_char_limit')

        agent_name = kwargs.get('agent_instance_name', 'unknown')
        return self.agent_pool.operation_manager.grep(pattern, path, include, char_limit=int(char_limit), agent_name=agent_name)


class DeleteFile(BaseTool):
    """Delete a file (requires user approval)."""

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


class CopyFile(BaseTool):
    """Copy a file or directory."""

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
    """Move a file or directory (requires user approval)."""

    name = 'move_file'
    description = TOOL_METADATA['move_file']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'source': {
                'type': 'string',
                'description': TOOL_METADATA['move_file']['parameters']['source']
            },
            'destination': {
                'type': 'string',
                'description': TOOL_METADATA['move_file']['parameters']['destination']
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
