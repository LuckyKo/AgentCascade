"""
Async Tools Module — Phase 4 Infrastructure.

Provides structured classes for managing background tool execution across all agents.
Replaces inline dict-based async infrastructure with proper typed classes.

Components:
- BackgroundToolEntry: Dataclass tracking individual tool executions
- AsyncToolRegistry: Manages background tool execution with ThreadPoolExecutor
"""

from dataclasses import dataclass, field
import time
import threading
from typing import Callable, Optional, Dict, List, Tuple
from concurrent.futures import Future, ThreadPoolExecutor

from agent_cascade.log import logger
from agent_cascade.settings import AGENT_MAX_WORKERS


@dataclass
class BackgroundToolEntry:
    """Tracks a background tool execution.
    
    Attributes:
        tool_call: The callable that executes the tool (takes no args, returns str)
        agent_instance_name: Which agent owns this tool
        timeout: Max seconds to wait for completion (default: 30.0)
                  Note: Currently not enforced — planned for future enhancement.
        start_time: When the tool started execution
        result: Completed result string (None until completed)
        error: Error message as string if error occurred (None if successful).
               Note: Using str instead of Exception to avoid serialization issues.
        completed: Whether the tool has finished executing
        function_id: The LLM's tool_call_id (function_id) for this async tool call.
                     Used to match results back to original tool calls in the LLM API.
        future: ThreadPoolExecutor Future for cancellation support (Fix TODO #41).
                Set after registration via entry.future = future.
    """
    tool_call: Callable[[], str]
    agent_instance_name: str
    timeout: float = 30.0
    start_time: float = field(default_factory=time.time)
    result: Optional[str] = None
    error: Optional[str] = None
    completed: bool = False
    function_id: Optional[str] = None
    future: Optional[Future] = None  # Set after registration, allows cancellation (Fix TODO #41)
    child_instance_name: Optional[str] = None  # Name of async child agent, if this entry runs a child agent


