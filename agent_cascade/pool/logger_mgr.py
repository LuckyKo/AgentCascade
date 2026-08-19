"""
LoggerManager — manages per-agent loggers. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.settings import DEFAULT_WORKSPACE
from agent_cascade.instance_id import get_instance_id, make_instance_dir
class LoggerManager:
    """Manages per-agent loggers. Returns real AgentInstanceLogger instances.

    Thread-safe via _lock for concurrent access during parallel agent execution.
    Log files are stored in <workspace_dir>/logs/ subdirectory.
    """

    def __init__(self, pool: AgentPool, workspace_dir: Optional[str]):
        self.pool = pool
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path(DEFAULT_WORKSPACE)
        # Instance-specific log directory
        instance_log_base = make_instance_dir(str(self.workspace_dir / "logs"))
        self.log_dir = Path(instance_log_base)

        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RuntimeError(f"Cannot create agent log directory {self.log_dir}: {e}") from e
        self._loggers: Dict[Tuple[str, str], Any] = {}  # (instance_name, agent_class.lower()) → logger instance
        self._lock = threading.Lock()  # Protects _loggers dict access

    def get_logger(self, instance_name: str, agent_class: str, base_metadata: Optional[Dict] = None):
        """Get or create a real AgentInstanceLogger for an instance.
        
        Uses composite key (instance_name, normalized agent_class) as defense-in-depth
        against case sensitivity mismatches in caller code.
        """
        with self._lock:
            # Defensive handling for None/empty agent_class
            normalized_agent_class = (agent_class or '').strip().lower()
            key = (instance_name, normalized_agent_class)
            if key not in self._loggers:
                from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
                self._loggers[key] = AgentInstanceLogger(
                    agent_class=agent_class,
                    instance_name=instance_name,
                    log_dir=str(self.log_dir),
                    base_metadata=base_metadata,
                )
            return self._loggers[key]

    def create_new_session(self, instance_name: str, agent_class: str) -> None:
        """Replace the logger for an instance with a fresh one (new timestamp = new JSONL file).

        Used by "New Session" to start writing to a new log file instead of appending.
        Closes the old logger's file handle before replacing it.
        
        Uses composite key (instance_name, normalized agent_class) for consistency.
        """
        with self._lock:
            # Defensive handling for None/empty agent_class
            normalized_agent_class = (agent_class or '').strip().lower()
            key = (instance_name, normalized_agent_class)
            # Close old logger's file handle if present
            if key in self._loggers:
                try:
                    self._loggers[key].close()
                except Exception as e:
                    logger.debug(f"Logger close during reinit failed for {instance_name} (non-critical): {e}")
            from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
            # New session gets fresh metadata — no inheritance from previous session's state.
            # FIX (todo.md:117): Root agents always have "User" as supervisor.
            self._loggers[key] = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(self.log_dir),
                base_metadata={"supervisor": "User"},
            )
        return
