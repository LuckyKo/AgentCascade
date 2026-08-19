"""
Lean Agent Pool — Phase 1 of the AgentCascade Architecture Rewrite.

Replaces the old god-object AgentPool (~25 attributes, ~1100 lines) with a thin
coordinator that owns only the instance registry, template registry, and simple
state structures. Logger lifecycle, idle detection, and parallel execution are
delegated to focused managers (LoggerManager, IdleManager, ParallelAgentManager).

See DESIGN_REWRITE.md §2.2 for design rationale.

Phase 2 module-split: the implementation now lives in the ``agent_cascade.pool``
sub-package (see ``pool/core.py`` and its mixins). This file is a thin facade that
preserves the historical import surface — production code and tests continue to do
``from agent_cascade.agent_pool import AgentPool`` / ``_InstanceConversationMapping``.
"""

from __future__ import annotations

# Public facade symbols (the only names production/tests import from this module).
from .pool.core import AgentPool
from .pool.conversation_map import _InstanceConversationMapping

# Re-export the internal managers for backward compatibility (harmless; not part of
# the production import surface but previously importable from this module).
from .pool.parallel_manager import ParallelAgentManager
from .pool.logger_mgr import LoggerManager
from .pool.idle_manager import IdleManager

__all__ = [
    "AgentPool",
    "_InstanceConversationMapping",
    "ParallelAgentManager",
    "LoggerManager",
    "IdleManager",
]
