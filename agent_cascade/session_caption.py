"""Session display caption extraction and caching (shared by api_server and snapshot).

Avoids duplicate implementation and event-loop blocking by reading only the header metadata
or early user message from session JSONL files without parsing the whole log.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Optional, Union

from agent_cascade.llm.schema import USER
from agent_cascade.log import logger
from agent_cascade.utils.utils import extract_text_from_message

# Display length cap for the /api/sessions caption fallback (first USER message). A first
# user message can be a long prompt or a pasted file, so it is never returned raw. 120 chars
# is a UI display heuristic — roughly one readable line in the session-list column; not a
# protocol limit, so a plain module constant (no env override) is intentional.
_SESSION_CAPTION_MAX_LEN = 120

# Cache for read_session_caption: key = (path_str, mtime, size) -> caption string.
# Avoids re-reading unchanged log files on every call.
_caption_cache: dict[tuple[str, float, int], str] = {}
_CAPTION_CACHE_MAX = 2000  # cap to prevent unbounded growth
_caption_cache_lock = threading.Lock()


def _caption_cache_get(path_str: str, mtime: float, size: int) -> Optional[str]:
    """Return cached caption or None on miss."""
    with _caption_cache_lock:
        return _caption_cache.get((path_str, mtime, size))


def _caption_cache_set(path_str: str, mtime: float, size: int, caption: str) -> None:
    """Store caption; evict oldest entries if over cap (simple FIFO by insertion)."""
    with _caption_cache_lock:
        if len(_caption_cache) >= _CAPTION_CACHE_MAX:
            # Remove first 10% of entries (dict preserves insertion order)
            for k in list(_caption_cache.keys())[:_CAPTION_CACHE_MAX // 10]:
                del _caption_cache[k]
        _caption_cache[(path_str, mtime, size)] = caption


def truncate_caption(text: str) -> str:
    """Collapse whitespace and truncate ``text`` to ``_SESSION_CAPTION_MAX_LEN`` chars.

    Appends an ellipsis (``…``) when the text was cut. Returns "" for falsy input.
    """
    if not text:
        return ''
    collapsed = ' '.join(text.split())
    if len(collapsed) <= _SESSION_CAPTION_MAX_LEN:
        return collapsed
    return collapsed[:_SESSION_CAPTION_MAX_LEN].rstrip() + '…'


def read_session_caption(path: Union[str, Path], mtime: Optional[float] = None,
                         size: Optional[int] = None, max_scan_lines: int = 200) -> str:
    """Read a session log's display caption without loading the whole file.

    Results are cached by (path, mtime, size) so unchanged files are O(1) on repeat calls.
    If mtime/size are not provided, stat() is queried to populate the cache if possible.
    """
    p = Path(path)
    if not p.exists():
        return ''
    if mtime is None or size is None:
        try:
            st = p.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            pass

    path_str = str(p)
    if mtime is not None and size is not None:
        cached = _caption_cache_get(path_str, mtime, size)
        if cached is not None:
            return cached

    caption = read_session_caption_uncached(p, max_scan_lines)

    if mtime is not None and size is not None:
        _caption_cache_set(path_str, mtime, size, caption)
    return caption


def read_session_caption_uncached(path: Union[str, Path], max_scan_lines: int = 200) -> str:
    """Read a session log's display caption without loading the whole file (no caching).

    Semantics (single "caption" field for the UI):
      1. ``metadata.caption`` from line 1, if non-empty (the compressor-generated caption).
      2. Otherwise, the first USER message's text (scanned forward from line 2, stopping
         early — at most ``max_scan_lines`` lines) truncated to ~120 chars with an ellipsis.
      3. Otherwise "" (no caption and no user message found / unreadable file).
    """
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            first_line = f.readline()
            if first_line:
                try:
                    meta = json.loads(first_line)
                    cap = (meta.get('metadata') or {}).get('caption', '')
                    if isinstance(cap, str) and cap.strip():
                        return truncate_caption(cap)
                except (json.JSONDecodeError, AttributeError):
                    pass

            for _ in range(max_scan_lines):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get('role') == USER:
                    text = extract_text_from_message(item, add_upload_info=False)
                    if text and text.strip():
                        return truncate_caption(text)
    except Exception as e:
        logger.debug('Failed to read session caption for %s: %s', path, e)
    return ''
