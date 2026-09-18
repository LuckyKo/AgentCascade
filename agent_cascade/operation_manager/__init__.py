"""Operation Manager — Facade for focused operation sub-modules.

Maintains the same API as the original monolithic OperationManager class
for full backward compatibility. The only importer is api_server.py which does:

    from agent_cascade.operation_manager import (
        OperationManager, SECURITY_ADVISOR_TIMEOUT_SECONDS, SECURITY_ADVISOR_WARNING_SECONDS
    )
"""

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from agent_cascade.settings import DEFAULT_WORKSPACE

# Import mixins
# Re-export constants and module-level helpers so existing imports still work
from .approval import SECURITY_ADVISOR_TIMEOUT_SECONDS  # noqa: F401  (re-exported for backward compat)
from .approval import SECURITY_ADVISOR_WARNING_SECONDS, ApprovalMixin, OperationType, PendingApproval
from .file_operations import FileOpsMixin
from .grep import _check_tool_availability  # noqa: F401  (re-exported for backward compat)
from .grep import GrepMixin, _compile_grep_pattern
from .path_security import PathSecurityMixin  # noqa: F401  (re-exported for backward compat)
from .path_security import (_get_current_instance_name, _path_is_contained_cached, _queue_tool_warning,
                            clear_current_instance_name, set_current_instance_name)
from .shell import ShellMixin


class OperationManager(ApprovalMixin, PathSecurityMixin, FileOpsMixin, GrepMixin, ShellMixin):
    """
    Manages blocking user-approval for tool operations.

    Facade delegating to focused sub-modules via mixin inheritance:
      - ApprovalMixin: approval types, pending approvals, timeout config
      - PathSecurityMixin: path resolution and containment checks
      - FileOpsMixin: read/write/edit/delete/copy/move/list directory
      - GrepMixin: file search (subprocess + Python fallback)
      - ShellMixin: shell command execution

    Maintains the same API as the original monolithic OperationManager for backward compatibility.
    """

    def __init__(self, base_dir: str = DEFAULT_WORKSPACE, agent_pool=None):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.agent_pool = agent_pool
        self.extra_work_folders_ro: List[Path] = []
        self.extra_work_folders_rw: List[Path] = []

        # Currently pending approvals (request_id -> PendingApproval)
        self.pending: Dict[str, PendingApproval] = {}

        # Lock for thread-safe access to pending dict
        self._lock = threading.Lock()

        # Dedicated lock for the file_ownership dict. The _lock above only guards
        # `pending`; ownership is mutated concurrently by multiple file ops, so it
        # needs its own lock to avoid lost updates / wrong attribution (A1).
        self._ownership_lock = threading.Lock()

        # File ownership tracking (still useful for context in approval UI)
        # Keys are normalized with os.path.normcase at storage time (see _own/_unown)
        # so lookups and comparisons are case/slash-insensitive on Windows.
        self.file_ownership: Dict[str, str] = {}

        # Track heuristic edit counts per file to warn about indentation drift
        # Key: resolved file path string, Value: count of heuristic edits
        self._heuristic_edit_counts: Dict[str, int] = {}

        # User toggleable timeout
        self.enable_timeout: bool = True
        self.approval_timeout_seconds: int = 300  # Default 5 minutes (can be overridden from UI)

        import atexit
        atexit.register(self.cleanup_backups)

    def set_base_dir(self, path: str):
        """Update the base workspace directory."""
        new_path = Path(path).resolve()
        if new_path != self.base_dir:
            self.base_dir = new_path
            self.base_dir.mkdir(parents=True, exist_ok=True)
            if self.agent_pool:
                self.agent_pool.notify_config_changed()
            return True
        else:
            from agent_cascade.log import logger
            logger.debug('[Workspace] Base dir unchanged (%s), skipping notification', new_path)
        return False

    def set_extra_work_folders(self, folders_ro: List[str], folders_rw: List[str]):
        """Set extra directories that the agents can access."""
        from agent_cascade.log import logger

        new_folders_ro = []
        for folder in (folders_ro or []):
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                new_folders_ro.append(p)
            except Exception as e:
                logger.warning('Failed to resolve extra RO work folder %s: %s', folder, e)

        new_folders_rw = []
        for folder in (folders_rw or []):
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                new_folders_rw.append(p)
            except Exception as e:
                logger.warning('Failed to resolve extra RW work folder %s: %s', folder, e)

        folders_changed = (frozenset(new_folders_ro) != frozenset(self.extra_work_folders_ro) or
                           frozenset(new_folders_rw) != frozenset(self.extra_work_folders_rw))

        if folders_changed:
            self.extra_work_folders_ro = new_folders_ro
            self.extra_work_folders_rw = new_folders_rw
            logger.info('[Workspace] Tiered folders updated: RO=%d, RW=%d', len(self.extra_work_folders_ro),
                        len(self.extra_work_folders_rw))
            if self.agent_pool:
                self.agent_pool.notify_config_changed()
                # Memory-hint rescan (feature: memory_hint, plan §6.2): vaults live under the
                # working dirs, so a folder change may add/remove vaults. Best-effort; the
                # manager's daemon worker also polls _config_version as a safety net.
                try:
                    mh_manager = getattr(self.agent_pool, 'memory_hint_manager', None)
                    if mh_manager is not None:
                        mh_manager.rescan_vaults()
                except Exception as e:  # noqa: BLE001 — best-effort, never break folder update
                    logger.debug('[MEMORY_HINT] rescan on folder change failed: %s', e)
        else:
            logger.debug('[Workspace] Tiered folders unchanged, skipping config notification')

    # ─── File ownership helpers (thread-safe, normalized) ──────────────────

    def _own(self, path, agent_name: str) -> None:
        """Record *agent_name* as owner of *path*.

        The key is stored normalized with os.path.normcase so that later lookups
        and comparisons are case/slash-insensitive (Decision #4: normcase-at-storage).
        All mutation of file_ownership goes through this helper under _ownership_lock.
        """
        key = os.path.normcase(str(path))
        with self._ownership_lock:
            self.file_ownership[key] = agent_name

    def _unown(self, paths) -> None:
        """Remove ownership entries for the given paths (normalized).

        Accepts a single path or an iterable of paths. Keys are normalized with
        os.path.normcase to match what _own stored. No-op for unknown keys.
        """
        if isinstance(paths, (str, Path)):
            paths = [paths]
        keys = {os.path.normcase(str(p)) for p in paths}
        with self._ownership_lock:
            for key in keys:
                self.file_ownership.pop(key, None)

    def _get_owner(self, path) -> Optional[str]:
        """Return the owner of *path* (normalized lookup), or None if unowned."""
        key = os.path.normcase(str(path))
        with self._ownership_lock:
            return self.file_ownership.get(key)

    def _unown_recursive(self, path) -> None:
        """Atomically remove the ownership entry for *path* and all entries under it.

        Used when deleting a directory: clears the dir's own key plus every child
        file/dir key in one locked pass so no stale keys leak and there is no
        lost-update window between scanning and removal (A1/A3). The comparison
        uses normcase on both sides so case/slash differences on Windows still match.
        """
        prefix = os.path.normcase(str(path)) + os.sep
        with self._ownership_lock:
            to_remove = [
                k for k in self.file_ownership.keys() if k == os.path.normcase(str(path)) or k.startswith(prefix)
            ]
            for key in to_remove:
                del self.file_ownership[key]
