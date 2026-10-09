"""Path security utilities for /api/file endpoint."""

import os
from pathlib import Path
from typing import Iterable, List, Optional

# Sensitive filenames that should never be served
_SENSITIVE_FILENAMES = {'.env', '.gitconfig', 'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519'}


def _get_allowed_file_roots(extra_roots: Optional[Iterable[Path]] = None) -> List[Path]:
    """Return the list of directories that /api/file is allowed to serve from.

    Includes:
      - Media directory (<workspace>/logs/media/ or <workspace>/logs_<instance>/media/)
      - Workspace root (DEFAULT_WORKSPACE)
      - Any caller-supplied ``extra_roots`` (session extra work folders), appended last.

    Index contract is preserved: media is always ``[0]`` and the workspace root is always
    ``[1]``; ``extra_roots`` are strictly appended after them. Entries that are ``None``
    or empty strings are dropped defensively, and each remaining entry is wrapped in
    ``Path(...)`` so callers may pass strings.
    """
    from agent_cascade.instance_id import make_instance_dir
    from agent_cascade.settings import DEFAULT_WORKSPACE

    workspace_root = Path(DEFAULT_WORKSPACE)
    base_logs = str(workspace_root / 'logs')
    instance_logs = make_instance_dir(base_logs)
    media_dir = Path(instance_logs) / 'media'

    roots = [media_dir, workspace_root]
    if extra_roots:
        for extra in extra_roots:
            if extra is None:
                continue
            if isinstance(extra, str) and not extra.strip():
                continue
            roots.append(Path(extra))
    return roots


def _is_within(child: Path, root: Path) -> bool:
    """Return True if ``child`` is the same as, or located under, ``root``.

    Normalizes both sides via ``normcase(normpath(...))``, then re-appends ``os.sep``
    to each before the ``startswith`` comparison. Re-appending the separator prevents a
    sibling-prefix escape (root ``.../WD`` must NOT match ``.../WDEvil``); a bare
    ``normpath`` would strip it and silently re-open that hole.
    """
    child_norm = os.path.normcase(os.path.normpath(str(child)))
    root_norm = os.path.normcase(os.path.normpath(str(root)))
    if child_norm == root_norm:
        return True
    return (child_norm + os.sep).startswith(root_norm + os.sep)


def _is_path_allowed(path: str, extra_roots: Optional[Iterable[Path]] = None) -> bool:
    """Check if a file path is allowed to be served via /api/file.

    URL-decodes and resolves the path, then verifies it falls under an allowed root
    using prefix matching with os.sep to avoid partial directory name matches.

    Args:
        path: The raw path string from the request (may be URL-encoded).
        extra_roots: Optional extra directories (session extra work folders) to also
            allow. Forwarded to ``_get_allowed_file_roots`` and appended after the
            built-in media/workspace roots.

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
    if basename.startswith('.'):
        return False

    # Block known sensitive filenames
    if basename in _SENSITIVE_FILENAMES:
        return False

    # Check that path is under an allowed root
    allowed_roots = _get_allowed_file_roots(extra_roots)
    for root in allowed_roots:
        try:
            root_resolved = root.resolve()
        except (OSError, ValueError):
            continue
        if _is_within(resolved, root_resolved):
            return True

    logger.warning(
        f"Path outside allowed roots for /api/file: {decoded} (resolved: {resolved}) "
        f"(checked {len(allowed_roots)} roots)"
    )
    return False
