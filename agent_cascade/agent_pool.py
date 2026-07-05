"""
Lean Agent Pool — Phase 1 of the AgentCascade Architecture Rewrite.

Replaces the old god-object AgentPool (~25 attributes, ~1100 lines) with a thin
coordinator that owns only the instance registry, template registry, and simple
state structures. Logger lifecycle, idle detection, and parallel execution are
delegated to focused managers (LoggerManager, IdleManager, ParallelAgentManager).

See DESIGN_REWRITE.md §2.2 for design rationale.
"""

import hashlib
import time
import threading
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent_cascade.agents import Assistant
from agent_cascade.llm.schema import Message, USER
from agent_cascade.log import logger
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.settings import DEFAULT_WORKSPACE

from .agent_instance import AgentInstance, PoolSettings, AgentState
from .async_tools import AsyncResultBuffer, AsyncToolRegistry


# Active states for agent lifecycle management (not IDLE — it means "not executing")
ACTIVE_STATES = (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING)


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


class AgentPool:
    """
    Thin coordinator for all agent state. Delegates to focused managers
    rather than holding 25+ unrelated attributes.

    The pool coordinates — it doesn't own everything.

    Core design principle: Only data structures that genuinely need to be in one
    place live here. Halt state and message routing are simple dicts/sets.
    LoggerManager and IdleManager are separate modules (they have distinct
    lifecycles: file I/O, background threads).
    """

    def __init__(
        self,
        llm_cfg: dict,
        agents_dir: str = 'agents',
        workspace_dir: Optional[str] = None,
        api_router=None,
        telemetry=None,
        operation_manager=None,
    ):
        """Initialize the lean AgentPool.

        Args:
            llm_cfg: LLM configuration dictionary.
            agents_dir: Path to the agents directory.
            workspace_dir: Path to the workspace directory.
            api_router: APIRouter for multi-endpoint management (injected, not owned).
            telemetry: TelemetryCollector for performance tracking (injected, not owned).
            operation_manager: OperationManager for blocking approvals (injected, not owned).
        """
        # ── Injected dependencies (not owned by pool) ────────────────────────
        # If api_router is not injected, create one (matches main branch behavior).
        # This ensures agents loaded during _discover_agents() get their correct endpoints.
        if api_router is not None:
            self.api_router = api_router
        else:
            from agent_cascade.api_router import APIRouter
            # API config lives in the project root config/ dir, not workspace
            project_root = Path(__file__).resolve().parent.parent
            config_dir = str(project_root / 'config')
            self.api_router = APIRouter(
                default_llm_cfg=llm_cfg,
                config_dir=config_dir
            )
        self.telemetry = telemetry
        self.operation_manager = operation_manager

        # ── Core registries (owned directly) ─────────────────────────────────
        self.instances: Dict[str, AgentInstance] = {}  # instance_name → AgentInstance
        self.templates: Dict[str, Assistant] = {}      # agent_class → template

        # ── Configuration ───────────────────────────────────────────────────
        self.llm_cfg = llm_cfg                          # LLM config (used as fallback when no api_router)
        self.settings = PoolSettings()                  # Configurable thresholds and timeouts

        # ── Focused managers (delegation targets) ───────────────────────────
        # Only LoggerManager and IdleManager get their own files — they have
        # distinct lifecycles (file I/O, background thread). Halt state and
        # message routing are simple data structures that belong on the pool.
        self._execution = ParallelAgentManager(self)       # parallel execution, active_stack
        self._logger = LoggerManager(self, workspace_dir)  # logger lifecycle, recovery
        self._idle = IdleManager(self)                      # idle detection and auto-dismissal

        # ── Simple state (owned directly by pool, no separate manager) ───────
        self._paused = threading.Event()                   # global pause flag (thread-safe Event)
        self._paused.set()                                  # start in resumed state (clear = paused, set = resumed)
        self._halted_instances: set = set()                # per-instance halt state (legacy, kept for compat)
        self._compression_halted: set = set()              # instances halted by forced compression (not manual)
        self.terminated_instances: set = set()             # instances marked for immediate termination
        self.children: Dict[str, List[str]] = {}           # parent_name -> [child_names] for cascade termination

        # ── Run generation counter (prevents resume race condition) ───────────
        # Each time a new execution thread starts, this is incremented. Old threads
        # check their captured generation value to detect they've been superseded.
        self._run_generation = 0                           # monotonically increasing run ID

        # ── Attributes required by api_server.py and agent_invoker.py ──
        # These bridge the new unified model with existing call patterns.
        self.last_tool_args: Dict[str, Dict[str, Dict[str, Any]]] = {}  # tool arg cache for __USE_PREV_ARG__
        self.instance_summaries: Dict[str, str] = {}         # per-instance compression summaries
        self._ws_loop = None                                 # asyncio event loop ref (set by api_server at runtime)

        # instance_state bridges the old WebUI state pattern with the new unified model.
        # Maintained for agent_invoker.py and session rename patterns.
        self.instance_state: Dict[str, dict] = {}
        self.message_queues: Dict[str, List[str]] = {}     # per-agent message queues

        # ── Async Tools Infrastructure (SLEEPING state support) ─────────────
        # These attributes support the SLEEPING state guard for async background tools.
        # _async_results: buffer storing completed async tool results by instance name
        # _async_registry: tracks pending async tool calls by instance name
        self._async_results: AsyncResultBuffer = AsyncResultBuffer()
        self._async_registry: AsyncToolRegistry = AsyncToolRegistry(pool=self)

        # ── Global state ─────────────────────────────────────────────────────
        self._stopped_event = threading.Event()         # M3 fix: stopped flag for emergency shutdown

        # ── Version counter for lazy sync of instance_conversations (Fix #3) ──
        self._instances_version = 0                        # increments on create/remove/dismiss/reset
        self._mapping_synced_to_version = -1              # tracks last version instance_conversations was synced to

        # ── Configuration Version (Fix LLM Reprocessing) ─────────────────────
        # Incremented when global config changes (workspace dir, extra folders, refresh_agents).
        # Used by ExecutionEngine._setup_turn() to detect if system prompt needs rebuild.
        self._config_version = 0

        # Dismissal callbacks (used by api_server to broadcast real-time tab removal)
        self._on_dismissed_callbacks: list = []

        # ── Agent discovery (unchanged) ──────────────────────────────────────
        self.agents_dir = Path(agents_dir)
        self._discover_agents(agents_dir)

    def get_template(self, name: str) -> Optional[Assistant]:
        """Get template by name with case-insensitive fallback.
        
        This method provides robustness against case mismatches between the agent_class
        specified during instance creation and how templates are registered in the pool.
        Fallback chain: exact → lowercase → titlecase.
        For example, if 'Security' is passed but template is registered as 'security',
        or vice versa (e.g. tool_dispatcher lowercases to 'security' but key is 'Security'),
        this will still find it.
        
        Args:
            name: Template name to look up (e.g., 'Security', 'coder', etc.)
            
        Returns:
            The Assistant template if found, None otherwise.
            
        Example:
            >>> template = pool.get_template('Security')  # Works even if registered as 'security'
        """
        if not name or not isinstance(name, str):
            return None
            
        template = self.templates.get(name)
        if template is None:
            template = self.templates.get(name.lower())
        if template is None:
            template = self.templates.get(name.title())
        return template

    def start(self):
        """Start background services (idle checker, etc.). Call after pool initialization."""
        self._idle.start()

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def stopped(self) -> bool:
        """Check if pool has been told to stop."""
        return self._stopped_event.is_set()

    @stopped.setter
    def stopped(self, value: bool):

        if value:
            self._stopped_event.set()
            # Shut down background services when pool stops
            try:
                self._idle.stop()
            except Exception as e:
                logger.debug(f"Idle manager shutdown failed (non-critical): {e}")
            try:
                self._async_registry.shutdown(wait=False)  # Quick stop — don't block waiting for tasks
            except Exception as e:
                logger.debug(f"Async registry shutdown failed (non-critical): {e}")
            logger.debug("Background services shut down (idle_checker + async_registry)")
        else:
            self._stopped_event.clear()
            # Restart background services on resume (they were shut down during stop)
            try:
                self._idle.start()
                logger.debug("Idle checker restarted")
            except Exception as e:
                logger.debug(f"Idle manager restart (non-critical): {e}")
            # Restart async registry executor
            try:
                # Check if executor was shut down and recreate if needed
                if self._async_registry._executor is not None:
                    # ThreadPoolExecutor doesn't have a direct 'closed' check,
                    # but shutdown() makes submit() return PendingResult
                    # We recreate to be safe
                    self._async_registry._executor.shutdown(wait=False)
                    from concurrent.futures import ThreadPoolExecutor
                    self._async_registry._executor = ThreadPoolExecutor(
                        max_workers=4,
                        thread_name_prefix="async_tool",
                    )
                    logger.debug("Async registry executor recreated")
            except Exception as e:
                logger.debug(f"Async registry restart (non-critical): {e}")
            logger.debug("Stopped flag cleared — ready for new execution")

    # ── Instance lifecycle ───────────────────────────────────────────────────

    def create_instance(
        self,
        instance_name: str,
        agent_class: str,
        parent_instance: Optional[str] = None,
        max_turns: Optional[int] = None,
        conversation: Optional[List[Message]] = None,
    ) -> AgentInstance:
        """Create a new agent instance and register it in the pool.

        Args:
            instance_name: Unique identifier for this instance.
            agent_class: Template class name (e.g., "coder", "researcher").
            parent_instance: Name of the calling agent (None for root/main).
            max_turns: Per-instance turn limit (None = use default 50).
            conversation: Initial conversation history (default: empty list).

        Returns:
            The newly created AgentInstance.
        """
        now = time.monotonic()
        instance = AgentInstance(
            instance_name=instance_name,
            agent_class=agent_class,
            conversation=conversation or [],
            max_turns=max_turns,
            parent_instance=parent_instance,
            created_at=now,
            last_activity=now,
            compression_summary=None,
            latest_marker_index=-1,
        )
        self.instances[instance_name] = instance
        self._instances_version += 1  # Fix #3: signal that instances changed
        # Track parent-child relationship for cascade termination (Fix Bug41)
        if parent_instance:
            if parent_instance not in self.children:
                self.children[parent_instance] = []
            self.children[parent_instance].append(instance_name)
        # RECOMMENDED FIX: Removed redundant _mark_activity() call - constructor already sets last_activity=now above
        return instance

    def get_instance(self, instance_name: str) -> Optional[AgentInstance]:
        """Get an agent instance by name.

        Returns None if the instance doesn't exist (instead of raising KeyError).
        This is intentional — callers often check existence before acting.
        """
        return self.instances.get(instance_name)

    def remove_instance(self, instance_name: str):
        """Remove an agent instance from the pool.

        Used by IdleManager for auto-dismissal and by dismiss_agent tool execution.
        Fires dismissal callbacks and cleans up message queues.
        """
        self._instances_version += 1  # Fix #3: signal that instances changed
        self.instances.pop(instance_name, None)
        self.terminated_instances.discard(instance_name)  # Issue #4 fix: prevent memory leaks
        self.message_queues.pop(instance_name, None)
        # Clean up mapping's dict storage to prevent stale keys
        if hasattr(self, '_instance_conversations'):
            try:
                del self._instance_conversations[instance_name]
            except KeyError as e:
                logger.debug(f"Instance conversation cleanup key missing (expected): {e}")
        # Clean up logger entry for the instance
        with self._logger._lock:
            # Use composite key format (instance_name, agent_class.lower()) to match get_logger
            agent_class = self.instance_classes.get(instance_name, '').lower()
            key = (instance_name, agent_class)
            log_inst = self._logger._loggers.pop(key, None)

        # Fix #3: Clean up stale instance_state entries
        if hasattr(self, 'instance_state'):
            self.instance_state.pop(instance_name, None)

        # Fix #1: Close the cached file handle for the logger if it exists
        if log_inst and hasattr(log_inst, 'close'):
            try:
                log_inst.close()
            except Exception as e:
                logger.debug(f"Logger close failed for {instance_name} (non-critical): {e}")

        # Capture log path before it's lost — needed by dismissal callbacks to tell frontend where logs are
        log_path = log_inst.log_path if log_inst else None
        self._fire_on_dismissed(instance_name, log_path)

        # Clean up children tracking (Fix Bug41)
        self.children.pop(instance_name, None)
        # Also remove from parent's children list
        for parent, kids in self.children.items():
            if instance_name in kids:
                kids.remove(instance_name)

        # BUG31 Fix: Clean up api_integration module-level caches to prevent memory leaks
        # and stale data when instances are dismissed and re-created with same name.
        from agent_cascade.api_integration import _cache_mgr
        _cache_mgr.evict_instance(instance_name)
        # Note: _token_stats_cache is NOT cleaned here — it's keyed by conversation identity
        # (msg_count, id(last_msg)), not instance name. Entries auto-evict via FIFO at 5000 cap.

    def halt_all_instances(self, except_instance: str = None,
                           except_instances: Optional[List[str]] = None):
        """Halt all active instances except the given one(s). Used before forced compression.

        Tracks which instances were halted by compression (not manual) so that
        resume_all_instances only clears those — preserving manual halts.
        """
        skip = set()
        if except_instance:
            skip.add(except_instance)
        if except_instances:
            skip.update(except_instances)

        for inst_name in self.instances:
            if inst_name not in skip:
                was_already_halted = inst_name in self._halted_instances  # check per-instance only, not global pause
                self.halt_instance(inst_name)
                # Only track instances that weren't already halted — preserves manual halts
                if not was_already_halted:
                    self._compression_halted.add(inst_name)

    def resume_all_instances(self):
        """Resume only the instances that were halted by forced compression (not manual halts)."""
        for inst_name in list(self._compression_halted):
            self.resume_instance(inst_name)
        self._compression_halted.clear()

    def terminate_instance(self, instance_name: str, set_global_stopped: bool = False):
        """Mark an instance for immediate termination.

        Adds to terminated_instances set and transitions state to TERMINATED.
        Cascade-terminates all child agents recursively (Fix Bug41).
        Mirrors old AgentPool.terminate_instance() semantics.

        Args:
            instance_name: Name of the instance to terminate.
            set_global_stopped: If True, sets the global _stopped_event which signals
                              ALL agents to stop. If False (default for dismissal), only
                              THIS instance's state is changed without signaling other agents
                              via the global event. Bug5 Fix: Dismissal uses False to avoid
                              affecting other agents mid-execution.
        """
        # First cascade-terminate all children (recursive, Fix Bug41)
        for child_name in list(self.children.get(instance_name, [])):
            if self.instances.get(child_name):
                self.terminate_instance(child_name, set_global_stopped=False)  # Recursive — handles nested trees

        self.terminated_instances.add(instance_name)
        inst = self.instances.get(instance_name)

        # FIX: Thread-safe state read - snapshot under lock before checking ACTIVE_STATES
        is_active = False
        if inst:
            with inst._state_lock:
                is_active = inst.state in ACTIVE_STATES

        if is_active:
            # Bug5 Fix #1: Only set global _stopped_event when explicitly requested
            if set_global_stopped:
                self._stopped_event.set()  # Global signal for ALL agents
            
            # RECOMMENDED FIX: Mark activity before transitioning to TERMINATED for consistency
            self._mark_activity(instance_name)
            
            with inst._state_lock:
                inst._transition(AgentState.TERMINATED)

        # Drain async results buffer to prevent memory leaks and stale messages
        if hasattr(self, '_async_results'):
            try:
                self._async_results.drain(instance_name)
            except Exception as e:
                logger.debug(f"Draining async results for {instance_name} failed (non-critical): {e}")

        # Clear message queue to prevent stale messages from being processed
        if instance_name in self.message_queues:
            try:
                self.message_queues[instance_name].clear()
            except Exception as e:
                logger.debug(f"Clearing message queue for {instance_name} failed (non-critical): {e}")

        # Clear _streaming_responses to discard half-completed LLM output.
        # Without this, the streaming UI keeps showing partial content from a terminated agent.
        if inst:
            try:
                with inst._compression_lock:
                    inst._streaming_responses.clear()
            except Exception as e:
                logger.debug(f"Clearing streaming responses for {instance_name} failed (non-critical): {e}")

    def dismiss_instance(self, instance_name: str):
        """Remove an instance from the pool. If active, terminate it; otherwise clean up.

        Recursively dismisses all child agents first (cascade termination, Fix Bug41).
        This is the UI-initiated termination path (WebSocket terminate_agent_instance message).
        Mirrors old AgentPool.dismiss_instance() semantics.

        Bug5 Fix #1: Dismissal should NOT set global _stopped_event to avoid affecting
        other agents mid-execution. Only this specific instance is terminated.
        """
        # First dismiss all children (recursive cascade, Fix Bug41)
        for child_name in list(self.children.get(instance_name, [])):
            if self.instances.get(child_name):
                self.dismiss_instance(child_name)  # Recursive — handles nested trees

        inst = self.instances.get(instance_name)

        # FIX: Thread-safe state read - snapshot under lock before checking ACTIVE_STATES
        is_active = False
        if inst:
            with inst._state_lock:
                is_active = inst.state in ACTIVE_STATES

        if is_active:
            # Bug5 Fix: Pass set_global_stopped=False to ensure only THIS instance
            # is terminated, not all agents via the global _stopped_event.
            self.terminate_instance(instance_name, set_global_stopped=False)
        # Always remove the instance from the pool so its tab disappears from the UI
        # remove_instance() handles terminated_instances.discard() (Issue #4 fix)
        self.remove_instance(instance_name)

    # ── API bridge methods for api_server.py ────────────────────────────────
    # These methods provide access patterns that api_server.py expects.

    def list_agents(self) -> List[str]:
        """Return all available agent template names."""
        return list(self.templates.keys())

    def get_agent_info(self, name: str) -> dict | None:
        """Return info dict for an agent template (name, tagline/description, tools).

        Used by list_agents tool and the default prompt builder.
        Returns None if the template is not found.
        """
        template = self.get_template(name)
        if template is None:
            return None

        # Get active functions using the same logic as agents use at runtime
        # This respects disabled_tools configuration from UI settings
        # Defensive guard: fallback to empty list if method doesn't exist
        active_functions = getattr(template, '_get_active_functions', lambda: [])()
        active_tool_names = [f['name'] for f in active_functions]

        return {
            'name': getattr(template, 'name', name),
            'tagline': getattr(template, 'description', ''),
            'tools': active_tool_names,  # Now filtered by disabled_tools config
        }

    def reset(self):
        """Full reset of agent state for "New Session".

        Order of operations:
          1. Clear pending approvals (unblocks any threads waiting in operation_manager)
          2. Dismiss all non-orchestrator sub-agents (with cascade and double-dismiss guard)
          3. Create new logger session for the main orchestrator (so new messages
             go to a fresh JSONL file instead of appending to the old one)
          4. Clear instance conversations mapping
          5. Clear per-instance state (halted, active_stack, tool args, etc.)
          6. Clear performance caches and WebSocket references
          7. Shutdown and recreate async infrastructure (Phase 4)

        Does NOT delete AgentInstances — only clears their conversations.
        The main orchestrator instance (parent_instance is None) survives reset.

        Note: This method sets pool.stopped as a safety net to signal active threads
        to halt even if the caller forgot. The stopped event is cleared at the end of
        reset so executors can run in the new session. Callers may re-set
        pool.stopped = True after reset if they need threads halted during post-reset
        operations (e.g., api_server.py line 1697).
        """
        # Safety net: signal all active threads to halt even if caller forgot.
        # Direct _stopped_event manipulation avoids triggering the property setter's
        # side effects (idle.stop() + async_registry.shutdown()) which are handled
        # explicitly later in this method at steps 6-7.
        self._stopped_event.set()

        # ── Step 1: Clear pending approvals ──────────────────────────────────
        # Prevent dangling threads waiting for user approval.
        if self.operation_manager:
            try:
                with self.operation_manager._lock:
                    for approval in self.operation_manager.pending.values():
                        if not approval.event.is_set():
                            approval.approved = False
                            approval.outcome_reason = "Session reset"
                            approval.event.set()
                    self.operation_manager.pending.clear()
            except Exception as e:
                logger.warning(f"clear_pending failed during reset (threads may hang): {e}")

        # ── Step 2: Dismiss all sub-agents (non-orchestrator) ───────────────
        # Take a snapshot of instance keys to avoid RuntimeError during iteration.
        # dismiss_instance() recursively cascade-dismisses children first, then
        # calls remove_instance() which cleans up loggers, queues, caches.
        for name in list(self.instances.keys()):
            inst = self.instances.get(name)
            if inst is None or inst.parent_instance is None:
                continue  # Skip main orchestrator and already-removed instances
            # Double-dismiss guard: instance may have been cascade-dismissed by parent
            if name not in self.instances:
                continue
            self.dismiss_instance(name)

        # ── Step 3: New logger session for main orchestrator ────────────────
        # Create a new JSONL log file so the new session doesn't append to old.
        for name, inst in list(self.instances.items()):
            if inst.parent_instance is None:
                try:
                    self._logger.create_new_session(name, inst.agent_class)
                except Exception as e:
                    logger.warning(f"Logger reset failed for {name} (new session may append to old logs): {e}")
                break

        # ── Step 4: Clear instance conversations mapping ──────────────────────
        if hasattr(self, '_instance_conversations'):
            self._instance_conversations.clear()
        else:
            for inst in self.instances.values():
                inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
        self._instances_version += 1

        # ── Step 5: Clear per-instance state ────────────────────────────────
        self._paused.set()  # reset to resumed state
        self._halted_instances.clear()
        self._compression_halted.clear()
        self.terminated_instances.clear()
        self.children.clear()
        # Keep unified's active_stack_clear() — temp removed it but unified still needs it
        if hasattr(self, 'active_stack_clear'):
            self.active_stack_clear()
        self.last_tool_args.clear()
        self.instance_state.clear()
        self.instance_summaries.clear()

        # ── Step 6: Clear performance caches and WebSocket references ───────
        try:
            from agent_cascade.api_integration import _clear_performance_caches
            _clear_performance_caches()
        except Exception as e:
            logger.warning(f"Cache clear failed during reset (stale data may persist): {e}")
        self._ws_send_queue = None
        self._ws_loop = None

        # ── Step 7: Shutdown and recreate async infrastructure (Phase 4) ─────
        # Shutdown executor to clean up background tool threads
        try:
            self._async_registry.shutdown()
        except Exception as e:
            logger.warning(f"Async registry shutdown failed during reset (threads may leak): {e}")
        # Recreate for new session
        self._async_registry = AsyncToolRegistry(pool=self)
        # Clear async results buffer
        self._async_results = AsyncResultBuffer()

        # Restart idle checker — it may have been stopped by the caller's
        # pool.stopped = True. IdleManager.start() is idempotent.
        self._idle.start()

        # Clear the safety-net stopped signal so executors can run in the new session.
        # Callers that explicitly set stopped=True before reset will need to re-set it
        # if they want threads halted during post-reset operations (e.g., api_server line 1697).
        self._stopped_event.clear()

    def clear_sub_agents(self):
        """Clear all sub-agent instances from the pool, preserving root orchestrator(s).
        
        This method dismisses all non-root instances (where parent_instance is not None),
        which are typically delegated workers created during session execution. Root 
        orchestrator instances (parent_instance is None) are preserved.
        
        Use case: Called before loading a saved session to remove stale sub-agents from
        previous sessions that would otherwise appear in the UI as if they belong to
        the newly loaded session.
        
        Order of operations:
          1. Temporarily suppress dismissal callbacks (prevents premature broadcasts)
          2. Take snapshot of instance keys (avoids RuntimeError during iteration)
          3. Dismiss all instances where parent_instance is not None
             - dismiss_instance() recursively cascade-dismisses children first
          4. Use double-dismiss guard (instance may have been cascade-dismissed by parent)
          5. Clean up instance_summaries for dismissed instances
          6. Increment _instances_version to signal the change
          7. Restore dismissal callbacks
        
        Does NOT:
          - Touch root orchestrator instances (parent_instance is None)
          - Clear conversations of remaining instances
          - Reset logger sessions or async infrastructure
          - Fire dismissal callbacks during clear (suppressed to prevent UX flicker)
        
        See reset() for full session reset that clears everything including root instances.
        See load_session_from_log() for where this is typically called before loading.
        
        Example:
            >>> # Before loading a new session, clear stale sub-agents
            >>> agent_pool.clear_sub_agents()
            >>> agent_pool.load_session_from_log(path, target_instance='Maine')
        """
        # Issue 1 fix: Temporarily suppress dismissal callbacks to prevent premature 
        # frontend broadcasts. The final state broadcast happens after load completes.
        _callbacks = self._on_dismissed_callbacks.copy()
        self._on_dismissed_callbacks = []
        
        try:
            # Take a snapshot of instance keys to avoid RuntimeError during iteration.
            # dismiss_instance() modifies self.instances by removing dismissed instances
            # and recursively cascade-dismisses children first.
            for name in list(self.instances.keys()):
                inst = self.instances.get(name)
                if inst is None or inst.parent_instance is None:
                    continue  # Skip root orchestrator and already-removed instances
                # Double-dismiss guard: instance may have been cascade-dismissed by parent
                if name not in self.instances:
                    continue
                self.dismiss_instance(name)
            
            # Clean up instance_summaries for dismissed instances (Issue 2 fix)
            for name in list(self.instance_summaries.keys()):
                if name not in self.instances:
                    self.instance_summaries.pop(name, None)
            
            # Signal that instances changed (for lazy sync compatibility)
            self._instances_version += 1
        finally:
            # Restore dismissal callbacks
            self._on_dismissed_callbacks = _callbacks

    def stop_session(self, release_slots: bool = True):
        """Minimal interrupt for "Stop" action — halts execution but preserves sessions.
        
        This method is used when the user clicks "Stop" to halt all streaming and put
        agents in IDLE state WITHOUT dismissing them (unlike reset() which dismisses
        sub-agents, setting them to TERMINATED).
        
        The key design principle: Stop is NON-DESTRUCTIVE. It should only interrupt
        execution — NOT clear conversations, summaries, or any user-visible session data.
        The user expects to be able to Resume exactly where they left off.
        
        Order of operations (MINOR-1 FIX - updated docstring):
          1. Set _stopped_event (to halt threads)
          2. Release concurrency slots for all active instances (NEW — prevents stuck API slots)
          3. Clear pending approvals (unblocks any threads waiting for user approval)
        
        Does NOT:
          - Dismiss sub-agents (they remain in pool with their current state)
          - Clear conversations (user expects to Resume from the same point)
          - Clear instance_summaries, terminated_instances, or any session data
          - Create a new logger session
          - Shutdown/recreate async infrastructure
        
        Args:
            release_slots: If True, immediately release concurrency slots for all instances.
                         This ensures API endpoints are freed even if execution threads
                         haven't noticed the stop signal yet. Default is True.
        
        See reset() for full session reset that dismisses sub-agents and clears everything.
        """
        # Step 1: Set stopped event to signal threads to halt (use property setter for side effects)
        self.stopped = True

        # ── Step 2: Release concurrency slots for all active instances ──────────────
        # This ensures API endpoints are freed immediately, even if execution threads
        # haven't noticed the stop signal yet. Prevents "stuck slot" issues where
        # agents transitioned to IDLE still hold their semaphores.
        # Uses instance._state_lock for thread safety against concurrent slot acquisition/release.
        released_count = 0
        held_count = 0
        if release_slots:
            try:
                for inst_name, instance in list(self.instances.items()):
                    with instance._state_lock:
                        if hasattr(instance, '_slot_release') and instance._slot_release is not None:
                            try:
                                instance._slot_release()
                                instance._slot_release = None
                                released_count += 1
                            except Exception as e:
                                logger.warning(f"[STOP_SLOT] Failed to release slot for '{inst_name}': {e}")
                        else:
                            # Check if instance is in active state but has no slot held
                            if instance.state.name not in ('IDLE', 'TERMINATED'):
                                held_count += 1
            except Exception as e:
                logger.warning(f"slot_release failed during stop_session (non-critical): {e}")

        # ── Step 2.5: Reset API router semaphores ──────────────────────────────
        # The call_with_fallback per-API-call semaphores may be held if generator
        # wrappers were closed before full consumption (stop interrupt during streaming).
        # Reset them to prevent hangs on resume when new calls try to acquire the semaphore.
        if hasattr(self, 'api_router') and self.api_router:
            try:
                self.api_router.reset_semaphores()
            except Exception as e:
                logger.debug(f"Semaphore reset during stop_session (non-critical): {e}")

        # ── Step 3: Clear cached message sets, message queues, and async results ──
        # After stop, the cached working sets may be stale (from interrupted turns).
        # Clear them so the next turn rebuilds from current conversation state.
        # Also drain message queues and async results buffers to prevent stale data.
        cache_cleared = 0
        queue_cleared = 0
        async_cleared = 0
        try:
            for inst_name, instance in list(self.instances.items()):
                if instance._cached_messages or instance._cached_llm_messages:
                    instance._cached_messages = []
                    instance._cached_llm_messages = []
                    instance._last_config_version = 0  # Force rebuild
                    cache_cleared += 1
                # Clear message queue for this instance
                if inst_name in self.message_queues:
                    try:
                        self.message_queues[inst_name].clear()
                        queue_cleared += 1
                    except Exception:
                        pass
                # Drain async results buffer
                if hasattr(self, '_async_results'):
                    try:
                        self._async_results.drain(inst_name)
                        async_cleared += 1
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Cache clear during stop_session (non-critical): {e}")

        # ── Step 4: Clear pending approvals ────────────────────────────────────────
        # Prevent dangling threads waiting for user approval.
        approval_count = 0
        if self.operation_manager:
            try:
                with self.operation_manager._lock:
                    for approval in self.operation_manager.pending.values():
                        if not approval.event.is_set():
                            approval.approved = False
                            approval.outcome_reason = "Session stopped"
                            approval.event.set()
                            approval_count += 1
                    self.operation_manager.pending.clear()
            except Exception as e:
                logger.warning(f"clear_pending failed during stop_session (threads may hang): {e}")

        # ── Instrumentation: Report stop state ──────────────────────────────────────
        with self._execution._state_lock:
            stack_len = len(self._execution.active_stack)
        slot_info = ""
        if hasattr(self, 'api_router') and self.api_router:
            sched = self.api_router.scheduler
            with sched._lock:
                totals = {k: v['active_count'] for k, v in sched._schedules.items() if v['active_count'] > 0}
            if totals:
                slot_info = f" active_slots={totals}"
        
        logger.info(
            f"Stop session done: released={released_count} slots, "
            f"cache_cleared={cache_cleared}, "
            f"queue_cleared={queue_cleared}, "
            f"async_cleared={async_cleared}, "
            f"active_instances={len(self.instances)}, "
            f"active_stack={stack_len}{slot_info}, "
            f"approvals_cleared={approval_count}"
        )
        if held_count > 0:
            logger.warning(f"[STOP_SESSION] {held_count} instance(s) were active but held no slot — possible slot leak")

    @property
    def _state_lock(self):
        """Delegate to ParallelAgentManager's state lock.

        Required by code that references self.agent_pool._state_lock (e.g.,
        agent_invoker.py for thread-safe access).
        """
        return self._execution._state_lock

    @property
    def active_stack(self) -> List[tuple]:
        """Active execution stack — delegates to ParallelAgentManager (thread-safe read).

        Returns a list of (instance_name, nest_depth) tuples.
        Lock is held during copy to ensure a consistent snapshot even under concurrent mutation.
        Writes go through mutation methods which acquire _execution._state_lock.
        """
        with self._execution._state_lock:
            return list(self._execution.active_stack)  # defensive copy for thread safety

    # ── Active stack mutation methods (Fix #2) ────────────────────────────────
    # The active_stack property returns a defensive copy, so mutations must go
    # through these methods to actually modify the underlying stack.

    def active_stack_append(self, name: str, depth: int = 0):
        """Append an instance name with nesting depth to the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            self._execution.active_stack.append((name, depth))

    def active_stack_remove(self, name: str):
        """Remove an instance name from the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            for i, (n, _depth) in enumerate(self._execution.active_stack):
                if n == name:
                    self._execution.active_stack.pop(i)
                    break

    def active_stack_clear(self):
        """Clear the entire active execution stack (thread-safe)."""
        with self._execution._state_lock:
            self._execution.active_stack.clear()

    def active_stack_pop_at(self, index: int):
        """Pop an entry at a specific index from the active execution stack (thread-safe)."""
        with self._execution._state_lock:
            if 0 <= index < len(self._execution.active_stack):
                self._execution.active_stack.pop(index)

    # ── Conversation management (Fix #5) ─────────────────────────────────────

    def clear_conversation(self, instance_name: str):
        """Clear an agent's conversation while keeping the instance alive.

        Used by agent_orchestrator.py at lines 1781 and 2245 for class mismatch
        cleanup and terminated instance cleanup respectively.
        """
        inst = self.instances.get(instance_name)
        if inst:
            inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync

    def capture_snapshots(self) -> Dict[str, int]:
        """Capture current conversation lengths for all instances."""
        result = {}
        for name, inst in self.instances.items():
            with inst._compression_lock:
                result[name] = len(inst.conversation)
        return result

    def rollback_to_snapshots(self, snapshots: Dict[str, int], reason: Optional[str] = None):
        """Rollback all instances to the lengths recorded in snapshots.

        Uses the unified _rollback_instance helper for each instance, which handles
        cache clearing, logger sync, and tail-sync checks internally.
        Preserve/refine are disabled so snapshot lengths are honored exactly (including 0).
        """
        for name, target_len in snapshots.items():
            self._rollback_instance(
                name,
                target_length=target_len,
                preserve_system_user=False,   # Allow exact length including full reset
                refine_function_boundary=False,  # Avoid altering the target length
                sync_logger=True,
                reason=f"Snapshot rollback: {reason}" if reason else "Snapshot rollback",
            )

    def _parse_json_input(self, log_input: str) -> Tuple[List[dict], dict]:
        """Parse log input as file path, multi-line JSONL, or single JSON block.

        Tries each strategy in order and returns the first successful parse.
        Filters to only dict items (BOOL_LEAK guard). Extracts metadata entries.

        Returns:
            Tuple of (messages_list, metadata_dict)
        """
        import json

        messages = []
        metadata = {}

        # Resolve potential file path
        potential_path = Path(log_input)
        if not potential_path.is_absolute():
            ws = self._logger.workspace_dir if self._logger.workspace_dir else Path(DEFAULT_WORKSPACE)
            potential_path = ws / potential_path

        # --- Strategy 1: File path ---
        if potential_path.exists() and potential_path.is_file():
            try:
                with open(potential_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        item = self._parse_json_line(line.strip())
                        messages.extend(item["messages"])
                        metadata.update(item["metadata"])
                return messages, metadata
            except Exception as e:
                logger.debug(f"Error reading log file '{potential_path.name}': {e}")

        # --- Strategy 2: Multi-line JSONL (one JSON object per line) ---
        lines = [l.strip() for l in log_input.split('\n') if l.strip()]
        parse_messages, parse_metadata = [], {}
        single_line = len(lines) == 1
        for line in lines:
            item = self._parse_json_line(line)
            parse_messages.extend(item["messages"])
            parse_metadata.update(item["metadata"])
        if parse_messages or parse_metadata:
            return parse_messages, parse_metadata

        # --- Strategy 3: Single JSON block (array or object) ---
        if not single_line:
            return [], {}  # Already tried line-by-line above; don't double-try
        try:
            item = json.loads(log_input)
            if isinstance(item, list):
                filtered = [msg for msg in item if isinstance(msg, dict)]
                if len(filtered) != len(item):
                    logger.debug(f"_parse_json_input: filtered {len(item)-len(filtered)} non-dict items from JSON block")
                return filtered, {}
            elif isinstance(item, dict):
                if "history" in item:
                    history = item["history"]
                    if isinstance(history, list):
                        filtered = [msg for msg in history if isinstance(msg, dict)]
                        meta = {}
                        if "metadata" in item:
                            meta.update(item["metadata"])
                        return filtered, meta
                    elif isinstance(history, dict):
                        return [history], {}
                else:
                    meta = {}
                    if "metadata" in item:
                        meta.update(item["metadata"])
                    return [item], meta
        except json.JSONDecodeError:
            pass

        return [], {}

    @staticmethod
    def _parse_json_line(line: str) -> dict:
        """Parse a single JSON line and return {'messages': [...], 'metadata': {...}}.

        Handles plain dicts, metadata wrappers, and inline lists.
        Filters to only dict items (BOOL_LEAK guard).
        """
        import json
        result = {"messages": [], "metadata": {}}
        if not line:
            return result
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return result

        if isinstance(item, dict):
            if "metadata" in item:
                # Metadata wrapper: extract the metadata payload and any messages
                if isinstance(item["metadata"], dict):
                    result["metadata"].update(item["metadata"])
                elif not item.get("event"):  # Skip event markers
                    result["messages"].append(item)
            elif "event" in item:
                pass  # Skip COMPRESSION/ROLLBACK event markers
            else:
                result["messages"].append(item)
        elif isinstance(item, list):
            filtered = [msg for msg in item if isinstance(msg, dict)]
            if len(filtered) != len(item):
                logger.debug(f"_parse_json_line: filtered {len(item)-len(filtered)} non-dict items from inline list")
            result["messages"].extend(filtered)

        return result

    def load_session_from_log(
        self, 
        log_input: str, 
        target_instance: Optional[str] = None,
        clear_sub_agents_before_load: bool = False
    ) -> str:
        """Load session history from a log file path or JSON string.

        Reads JSONL log, restores conversation into self.instances[name].conversation,
        and sets up the logger for the restored session.
        Returns a status message string.
        
        Args:
            log_input: Path to log file or JSON string containing session history.
            target_instance: Name of the instance to load into (default: from metadata or 'RecoveredSession').
            clear_sub_agents_before_load: If True, clears sub-agents before loading to prevent
                stale agents from previous sessions appearing in UI. Defaults to False for
                backward compatibility.
        
        Example:
            >>> # Load session and automatically clear stale sub-agents
            >>> status = pool.load_session_from_log(path, target_instance='Maine', clear_sub_agents_before_load=True)
        """
        from agent_cascade.llm.schema import ASSISTANT, CONTENT, FUNCTION, ROLE, SYSTEM, USER

        # Issue 1 fix: Clear sub-agents at the very start if requested
        if clear_sub_agents_before_load:
            self.clear_sub_agents()
        
        log_input = log_input.strip()
        if not log_input:
            return "Error: Empty log input."

        # Delegate all parsing to the centralized helper (eliminates triple-duplication)
        messages, metadata = self._parse_json_input(log_input)
        if not messages and not metadata:
            return "Error: Input is neither a valid file path nor a valid JSON."

        if not messages:
            return "Error: No valid messages found in log input."

        # Determine instance and class
        instance_name = target_instance or metadata.get("instance_name") or "RecoveredSession"
        agent_class = (metadata.get("agent_class") or "Orchestrator").strip().lower()

        # Filter out event markers and ensure role/content exist
        cleaned_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "event" in msg:  # Skip COMPRESSION markers
                continue
            if ROLE in msg and CONTENT in msg:
                cleaned_messages.append(msg)

        if not cleaned_messages:
            return "Error: No valid conversation messages found."

        # ── Design spec §2.6: Build working set from JSONL ────────────────
        # Forward pass — find all compression markers
        def _is_compression_marker(msg: dict) -> bool:
            content = msg.get(CONTENT, '') if isinstance(msg.get(CONTENT), str) else ''
            return msg.get(ROLE) == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER)

        markers = []
        last_marker_index = -1
        for i, msg in enumerate(cleaned_messages):
            if _is_compression_marker(msg):
                markers.append(msg)
                last_marker_index = i

        # Extract summaries from ALL markers (not just the last one)
        for marker_msg in markers:
            summary_text = marker_msg.get(CONTENT, '')
            start_tag = '<context_summary>'
            end_tag = '</context_summary>'
            start = summary_text.find(start_tag) + len(start_tag)
            end = summary_text.find(end_tag, start)
            if start > len(start_tag) - 1 and end > start:
                self.instance_summaries[instance_name] = summary_text[start:end].strip()  # Latest wins

        # Build working set per design spec §5.2 line 427: [SYS][U0(first user message)][COMP1...][tail]
        if last_marker_index >= 0:
            # Extract SYSTEM message (first in list)
            system_msg = None
            if cleaned_messages and cleaned_messages[0].get(ROLE) == SYSTEM:
                system_msg = cleaned_messages[0]
            
            # Per design doc §5.2 line 427: always preserve first USER message (U0)
            first_user_msg = None
            for msg in cleaned_messages:
                if msg.get(ROLE) == USER and not _is_compression_marker(msg):
                    first_user_msg = msg
                    break
            
            tail = cleaned_messages[last_marker_index + 1:]
            working_set = ([system_msg] if system_msg else []) + ([first_user_msg] if first_user_msg else []) + markers + tail
        else:
            # No compression — full history is the working set
            working_set = cleaned_messages

        # Restore to pool — convert raw dicts from JSONL into Message objects
        restored_messages = []
        for msg_dict in working_set:
            try:
                restored_messages.append(Message(**msg_dict))
            except Exception as e:
                logger.warning(f"Failed to convert loaded message to Message object: {e}")

        # Create or update the instance with the restored conversation
        existing = self.instances.get(instance_name)
        if existing:
            # FIX 3: Wrap ALL shared state modifications under _compression_lock (reentrant RLock)
            with existing._compression_lock:
                # PR3 migration: Use centralized API for conversation replacement during session load
                existing.rebuild_conversation(restored_messages)  # Handles conversation + cache sync
                
                # Reset state fields under lock to prevent races
                existing.agent_class = agent_class
                # Clear streaming responses to prevent old session's partial messages from appearing
                # (Bug: when loading a new session, _streaming_responses from previous session persists)
                existing._streaming_responses = []
                # Reset cached working sets config version to force rebuild with new conversation
                existing._last_config_version = -1
                # Clear per-instance LLM config overrides from previous session
                existing._generate_cfg_override = None
                # Clear compression cooldown tracking (prevents stale timers affecting new session)
                existing._last_force_compress_time = 0.0
                existing._force_compress_count = 0
                # Clear slot release callback to prevent stale callbacks firing
                existing._slot_release = None
                # Reset loop detection suppression flag (one-turn cooldown shouldn't persist)
                existing._suppress_loop_detection_next_turn = False
                # Clear pending notifications from previous session
                existing._pending_notifications = []
                # Clear tool warnings from previous session
                existing._tool_warnings = []
                # Reset state to IDLE for loaded sessions (they're not actively running)
                from agent_cascade.agent_instance import AgentState
                existing.state = AgentState.IDLE
            
            # FIX 2: Increment version so instance_conversations mapping stays in sync
            self._instances_version += 1
        else:
            now = time.monotonic()
            new_inst = AgentInstance(
                instance_name=instance_name,
                agent_class=agent_class,
                conversation=restored_messages,
                max_turns=None,
                parent_instance=None,
                created_at=now,
                last_activity=now,
                compression_summary=None,
                latest_marker_index=-1,
            )
            self.instances[instance_name] = new_inst
            self._instances_version += 1

        # Set up logger for the restored session
        # CRITICAL: Must replace the logger entirely to avoid writing to the wrong file.
        # get_logger() returns the cached logger from the previous session (same instance_name),
        # whose log_path still points to the old session's JSONL file. Using update_history()
        # would append new messages to the old file, corrupting it.
        
        # Normalize agent class once (Fix #5 - deduplicate computation)
        normalized_agent_class = (agent_class or '').strip().lower()
        key = (instance_name, normalized_agent_class)

        logger_setup_ok = True  # Track success of logger setup operations
        try:
            # 1. Close the old logger's file handle (release the old file)
            with self._logger._lock:
                if key in self._logger._loggers:
                    try:
                        self._logger._loggers[key].close()
                    except Exception as e:
                        logger.warning(f"Logger close during session load failed for {instance_name} (non-critical): {e}")
                # Remove from cache so we can add the new one below (safe pop avoids KeyError)
                self._logger._loggers.pop(key, None)

            # 2. Copy original session file to new timestamped path and create logger pointing to the copy
            from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
            original_log_path = metadata.get("current_log_path")
            
            if original_log_path and Path(original_log_path).exists():
                # Copy to preserve original, work on a fresh timestamped file with new session context
                new_log_path = AgentInstanceLogger.copy_session_file(
                    source_path=original_log_path,
                    log_dir=str(self._logger.log_dir),
                    agent_class=normalized_agent_class,
                    instance_name=instance_name
                )
            else:
                # No original file - let logger create fresh timestamped file
                new_log_path = None
            
            # Create logger pointing to the copy with updated metadata for this working session
            log_inst = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(self._logger.log_dir),
                base_metadata=metadata if metadata else None,
                log_path=new_log_path,  # Point to the copy (or None for fresh)
            )

            # 3. Rewrite the copy with restored messages (rewrite_log_with_history handles truncation and new metadata)
            # BUG FIX: Pass cleaned_messages (full history from JSONL) instead of restored_messages (partial working set).
            # This ensures rewrite_log_with_history has complete marker context for accurate insert_pos calculation,
            # preventing permanent loss of pre-marker history when copy fails or file is inaccessible.
            log_inst.rewrite_log_with_history(cleaned_messages)
            
            # Cache the new logger instance
            with self._logger._lock:
                self._logger._loggers[key] = log_inst
        except Exception as e:
            logger.warning(f"Logger setup after log load failed for {instance_name} (non-critical): {e}")
            logger_setup_ok = False

        log_source = "file" if Path(log_input).exists() else "JSON input"
        return f"Successfully loaded {len(restored_messages)} messages for instance '{instance_name}' ({agent_class}) from {log_source}."

    def _compute_template_hash(self, template_name: str) -> Optional[str]:
        """Compute a hash of the template's system message for change detection.
        
        Args:
            template_name: Name of the agent template
            
        Returns:
            SHA256 hex digest of the template content, or None if template not found.
            
        Note: This method is NOT thread-safe. For thread-safety, callers should hold
              an appropriate lock when calling refresh_agents().
        """
        try:
            template = self.templates.get(template_name)
            if template is None:
                return None
            
            # system_message is a plain str (per agent.py line 69), not a Message object
            # So we access it directly, not via .content attribute
            system_msg = getattr(template, 'system_message', '')
            
            # Create a deterministic string representation for hashing
            content_str = f"{template_name}|{system_msg}"
            return hashlib.sha256(content_str.encode('utf-8')).hexdigest()
        except Exception as e:
            logger.debug(f"Error computing hash for template {template_name}: {e}")
            return None
    
    def _get_template_state(self) -> Dict[str, Any]:
        """Get current state of templates for comparison.
        
        Returns:
            Dictionary mapping template names to their hashes
        """
        return {name: self._compute_template_hash(name) for name in self.templates.keys()}
    
    def refresh_agents(self):
        """Reload all agent souls and templates from disk.
        
        Compares before/after state (both template keys and content hashes).
        Only calls notify_config_changed() if something actually changed on disk.
        This prevents unnecessary cache invalidation when user clicks "Refresh Agents"
        but no files were modified.
        """
        # Capture current state before reload
        old_template_keys = set(self.templates.keys())
        old_template_state = self._get_template_state()
        
        # Perform the reload
        self.templates.clear()
        self._discover_agents(str(self.agents_dir))
        
        # Capture new state after reload
        new_template_keys = set(self.templates.keys())
        new_template_state = self._get_template_state()
        
        # Compare keys (agents added/removed)
        keys_changed = old_template_keys != new_template_keys
        
        # Compare content hashes (agent system prompts edited)
        # Only compare templates that exist in BOTH old and new states (intersection)
        all_hashes_match = True
        common_keys = old_template_keys & new_template_keys  # templates present before AND after reload
        for key in common_keys:
            if old_template_state.get(key) != new_template_state.get(key):
                all_hashes_match = False
                logger.debug(f"[REFRESH] Template content changed: {key}")
                break
        
        # Only notify if something actually changed
        if keys_changed or not all_hashes_match:
            if keys_changed:
                added = new_template_keys - old_template_keys
                removed = old_template_keys - new_template_keys
                logger.info(f"[REFRESH] Templates changed - Added: {added}, Removed: {removed}")
            else:
                # Content modification triggers config update, log at info level for visibility
                logger.info("[REFRESH] Template content modified, triggering config update")
            self.notify_config_changed()
        else:
            logger.debug("[REFRESH] No changes detected in agent templates, skipping notification")

    def notify_config_changed(self):
        """Signal that global configuration has changed (workspace dir, templates, etc).
        
        Increments _config_version, which triggers ExecutionEngine to rebuild system prompts.
        """
        self._config_version += 1
        logger.debug(f"[CONFIG] Global configuration version incremented to {self._config_version}")

    @property
    def instance_classes(self) -> Dict[str, str]:
        """Mapping of instance_name → agent_class (derived from instances dict)."""
        return {name: inst.agent_class for name, inst in self.instances.items()}

    @property
    def instance_loggers(self) -> Dict[str, Any]:
        """Return a snapshot of per-instance loggers (string-keyed by instance_name for backward compatibility)."""
        with self._logger._lock:
            return {k[0]: v for k, v in self._logger._loggers.items()}

    @property
    def agents(self) -> Dict[str, Assistant]:
        """Alias for templates — old api_server code accesses pool.agents."""
        return self.templates

    # ── Agent template access (for compression agent invoker) ────────────

    def get_agent(self, name: str):
        """Get an agent template by name. Returns None if not found."""
        return self.get_template(name)

    def load_agent(self, name: str):
        """Load a single agent template by name (if not already loaded)."""
        from agent_cascade.agent_factory import load_agent_template

        cached = self.get_template(name)
        if cached is not None:
            return cached

        llm_cfg = (getattr(self.api_router, 'default_llm_cfg', {})
                   if self.api_router else {})
        try:
            template = load_agent_template(self, name, llm_cfg)
            self.templates[name] = template
            logger.info("[OK] Loaded agent on demand: %s", name)
            return template
        except Exception as e:
            logger.error("[ERROR] Failed to load agent %s: %s", name, e)
            raise

    def on_dismissed(self, callback):
        """Register a callback invoked when an agent instance is dismissed.

        Callback signature: callback(instance_name: str, log_path: Optional[str])
        """
        self._on_dismissed_callbacks.append(callback)

    def _fire_on_dismissed(self, instance_name: str, log_path=None):
        """Fire all registered dismissal callbacks for a dismissed agent."""
        for cb in self._on_dismissed_callbacks:
            try:
                cb(instance_name, log_path)
            except Exception as e:
                logger.error(f"Error in on_dismissed callback for {instance_name}: {e}")

    # ── Conversation management ────────────────────────────────────────────

    def add_message(self, instance_name: str, message: Message):
        """Append a message (thread-safe) to an agent's conversation.

        Simple append operation - no version tracking. Token count cache is
        invalidated on each append. The LLM API handles prefix caching automatically.
        
        This is the single point of truth for adding messages — all writes go
        directly to instances[name].conversation.
        """
        inst = self.instances.get(instance_name)
        if inst:
            inst.append_message(message)  # PR2: centralized mutation API handles cache sync
            self._mark_activity(instance_name)

    # ── Compression module compatibility layer
    # The compress_context() API in core.py expects agent_pool.get_conversation() and
    # agent_pool.instance_conversations[] — these bridge to the new instance.conversation model.

    @property
    def instance_conversations(self) -> Dict[str, List[Message]]:
        """View of all conversations as a dict (required by compression module and api_server.py).

        Returns the live _instance_conversations mapping which is kept in sync with
        self.instances. Writes to this dict propagate back to instances[name].conversation.

        Uses version-based lazy sync (Fix #3): only re-syncs when instances have changed,
        avoiding O(n) work on every read during streaming (~23+ accesses/sec).

        Version tracking lives on AgentPool (not the mapping) so it survives recreation.
        """
        if not hasattr(self, '_instance_conversations'):
            self._sync_instance_conversations()
        elif self._instances_version != self._mapping_synced_to_version:
            # Instances changed — refresh mapping
            self._instance_conversations._sync_from_instances()
            self._mapping_synced_to_version = self._instances_version
        return self._instance_conversations

    def _sync_instance_conversations(self):
        """Initialize the instance_conversations mapping from pool.instances."""
        self._instance_conversations = _InstanceConversationMapping(self)
        self._mapping_synced_to_version = self._instances_version

    def get_conversation(self, instance_name: str) -> List[Message]:
        """Get the conversation list for an agent. Returns empty list if not found."""
        inst = self.instances.get(instance_name)
        if inst is None:
            return []
        with inst._compression_lock:
            return list(inst.conversation)

    def get_compression_target_set(self, instance_name: str):
        """Returns (active_start_idx, active_set, latest_summary_idx) for compression.

        This is used by compress_context() in core.py to determine what to compress.
        """
        conv = self.get_conversation(instance_name)
        if not conv:
            return 0, [], -1

        latest_marker = self.find_last_marker(conv)

        # active_start_idx points right after the last compression marker (or after SYS if none).
        # This ensures new markers are stacked immediately after existing ones, preserving
        # the tail distance from the end of conversation.
        #
        # With multiple compressions: [SYS][COMP1][COMP2|active_start]|U3|A3|U4|A4]
        # find_last_marker returns index of COMP2, so active_start_idx = COMP2 + 1.
        # New markers are inserted right after all existing ones (stacking behavior).
        
        # active_start_idx: where the "active" (post-marker) window starts
        if latest_marker >= 0:
            active_start_idx = latest_marker + 1  # Skip past marker — markers are not part of active set
        else:
            # Skip system message at index 0 AND first user message (U0) to protect it from compression.
            # U0 contains the initial prompt/context and should always be preserved per SYSTEM_DOCS §5.2.
            # When no system message, we still skip past the first message (U0).
            from agent_cascade.llm.schema import SYSTEM as SYS_ROLE
            first_role = conv[0].get('role') if isinstance(conv[0], dict) else getattr(conv[0], 'role', '')
            active_start_idx = 2 if first_role == SYS_ROLE else 1

        active_set = conv[active_start_idx:]
        return active_start_idx, active_set, latest_marker

    def slice_history_for_llm(self, history: List[Message]) -> List[Message]:
        """Extract the working set from a conversation.

        After load_session_from_log() Fix 1, the working set is already built correctly
        (culling happened at load time). This function now acts as a safety guard:
        - If markers are already stacked near the start (post-cull), return a copy.
        - If there are gaps between markers (unculled data still present), apply culling.
        """
        if not history:
            return []

        # Find ALL marker indices to detect stacking vs unculled gaps
        marker_indices = []
        for i in range(len(history)):
            content = (
                history[i].get('content', '')
                if isinstance(history[i], dict)
                else getattr(history[i], 'content', '')
            )
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                marker_indices.append(i)

        if not marker_indices:
            return list(history)  # No markers — nothing to slice

        # Check if markers are already stacked (consecutive near the start)
        # If they're consecutive starting from index 1 (after system), culling already happened
        from agent_cascade.llm.schema import SYSTEM as SYS_ROLE
        first_role = history[0].get('role') if isinstance(history[0], dict) else getattr(history[0], 'role', '')
        expected_start = 1 if first_role == SYS_ROLE else 0

        # Redundant len() check removed: early return at line 1484 guarantees marker_indices is non-empty
        markers_stacked = (
            marker_indices[0] == expected_start and
            marker_indices[-1] == expected_start + len(marker_indices) - 1
        )

        if markers_stacked:
            # Already culled at load time — return a copy
            return list(history)

        # Unculled data still present — apply culling now (same logic as Fix 1)
        last_marker_idx = marker_indices[-1]
        tail = list(history[last_marker_idx + 1:])

        # Collect all marker messages
        marker_msgs = [history[i] for i in marker_indices]

        # Include system message at top if present — check history[0], not tail[0]
        if first_role == SYS_ROLE:
            return [history[0]] + marker_msgs + tail
        return marker_msgs + tail

    # ── Message queue operations ───────────────────────────────────────────

    def send_message(self, from_name: str, to_name: str, text: str):
        """Route a message to an agent."""
        self.message_queues.setdefault(to_name, []).append(text)

    def enqueue_message(self, instance_name: str, text: str):
        """Push a message into a specific agent's queue (no sender tracking)."""
        self.message_queues.setdefault(instance_name, []).append(text)
        self._mark_activity(instance_name)

    def drain_queue(self, instance_name: str) -> List[str]:
        """Drain all pending messages for an instance atomically.

        Batched drain operation for efficiency.

        This operation pops the entire queue at once, minimizing lock contention
        and ensuring no messages are missed between drain calls. Returns empty
        list if no messages queued.

        Args:
            instance_name: The agent instance to drain messages for.

        Returns:
            List of message texts (may be empty). Original queue is cleared.
        """
        # Atomic pop ensures thread-safe batched drain
        return self.message_queues.pop(instance_name, [])

    def has_messages(self, instance_name: str) -> bool:
        """Check if there are pending messages for an instance."""
        return bool(self.message_queues.get(instance_name))

    # ── Async Results Buffer (SLEEPING state support) ───────────────────────

    def add_async_result(self, instance_name: str, result: str, function_id: Optional[str] = None):
        """Add a completed async tool result to the buffer for an instance.

        Delegate method for backward compatibility — delegates to AsyncResultBuffer.

        Args:
            instance_name: The agent instance that dispatched this async tool.
            result: The string result from the completed async tool.
            function_id: The LLM's tool_call_id for this async call (optional).
        """
        self._async_results.put(instance_name, result, function_id=function_id)

    def drain_async_results(self, instance_name: str) -> List[Tuple[str, Optional[str]]]:
        """Drain all completed async results for an instance atomically.

        Batched drain operation for efficiency.

        Delegate method for backward compatibility — delegates to AsyncResultBuffer.
        This operation pops the entire results list at once under lock, minimizing
        lock contention and ensuring thread-safe access to async results.

        Args:
            instance_name: The agent instance to drain results for.

        Returns:
            List of (result_string, function_id) tuples (may be empty). Original buffer is cleared.
            Each tuple element is Tuple[str, Optional[str]] where the second element may be None.
        """
        return self._async_results.drain(instance_name)

    # Alias for compatibility with execution_engine.py usage
    _async_results_drain = drain_async_results

    def has_pending(self, instance_name: str) -> bool:
        """Check if there are pending async tool calls for an instance.

        Uses AsyncToolRegistry to track pending background tool entries.

        Args:
            instance_name: The agent instance to check.

        Returns:
            True if the instance has pending async tools, False otherwise.
        """
        return self._async_registry.has_pending(instance_name)

    def _acquire_slot(self, agent_class: str, instance_name: str):
        """Acquire an endpoint scheduling slot. Returns a release callback or None for unlimited endpoints."""

        if not hasattr(self, 'api_router') or not self.api_router:
            return None

        router = self.api_router
        try:
            # Get the effective concurrency for this agent class (includes default fallback)
            concurrency_limit = router.get_effective_concurrency(agent_class)

            # Resolve the actual api_base that will be used
            llm_cfg = router.get_llm_config(agent_class)
            api_base = llm_cfg.get('api_base') or llm_cfg.get('model_server', 'unknown')

            logger.debug(
                f"[CALL_AGENT_DEBUG] _acquire_slot — agent_class={agent_class}, "
                f"instance_name={instance_name}, api_base={api_base}, concurrency_limit={concurrency_limit}"
            )

            # Acquire a slot on the endpoint scheduler (blocks if at capacity)
            # SLOT_TIMEOUT FIX v2: Pass instance_name and agent_class for tracking
            return router.scheduler.acquire(api_base, concurrency_limit, instance_name, agent_class)
        except Exception as e:
            logger.error(f"Failed to acquire endpoint slot for {instance_name}: {e}")
            raise

    def register_async_call(self, instance_name: str, function_id: Optional[str] = None,
                            agent_class: Optional[str] = None, child_instance_name: Optional[str] = None,
                            args: Optional[dict] = None, caller: Optional[str] = None, nest_depth: int = 0):
        """Register and execute an async tool call via AsyncToolRegistry.

        Creates a callable that wraps the child agent execution logic (endpoint slot
        acquisition, ExecutionEngine creation, result extraction) and submits it to
        the thread pool via AsyncToolRegistry.

        Args:
            instance_name: The caller's instance name (results go here)
            function_id: The LLM's tool_call_id for this call
            agent_class: Class of child agent to run
            child_instance_name: Name of the child agent instance
            args: Tool arguments for the child agent
            caller: Name of the calling agent
            nest_depth: Nesting depth for max_nesting_depth enforcement
        """
        if not agent_class or not child_instance_name:
            logger.error(f"register_async_call requires agent_class and child_instance_name")
            return

        def run_child_agent() -> str:
            """Callable that runs the child agent and returns the result string.

            NOTE: We do NOT acquire the endpoint slot here — engine.run() inside
            _create_and_run_agent acquires its own slot at line 348 of execution_engine.py.
            Acquiring before AND inside would deadlock on Semaphore(1) (same thread,
            same semaphore). The child's engine.run() handles all concurrency control.
            """
            from agent_cascade.execution_engine import ExecutionEngine
            from agent_cascade.child_runner import run_child_core

            engine = ExecutionEngine(self)
            # initialize() now called automatically in __init__ (Phase 4.5 cleanup)

            try:
                result = run_child_core(
                    engine=engine,
                    pool=self,
                    agent_class=agent_class,
                    instance_name=child_instance_name,
                    args=args,
                    caller_name=caller,
                    child_depth=nest_depth,
                    prefix="Parallel Agent",
                )
                return result
            except Exception as e:
                # Catch generic exceptions to preserve the structured agent-specific prefix
                return f"[Parallel Agent '{child_instance_name}' Failed]:\n{str(e)}"

        self._async_registry.register(instance_name, run_child_agent, function_id=function_id)

    # ── Pause/Resume state management ───────────────────────────────────────

    def pause(self):
        """Pause ALL instances by clearing the global pause flag.
        
        Clears _paused Event so all agents block in wait_if_paused() until resumed.
        Unlike stop(), this does NOT trigger idle.stop() or async_registry.shutdown() —
        those are side effects of pool.stopped=True (the stop path), not pause.
        """
        self._paused.clear()

    def resume(self):
        """Resume all paused instances by setting the global pause flag."""
        self._paused.set()

    def is_paused(self) -> bool:
        """Check if the pool is currently paused."""
        return not self._paused.is_set()

    def wait_if_paused(self, timeout: float = 1.0) -> None:
        """Block until resumed or timeout expires. Used by execution loop to wait efficiently on pause."""
        self._paused.wait(timeout=timeout)

    # ── Instance halt check (checks both global pause + per-instance halt) ───

    def is_instance_halted(self, instance_name: str) -> bool:
        """Check if an instance is halted. Returns True if globally paused or per-instance halted.
        
        Note: resume_instance() only clears per-instance halt; call resume() first to clear _paused."""
        return self.is_paused() or instance_name in self._halted_instances
    
    # (internal helpers used by compression handler and REST endpoints)

    def halt_instance(self, instance_name: str):
        """Halt a specific instance (per-instance tracking)."""
        self._halted_instances.add(instance_name)

    def resume_instance(self, instance_name: str):
        """Resume a halted instance."""
        self._halted_instances.discard(instance_name)

    # ── Activity tracking ──────────────────────────────────────────────────

    def _mark_activity(self, instance_name: str):
        """Update last_activity timestamp for an instance."""
        inst = self.instances.get(instance_name)
        if inst:
            inst.last_activity = time.monotonic()

    # ── Convenience methods (thin wrappers around instance state) ───────────

    def is_active(self, instance_name: str) -> bool:
        """Check if an instance is currently executing (derived from state machine)."""
        inst = self.instances.get(instance_name)
        return inst.is_running if inst else False

    def is_instance_terminated(self, instance_name: str) -> bool:
        """Check if an instance has been marked for termination.

        Per-instance termination check — does NOT affect other agents (unlike _stopped_event).
        Checks terminated_instances set first (authoritative), then falls back to inst.is_terminated flag.
        """
        if instance_name in self.terminated_instances:
            return True
        inst = self.instances.get(instance_name)
        return inst.is_terminated if inst else False

    @staticmethod
    def find_last_marker(history: List[Message]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation.

        Only considers messages with role=USER (compression markers are user messages).
        Returns -1 if no marker is found.
        """
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            # Only consider USER messages (compression markers are always user role)
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def _rollback_instance(
        self,
        instance_name: str,
        *,
        pop_count: int = 0,
        target_length: int = -1,
        preserve_system_user: bool = True,
        refine_function_boundary: bool = True,
        sync_logger: bool = True,
        tail_sync_check: bool = True,
        reason: Optional[str] = None,
    ) -> int:
        """Unified shared rollback helper for a single agent instance.

        Accepts EITHER pop_count (remove N messages from end) OR target_length
        (truncate to exactly this many messages). Mutually exclusive inputs;
        if both are 0/-1 respectively, nothing happens.

        Used by all rollback paths: loop recovery, user retry, /rollback command.

        Args:
            instance_name: Name of the agent instance to rollback.
            pop_count: Number of messages to remove from the end (default 0).
            target_length: Truncate conversation to this exact length (default -1 = ignore).
            preserve_system_user: Keep SYSTEM + first USER messages intact.
            refine_function_boundary: Avoid cutting at a FUNCTION message boundary.
            sync_logger: Sync the JSONL logger after truncation.
            tail_sync_check: Run the tail-sync drift check after rollback.
            reason: Optional reason string for logging.

        Returns:
            The actual number of messages rolled back, or 0 if no rollback occurred.

        Safety guarantees (§7.3):
            1. Never removes SYSTEM message or first USER message (when preserve_system_user=True)
            2. Refines pop_count to avoid leaving dangling tool calls (when refine_function_boundary=True)
        """
        from agent_cascade.llm.schema import FUNCTION, SYSTEM

        # Resolve effective pop_count from target_length if provided
        if target_length >= 0 and pop_count == 0:
            # We'll compute pop_count inside after finding the instance length
            pass  # handled below

        inst = self.instances.get(instance_name)
        if not inst:
            logger.warning(
                f"Rollback for '{instance_name}' failed — instance not found in pool"
                + (f" ({reason})" if reason else "")
            )
            return 0

        with inst._compression_lock:
            if not inst.conversation:
                return 0

            conv = inst.conversation
            current_len = len(conv)

            # Compute effective pop_count
            if target_length >= 0 and pop_count == 0:
                pop_count = max(0, current_len - target_length)
            elif pop_count <= 0:
                return 0

            # Safety: determine minimum messages to preserve (SYSTEM + first USER)
            keep_at_least = 0
            if preserve_system_user:
                if len(conv) > 0 and getattr(conv[0], 'role', '') == SYSTEM:
                    keep_at_least = 1
                    if len(conv) > 1 and getattr(conv[1], 'role', '') == USER:
                        keep_at_least = 2

            # Refine: avoid leaving dangling FUNCTION messages at the cut boundary
            start_idx = current_len - pop_count
            if refine_function_boundary and start_idx >= keep_at_least:
                if getattr(conv[start_idx], 'role', '') == FUNCTION:
                    pop_count += 1

            new_len = max(keep_at_least, current_len - pop_count)
            del conv[new_len:]

            # Clear working set caches — conversation was trimmed so cached lists are stale.
            inst._cached_messages.clear()
            inst._cached_llm_messages.clear()
            inst._cached_token_count = 0
            inst._last_token_count_conversation_length = -1

            actual_removed = current_len - len(conv)

            # Sync logger under lock to avoid stale reads
            if sync_logger:
                try:
                    log_inst = self._logger.get_logger(instance_name, inst.agent_class)
                    log_inst.truncate_to(len(inst.conversation))

                    # ── Tail sync check after rollback (design doc §5.2 — D1 fix) ──
                    if tail_sync_check and getattr(self.settings, 'tail_sync_check_enabled', True):
                        from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                        _check_tail(instance_name, list(inst.conversation), log_inst.log_path, context="rollback")
                except Exception as e:
                    logger.debug(f"Logger truncation failed during rollback for {instance_name} (non-critical): {e}")

            return actual_removed

    # ── Public alias: surgical_rollback (backward-compatible wrapper) ───────

    def surgical_rollback(self, instance_name: str, pop_count: int, reason: Optional[str] = None) -> int:
        """Remove the last `pop_count` messages from an agent's conversation.

        Backward-compatible wrapper around `_rollback_instance`.
        Uses default safety settings (preserve SYSTEM+USER, refine boundary).
        """
        return self._rollback_instance(
            instance_name, pop_count=pop_count, reason=reason,
        )

    # ── Logger delegation ──────────────────────────────────────────────────

    def get_logger(self, instance_name: str, agent_class: str, base_metadata: Optional[Dict] = None):
        """Get or create a logger for an instance.
        
        Passes base_metadata through to LoggerManager.get_logger for supervisor tracking.
        """
        return self._logger.get_logger(instance_name, agent_class, base_metadata=base_metadata)

    # ── Agent discovery (unchanged from existing implementation) ───────────

    def _discover_agents(self, agents_dir: str):
        """Load all agent templates from the agents directory.

        Mirrors the old AgentPool._discover_agents() — scans for *_soul.md files
        and loads each one via load_agent_template().
        """
        from agent_cascade.agent_factory import load_agent_template

        agents_path = Path(agents_dir)
        if not agents_path.exists():
            agents_path.mkdir(exist_ok=True)
            return

        for soul_file in agents_path.glob('*_soul.md'):
            agent_name = soul_file.name.replace('_soul.md', '')
            try:
                # Need llm_cfg from api_router or fall back to empty dict
                llm_cfg = (getattr(self.api_router, 'default_llm_cfg', {})
                           if self.api_router else {})
                template = load_agent_template(self, agent_name, llm_cfg)
                self.templates[agent_name] = template
                logger.info("[OK] Loaded agent: %s", agent_name)
            except Exception as e:
                logger.error("[ERROR] Failed to load agent %s: %s", agent_name, e)


# ── Manager classes for parallel execution state ─────

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


class LoggerManager:
    """Manages per-agent loggers. Returns real AgentInstanceLogger instances.

    Thread-safe via _lock for concurrent access during parallel agent execution.
    Log files are stored in <workspace_dir>/logs/ subdirectory.
    """

    def __init__(self, pool: AgentPool, workspace_dir: Optional[str]):
        self.pool = pool
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path(DEFAULT_WORKSPACE)
        # Ensure log directory exists
        self.log_dir = self.workspace_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
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
            self._loggers[key] = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(self.log_dir),
                base_metadata=None,  # Fresh start, no inherited context
            )
        return


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

        # MAJOR FIX: Read state and last_activity under same lock for consistency (prevents TOCTOU race)
        with inst._state_lock:
            state = inst.state
            last_activity = inst.last_activity
        
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
        idle_secs = time.monotonic() - last_activity
        if idle_secs < self.pool.settings.idle_timeout_seconds:
            return False

        return True

    def _auto_dismiss(self, instance_name: str):
        """Dismiss a single idle agent and clean up its resources."""
        inst = self.pool.instances.get(instance_name)
        if not inst:
            return

        idle_secs = time.monotonic() - inst.last_activity

        # Capture log path before clearing
        log_path = None
        try:
            log_inst = self.pool._logger.get_logger(instance_name, inst.agent_class)
            log_path = getattr(log_inst, 'log_path', None)
        except Exception as e:
            logger.debug(f"Idle checker log path lookup failed for {instance_name} (non-critical): {e}")

        logger.info(
            f"[idle_checker] Auto-dismissing idle agent '{instance_name}' "
            f"(idle for {idle_secs:.0f}s, threshold={self.pool.settings.idle_timeout_seconds:.0f}s)"
        )

        # Remove the instance (fires dismissal callbacks)
        self.pool.dismiss_instance(instance_name)