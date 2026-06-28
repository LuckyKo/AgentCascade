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

# Re-export constants and module-level helpers so existing imports still work
from .approval import (
    OperationType,
    PendingApproval,
    SECURITY_ADVISOR_TIMEOUT_SECONDS,
    SECURITY_ADVISOR_WARNING_SECONDS,
)
from .path_security import (
    _path_is_contained_cached,
    _queue_tool_warning,
    _get_current_instance_name,
    set_current_instance_name,
    clear_current_instance_name,
)
from .grep import _compile_grep_pattern, _check_tool_availability

# Import mixins
from .approval import ApprovalMixin
from .path_security import PathSecurityMixin
from .file_operations import FileOpsMixin
from .grep import GrepMixin
from .shell import ShellMixin

from agent_cascade.settings import DEFAULT_WORKSPACE


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

        # File ownership tracking (still useful for context in approval UI)
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
            logger.debug("[Workspace] Base dir unchanged (%s), skipping notification", new_path)
        return False

    def set_extra_work_folders(self, folders_ro: List[str], folders_rw: List[str]):
        """Set extra directories that the agents can access."""
        import re
        from agent_cascade.log import logger

        new_folders_ro = []
        for folder in (folders_ro or []):
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                new_folders_ro.append(p)
            except Exception as e:
                logger.warning("Failed to resolve extra RO work folder %s: %s", folder, e)

        new_folders_rw = []
        for folder in (folders_rw or []):
            if not folder.strip():
                continue
            try:
                p = Path(folder.strip()).resolve()
                new_folders_rw.append(p)
            except Exception as e:
                logger.warning("Failed to resolve extra RW work folder %s: %s", folder, e)

        folders_changed = (frozenset(new_folders_ro) != frozenset(self.extra_work_folders_ro) or
                          frozenset(new_folders_rw) != frozenset(self.extra_work_folders_rw))

        if folders_changed:
            self.extra_work_folders_ro = new_folders_ro
            self.extra_work_folders_rw = new_folders_rw
            logger.info("[Workspace] Tiered folders updated: RO=%d, RW=%d", len(self.extra_work_folders_ro), len(self.extra_work_folders_rw))
            if self.agent_pool:
                self.agent_pool.notify_config_changed()
        else:
            logger.debug("[Workspace] Tiered folders unchanged, skipping config notification")