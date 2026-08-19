"""
AgentPool sub-package (Phase 2 split of agent_pool.py).

This init stays intentionally light — it re-exports only the public facade symbols. Heavy imports are deferred to the individual sub-modules.
"""

from .core import AgentPool
from .conversation_map import _InstanceConversationMapping

__all__ = ['AgentPool', '_InstanceConversationMapping']
