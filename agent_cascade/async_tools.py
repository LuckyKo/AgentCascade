"""
Async Tools Module — Phase 4 Infrastructure.

Provides structured classes for managing background tool execution across all agents.
Replaces inline dict-based async infrastructure with proper typed classes.

Components:
- BackgroundToolEntry: Dataclass tracking individual tool executions
- AsyncToolRegistry: Manages background tool execution with ThreadPoolExecutor
- AsyncResultBuffer: Thread-safe result storage
"""

from dataclasses import dataclass, field
import time
import threading
from typing import Callable, Optional, Dict, List
from concurrent.futures import ThreadPoolExecutor

from agent_cascade.log import logger
from agent_cascade.llm.schema import Message, USER


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
    """
    tool_call: Callable[[], str]
    agent_instance_name: str
    timeout: float = 30.0
    start_time: float = field(default_factory=time.time)
    result: Optional[str] = None
    error: Optional[str] = None
    completed: bool = False
    function_id: Optional[str] = None


class AsyncToolRegistry:
    """Manages background tool execution across all agents.
    
    Uses ThreadPoolExecutor for concurrent execution and tracks completion status
    per instance. Automatically puts results into AsyncResultBuffer when complete.
    
    Attributes:
        _pending: Maps instance_name to list of BackgroundToolEntry objects
        _lock: Lock protecting _pending dictionary
        pool: Reference to AgentPool for result buffering
        _executor: ThreadPoolExecutor for running background tools
    """
    
    def __init__(self, pool=None):
        """Initialize the async tool registry.
        
        Args:
            pool: Optional reference to AgentPool instance for result buffering.
                  If provided, completed results will be automatically added to
                  the pool's AsyncResultBuffer.
        """
        self._pending: Dict[str, List[BackgroundToolEntry]] = {}
        self._lock = threading.Lock()
        self.pool = pool
        # ThreadPoolExecutor with 4 workers for background tool execution
        self._executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="async_tool"
        )
    
    def register(self, instance_name: str, tool_call: Callable[[], str], function_id: Optional[str] = None) -> BackgroundToolEntry:
        """Register a background tool for execution.
        
        Creates a BackgroundToolEntry, adds it to the pending list, and submits
        it to the thread pool for execution.
        
        Args:
            instance_name: The agent instance name owning this tool call.
            tool_call: Callable that executes the tool (no args, returns str).
            function_id: The LLM's tool_call_id for this async call (optional).
            
        Returns:
            BackgroundToolEntry tracking this tool's execution.
        """
        with self._lock:
            entry = BackgroundToolEntry(
                tool_call=tool_call,
                agent_instance_name=instance_name,
                function_id=function_id
            )
            self._pending.setdefault(instance_name, []).append(entry)
            # Submit to executor outside lock to avoid holding lock during execution
            self._executor.submit(self._execute, entry)
            return entry
    
    def _execute(self, entry: BackgroundToolEntry):
        """Execute a background tool in a thread pool.
        
        Runs the tool_call in a worker thread, captures result or error, and
        marks the entry as completed. If pool is configured, puts result into
        AsyncResultBuffer with the function_id for proper LLM API matching.
        
        Lock ordering note: _lock is held through BOTH marking completed AND put() 
        to prevent race condition where has_pending returns False (entry.completed=True)
        but the result isn't in the buffer yet. If an exception occurs between 
        has_pending and the safety drain, results could be lost without this fix.
        
        Args:
            entry: BackgroundToolEntry to execute.
        """
        try:
            entry.result = entry.tool_call()
        except Exception as e:
            entry.error = str(e)
        finally:
            # Mark completed AND put result into buffer WHILE holding lock to prevent
            # race condition where has_pending returns False but result isn't in buffer yet
            with self._lock:
                entry.completed = True
                # Put result into buffer while holding lock (put() is also thread-safe)
                if self.pool and hasattr(self.pool, '_async_results'):
                    if entry.error:
                        result_msg = f"[Background Tool Error]:\n{entry.error}"
                    else:
                        result_msg = f"[Background Tool Result]:\n{entry.result}"
                    try:
                        self.pool._async_results.put(entry.agent_instance_name, result_msg, function_id=entry.function_id)
                        
                        # Note: Message is appended to conversation at Drain Point 2 (execution_engine.py),
                        # not here, to avoid double-append when agent is RUNNING.
                    except Exception as e:
                        # Log but don't propagate — we want to mark entry as completed even if put fails
                        # This prevents the tool from being stuck in pending state forever
                        logger.error(
                            f"[AsyncToolRegistry] Failed to buffer result for {entry.agent_instance_name}: {e}"
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
    
    def shutdown(self):
        """Shutdown the executor.
        
        Call during pool teardown to cleanly stop background tool execution.
        Waits for all pending tasks to complete before returning.
        """
        self._executor.shutdown(wait=True)


class AsyncResultBuffer:
    """Thread-safe buffer for async tool results.
    
    Stores completed async tool results by instance name and provides atomic
    drain operation for efficient batch retrieval.
    
    Attributes:
        _results: Maps instance_name to list of (result_string, function_id) tuples
        _lock: Lock protecting _results dictionary
    """
    
    def __init__(self):
        """Initialize the async result buffer."""
        # Store (result_string, function_id) tuples so the caller knows which tool_call_id the result belongs to
        self._results: Dict[str, List[tuple]] = {}
        self._lock = threading.Lock()
    
    def put(self, instance_name: str, result: str, function_id: Optional[str] = None):
        """Add a result to the buffer for this instance.
        
        Thread-safe append operation. Creates new list if instance not present.
        
        Args:
            instance_name: The agent instance this result belongs to.
            result: The string result from a completed async tool.
            function_id: The LLM's tool_call_id for this async call (optional).
        """
        with self._lock:
            self._results.setdefault(instance_name, []).append((result, function_id))
    
    def drain(self, instance_name: str) -> List[tuple]:
        """Remove and return all results for this instance.
        
        Atomically pops the entire results list under lock, minimizing lock
        contention and ensuring thread-safe access.
        
        Args:
            instance_name: The agent instance to drain results for.
            
        Returns:
            List of (result_string, function_id) tuples (may be empty). Original buffer is cleared.
        """
        with self._lock:
            return self._results.pop(instance_name, [])