"""Path resolution and security — cached containment check, thread-local helpers, and mixin."""

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Thread-local storage for current instance name during tool execution.
# Set by execution_engine before each tool call so _resolve_path can queue warnings.
_thread_locals = threading.local()


# ─── Module-level cached helpers ──────────────────────────────────────────

@lru_cache(maxsize=512)
def _path_is_contained_cached(path_str: str, container_str: str) -> bool:
    """Cached path containment check using os.path.commonpath().
    
    Prevents sibling-directory escape. Case-insensitive on all platforms.
    Cached to avoid repeated commonpath() calls during file operations.
    """
    try:
        common = os.path.commonpath([path_str, container_str])
        return common.lower() == container_str.lower()
    except ValueError:
        # Different drive letters on Windows (e.g., C:\ vs D:\)
        return False


# ─── Thread-local instance name helpers ───────────────────────────────────

def _queue_tool_warning(pool, instance_name: Optional[str], warning_text: str) -> None:
    """Queue a tool warning for the given agent instance.

    Appends *warning_text* to the instance's _tool_warnings list so it can be
    drained into the next tool response by execution_engine. Thread-safe via
    the instance's _compression_lock (reentrant RLock). Dedup guard prevents
    identical warnings from being queued multiple times in one drain cycle.

    Args:
        pool: AgentPool for looking up instances (or None — best-effort).
        instance_name: Agent instance name (None → no-op).
        warning_text: Warning string to queue.
    """
    if not instance_name or not pool:
        return

    try:
        inst = pool.get_instance(instance_name)
        if inst is not None:
            with inst._compression_lock:
                # Dedup guard — skip if exact same warning already queued (field is typed List[str])
                if warning_text in inst._tool_warnings:
                    return
                inst._tool_warnings.append(warning_text)
    except Exception:
        # Non-critical — warnings are best-effort hints for the agent
        from agent_cascade.log import logger
        logger.debug(f"Failed to queue tool warning for '{instance_name}'")
        pass


def _get_current_instance_name() -> Optional[str]:
    """Get the current agent instance name from thread-local storage.

    Set by execution_engine before each tool call via set_current_instance_name().
    Returns None if not set (e.g., called outside tool execution context).
    """
    return getattr(_thread_locals, 'instance_name', None)


def clear_current_instance_name() -> None:
    """Clear the current instance name from thread-local storage.

    Called after draining warnings to prevent stale references in subsequent calls.
    """
    _thread_locals.instance_name = None


def set_current_instance_name(name: str) -> None:
    """Set the current agent instance name in thread-local storage.

    Called by execution_engine before each tool call so that _resolve_path
    can queue warnings to the correct instance.
    """
    _thread_locals.instance_name = name


# ─── Mixin: Path resolution methods for OperationManager ──────────────────

class PathSecurityMixin:
    """Path resolution and security methods. Expects self to have __init__-set attributes."""

    @staticmethod
    def _path_is_contained(path: Path, container: Path) -> bool:
        """Check if *path* is inside *container* using the cached containment check."""
        return _path_is_contained_cached(str(path), str(container))

    def _resolve_path(self, path: str, mode: str = "ro", instance_name: Optional[str] = None) -> Path:
        """Resolve a path to be within the allowed directories (security).

        Args:
            path: The path string to resolve.
            mode: Access mode — "ro" for read-only, "rw" for read-write.
            instance_name: Agent instance name for queuing warnings when paths
                resolve from extra work folders instead of base_dir. If None,
                falls back to the thread-local current instance name.

        Returns:
            Resolved Path object within allowed directories.
        """
        # Resolve instance name: explicit param > thread-local > None
        if instance_name is None:
            instance_name = _get_current_instance_name()

        # Handle virtual /workspace/ prefix
        clean_path = path
        if clean_path.startswith('/workspace/'):
            clean_path = clean_path[len('/workspace/'):]
        elif clean_path.startswith('workspace/'):
            clean_path = clean_path[len('workspace/'):]
        elif clean_path == '/workspace' or clean_path == 'workspace':
            clean_path = '.'

        # If the path is already absolute (e.g., an agent passing
        # "N:\work\WD\AgentCascade" to access an extra work folder), use it directly
        # instead of joining with base_dir — on Windows, Path(base) / abs_path
        # replaces base entirely, which can cause security check mismatches.
        if Path(clean_path).is_absolute():
            resolved = Path(clean_path).resolve()
        else:
            # Try base_dir first
            resolved = (self.base_dir / clean_path).resolve()

            # If not found in base_dir, try extra work folders (RW then RO)
            if not resolved.exists():
                for extra in self.extra_work_folders_rw:
                    candidate = (extra / clean_path).resolve()
                    if candidate.exists():
                        _queue_tool_warning(self.agent_pool, instance_name, f"Path '{path}' was not found in workspace. Resolved from extra RW folder ({extra}): {candidate}")
                        resolved = candidate
                        break
                else:
                    for extra in self.extra_work_folders_ro:
                        candidate = (extra / clean_path).resolve()
                        if candidate.exists():
                            _queue_tool_warning(self.agent_pool, instance_name, f"Path '{path}' was not found in workspace. Resolved from extra RO folder ({extra}): {candidate}")
                            resolved = candidate
                            break

        # 1. Base directory is always RW (and thus RO)
        if self._path_is_contained(resolved, self.base_dir):
            return resolved

        # 2. Check extra RW folders (allowed for both RO and RW)
        for extra in self.extra_work_folders_rw:
            if self._path_is_contained(resolved, extra):
                return resolved

        # 3. Check extra RO folders (allowed only if mode is "ro")
        if mode == "ro":
            for extra in self.extra_work_folders_ro:
                if self._path_is_contained(resolved, extra):
                    return resolved

        raise ValueError(f"Path '{path}' is outside the allowed {mode.upper()} directories")