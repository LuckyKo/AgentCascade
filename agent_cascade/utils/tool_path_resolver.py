"""Shared path resolution helper for tool usage.

Provides a single function to resolve file paths against the allowed directory
set (base_dir + extra_rw + extra_ro). All tools should use this instead of
duplicating the resolution pattern.

Delegates to operation_manager when available; falls back to a minimal
resolution against DEFAULT_WORKSPACE when running standalone.

Note: fallback mode does not check extra work folders — only the base
workspace directory.
"""

import os
from pathlib import Path
from typing import Optional, Any

from agent_cascade.settings import DEFAULT_WORKSPACE


def resolve_tool_path(
    path: str,
    mode: str = "ro",
    agent_pool: Optional[Any] = None,
) -> Path:
    """Resolve *path* against the allowed directory set.

    Delegates to ``operation_manager._resolve_path`` when an agent_pool is
    available. Falls back to a minimal resolution against ``DEFAULT_WORKSPACE``
    otherwise (used by standalone tools and tests).

    Args:
        path: The path string to resolve. Can be relative (resolved against
            work_dir), absolute host paths, or prefixed with ``/workspace/``.
        mode: Access mode — ``"ro"`` for read-only, ``"rw"`` for read-write.
        agent_pool: The AgentPool instance (provides operation_manager).

    Returns:
        Resolved ``Path`` object within the allowed directories.

    Raises:
        ValueError: If the resolved path is outside the allowed directories.
    """
    # --- Fast path: operation_manager available (production use) ---
    if agent_pool is not None:
        om = getattr(agent_pool, 'operation_manager', None)
        if om is not None:
            return om._resolve_path(path, mode=mode)

    # --- Fallback: resolve against DEFAULT_WORKSPACE ---
    base_dir = Path(DEFAULT_WORKSPACE).resolve()

    # Strip /workspace/ prefix if present
    clean_path = path
    if clean_path.startswith('/workspace/'):
        clean_path = clean_path[len('/workspace/'):]
    elif clean_path.startswith('workspace/'):
        clean_path = clean_path[len('workspace/'):]
    elif clean_path in ('/workspace', 'workspace'):
        clean_path = '.'

    if Path(clean_path).is_absolute():
        resolved = Path(clean_path).resolve()
    else:
        resolved = (base_dir / clean_path).resolve()

    try:
        common = os.path.commonpath([str(resolved), str(base_dir)])
    except ValueError:
        raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")

    if common != str(base_dir):
        raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")

    return resolved