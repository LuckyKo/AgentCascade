"""
ParallelAgentManager — manages parallel agent execution state (active_stack). Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
class ParallelAgentManager:
    """Manages parallel agent execution state. Active_stack for tracking nested agent calls."""

    def __init__(self, pool: AgentPool):
        """Initialize the parallel agent manager.

        Args:
            pool: Reference to the AgentPool instance.
        """
        self.pool = pool
        self.active_stack: List[tuple] = []  # Stack of (instance_name, nest_depth) tuples for active agents
        # RLock (re-entrant) — compression can run in the same thread as outer ExecutionEngine.run()
        # which may already hold this lock. Using RLock prevents deadlock.
        self._state_lock = threading.RLock()
