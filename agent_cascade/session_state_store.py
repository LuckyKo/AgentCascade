"""Server-wide active-session persistence (config/session_state.json).

The active session name is server-wide state: it must survive a restart so the
UI resumes the same named session. It is stored in a dedicated file so it does
not pollute the LLM endpoint config (config/api_endpoints.json).

The file is runtime-generated and user-specific: ``config/session_state.json``
(and its ``.bak``) are listed in ``.gitignore``, and every write goes through
:func:`agent_cascade.utils.atomic_json.atomic_write_json` so a reader never
observes a partially written state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent_cascade.log import logger
from agent_cascade.utils.atomic_json import atomic_write_json

#: Default session name when no persisted state exists.
DEFAULT_SESSION_NAME = 'Maine'


def _read_existing(path: Path) -> dict:
    """Read the session-state file, returning {} when it is missing or unreadable.

    Merge-preserve readers (save_session_name, mark_settings_migrated) rely on this so
    a partial write or a corrupt file never raises and never loses unrelated keys.
    """
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"[session_state] Failed to read {path}: {e}")
    return {}


def _session_state_path() -> Path:
    """Path to config/session_state.json (sibling of api_endpoints.json).

    In tests, AGENT_CASCADE_TEST_CONFIG_DIR points at a per-test temp dir, so the
    state file is isolated from the real config/ (matching APIRouter's behavior) and no
    test can see another test's session state.
    """
    env_dir = os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    if env_dir:
        return Path(env_dir) / 'session_state.json'
    return Path(__file__).resolve().parent.parent / 'config' / 'session_state.json'


def load_session_name() -> str:
    """Load the persisted active session name, falling back to the default.

    Returns the stored name if present and non-empty, else ``DEFAULT_SESSION_NAME``.
    Never raises: a missing/corrupt file simply yields the default.
    """
    path = _session_state_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            name = data.get('session_name')
            if isinstance(name, str) and name.strip():
                return name
    except (OSError, json.JSONDecodeError) as e:
        logger.debug(f"[session_state] Failed to read {path}: {e}")
    return DEFAULT_SESSION_NAME


def save_session_name(name: str) -> bool:
    """Persist the active session name (atomic write). Returns True on success."""
    path = _session_state_path()
    try:
        existing = _read_existing(path)
        existing['session_name'] = name
        atomic_write_json(path, existing)
        # Session name is a settings persist path — bump the shared revision.
        from agent_cascade.runtime_state import state as _runtime_state
        _runtime_state.bump_settings_revision()
        return True
    except OSError as e:
        logger.error(f"[session_state] Failed to save session name to {path}: {e}")
        return False


#: Sentinel key recording that the one-time localStorage->server migration ran.
_SETTINGS_MIGRATED_KEY = '_settings_migrated_v1'


def is_settings_migrated() -> bool:
    """True if the server has already run the one-time localStorage->server migration.

    Reader counterpart of :func:`mark_settings_migrated`. The migration handler uses it
    to skip a redundant pass when a second client (or a headless run) sends the local
    blob again after the server already migrated.
    """
    return bool(_read_existing(_session_state_path()).get(_SETTINGS_MIGRATED_KEY))


def mark_settings_migrated() -> bool:
    """Record the one-time localStorage→server migration sentinel.

    Writes ``_settings_migrated_v1: true`` into ``config/session_state.json`` (atomic,
    merge-preserve). Idempotent: a second call is a no-op. Returns True on success.
    """
    path = _session_state_path()
    try:
        existing = _read_existing(path)
        if existing.get(_SETTINGS_MIGRATED_KEY):
            return True  # already marked
        existing[_SETTINGS_MIGRATED_KEY] = True
        atomic_write_json(path, existing)
        return True
    except OSError as e:
        logger.error(f"[session_state] Failed to mark settings migrated: {e}")
        return False
