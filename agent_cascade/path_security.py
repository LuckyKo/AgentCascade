"""Path security utilities for /api/file endpoint."""

import os
from pathlib import Path
from typing import List

from agent_cascade.settings import DEFAULT_WORKSPACE

# Sensitive filenames that should never be served
_SENSITIVE_FILENAMES = {".env", ".gitconfig", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}


def _get_allowed_file_roots() -> List[Path]:
    """Return the list of directories that /api/file is allowed to serve from.

    Includes:
      - Media directory (<workspace>/logs/media/ or <workspace>/logs_<instance>/media/)
      - Workspace root (DEFAULT_WORKSPACE)
    """
    from agent_cascade.instance_id import make_instance_dir
    from agent_cascade.settings import DEFAULT_WORKSPACE

    workspace_root = Path(DEFAULT_WORKSPACE)
    base_logs = str(workspace_root / "logs")
    instance_logs = make_instance_dir(base_logs)
    media_dir = Path(instance_logs) / "media"
    return [media_dir, workspace_root]


def _is_path_allowed(path: str) -> bool:
    """Check if a file path is allowed to be served via /api/file.

    URL-decodes and resolves the path, then verifies it falls under an allowed root
    using prefix matching with os.sep to avoid partial directory name matches.

    Args:
        path: The raw path string from the request (may be URL-encoded).

    Returns:
        True if the path is safe to serve, False otherwise.
    """
    from urllib.parse import unquote
    from agent_cascade.log import logger

    # URL-decode the path
    decoded = unquote(path)

    try:
        resolved = Path(decoded).resolve()
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to resolve path for /api/file: {decoded} ({e})")
        return False

    # Check filename-level restrictions
    basename = resolved.name.lower()

    # Block hidden files/dirs (starting with dot)
    if basename.startswith("."):
        return False

    # Block known sensitive filenames
    if basename in _SENSITIVE_FILENAMES:
        return False

    # Check that path is under an allowed root
    allowed_roots = _get_allowed_file_roots()
    resolved_str = str(resolved) + os.sep

    for root in allowed_roots:
        root_str = str(root.resolve()) + os.sep
        if resolved_str.startswith(root_str):
            return True

    logger.warning(f"Path outside allowed roots for /api/file: {decoded} (resolved: {resolved})")
    return False
