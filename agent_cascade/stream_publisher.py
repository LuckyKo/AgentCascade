"""
StreamPublisher — Phase 4.4 of the AgentCascade Architecture Rewrite.

Handles all WebSocket push logic for real-time sub-agent visibility in the frontend.
Extracted from ExecutionEngine to reduce coupling and improve testability.

See audit_reports/execution_engine_refactor_plan.md §4.4 for design rationale.
"""

import asyncio
import time
from typing import List, TYPE_CHECKING

from agent_cascade.log import logger

if TYPE_CHECKING:
    from agent_cascade.agent_instance import AgentInstance
    from agent_cascade.execution_engine import ExecutionEngine


class StreamPublisher:
    """Manages WebSocket stream updates for sub-agent visibility.
    
    Extracts all WebSocket push logic from ExecutionEngine._create_and_run_agent()
    to reduce coupling and enable independent testing of streaming behavior.
    
    Uses lazy initialization pattern: set via pool in __init__, then engine reference
    is set by ExecutionEngine.initialize() after construction completes.
    
    Attributes:
        pool: Reference to AgentPool for settings access and WebSocket queue/loop.
        _engine: Lazy-initialized reference to parent ExecutionEngine.
        _error_count: Consecutive WebSocket push failure counter.
        _pushing_disabled: Flag set True after max_errors consecutive failures.
    """
    
    def __init__(self, pool):
        """Initialize StreamPublisher with pool reference.
        
        Args:
            pool: The AgentPool instance for settings and WebSocket access.
        """
        self.pool = pool
        self._engine = None
        self._error_count = 0
        self._pushing_disabled = False
    
    @property
    def engine(self) -> 'ExecutionEngine':
        """Get the parent ExecutionEngine reference.
        
        Returns:
            ExecutionEngine instance.
            
        Raises:
            RuntimeError: If _engine not set (initialize() not called).
        """
        if self._engine is None:
            raise RuntimeError("StreamPublisher._engine not set")
        return self._engine
    
    def set_engine(self, engine: 'ExecutionEngine') -> None:
        """Set the parent ExecutionEngine reference.
        
        Called by ExecutionEngine.initialize() after __init__ completes
        to break circular dependency.
        
        Args:
            engine: The ExecutionEngine instance that owns this publisher.
        """
        self._engine = engine
    
    @property
    def max_errors(self) -> int:
        """Get maximum consecutive WebSocket errors before disabling pushes.
        
        Returns:
            Integer limit from pool settings, defaults to 3.
        """
        return getattr(self.pool.settings, 'subagent_ws_max_errors', 3)
    
    def push_initial_state(
        self,
        instance: 'AgentInstance',
        caller: str
    ) -> None:
        """Push initial state for new sub-agent (tab appears in UI).
        
        Extracts from _create_and_run_agent() L2987-3007.
        Called after AgentInstance creation to make the sub-agent tab appear
        immediately in the frontend before execution begins.
        
        Args:
            instance: The newly created AgentInstance.
            caller: Root instance name for header stats.
            
        Note:
            All exceptions are caught and logged at debug level. Error counting
            tracks consecutive failures; after max_errors, pushing is disabled.
        """
        if self._pushing_disabled:
            return
        
        try:
            ws_queue = getattr(self.pool, '_ws_send_queue', None)
            ws_loop = getattr(self.pool, '_ws_loop', None)
            if not (ws_queue and ws_loop and not ws_loop.is_closed()):
                return
            
            from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
            su = build_stream_update_from_pool(
                pool=self.pool,
                instance_name=caller,  # Root instance for header stats
                responses=None,        # Reads full conversations from pool
            )
            if su is not None:
                asyncio.run_coroutine_threadsafe(
                    _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                    ws_loop,
                )
            self._error_count = 0  # Reset on success
            
        except Exception as e:
            self._error_count += 1
            logger.debug(f"WebSocket push failed for {instance.instance_name}: {e}")
            if self._error_count >= self.max_errors:
                self._pushing_disabled = True
    
    def push_periodic_update(
        self,
        caller: str
    ) -> None:
        """Push periodic stream update during execution (throttled).
        
        Extracts from _create_and_run_agent() L3076-3115.
        Called every ~150ms during sub-agent execution to keep the frontend
        updated with real-time progress. Error counting and disable-after-failures
        logic included.
        
        Args:
            caller: Root instance name for header stats.
            
        Note:
            Throttling (time-based check) is handled by the caller. This method
            performs the actual WebSocket push if not disabled.
        """
        if self._pushing_disabled:
            return
        
        try:
            ws_queue = getattr(self.pool, '_ws_send_queue', None)
            ws_loop = getattr(self.pool, '_ws_loop', None)
            if not (ws_queue and ws_loop and not ws_loop.is_closed()):
                return
            
            from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
            su = build_stream_update_from_pool(
                pool=self.pool,
                instance_name=caller,  # Root instance for header stats
                responses=None,        # Reads full conversations from pool
            )
            if su is not None:
                asyncio.run_coroutine_threadsafe(
                    _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                    ws_loop,
                )
            self._error_count = 0  # Reset on success
            
        except Exception as e:
            self._error_count += 1
            logger.debug(f"Periodic WebSocket push failed: {e}")
            if self._error_count >= self.max_errors:
                self._pushing_disabled = True
    
    def push_final_state(
        self,
        instance: 'AgentInstance',
        caller: str
    ) -> None:
        """Push final state after sub-agent completes.
        
        Extracts from _create_and_run_agent() L3146-3165 and L3294.
        Covers the final push location outside _create_and_run_agent().
        Ensures even short-lived agents (<5 turns) appear in the WebUI.
        
        Args:
            instance: The completed AgentInstance.
            caller: Root instance name for header stats.
            
        Note:
            Exceptions are caught and logged at debug level but do not affect
            error counting (final push is best-effort).
        """
        if self._pushing_disabled:
            return
        
        try:
            ws_queue = getattr(self.pool, '_ws_send_queue', None)
            ws_loop = getattr(self.pool, '_ws_loop', None)
            if not (ws_queue and ws_loop and not ws_loop.is_closed()):
                return
            
            from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
            su = build_stream_update_from_pool(
                pool=self.pool,
                instance_name=caller,  # Root instance for header stats
                responses=None,        # Reads full conversations from pool
            )
            if su is not None:
                asyncio.run_coroutine_threadsafe(
                    _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                    ws_loop,
                )
            
        except Exception as e:
            # Best-effort final push — do not count errors or disable pushing
            logger.debug(f"Final WebSocket push failed for {instance.instance_name}: {e}")