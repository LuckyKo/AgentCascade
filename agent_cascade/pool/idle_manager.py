"""
IdleManager — idle detection and auto-dismissal. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from ..agent_instance import AgentInstance, PoolSettings, AgentState, ACTIVE_STATES

if TYPE_CHECKING:  # pragma: no cover - annotation-only; avoids circular import of core.py
    from .core import AgentPool

class IdleManager:
    """Manages idle detection and auto-dismissal of agents.

    Runs a background daemon thread that periodically checks for agents that have
    been inactive longer than the configured timeout. Auto-dismissed agents have
    their conversations cleared and dismissal callbacks fired for real-time UI tab removal.

    Safety rules:
    - Never dismisses the main orchestrator (parent_instance is None)
    - Never dismisses active agents (in active_stack)
    - Never dismisses halted agents (intentionally paused)
    """

    def __init__(self, pool: AgentPool):
        self.pool = pool
        self._stop_event = threading.Event()
        self._checker_thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background idle checker thread."""
        if self._checker_thread is not None and self._checker_thread.is_alive():
            return  # Already running
        self._stop_event.clear()
        self._checker_thread = threading.Thread(
            target=self._checker_loop,
            name="IdleAgentChecker",
            daemon=True,
        )
        self._checker_thread.start()

    def stop(self):
        """Signal the checker to stop (non-blocking).

        Sets the stop event so the checker loop exits on its next iteration.
        Does NOT join the thread — that would block the calling thread for up to
        (idle_check_interval + 5) seconds when called from an async handler.
        Use stop_and_join() for blocking cleanup during server shutdown.
        """
        self._stop_event.set()

    def stop_and_join(self, timeout: Optional[float] = None):
        """Signal the checker and wait for it to exit (blocking).

        Used during full server shutdown where blocking is acceptable.
        Falls back to idle_check_interval + 5s default if no timeout given.

        Note: After calling this method, _checker_thread is set to None. Calling
        start() again will create a new thread.
        """
        self.stop()
        if self._checker_thread is not None and self._checker_thread.is_alive():
            join_timeout = timeout if timeout is not None else self.pool.settings.idle_check_interval + 5.0
            self._checker_thread.join(timeout=join_timeout)
            if self._checker_thread.is_alive():
                logger.warning("Idle checker thread did not exit in time.")
        self._checker_thread = None

    @staticmethod
    def _is_system_agent(agent_class: str) -> bool:
        """Check if agent class is a system-invoked type (Compressor, Security)."""
        return agent_class.lower() in ('security', 'compressor')

    def _checker_loop(self):
        """Background loop that periodically checks for and dismisses idle agents."""
        while not self._stop_event.is_set():
            try:
                # Snapshot instance names to avoid holding locks during check
                inst_names = list(self.pool.instances.keys())
                dismissed_this_round = []

                for name in inst_names:
                    if self._stop_event.is_set():
                        break
                    try:
                        if self._is_idle(name):
                            self._auto_dismiss(name)
                            dismissed_this_round.append(name)
                    except Exception as e:
                        logger.error(f"[idle_checker] Error processing '{name}': {e}", exc_info=True)

                if dismissed_this_round:
                    logger.info(
                        f"[idle_checker] Auto-dismissed {len(dismissed_this_round)} idle agent(s): "
                        f"{', '.join(dismissed_this_round)}"
                    )
            except Exception as e:
                logger.error(f"[idle_checker] Loop error: {e}", exc_info=True)

            # Wait for next check interval (or until stop event fires)
            self._stop_event.wait(timeout=self.pool.settings.idle_check_interval)

    def _is_idle(self, instance_name: str) -> bool:
        """Determine whether an agent is idle and eligible for auto-dismissal."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return False

        # Never dismiss the main orchestrator (no parent)
        if inst.parent_instance is None:
            return False

        # Read state, last_activity, and agent_class under same lock for consistency (prevents TOCTOU race)
        with inst._state_lock:
            state = inst.state
            last_activity = inst.last_activity
            agent_class = inst.agent_class

        # Must NOT be sleeping (waiting for async results)
        if state == AgentState.SLEEPING:
            return False

        # Must NOT be actively running
        with self.pool._execution._state_lock:
            if any(n == instance_name for n, _depth in self.pool._execution.active_stack):
                return False

        # Must NOT be halted (halted agents are intentionally paused, e.g. during compression)
        if self.pool.is_instance_halted(instance_name):
            return False

        # Must have exceeded the idle timeout threshold
        # System agents (Compressor, Security) use a separate timeout setting
        idle_secs = time.monotonic() - last_activity
        is_system_agent = IdleManager._is_system_agent(agent_class)
        effective_timeout = self.pool.settings.system_agent_idle_timeout_seconds if is_system_agent else self.pool.settings.idle_timeout_seconds

        # 0 means "off" — never auto-dismiss; NaN/inf treated as always idle
        if effective_timeout == 0:
            return False

        if idle_secs < effective_timeout:
            return False

        return True

    def _auto_dismiss(self, instance_name: str):
        """Dismiss a single idle agent and clean up its resources."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return

        idle_secs = time.monotonic() - inst.last_activity

        # Determine which timeout threshold applies to this agent
        is_system_agent = IdleManager._is_system_agent(inst.agent_class)
        effective_timeout = self.pool.settings.system_agent_idle_timeout_seconds if is_system_agent else self.pool.settings.idle_timeout_seconds

        # Capture log path before clearing
        log_path = None
        try:
            log_inst = self.pool._logger.get_logger(instance_name, inst.agent_class)
            log_path = getattr(log_inst, 'log_path', None)
        except Exception as e:
            logger.debug(f"Idle checker log path lookup failed for {instance_name} (non-critical): {e}")

        agent_type_label = f"system agent ({inst.agent_class})" if is_system_agent else "agent"
        logger.info(
            f"[idle_checker] Auto-dismissing idle {agent_type_label} '{instance_name}' "
            f"(idle for {idle_secs:.0f}s, threshold={effective_timeout:.0f}s)"
        )

        # Remove the instance (fires dismissal callbacks)
        self.pool.dismiss_instance(instance_name)