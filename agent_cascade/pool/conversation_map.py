"""
_InstanceConversationMapping — bridges writes to instance_conversations with instances[name].conversation. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER

if TYPE_CHECKING:  # pragma: no cover - annotation-only; avoids circular import of core.py
    from .core import AgentPool

class _InstanceConversationMapping(dict):
    """Custom dict that bridges writes to instance_conversations with instances[name].conversation.

    When api_server.py does `pool.instance_conversations[name] = some_list`, this class
    intercepts the write and also updates `pool.instances[name].conversation` to maintain
    bidirectional sync. Reads return the conversation from instances, not from dict storage.
    """

    def __init__(self, pool: 'AgentPool'):
        super().__init__()
        self._pool = pool

    def _sync_from_instances(self):
        """Rebuild the mapping from pool.instances.

        Preserves dict-only entries (from session rename patterns) that don't have
        corresponding instances — they would otherwise be lost on every sync.
        """
        # Save dict-only entries before clear
        dict_only = {}
        for key in super().keys():
            if key not in self._pool.instances:
                dict_only[key] = super().__getitem__(key)

        super().clear()
        for name, inst in self._pool.instances.items():
            with inst._compression_lock:
                super().__setitem__(name, list(inst.conversation))

        # Restore dict-only entries (e.g., renamed session names without instances)
        for key, val in dict_only.items():
            super().__setitem__(key, val)

    def __getitem__(self, key: str) -> List[Message]:
        """Read from instances[name].conversation, falling back to dict storage.

        Falls back to dict storage for entries created by pop+write rename patterns
        (e.g., set_session_name in api_server.py which pops an old name and writes
        the value under a new name without creating a corresponding instance).
        """
        inst = self._pool.instances.get(key)
        if inst is not None:
            with inst._compression_lock:
                return list(inst.conversation)
        # Fall back to dict storage for entries without instances
        try:
            return super().__getitem__(key)
        except KeyError:
            raise KeyError(key)

    def __setitem__(self, key: str, value: List[Message]) -> None:
        """Write propagates to instances[name].conversation AND dict storage.

        PR3 migration: Uses centralized rebuild_conversation() API for proper cache sync.
        If callers bypass this method (e.g., direct assignment), they must invalidate manually.
        See Fix #2 for details.
        """
        inst = self._pool.instances.get(key)
        if inst is not None:
            # PR3 migration: Use centralized API for conversation replacement with full cache sync
            inst.rebuild_conversation(list(value))  # defensive copy, handles all cache invalidation
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        """Delete from dict storage and clear conversation, but don't delete the instance."""
        super().__delitem__(key)
        inst = self._pool.instances.get(key)
        if inst is not None:
            inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync

    def get(self, key: str, default=None):
        """Get conversation for instance, returning default if not found."""
        try:
            return self[key]
        except KeyError:
            return default if default is not None else []

    def pop(self, key: str, *args):
        """Pop entry without deleting the underlying AgentInstance."""
        # Read the value from instances (source of truth)
        inst = self._pool.instances.get(key)
        if inst is None:
            if args:
                return args[0]
            raise KeyError(key)

        # Copy conversation data BEFORE clearing — prevents both data loss and races
        with inst._compression_lock:
            value = list(inst.conversation)
        
        # Use centralized API for cache sync (PR3 migration)
        inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync

        # Remove from dict storage only — don't delete the instance
        super().pop(key, None)

        return value  # Returns a copy of the old conversation data

    def items(self):
        """Return (name, conversation) pairs from instances + dict-only entries.

        Includes dict-only entries created by session rename patterns where the
        new name has no corresponding AgentInstance yet.
        """
        seen = set()
        for name, inst in self._pool.instances.items():
            with inst._compression_lock:
                yield name, list(inst.conversation)
            seen.add(name)
        # Also yield dict-only keys (e.g., renamed session names without instances)
        for key in super().keys():
            if key not in seen:
                yield key, super().__getitem__(key)

    def values(self):
        """Return conversation lists from instances + dict-only entries.

        Includes dict-only entries created by session rename patterns.
        """
        seen = set()
        for name, inst in self._pool.instances.items():
            with inst._compression_lock:
                yield list(inst.conversation)
            seen.add(name)
        # Also yield values for dict-only keys (e.g., renamed session names without instances)
        for key in super().keys():
            if key not in seen:
                yield super().__getitem__(key)

    def keys(self):
        """Return instance names from pool + any dict-only entries.

        Includes dict-only entries created by session rename patterns where the
        new name has no corresponding AgentInstance yet. Returns a view-like iterable
        rather than a static set to reflect live state.
        """
        seen = set()
        for name in self._pool.instances:
            yield name
            seen.add(name)
        # Also yield dict-only keys (e.g., renamed session names without instances)
        for key in super().keys():
            if key not in seen:
                yield key

    def __contains__(self, key):
        """Check if key exists in pool.instances OR dict storage."""
        if key in self._pool.instances:
            return True
        # Also check dict storage for entries without instances (e.g., session rename)
        try:
            super().__getitem__(key)
            return True
        except KeyError:
            return False

    def __iter__(self):
        """Iterate over all keys (from instances + any dict-only entries)."""
        seen = set()
        for name in self._pool.instances:
            yield name
            seen.add(name)
        # Also yield keys from dict storage that don't have instances (rename pattern)
        for key in super().__iter__():
            if key not in seen:
                yield key

    def clear(self):
        """Clear dict storage and conversations, but don't delete instances."""
        super().clear()
        for inst in self._pool.instances.values():
            inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
