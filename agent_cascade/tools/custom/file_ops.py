import atexit
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)
import io
import json

import requests
from PIL import Image

from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.settings import (DEFAULT_READ_FILE_MAX_LINES, DEFAULT_TOOL_RESULT_MAX_CHARS,
                                    DEFAULT_WILD_READ_TRUNCATION_CHARS)
from agent_cascade.tool_utils import set_truncation_hints
from agent_cascade.tools.base import BaseTool, register_tool
from agent_cascade.utils.media_utils import MediaStorageError, save_image_to_media
from agent_cascade.utils.utils import (_HTTP_FETCH_HEADERS, _HTTP_FETCH_TIMEOUT, MAX_DATA_URL_SIZE,
                                       encode_image_as_base64, is_http_url, json_loads)


def _is_temp_file(p) -> bool:
    """True if p points under the system tempdir.

    Used by view_image cleanup so a persistent media file is never unlinked, even
    if a future source ever pointed temp_png/crop_tmp at a non-temp location.
    """
    return str(p).startswith(tempfile.gettempdir())


class PathResolutionMixin:
    """Mixin providing _resolve_path() for all file-op tool classes."""

    def _resolve_path(self, path: str, mode: str = 'ro') -> Path:
        from agent_cascade.utils.tool_path_resolver import resolve_tool_path
        return resolve_tool_path(path, mode=mode, agent_pool=self.agent_pool)


# --- Module-level cairosvg DLL state (Windows only) --------------------------- #
_cairosvg_dll_handles: list = []  # handles returned by os.add_dll_directory()
_cairosvg_setup_done: bool = False  # ensures DLL setup runs exactly once


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
    os.environ.get('GTK_LIBS', ''),
    os.environ.get('CAIROCFFI_DLL_DIRECTORIES', ''),
    r'C:\Program Files\GTK3-Runtime Win64\bin',
    r'D:\Program Files\GTK3-Runtime Win64\bin',
    r'C:\Program Files (x86)\GTK3-Runtime Win64\bin',
]

# --- read_file constants ----------------------------------------------------- #
DEFAULT_READ_LINES = DEFAULT_READ_FILE_MAX_LINES  # From settings (default: 150)
MAX_LINE_LIMIT_EXPLICIT = 100000  # Max lines when user explicitly sets a limit
HEX_DUMP_BYTES = 1024  # Bytes to show in hex view for binary files
MAX_SINGLE_LINE_CHARS = 100_000  # Per-line memory guard: truncate pathological lines >100KB