class AsyncToolRegistry:
    """Manages background tool execution across all agents.
    
    Uses ThreadPoolExecutor for concurrent execution and tracks completion status
    per instance. Automatically enqueues results to the pool's message queue when complete.
    
    Attributes:
        _pending: Maps instance_name to list of BackgroundToolEntry objects
        _lock: Lock protecting _pending dictionary
        pool: Reference to AgentPool for result buffering via enqueue_message
        _executor: ThreadPoolExecutor for running background tools
    """
    
    def __init__(self, pool=None):
        """Initialize the async tool registry.
        
        Args:
            pool: Optional reference to AgentPool instance for result buffering.
                  If provided, completed results will be automatically enqueued to
                  the pool's message queue via enqueue_message().
        """
        self._pending: Dict[str, List[BackgroundToolEntry]] = {}
        self._lock = threading.Lock()
        self.pool = pool
        # Reverse mapping: child_instance_name -> (parent_instance_name, function_id)
        # Used to wake up SLEEPING parent when async child is dismissed
        self._child_to_parent: Dict[str, Tuple[str, str]] = {}
        # Size the executor from settings (single source of truth) when a pool
        # with settings is provided; fall back to AGENT_MAX_WORKERS otherwise.
        # Validate the value is a positive int so a misconfigured/mock settings
        # object can never be passed straight into ThreadPoolExecutor.
        max_workers = getattr(getattr(pool, 'settings', None), 'max_workers', None)
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            max_workers = AGENT_MAX_WORKERS
        self._worker_count = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="async_tool"
        )

    def resize_executor(self, max_workers: int) -> bool:
        """Thread-safely replace the executor with one sized to ``max_workers``.

        The old executor is shut down *without* cancel_futures so that queued
        async child agents drain naturally (none are dropped, no parent hangs).

        Args:
            max_workers: Desired worker count (clamped to >= 1).

        Returns:
            True on success, False if the input is non-numeric or constructing
            the new executor failed. Never raises for bad input.
        """
        try:
            max_workers = int(max_workers)
        except (TypeError, ValueError):
            logger.error(f"[ASYNC_REGISTRY] resize_executor given non-numeric {max_workers!r}; keeping current pool")
            return False
        max_workers = max(1, max_workers)
        old_max = self._worker_count
        with self._lock:
            old = self._executor
            try:
                new = ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="async_tool"
                )
            except Exception as e:
                logger.error(f"[ASYNC_REGISTRY] Failed to construct executor for resize to {max_workers}: {e}")
                return False
            self._worker_count = max_workers
            self._executor = new
        # Shutdown the old pool OUTSIDE the lock. No cancel_futures: queued work
        # drains to completion so no async child agent is lost or hangs its parent.
        if old is not None and old is not new:
            try:
                old.shutdown(wait=False)
            except Exception as e:
                logger.debug(f"[ASYNC_REGISTRY] Old executor shutdown failed (non-critical): {e}")
        if old_max != max_workers:
            logger.info(f"[ASYNC_REGISTRY] Resized executor to {max_workers} workers")
        else:
            logger.debug(f"[ASYNC_REGISTRY] Resize to same size ({max_workers} workers), no-op")
        return True
    
    def register(self, instance_name: str, tool_call: Callable[[], str], function_id: Optional[str] = None, child_instance_name: Optional[str] = None) -> BackgroundToolEntry:
        """Register a background tool for execution.
        
        Creates a BackgroundToolEntry, adds it to the pending list, and submits
        it to the thread pool for execution.
        
        Args:
            instance_name: The agent instance name owning this tool call.
            tool_call: Callable that executes the tool (no args, returns str).
            function_id: The LLM's tool_call_id for this async call (optional).
            child_instance_name: Name of the child agent being run asynchronously (optional).
                                 Used to track parent-child relationship for dismissal wakeup.
            
        Returns:
            BackgroundToolEntry tracking this tool's execution.
        """
        with self._lock:
            entry = BackgroundToolEntry(
                tool_call=tool_call,
                agent_instance_name=instance_name,
                function_id=function_id,
                child_instance_name=child_instance_name
            )
            self._pending.setdefault(instance_name, []).append(entry)
            # Track child->parent mapping for dismissal wakeup support
            if child_instance_name and function_id:
                self._child_to_parent[child_instance_name] = (instance_name, function_id)
            # Submit to executor while holding _lock so a concurrent resize sees a
            # consistent (fully-old or fully-new) executor — no torn read.
            future = self._executor.submit(self._execute, entry)
            # Store the future on the entry so it can be cancelled later (Fix TODO #41)
            entry.future = future
            return entry
    
    def _execute(self, entry: BackgroundToolEntry):
        """Execute a background tool in a thread pool.
        
        Runs the tool_call in a worker thread, captures result or error, and
        marks the entry as completed. If pool is configured, enqueues result to
        message queue via enqueue_message().
        
        Lock ordering note: _lock is held through BOTH marking completed AND 
        enqueue_message() to prevent race condition where has_pending returns False (entry.completed=True)
        but the result isn't in the buffer yet. If an exception occurs between 
        has_pending and the safety drain, results could be lost without this fix.
        
        Args:
            entry: BackgroundToolEntry to execute.
        """
        try:
            # Check if the owning instance was terminated before starting execution.
            if self.pool and getattr(self.pool, 'is_instance_terminated', None):
                if self.pool.is_instance_terminated(entry.agent_instance_name) is True:
                    logger.debug(
                        f"[AsyncToolRegistry] Skipping tool for '{entry.agent_instance_name}': "
                        f"instance was dismissed before execution started"
                    )
                    from agent_cascade.exceptions import AgentTerminatedError
                    raise AgentTerminatedError(entry.agent_instance_name)
            # Also check if this is an async child agent and that child was terminated.
            # This ensures dismissal of the child instance aborts its executor worker.
            if (entry.child_instance_name and self.pool and getattr(self.pool, 'is_instance_terminated', None)):
                if self.pool.is_instance_terminated(entry.child_instance_name) is True:
                    logger.debug(
                        f"[AsyncToolRegistry] Skipping async child '{entry.child_instance_name}': "
                        f"child instance was dismissed before execution started"
                    )
                    from agent_cascade.exceptions import AgentTerminatedError
                    raise AgentTerminatedError(entry.child_instance_name)
            entry.result = entry.tool_call()
        except Exception as e:
            entry.error = str(e)
        finally:
            # Mark completed AND put result into buffer WHILE holding lock to prevent
            # race condition where has_pending returns False but result isn't in buffer yet
            with self._lock:
                entry.completed = True
                # Put result into message queue while holding lock (enqueue_message is also thread-safe)
                if self.pool and hasattr(self.pool, 'enqueue_message'):
                    if entry.error:
                        result_msg = f"[Background Tool Error]:\n{entry.error}"
                    else:
                        # Don't double-wrap if result is already formatted with a prefix like [Agent ...]
                        if entry.result and entry.result.strip().startswith('[Agent '):
                            result_msg = entry.result
                        elif entry.result:
                            result_msg = f"[Background Tool Result]:\n{entry.result}"
                        else:
                            result_msg = "[Background Tool Result]: (no output)"
                    try:
                        self.pool.enqueue_message(entry.agent_instance_name, result_msg)
                    except Exception as e:
                        # Log but don't propagate — we want to mark entry as completed even if put fails
                        # This prevents the tool from being stuck in pending state forever
                        logger.error(
                            f"[AsyncToolRegistry] Failed to enqueue result for {entry.agent_instance_name}: {e}"
                        )
            # Fix B (idle-wakeup): an IDLE parent whose run() thread already exited
            # never drains its queue — relaunch it, mirroring the user-message path.
            # Enqueue happens first (above) so the relaunched run() finds the result
            # on its first drain. The helper is a no-op unless the instance is IDLE
            # and not stopped/terminated; called outside _lock (it spawns a thread).
            if self.pool:
                try:
                    from agent_cascade.utils.wakeup_helpers import relaunch_idle_agent
                    relaunch_idle_agent(self.pool, entry.agent_instance_name)
                except Exception as e:
                    logger.debug(
                        f"[AsyncToolRegistry] Idle relaunch failed for {entry.agent_instance_name} (non-critical): {e}"
                    )

    def has_pending(self, instance_name: str) -> bool:
        """Check if any background tools are still pending for this instance.
        
        Also cleans up completed entries to prevent unbounded memory growth.
        
        Args:
            instance_name: The agent instance to check.
            
        Returns:
            True if any BackgroundToolEntry for this instance is not completed,
            False otherwise (including if no entries exist).
        """
        with self._lock:
            entries = self._pending.get(instance_name, [])
            has_pending_tools = any(not e.completed for e in entries)
            
            # Cleanup: remove completed-only lists to prevent unbounded growth
            if entries and all(e.completed for e in entries):
                del self._pending[instance_name]
            
            return has_pending_tools
    
    def clear_pending(self, instance_name: str) -> int:
        """Remove and cancel all pending async tools for an instance (Fix TODO #41).
        
        Cancels futures via their ThreadPoolExecutor Future objects. Note: cancel() only
        works for tasks not yet started — already-running threads will complete normally
        but results are discarded when the pending list is removed. Returns the count of
        cleared entries.
        
        Args:
            instance_name: The agent instance to clear.
            
        Returns:
            Number of pending (uncompleted) entries that were removed and cancelled.
        """
        with self._lock:
            entries = self._pending.get(instance_name, [])
            # Cancel futures for uncompleted entries (only works if not yet started; running threads complete normally but results are discarded)
            cancelled = 0
            for entry in entries:
                if not entry.completed and entry.future is not None:
                    try:
                        entry.future.cancel()
                        cancelled += 1
                    except Exception:
                        pass  # Future cancel is best-effort
            # Remove the instance's pending list entirely
            self._pending.pop(instance_name, None)
            # Also clean up child->parent mappings for this parent
            self._child_to_parent = {
                child: (p, fid) for child, (p, fid) in self._child_to_parent.items()
                if p != instance_name
            }
            return cancelled
    
    def get_parent_for_child(self, child_instance_name: str) -> Optional[Tuple[str, str]]:
        """Get the parent instance name and function_id waiting for a specific child.
        
        Used to wake up a SLEEPING parent when its async child is dismissed.
        
        Args:
            child_instance_name: The child agent instance name.
            
        Returns:
            Tuple of (parent_instance_name, function_id) if found, None otherwise.
        """
        with self._lock:
            return self._child_to_parent.get(child_instance_name)
    
    def remove_child_mapping(self, child_instance_name: str):
        """Remove the child->parent mapping for a dismissed/completed child."""
        with self._lock:
            self._child_to_parent.pop(child_instance_name, None)

    def shutdown(self, wait: bool = True):
        """Shutdown the executor.
        
        Call during pool teardown to cleanly stop background tool execution.
        
        Args:
            wait: If True, wait for all pending tasks to complete before returning.
                  If False, return immediately (useful for "quick stop" scenarios).
        """
        self._executor.shutdown(wait=wait)