# list_dir default output truncation limit (chars) before spillover is applied.
DEFAULT_LIST_DIR_CHAR_LIMIT = 3000


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
class ReadFile(BaseTool, PathResolutionMixin):
    """Reads and returns the content of a specified file.

    Text files are read line-by-line with a line cap (default 150 for wild reads,
    up to 100000 when limit is explicit). Wild reads that exceed the high-water mark
    (~2000 chars) are truncated post-hoc with an unbound-read warning. The outer
    safety net in _assemble_tool_result provides a final char-based truncation.
    Binary files display a hex dump of the first 1024 bytes with ASCII representation.
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
    #  Helper: determine line limit and whether this is a "wild read"     #
    # ------------------------------------------------------------------ #
    def _determine_limits(self, limit: Optional[int]) -> tuple[int, bool]:
        """Return (line_limit, is_wild_read). Wild reads have no explicit limit
        set by the caller; they use the default line count and are subject to a
        post-hoc high-water-mark truncation (see _read_text_file).

        Priority: explicit limit > -1 (unlimited) > default."""
        if limit == -1:
            return MAX_LINE_LIMIT_EXPLICIT, False
        elif limit is not None:
            return min(int(limit), MAX_LINE_LIMIT_EXPLICIT), False
        else:
            return DEFAULT_READ_LINES, True  # wild read — high-water mark applied post-hoc

    # ------------------------------------------------------------------ #
    #  Helper: read text file with streaming line-by-line iteration       #
    # ------------------------------------------------------------------ #
    def _read_text_file(
        self,
        path: str,
        resolved: Path,
        start_line: int,
        limit: int,
        is_wild_read: bool = False,
        wild_truncation: int = 0,
        char_threshold: int = DEFAULT_TOOL_RESULT_MAX_CHARS,
    ) -> str:
        """Read a text file using streaming line-by-line iteration.

        For wild reads (no explicit limit), if accumulated content exceeds the
        high-water mark (`char_threshold`, default ``DEFAULT_TOOL_RESULT_MAX_CHARS``),
        the output is truncated to the smaller cut point (`wild_truncation`) with an
        unbound-read warning. The two values are distinct: `char_threshold` is the
        trip threshold (when to warn), `wild_truncation` is where to cut.

        Returns formatted content string ready for the user.
        """
        total_lines = 0
        lines_read: list[str] = []
        hit_line_limit = False  # Truncated because we hit the line count limit

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

                # Per-line memory guard: truncate pathological lines (>100KB)
                if len(formatted) > MAX_SINGLE_LINE_CHARS:
                    formatted = formatted[:MAX_SINGLE_LINE_CHARS] + ' ... [LINE TRUNCATED]\n'

                lines_read.append(formatted)
                total_lines = line_num

        # Count actual file lines to distinguish empty file from out-of-range start_line
        if not lines_read and total_lines == 0:
            with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
                total_lines = sum(1 for _ in f)

        content = ''.join(lines_read)

        # Wild read high-water mark: if an unbound read exceeded the trip threshold,
        # truncate to that limit and flag it.
        wild_truncated = False
        displayed_lines = len(lines_read)
        if (is_wild_read and wild_truncation > 0 and char_threshold > 0 and len(content) > char_threshold):
            # Cut at the line boundary closest to (and below) the threshold.
            cut_pos = content.rfind('\n', 0, wild_truncation)
            if cut_pos > 0:
                content = content[:cut_pos]  # exclude the newline -> whole lines only
            else:
                # No newline before the threshold (single very long line): hard-cut.
                content = content[:wild_truncation] + ' ...\n'
            wild_truncated = True
            # Count displayed lines (the last line has no trailing newline after the cut).
            displayed_lines = content.count('\n') + (0 if content.endswith('\n') else 1)
            displayed_lines = max(1, displayed_lines)

        if not lines_read:
            if total_lines == 0:
                file_size = resolved.stat().st_size if resolved.exists() else 0
                return f"OK: Read {path} (0 lines, {file_size} B)"
            else:
                return f"ERROR: start_line {start_line} exceeds file length ({total_lines} lines)"

        actual_end = start_line + displayed_lines - 1

        # m1: Encoding warning via replacement character U+FFFD count
        repl_count = content.count('\ufffd')
        encoding_note = f" [encoding: utf-8 with {repl_count} replacement(s)]" if repl_count > 0 else ''

        # File size for text files (inline formatting, no external deps)
        try:
            file_size_bytes = resolved.stat().st_size
            if file_size_bytes < 1024:
                file_size_str = f"{file_size_bytes} B"
            elif file_size_bytes < 1024 * 1024:
                file_size_str = f"{file_size_bytes / 1024:.1f} KB"
            else:
                file_size_str = f"{file_size_bytes / (1024 * 1024):.1f} MB"
        except OSError:
            file_size_str = '?'

        header = f"OK: Read {path} lines {start_line}-{actual_end}/{total_lines} (text, {file_size_str}){encoding_note}"

        truncated_msg = ''
        if wild_truncated:
            header += ' [TRUNCATION WARNING: Unbound read detected!]'
            truncated_msg = (f"\n\n[SYSTEM]: Content exceeded the "
                             f"{char_threshold}-char high-water mark and was truncated to "
                             f"{wild_truncation} chars. "
                             f"Use start_line/limit for targeted reads."
                             f"\n→ continue at start_line={actual_end + 1}")
        elif hit_line_limit:
            header += ' [TRUNCATED]'
            # m2: Compact pagination footer
            truncated_msg = f"\n→ continue at start_line={actual_end + 1}"

        # Set truncation hints so the outer safety-net footer reports file line counts
        # instead of rendered-output line counts (which include header + code fences).
        set_truncation_hints(total_lines=total_lines, shown_lines=displayed_lines)

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
            return f"OK: Read {path} (binary, 0 B)"

        hex_view = _format_hex_dump(data)

        # Inline size formatting consistent with text files
        if file_size < 1024:
            size_str = f"{file_size} B"
        elif file_size < 1024 * 1024:
            size_str = f"{file_size / 1024:.1f} KB"
        else:
            size_str = f"{file_size / (1024 * 1024):.1f} MB"

        return (
            f"OK: Read {path} (binary, {size_str}) showing first {len(data)} bytes as hex dump\n```\n{hex_view}\n```")

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

        # Wild-read truncation uses two distinct values:
        #   - char_threshold (trip threshold): tool_result_max_chars (~25k) — the high-water mark.
        #   - wild_truncation (cut point): wild_read_truncation_chars (~2k).
        wild_truncation = DEFAULT_WILD_READ_TRUNCATION_CHARS
        char_threshold = DEFAULT_TOOL_RESULT_MAX_CHARS
        if self.agent_pool is not None:
            _cfg = getattr(self.agent_pool, 'llm_cfg', {}) or {}
            wild_truncation = _cfg.get('wild_read_truncation_chars', wild_truncation)
            char_threshold = _cfg.get('tool_result_max_chars', char_threshold)

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
                path=path,
                resolved=resolved,
                start_line=start_line,
                limit=limit,
                is_wild_read=is_wild_read,
                wild_truncation=wild_truncation,
                char_threshold=char_threshold,
            )

        except ValueError as e:
            # Path resolution errors (outside allowed directories)
            return f"ERROR: Path error for '{path}' — {e}"
        except PermissionError as e:
            return f"ERROR: Permission denied reading '{path}' — {e}"
        except OSError as e:
            # Catches FileNotFoundError, IOError, etc. — merged handler (Fix #6)
            return f"ERROR: OS error reading '{path}' — {e}"
        except Exception as e:
            logger.exception(f"Unexpected error reading file '{path}'")
            return f"ERROR: Reading file — {str(e)}"


@register_tool('view_image', allow_overwrite=True)
class ViewImage(BaseTool, PathResolutionMixin):
    """View an image file from the workspace or capture screen/window content.

    Supports optional crop_region parameter ("x,y,w,h") for viewing a specific
    area of large images in more detail. The caption includes original image
    dimensions so the LLM can plan further crops.
    """

    IMAGE_EXTENSIONS = {
        '.png',
        '.jpg',
        '.jpeg',
        '.gif',
        '.webp',
        '.bmp',
        '.svg',
        '.tiff',
        '.tif',
        '.ico',
        '.avif',
        '.heic',
        '.heif',
    }

    name = 'view_image'
    description = TOOL_METADATA['view_image']['description']
    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': TOOL_METADATA['view_image']['parameters']['path']
            },
            'crop_region': {
                'type': 'string',
                'description': TOOL_METADATA['view_image']['parameters']['crop_region']
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
        if _cairosvg_setup_done or sys.platform != 'win32':
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
            raise ImportError('cairosvg is required for SVG viewing. Install it with: pip install cairosvg')
        except OSError as exc:
            # Cairosvg may fail on Windows if GTK3 runtime is missing
            raise OSError(f"cairosvg native library error: {exc}. "
                          'On Windows you may need GTK3 runtime. '
                          'Install from: https://github.com/tschoonj/GTK3-Runtime-for-Windows/releases '
                          'or set the GTK_LIBS environment variable.')

        # Read SVG, convert to PNG bytes
        svg_bytes = svg_path.read_bytes()
        png_data = cairosvg.svg2png(bytestring=svg_bytes)

        # Write to a temp file so the existing image-serving pipeline can use it
        tmp_fd, tmp_png_path = tempfile.mkstemp(suffix='.png', prefix='svg_view_')
        os.close(tmp_fd)
        with open(tmp_png_path, 'wb') as f:
            f.write(png_data)

        return Path(tmp_png_path)

    # ------------------------------------------------------------------ #
    #  Helper: save capture PNG to temp file                              #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    #  Main call()                                                        #
    # ------------------------------------------------------------------ #

    def call(self, params: str, **kwargs):
        from agent_cascade.llm.schema import ContentItem
        from agent_cascade.tools.custom import screen_capture

        params = self._verify_json_format_args(params)
        path = params['path']
        crop_region_str = params.get('crop_region')  # optional "x,y,w,h"

        # Check SCREEN_CAPTURE_ENABLED flag (Fix #1)
        if os.environ.get('SCREEN_CAPTURE_ENABLED', 'True').lower() not in ('true', '1', 'yes'):
            return 'ERROR: Screen capture is disabled by operator configuration.'

        temp_png: Path | None = None  # track temp file for cleanup
        crop_tmp: Path | None = None  # track cropped temp file for cleanup
        try:
            # Check for screen capture directives BEFORE _resolve_path() to avoid path validation errors
            if path == '__screen_capture' or path.startswith('__screen_capture:'):
                monitor_index = None
                if path.startswith('__screen_capture:'):
                    idx_str = path[len('__screen_capture:'):]
                    try:
                        monitor_index = int(idx_str)
                        if monitor_index < 0:
                            raise ValueError('Monitor index must be non-negative')
                    except ValueError:
                        return 'ERROR: Invalid screen capture format. Use __screen_capture or __screen_capture:N where N is a non-negative integer.'

                try:
                    png_bytes = screen_capture.capture_screen(monitor_index=monitor_index)
                except ImportError as e:
                    return f"ERROR: {str(e)}"
                except ValueError as e:
                    return f"ERROR: {str(e)}"
                except Exception as e:
                    msg = str(e)
                    logger.exception('Screen capture failed for __screen_capture directive')
                    if 'display' in msg.lower():
                        return 'ERROR: Screen capture requires a graphical display. No display server detected.'
                    return f"ERROR: Screen capture failed: {msg}"

                # Save to temp file, then fall through to normal image handling (including captions)
                tmp_fd, tmp_png_path = tempfile.mkstemp(suffix='.png', prefix='capture_view_')
                os.close(tmp_fd)
                with open(tmp_png_path, 'wb') as f:
                    f.write(png_bytes)
                temp_png = Path(tmp_png_path)
                logger.info('Screen capture completed via __screen_capture directive' +
                            (f":{monitor_index}" if monitor_index is not None else ''))
                # Fall through with the temp file path so normal image processing applies

            elif path.startswith('__window_capture:'):
                pid_str = path[len('__window_capture:'):]
                try:
                    pid = int(pid_str)
                    if pid <= 0:
                        raise ValueError('PID must be positive')
                except ValueError:
                    return 'ERROR: Invalid window capture format. Use __window_capture:PID where PID is a positive integer.'

                try:
                    png_bytes = screen_capture.capture_window_by_pid(pid)
                except ImportError as e:
                    return f"ERROR: {str(e)}"
                except ValueError as e:
                    msg = str(e)
                    if 'No visible window found' in msg or 'No window found' in msg:
                        return f"ERROR: {msg}. The process may not have a UI or may be hidden."
                    return f"ERROR: {msg}"
                except RuntimeError as e:
                    msg = str(e)
                    if 'display' in msg.lower():
                        return 'ERROR: Screen capture requires a graphical display. No display server detected.'
                    return f"ERROR: {msg}"
                except Exception as e:
                    logger.exception(f"Window capture failed for PID {pid}")
                    return f"ERROR: Window capture for PID {pid} failed: {e}"

                # Save to temp file, then fall through to normal image handling (including captions)
                tmp_fd, tmp_png_path = tempfile.mkstemp(suffix='.png', prefix='capture_view_')
                os.close(tmp_fd)
                with open(tmp_png_path, 'wb') as f:
                    f.write(png_bytes)
                temp_png = Path(tmp_png_path)
                logger.info('Window capture completed via __window_capture:%d directive', pid)
                # Fall through with the temp file path so normal image processing applies

            elif is_http_url(path):
                # HTTP(S) image URL: download to a TEMPDIR file and fall through so the
                # existing pipeline treats it EXACTLY like a screen capture (skips
                # _resolve_path, gets dimensions, applies any crop_region, and the single
                # save_image_to_media call below persists it to the media folder ONCE).
                # Do NOT call save_image_to_media here — that would double-encode.
                try:
                    response = requests.get(
                        path,
                        headers=_HTTP_FETCH_HEADERS,
                        timeout=_HTTP_FETCH_TIMEOUT,
                    )
                    response.raise_for_status()

                    # Pre-download size guard: reject over-cap payloads via Content-Length
                    # BEFORE reading the body (cheap; avoids pulling a huge file down).
                    content_length = response.headers.get('Content-Length')
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except (TypeError, ValueError):
                            declared = None
                        if declared is not None and declared > MAX_DATA_URL_SIZE:
                            response.close()
                            return (f"ERROR: Image too large to download ({declared / (1024 * 1024):.1f} MB "
                                    f"> {MAX_DATA_URL_SIZE // (1024 * 1024)} MB cap): {path}")

                    image_bytes = response.content
                except requests.RequestException as e:
                    return f"ERROR: Failed to download image from {path}: {e}"

                # Post-download backstop: the Content-Length check above is a fast path only.
                # When the header is absent (or lies), an over-cap body would otherwise be
                # written to disk — reject it here on the actual byte count instead.
                if len(image_bytes) > MAX_DATA_URL_SIZE:
                    return (f"ERROR: Image too large to download ({len(image_bytes) / (1024*1024):.1f} MB "
                            f"> {MAX_DATA_URL_SIZE // (1024*1024)} MB cap): {path}")

                # Deliberate EARLY validation: open+load the bytes before writing any temp
                # file so a non-image body yields a clear "URL did not return a valid
                # viewable image" error up front, rather than failing deeper in the pipeline.
                # save_image_to_media validates again later; this is intentional, not redundant.
                try:
                    _pil_img = Image.open(io.BytesIO(image_bytes))
                    _pil_img.load()
                except Exception:
                    return f"ERROR: URL did not return a valid viewable image ({path})."

                tmp_fd, tmp_png_path = tempfile.mkstemp(suffix='.png', prefix='url_view_')
                os.close(tmp_fd)
                with open(tmp_png_path, 'wb') as f:
                    f.write(image_bytes)
                temp_png = Path(tmp_png_path)
                logger.info('Image downloaded from URL via view_image: %s', path)
                # Fall through with the temp file path so normal image processing applies

            try:
                resolved = self._resolve_path(path) if not temp_png else None
            except ValueError as e:
                return f"ERROR: {str(e)}"

            if temp_png:
                # Captured image — use the temp file directly
                resolved = temp_png
            elif not resolved.exists():
                return f"ERROR: Image not found: {path}"

            # Validate it's actually an image file (skip for captured PNGs)
            if not temp_png and resolved.suffix.lower() not in ViewImage.IMAGE_EXTENSIONS:
                return f"ERROR: '{path}' is not a recognized image file (supported: {', '.join(e.lstrip('.').upper() for e in sorted(ViewImage.IMAGE_EXTENSIONS))})"

            # SVG files need conversion to PNG (PIL/Pillow can't read SVG natively)
            if resolved.suffix.lower() == '.svg':
                temp_png = self._convert_svg_to_png(resolved)

            # Determine the source path for image processing (temp_png takes priority)
            source_path = str(temp_png) if temp_png else str(resolved)

            # Open the image ONCE: get dimensions AND perform the crop in the same context.
            orig_width, orig_height = None, None
            crop_x = crop_y = crop_w = crop_h = None  # parsed values for caption reuse
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(source_path) as img:
                    orig_width, orig_height = img.size

                    # Apply crop_region if provided (must happen BEFORE any resizing)
                    if crop_region_str:
                        try:
                            parts = [p.strip() for p in crop_region_str.split(',')]
                            if len(parts) != 4:
                                return f"ERROR: Invalid crop_region '{crop_region_str}'. Expected format: 'x,y,w,h' (4 comma-separated integers)."
                            x, y, w, h = (int(p) for p in parts)
                        except ValueError:
                            return f"ERROR: Invalid crop_region '{crop_region_str}'. All values must be integers. Example: '100,200,500,300'"

                        # Validate against actual image dimensions (always available here)
                        if x < 0 or y < 0:
                            return f"ERROR: crop_region coordinates must be non-negative. Got x={x}, y={y}."
                        if w <= 0 or h <= 0:
                            return f"ERROR: crop_region width and height must be positive. Got w={w}, h={h}."
                        if x + w > orig_width:
                            return f"ERROR: crop_region out of bounds. Region (x={x}, y={y}, w={w}, h={h}) extends beyond image width {orig_width}. Right edge would be at x={x + w}."
                        if y + h > orig_height:
                            return f"ERROR: crop_region out of bounds. Region (x={x}, y={y}, w={w}, h={h}) extends beyond image height {orig_height}. Bottom edge would be at y={y + h}."

                        # Perform the crop and save to a temp file
                        cropped = img.crop((x, y, x + w, y + h))
                        tmp_fd, crop_tmp_str = tempfile.mkstemp(suffix='.png', prefix='crop_view_')
                        os.close(tmp_fd)
                        cropped.save(crop_tmp_str, format='PNG')
                        crop_tmp = Path(crop_tmp_str)
                        source_path = str(crop_tmp)
                        crop_x, crop_y, crop_w, crop_h = x, y, w, h

            except Exception as e:
                # If we can't open the image for dimensions, that's non-fatal (caption will omit size).
                # But if crop_region was requested and we failed, it IS fatal.
                if crop_region_str:
                    return f"ERROR: Failed to process image for cropping '{crop_region_str}': {e}. Ensure the image is valid."
                # Otherwise continue without dimensions

            # Save image to media dir (path-based) with base64 fallback
            caption_parts = [f"Viewing image: {path}"]
            if orig_width is not None and orig_height is not None:
                caption_parts.append(f"({orig_width}x{orig_height})")
            if crop_x is not None:
                caption_parts.append(f"[cropped region x={crop_x},y={crop_y},w={crop_w},h={crop_h}]")
            caption = ' '.join(caption_parts)

            # Only TRANSIENT inserts (screen/window capture, SVG->PNG conversion, or a
            # crop) have no stable on-disk file to reuse — they MUST be saved so the agent
            # can re-view them later. When neither temp_png nor crop_tmp is set we are
            # viewing an existing plain image file directly, so reusing its path avoids
            # writing a redundant duplicate copy (save_image_to_media always mints a new
            # filename and never reuses an existing one).
            needs_save = bool(temp_png) or bool(crop_tmp)

            if not needs_save:
                # Existing on-disk file: reuse the already-resolved absolute path as the
                # media path. Normalize to forward slashes to match save_image_to_media's
                # return format so downstream consumers see a consistent path shape.
                media_path = str(resolved).replace('\\', '/')
                return [
                    # Leave the image item UNCAPTIONED so the return-path guard
                    # (_has_uncaptioned_images) triggers a genuine vision/LLM caption via
                    # caption_images(). The separate text item carries the descriptive line
                    # for text-only agents; it does NOT count as an image caption, so it
                    # cannot suppress real captioning. (Reverts 5089a51's pre-filled caption.)
                    ContentItem(image=media_path),
                    ContentItem(text=f"{caption} (existing file, no copy saved)")
                ]

            try:
                media_path = save_image_to_media(
                    image_source=source_path,
                    max_short_side=1080,
                )
                return [
                    # Leave the image item UNCAPTIONED so the return-path guard
                    # (_has_uncaptioned_images) triggers a genuine vision/LLM caption via
                    # caption_images(). The separate text item carries the descriptive line
                    # for text-only agents; it does NOT count as an image caption, so it
                    # cannot suppress real captioning. (Reverts 5089a51's pre-filled caption.)
                    ContentItem(image=media_path),
                    ContentItem(text=f"{caption} Saved to: {media_path}")
                ]
            except MediaStorageError as e:
                # Fallback to base64 if media storage fails (disk full, permissions, etc.)
                logger.warning(f"Media storage failed for view_image, falling back to base64: {e}")
                try:
                    base64_data_url = encode_image_as_base64(source_path, max_short_side_length=1080)
                except Exception as enc_err:
                    # Fall back to file:// URL if encoding also fails
                    logger.warning(f"Failed to encode image as base64 '{source_path}': {enc_err}. Using file:// URL.")
                    fallback_path = crop_tmp if crop_tmp else (temp_png if temp_png else resolved)
                    base64_data_url = str(fallback_path.as_uri())

                return [
                    # Leave uncaptioned so the return path generates a real vision/LLM
                    # caption (same rationale as the media-path branch above).
                    ContentItem(image=base64_data_url),
                    ContentItem(text=f"{caption} Media storage failed; served inline (base64). Source: {source_path}")
                ]
        except (ValueError, TypeError) as e:
            # SVG parse errors from cairosvg come through as ValueError/TypeError
            return f"ERROR: SVG parse error in '{path}': {e}"
        except Exception as e:
            logger.exception(f"Unexpected error viewing image '{path}'")
            return f"ERROR: Error viewing image: {str(e)}"
        finally:
            # Clean up the temp PNG file after serving (best-effort). Only delete files
            # under the system tempdir so a persistent media file is never unlinked.
            if temp_png and os.path.exists(temp_png) and _is_temp_file(temp_png):
                try:
                    os.remove(temp_png)
                except OSError:
                    pass  # non-critical cleanup failure
            # Clean up the cropped temp file (same tempdir-only guard).
            if crop_tmp and os.path.exists(crop_tmp) and _is_temp_file(crop_tmp):
                try:
                    os.remove(crop_tmp)
                except OSError:
                    pass  # non-critical cleanup failure


@register_tool('write_file', allow_overwrite=True)
class WriteFile(BaseTool, PathResolutionMixin):
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

    def _verify_json_format_args(self, params, strict_json=False):
        """Override to protect content from future sanitization regressions.

        Defensive safeguard: if per-value thinking block stripping is ever added back
        to the base class, this ensures write_file's content parameter stays untouched.
        Content starting with tag-like text (e.g., "<thinking>code here") would be
        corrupted by naive stripping.
        """
        params_json = super()._verify_json_format_args(params, strict_json)

        # Ensure content is preserved exactly as-is from parsed JSON.
        # This is a no-op now but protects against future base class changes.
        if 'content' in params_json:
            pass  # content already untouched since base doesn't sanitize values

        return params_json

    def call(self, params: str, **kwargs) -> str:
        import re

        from agent_cascade.utils.utils import extract_code

        # --- Robust Fallback for Non-JSON Input ---
        # Handles the case where the model emits "path\n```code```" instead of JSON
        if isinstance(params, str) and not params.strip().startswith('{'):
            match = re.search(r'^(?:path:?\s*)?([^\n`]+)\s*?\n*?```[^\n]*\n(.*?)\n?```', params.strip(),
                              re.DOTALL | re.IGNORECASE)
            if match:
                path = match.group(1).strip()
                content = match.group(2)
                return self.agent_pool.operation_manager.write_file(
                    path=path,
                    content=content,
                    agent_name=self._get_agent_name(kwargs),
                    justification='',  # Non-JSON fallback: no justification available from LLM
                )

        # --- Standard JSON Path ---
        params_json = self._verify_json_format_args(params)
        path = params_json.get('path')
        content = params_json.get('content', '')
        justification = params_json.get('justification', '')

        # Only strip markdown wrappers if content looks like it was JSON-embedded
        # (i.e., starts with ``` — this is a legacy fallback for when XML extraction
        # didn't happen and the model put a code block inside the JSON string)
        if isinstance(content, str) and content.strip().startswith('```'):
            content = extract_code(content)

        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.write_file(
            path=path,
            content=content,
            agent_name=agent_name,
            justification=justification,
        )


@register_tool('edit_file', allow_overwrite=True)
class EditFile(BaseTool, PathResolutionMixin):
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
            'range': {
                'type': 'string',
                'description': TOOL_METADATA['edit_file']['parameters']['range']
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to edit this file'
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
        range_param = params_json.get('range')
        justification = params_json.get('justification', '')

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

        if match_mode == 'delete_and_insert':
            if new_content is None:
                new_content = ''
        else:
            if not old_content:
                return "ERROR: Missing 'old_content'. Please provide the exact text you want to replace."
            if new_content is None:
                return "ERROR: Missing 'new_content'. Please provide the text you want to replace old_content with."

        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.edit_file(
            path=path,
            agent_name=agent_name,
            old_content=old_content,
            new_content=new_content,
            match_mode=match_mode,
            range_param=range_param,
            justification=justification,
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
            },
            'min_size': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['min_size']
            },
            'max_size': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['max_size']
            },
            'modified_after': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['modified_after']
            },
            'modified_before': {
                'type': 'string',
                'description': TOOL_METADATA['list_dir']['parameters']['modified_before']
            },
            'files_only': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['list_dir']['parameters']['files_only']
            },
            'dirs_only': {
                'type': 'boolean',
                'default': False,
                'description': TOOL_METADATA['list_dir']['parameters']['dirs_only']
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
        min_size = params.get('min_size')
        max_size = params.get('max_size')
        modified_after = params.get('modified_after')
        modified_before = params.get('modified_before')
        files_only = bool(params.get('files_only', False))
        dirs_only = bool(params.get('dirs_only', False))

        if files_only and dirs_only:
            return 'Error: files_only and dirs_only are mutually exclusive. Use only one.'

        # Get the truncation limit from agent/tool options
        char_limit = DEFAULT_LIST_DIR_CHAR_LIMIT
        if self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            if isinstance(llm_cfg, dict):
                val = llm_cfg.get('list_dir_char_limit')
                if isinstance(val, (int, float, str)):
                    try:
                        char_limit = int(val)
                    except (ValueError, TypeError):
                        pass
        elif isinstance(self.cfg, dict):
            val = self.cfg.get('list_dir_char_limit')
            if isinstance(val, (int, float, str)):
                try:
                    char_limit = int(val)
                except (ValueError, TypeError):
                    pass

        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.list_directory(
            path,
            recursive=recursive,
            max_depth=max_depth,
            include=include,
            exclude=exclude,
            sort_by=sort_by,
            show_summary=show_summary,
            max_entries=max_entries,
            char_limit=char_limit,
            agent_name=agent_name,
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
            modified_before=modified_before,
            files_only=files_only,
            dirs_only=dirs_only,
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
            },
            'timeout': {
                'type':
                    'number',
                'description':
                    'Timeout in seconds for the grep operation (default: 5.0). Searches normally finish well under this; only raise it for very large codebases.',
                'default':
                    5.0
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
        # FIX: Handle None/Null values properly for ignore_vcs
        # When JSON has "ignore_vcs": null, params.get('ignore_vcs', True) returns None (key exists)
        # We need to treat None as True (default behavior). Also handle string values.
        ignore_vcs = params.get('ignore_vcs')
        if ignore_vcs is None:
            ignore_vcs = True
        elif isinstance(ignore_vcs, str):
            ignore_vcs = ignore_vcs.lower() in ('true', '1', 'yes', 'on')
        else:
            ignore_vcs = bool(ignore_vcs)
        context = params.get('context', 0)
        smart_case = params.get('smart_case', True)
        # Default timeout comes from settings (DEFAULT_GREP_TIMEOUT); an explicit
        # caller value still overrides it.
        try:
            from agent_cascade.settings import DEFAULT_GREP_TIMEOUT as _default_grep_timeout
        except Exception:
            _default_grep_timeout = 5.0
        timeout = params.get('timeout', _default_grep_timeout)

        # Get the truncation limit from agent/tool options
        char_limit = 2000
        if hasattr(self, 'agent_pool') and self.agent_pool:
            llm_cfg = getattr(self.agent_pool, 'llm_cfg', {})
            char_limit = llm_cfg.get('grep_char_limit', char_limit)
        elif self.cfg.get('grep_char_limit'):
            char_limit = self.cfg.get('grep_char_limit')

        agent_name = self._get_agent_name(kwargs)
        spill_file_path = kwargs.get('spill_file_path')  # Pre-computed by orchestrator
        return self.agent_pool.operation_manager.grep(
            pattern,
            path,
            include,
            char_limit=int(char_limit),
            timeout=float(timeout),  # Pass the configurable timeout
            agent_name=agent_name,
            exclude=exclude,
            ignore_vcs=ignore_vcs,  # Already resolved to True/False, no need for bool()
            context=int(context),
            smart_case=bool(smart_case),
            spill_file_path=spill_file_path)


@register_tool('delete_file', allow_overwrite=True)
class DeleteFile(BaseTool):
    """Delete a file or directory — auto-approved for agent-owned files, otherwise requires user approval. Creates a timestamped backup before deletion."""

    name = 'delete_file'
    description = TOOL_METADATA['delete_file']['description']

    parameters = {
        'type': 'object',
        'properties': {
            'path': {
                # oneOf (not type: ['string','array']) for maximum LLM-API
                # compatibility — mirrors the load_skill tool's pattern.
                'oneOf': [
                    {
                        'type': 'string'
                    },
                    {
                        'type': 'array',
                        'items': {
                            'type': 'string'
                        }
                    },
                ],
                'description': TOOL_METADATA['delete_file']['parameters']['path']
            },
            'include': {
                'type': 'string',
                'description': TOOL_METADATA['delete_file']['parameters']['include']
            },
            'justification': {
                'type': 'string',
                'description': TOOL_METADATA['delete_file']['parameters']['justification']
            }
        },
        # 'path' (string or list) is required — enforced in call() so the error
        # message can name the missing input rather than failing schema validation.
    }

    def __init__(self, cfg=None, **kwargs):
        try:
            super().__init__(cfg)
        except (ValueError, TypeError):
            super().__init__()
        self.agent_pool = kwargs.get('agent_pool')
        self.agent_name = kwargs.get('agent_name')

    @staticmethod
    def _normalize_path(params):
        """Normalize the raw 'path' input BEFORE jsonschema validation.

        'path' accepts a single string OR a list of strings. A list is moved to
        the (hidden) 'paths' kwarg; non-string entries in a list are dropped so
        the strict oneOf schema (array of strings) validates cleanly. Returns
        (normalized_params, error_message); error_message is set for invalid
        scalar types instead of surfacing a raw jsonschema ValidationError.
        """
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, ValueError):
                return params, None  # let _verify_json_format_args report the parse error
            if not isinstance(params, dict):
                return params, None
        raw = params.get('path')
        if raw is None:
            return params, None
        if isinstance(raw, list):
            valid = [p for p in raw if isinstance(p, str) and p.strip()]
            params = dict(params)
            params['paths'] = valid
            del params['path']
            return params, None
        if not isinstance(raw, str):
            return params, "ERROR: 'path' must be a string or a list of strings."
        return params, None

    def call(self, params: str, **kwargs) -> str:
        # Normalize input BEFORE schema validation (see _normalize_path). After
        # normalization 'path' is either absent (list input → hidden 'paths') or
        # a plain string, so the oneOf schema validates cleanly.
        params, err = self._normalize_path(params)
        if err:
            return err
        params = self._verify_json_format_args(params)
        # After _normalize_path, 'path' is either absent (list input → hidden
        # 'paths') or a plain string. No 'required' in the schema on purpose:
        # list inputs legitimately omit it, so presence is checked here instead.
        raw = params.get('path')
        path = (raw or None) if isinstance(raw, str) else None
        paths = params.get('paths') or None  # hidden legacy arg, still accepted
        if not path and not paths:
            return "ERROR: Provide at least one of 'path' (string or list)."
        justification = params.get('justification', '')
        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.delete_file(
            path,
            agent_name,
            paths=paths,
            include=params.get('include'),
            justification=justification,
        )


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
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to copy this file'
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
        justification = params.get('justification', '')
        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.copy_file(source, destination, agent_name, justification=justification)


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
            },
            'justification': {
                'type': 'string',
                'description': 'Why you need to re-indent this file'
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
        justification = params_json.get('justification', '')

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

        agent_name = self._get_agent_name(kwargs)
        return self.agent_pool.operation_manager.re_indent(
            path=path,
            agent_name=agent_name,
            lines=lines,
            indent=indent,
            indent_type=type_,  # FIX 6: Changed from type= to indent_type=
            mode=mode,
            justification=justification,
        )
