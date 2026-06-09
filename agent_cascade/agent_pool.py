"""
Lean Agent Pool — Phase 1 of the AgentCascade Architecture Rewrite.

Replaces the old god-object AgentPool (~25 attributes, ~1100 lines) with a thin
coordinator that owns only the instance registry, template registry, and simple
state structures. Logger lifecycle, idle detection, and parallel execution are
delegated to focused managers (LoggerManager, IdleManager, ParallelAgentManager).

See DESIGN_REWRITE.md §2.2 for design rationale.
"""

import time
import threading
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_cascade.agents import Assistant
from agent_cascade.llm.schema import Message
from agent_cascade.log import logger
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.settings import DEFAULT_WORKSPACE

from .agent_instance import AgentInstance, PoolSettings


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
        
        NOTE: Token count cache is invalidated automatically here when the instance exists.
        If callers bypass this method (e.g., direct assignment), they must invalidate manually.
        See Fix #2 for details.
        """
        inst = self._pool.instances.get(key)
        if inst is not None:
            with inst._compression_lock:
                inst.conversation = list(value)  # defensive copy
            # Fix #2: Invalidate token count cache — conversation was replaced
            inst._last_token_count_conversation_length = -1
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        """Delete from dict storage and clear conversation, but don't delete the instance."""
        super().__delitem__(key)
        inst = self._pool.instances.get(key)
        if inst is not None:
            with inst._compression_lock:
                inst.conversation.clear()

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
        
        # Copy under lock BEFORE clearing — prevents both data loss and races
        with inst._compression_lock:
            value = list(inst.conversation)
            inst.conversation.clear()
        
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
            with inst._compression_lock:
                inst.conversation.clear()


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
        self._halted_instances: set = set()                # per-instance halt state
        self._compression_halted: set = set()              # instances halted by forced compression (not manual)
        self.terminated_instances: set = set()             # instances marked for immediate termination
        self.children: Dict[str, List[str]] = {}           # parent_name -> [child_names] for cascade termination

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
        self._async_results = {}  # type: Dict[str, List[str]]  # instance_name -> list of result strings
        self._async_lock = threading.RLock()  # Lock for _async_results access
        
        # Simple inline registry for tracking pending async tools
        self._async_registry = {
            'pending': {},  # type: Dict[str, set]  # instance_name -> set of tool call IDs
            'lock': threading.RLock()  # Lock for pending tracking
        }

        # ── Global state ─────────────────────────────────────────────────────
        self._stopped_event = threading.Event()         # M3 fix: stopped flag for emergency shutdown

        # ── Version counter for lazy sync of instance_conversations (Fix #3) ──
        self._instances_version = 0                        # increments on create/remove/dismiss/reset
        self._mapping_synced_to_version = -1              # tracks last version instance_conversations was synced to

        # Dismissal callbacks (used by api_server to broadcast real-time tab removal)
        self._on_dismissed_callbacks: list = []

        # ── Agent discovery (unchanged) ──────────────────────────────────────
        self.agents_dir = Path(agents_dir)
        self._discover_agents(agents_dir)

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
        else:
            self._stopped_event.clear()

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
            is_active=False,
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
        self._mark_activity(instance_name)
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
        self.message_queues.pop(instance_name, None)
        # Clean up mapping's dict storage to prevent stale keys
        if hasattr(self, '_instance_conversations'):
            try:
                del self._instance_conversations[instance_name]
            except KeyError as e:
                logger.debug(f"Instance conversation cleanup key missing (expected): {e}")
        # Clean up logger entry for the instance
        with self._logger._lock:
            log_inst = self._logger._loggers.pop(instance_name, None)

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
        from agent_cascade.api_integration import (
            _max_tokens_cache, _last_stream_versions,
            _cached_instance_data, _stream_token_stats_cache
        )
        _max_tokens_cache.pop(instance_name, None)
        _last_stream_versions.pop(instance_name, None)
        _cached_instance_data.pop(instance_name, None)
        _stream_token_stats_cache.pop(instance_name, None)
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
                was_already_halted = self.is_instance_halted(inst_name)
                self.halt_instance(inst_name)
                # Only track instances that weren't already halted — preserves manual halts
                if not was_already_halted:
                    self._compression_halted.add(inst_name)

    def resume_all_instances(self):
        """Resume only the instances that were halted by forced compression (not manual halts)."""
        for inst_name in list(self._compression_halted):
            self.resume_instance(inst_name)
        self._compression_halted.clear()

    def terminate_instance(self, instance_name: str):
        """Mark an instance for immediate termination.

        Adds to terminated_instances set and sets the stopped event if active.
        Also marks the instance itself as terminated (Fix Bug41).
        Cascade-terminates all child agents recursively (Fix Bug41).
        Mirrors old AgentPool.terminate_instance() semantics.
        """
        # First cascade-terminate all children (recursive, Fix Bug41)
        for child_name in list(self.children.get(instance_name, [])):
            if self.instances.get(child_name):
                self.terminate_instance(child_name)  # Recursive — handles nested trees
        
        self.terminated_instances.add(instance_name)
        inst = self.instances.get(instance_name)
        if inst and inst.is_active:
            self._stopped_event.set()      # Global signal FIRST (minimize race window)
            inst.is_terminated = True      # Per-instance mark second

    def dismiss_instance(self, instance_name: str):
        """Remove an instance from the pool. If active, terminate it; otherwise clean up.

        Recursively dismisses all child agents first (cascade termination, Fix Bug41).
        This is the UI-initiated termination path (WebSocket terminate_agent_instance message).
        Mirrors old AgentPool.dismiss_instance() semantics.
        """
        # First dismiss all children (recursive cascade, Fix Bug41)
        for child_name in list(self.children.get(instance_name, [])):
            if self.instances.get(child_name):
                self.dismiss_instance(child_name)  # Recursive — handles nested trees
        
        if self.is_active(instance_name):
            self.terminate_instance(instance_name)
        # Always remove the instance from the pool so its tab disappears from the UI
        self.remove_instance(instance_name)
        # Clean up stale entry in terminated_instances set (Fix Bug41 reviewer feedback)
        self.terminated_instances.discard(instance_name)

    # ── API bridge methods for api_server.py ────────────────────────────────
    # These methods provide access patterns that api_server.py expects.

    def is_halted(self, instance_name: str) -> bool:
        """Alias for is_instance_halted — required by api_server.py."""
        return self.is_instance_halted(instance_name)

    def list_agents(self) -> List[str]:
        """Return all available agent template names."""
        return list(self.templates.keys())

    def reset(self):
        """Full reset of agent state (halted, active stack, tool args, terminated).

        Clears all per-instance state including halted instances, compression halts,
        terminated instances, active stack, last_tool_args, and instance_state.

        IMPORTANT: Does NOT delete AgentInstances — only clears their conversations.
        The old reset() destroyed all instances via _instance_conversations.clear(),
        which called self._pool.instances.clear(). That broke the main session after reset.
        """
        self._halted_instances.clear()
        self._compression_halted.clear()
        self.terminated_instances.clear()
        self.children.clear()  # Fix Bug41: clear parent-child tracking on reset
        self.active_stack_clear()
        self.last_tool_args.clear()
        self.instance_state.clear()
        # Fix #3: increment version since conversation state changed
        self._instances_version += 1
        # Clear conversations of all instances without deleting the instances
        if hasattr(self, '_instance_conversations'):
            self._instance_conversations.clear()
        else:
            for inst in self.instances.values():
                with inst._compression_lock:
                    inst.conversation.clear()
                    # Fix #2: Invalidate token count cache — conversation was cleared
                    inst._last_token_count_conversation_length = -1
                    # Reset compression tracking fields (Feature 018)
                    inst._last_force_compress_time = 0.0
                    inst._force_compress_count = 0
        # Clean up WebSocket references to prevent stale queue/loop usage in new sessions
        self._ws_send_queue = None
        self._ws_loop = None

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
            with inst._compression_lock:
                inst.conversation.clear()
                # Invalidate token count cache — conversation cleared
                inst._last_token_count_conversation_length = -1
                # Reset compression tracking fields (Feature 018)
                inst._last_force_compress_time = 0.0
                inst._force_compress_count = 0

    def capture_snapshots(self) -> Dict[str, int]:
        """Capture current conversation lengths for all instances."""
        result = {}
        for name, inst in self.instances.items():
            with inst._compression_lock:
                result[name] = len(inst.conversation)
        return result

    def rollback_to_snapshots(self, snapshots: Dict[str, int], reason: Optional[str] = None):
        """Rollback all instances to the lengths recorded in snapshots.
        
        Truncates conversation lists and notifies loggers via LoggerManager.
        """
        for name, target_len in snapshots.items():
            inst = self.instances.get(name)
            if inst:
                with inst._compression_lock:
                    if len(inst.conversation) > target_len:
                        del inst.conversation[target_len:]
                        # Invalidate token count cache — conversation length changed
                        inst._last_token_count_conversation_length = -1
                try:
                    log_inst = self._logger.get_logger(name, inst.agent_class)
                    log_inst.truncate_to(target_len)
                except Exception as e:
                    logger.debug(f"Logger truncation failed for {name} (non-critical): {e}")

    def load_session_from_log(self, log_input: str, target_instance: Optional[str] = None) -> str:
        """Load session history from a log file path or JSON string.
        
        Reads JSONL log, restores conversation into self.instances[name].conversation,
        and sets up the logger for the restored session.
        Returns a status message string.
        """
        import json
        from agent_cascade.llm.schema import ASSISTANT, CONTENT, FUNCTION, ROLE, SYSTEM, USER

        log_input = log_input.strip()
        if not log_input:
            return "Error: Empty log input."

        messages = []
        metadata = {}

        # Try as file path first (resolve relative paths against workspace_dir)
        potential_path = Path(log_input)
        if not potential_path.is_absolute():
            ws = self._logger.workspace_dir if self._logger.workspace_dir else Path(DEFAULT_WORKSPACE)
            potential_path = ws / potential_path

        if potential_path.exists() and potential_path.is_file():
            try:
                with open(potential_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                            if "metadata" in item:
                                metadata.update(item["metadata"])
                            else:
                                messages.append(item)
                        except json.JSONDecodeError as e:
                            logger.debug(f"Skipping malformed JSONL line in log file: {e}")
                            continue
                log_source = f"file '{potential_path.name}'"
            except Exception as e:
                return f"Error reading log file: {e}"
        else:
            # Try as JSON (single line or block)
            try:
                lines = log_input.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        if "metadata" in item:
                            metadata.update(item)
                        elif isinstance(item, list):  # Full history block
                            messages.extend(item)
                        else:
                            messages.append(item)
                    except json.JSONDecodeError as e:
                        if len(lines) == 1:
                            raise  # Re-raise to try full-block parse
                        logger.debug(f"Skipping malformed JSONL line in inline input: {e}")
                        continue
                log_source = "JSON input"
            except json.JSONDecodeError:
                try:
                    item = json.loads(log_input)
                    if isinstance(item, list):
                        messages = item
                    elif isinstance(item, dict) and "history" in item:
                        messages = item["history"]
                        if "metadata" in item:
                            metadata.update(item["metadata"])
                    else:
                        messages = [item]
                    log_source = "JSON block"
                except json.JSONDecodeError:
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

        # Build working set per design spec
        if last_marker_index >= 0:
            # [SYSTEM if present] + [all markers stacked] + [tail after last marker, no event markers]
            system_msg = None
            if cleaned_messages and cleaned_messages[0].get(ROLE) == SYSTEM:
                system_msg = cleaned_messages[0]
            tail = cleaned_messages[last_marker_index + 1:]
            working_set = ([system_msg] if system_msg else []) + markers + tail
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
            with existing._compression_lock:
                existing.conversation = restored_messages
                # Invalidate token count cache — conversation replaced
                existing._last_token_count_conversation_length = -1
                existing.agent_class = agent_class
        else:
            now = time.monotonic()
            new_inst = AgentInstance(
                instance_name=instance_name,
                agent_class=agent_class,
                conversation=restored_messages,
                is_active=False,
                max_turns=None,
                parent_instance=None,
                created_at=now,
                last_activity=now,
                compression_summary=None,
                latest_marker_index=-1,
            )
            self.instances[instance_name] = new_inst

        self._instances_version += 1  # Fix #3: version bump when instances change via load

        # Set up logger for the restored session
        try:
            log_inst = self._logger.get_logger(instance_name, agent_class)
            log_inst.update_history(restored_messages)
        except Exception as e:
            logger.debug(f"Logger history sync after log load failed for {instance_name} (non-critical): {e}")

        return f"Successfully loaded {len(restored_messages)} messages for instance '{instance_name}' ({agent_class}) from {log_source}."

    def refresh_agents(self):
        """Reload all agent souls and templates from disk."""
        self.templates.clear()
        self._discover_agents(str(self.agents_dir))

    @property
    def instance_classes(self) -> Dict[str, str]:
        """Mapping of instance_name → agent_class (derived from instances dict)."""
        return {name: inst.agent_class for name, inst in self.instances.items()}

    @property
    def instance_loggers(self) -> Dict[str, Any]:
        """Return a snapshot of per-instance loggers (thread-safe copy under lock)."""
        with self._logger._lock:
            return dict(self._logger._loggers)

    @property
    def agents(self) -> Dict[str, Assistant]:
        """Alias for templates — old api_server code accesses pool.agents."""
        return self.templates

    # ── Agent template access (for compression agent invoker) ────────────

    def get_agent(self, name: str):
        """Get an agent template by name. Returns None if not found."""
        return self.templates.get(name)

    def load_agent(self, name: str):
        """Load a single agent template by name (if not already loaded)."""
        from agent_cascade.agent_factory import load_agent_template

        if name in self.templates:
            return self.templates[name]

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

        This is the single point of truth for adding messages — all writes go
        directly to instances[name].conversation. The instance_conversations
        mapping is a convenience view used by other components.
        Also persists the message to the JSONL log file.
        """
        inst = self.instances.get(instance_name)
        if inst:
            with inst._compression_lock:
                inst.conversation.append(message)
                # Invalidate token count cache — conversation length changed
                inst._last_token_count_conversation_length = -1
            self._mark_activity(instance_name)
            # Persist message to JSONL log file
            try:
                log_inst = self.get_logger(instance_name, inst.agent_class)
                log_inst.log_message(message)
            except Exception as e:
                            logger.debug(f"Log message write failed for {instance_name} (non-critical): {e}")
            
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

        # active_start_idx: where the "active" (post-marker) window starts
        if latest_marker >= 0:
            active_start_idx = latest_marker + 1  # Skip past marker — markers are not part of active set
        else:
            # Skip system message at index 0 if present
            from agent_cascade.llm.schema import SYSTEM as SYS_ROLE
            first_role = conv[0].get('role') if isinstance(conv[0], dict) else getattr(conv[0], 'role', '')
            active_start_idx = 1 if first_role == SYS_ROLE else 0

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

        markers_stacked = (
            len(marker_indices) > 0 and
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
        """Drain all pending messages for an instance."""
        return self.message_queues.pop(instance_name, [])

    def has_messages(self, instance_name: str) -> bool:
        """Check if there are pending messages for an instance."""
        return bool(self.message_queues.get(instance_name))

    # ── Async Results Buffer (SLEEPING state support) ───────────────────────

    def add_async_result(self, instance_name: str, result: str):
        """Add a completed async tool result to the buffer for an instance.
        
        Args:
            instance_name: The agent instance that dispatched this async tool.
            result: The string result from the completed async tool.
        """
        with self._async_lock:
            if instance_name not in self._async_results:
                self._async_results[instance_name] = []
            self._async_results[instance_name].append(result)

    def drain_async_results(self, instance_name: str) -> List[str]:
        """Drain all completed async results for an instance.
        
        Args:
            instance_name: The agent instance to drain results for.
            
        Returns:
            List of result strings (may be empty).
        """
        with self._async_lock:
            return self._async_results.pop(instance_name, [])

    # Alias for compatibility with execution_engine.py usage
    _async_results_drain = drain_async_results

    def has_pending(self, instance_name: str) -> bool:
        """Check if there are pending async tool calls for an instance.
        
        Args:
            instance_name: The agent instance to check.
            
        Returns:
            True if the instance has pending async tools, False otherwise.
        """
        with self._async_registry['lock']:
            return bool(self._async_registry['pending'].get(instance_name))

    def register_async_call(self, instance_name: str, call_id: str):
        """Register a new async tool call for an instance.
        
        Args:
            instance_name: The agent instance making the async call.
            call_id: Unique identifier for this async call.
        """
        with self._async_registry['lock']:
            if instance_name not in self._async_registry['pending']:
                self._async_registry['pending'][instance_name] = set()
            self._async_registry['pending'][instance_name].add(call_id)

    def complete_async_call(self, instance_name: str, call_id: str):
        """Mark an async tool call as completed for an instance.
        
        Args:
            instance_name: The agent instance that made the async call.
            call_id: The identifier of the completed call.
        """
        with self._async_registry['lock']:
            if instance_name in self._async_registry['pending']:
                self._async_registry['pending'][instance_name].discard(call_id)
                # Clean up empty sets
                if not self._async_registry['pending'][instance_name]:
                    del self._async_registry['pending'][instance_name]

    # ── Halt state management ──────────────────────────────────────────────

    def halt_instance(self, instance_name: str):
        """Halt a specific instance."""
        self._halted_instances.add(instance_name)

    def resume_instance(self, instance_name: str):
        """Resume a halted instance."""
        self._halted_instances.discard(instance_name)

    def is_instance_halted(self, instance_name: str) -> bool:
        """Query halt state for an instance."""
        return instance_name in self._halted_instances

    # ── Activity tracking ──────────────────────────────────────────────────

    def _mark_activity(self, instance_name: str):
        """Update last_activity timestamp for an instance."""
        inst = self.instances.get(instance_name)
        if inst:
            inst.last_activity = time.monotonic()

    # ── Convenience methods (thin wrappers around instance state) ───────────

    def is_active(self, instance_name: str) -> bool:
        """Check if an instance is currently executing."""
        inst = self.instances.get(instance_name)
        return inst.is_active if inst else False

    def is_instance_terminated(self, instance_name: str) -> bool:
        """Check if an instance has been marked for termination.
        
        Per-instance termination check — does NOT affect other agents (unlike _stopped_event).
        Checks terminated_instances set first (authoritative), then falls back to inst.is_terminated flag.
        """
        if instance_name in self.terminated_instances:
            return True
        inst = self.instances.get(instance_name)
        return inst.is_terminated if inst else False

    def find_last_marker(self, history: List[Message]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation.

        Returns -1 if no marker is found.
        """
        for i in range(len(history) - 1, -1, -1):
            content = (
                history[i].get('content', '')
                if isinstance(history[i], dict)
                else getattr(history[i], 'content', '')
            )
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def surgical_rollback(self, instance_name: str, pop_count: int, reason: Optional[str] = None):
        """Remove the last `pop_count` messages from an agent's conversation.

        Used by loop recovery to roll back repetitive patterns.

        Args:
            instance_name: Name of the agent instance to rollback.
            pop_count: Number of messages to remove from the end.
            reason: Optional reason string for logging.

        Safety guarantees (§7.3):
            1. Never removes SYSTEM message or first USER message
            2. Caps rollback at 50% of removable history per operation
            3. Refines pop_count to avoid leaving dangling tool calls
        """
        from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM

        if pop_count <= 0:
            return

        inst = self.instances.get(instance_name)
        if not inst:
            return

        with inst._compression_lock:
            if not inst.conversation:
                return

            conv = inst.conversation

            # Safety: determine minimum messages to preserve (SYSTEM + first USER)
            keep_at_least = 0
            if len(conv) > 0 and getattr(conv[0], 'role', '') == SYSTEM:
                keep_at_least = 1
                if len(conv) > 1 and getattr(conv[1], 'role', '') == 'user':
                    keep_at_least = 2

            removable = len(conv) - keep_at_least
            if removable <= 0:
                return

            # Safety cap: never rollback more than 50% of removable history in one op
            max_pop = max(1, removable // 2)
            if pop_count > max_pop:
                logger.warning(
                    f"Surgical rollback for {instance_name}: capping from "
                    f"{pop_count} to {max_pop}" + (f" ({reason})" if reason else "")
                )
                pop_count = max_pop

            # Refine: avoid leaving dangling FUNCTION messages at the cut boundary
            start_idx = len(conv) - pop_count
            if (start_idx >= keep_at_least
                    and getattr(conv[start_idx], 'role', '') == FUNCTION):
                pop_count += 1

            new_len = max(keep_at_least, len(conv) - pop_count)
            del conv[new_len:]
            # Invalidate token count cache — conversation length changed
            inst._last_token_count_conversation_length = -1

            # Sync logger under lock to avoid stale reads
            try:
                log_inst = self._logger.get_logger(instance_name, inst.agent_class)
                log_inst.truncate_to(len(inst.conversation))
            except Exception as e:
                logger.debug(f"Logger truncation failed during rollback for {instance_name} (non-critical): {e}")

    # ── Parallel execution delegation ──────────────────────────────────────

    def submit_parallel(
        self, agent_class: str, instance_name: str, args: dict, history: List[Message], caller: str, nest_depth: int = 0, call_id: str = None
    ):
        """Submit a parallel agent task.

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
            call_id: Unique identifier for this async call (generated by caller for tracking).
        """
        logger.debug(
            f"[CALL_AGENT_DEBUG] submit_parallel ENTRY — target={instance_name}, class={agent_class}, "
            f"caller={caller}, nest_depth={nest_depth}, call_id={call_id}"
        )
        result = self._execution.submit_task(agent_class, instance_name, args, history, caller, nest_depth, call_id=call_id)
        logger.debug(f"[CALL_AGENT_DEBUG] submit_parallel EXIT — target={instance_name}, result_preview={str(result)[:200]}")
        return result

    # ── Logger delegation ──────────────────────────────────────────────────

    def get_logger(self, instance_name: str, agent_class: str):
        """Get or create a logger for an instance."""
        return self._logger.get_logger(instance_name, agent_class)

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


# ── Placeholder manager classes (to be implemented in later phases) ─────

class ParallelAgentManager:
    """Manages parallel agent execution via thread pool. Active_stack, task lifecycle."""

    def __init__(self, pool: AgentPool):
        
        self.pool = pool
        self.active_stack: List[tuple] = []  # Stack of (instance_name, nest_depth) tuples for active agents
        self.active_tasks: Dict[str, tuple] = {}  # instance_name → (Future, caller, agent_class)
        # RLock (re-entrant) — compression can run in the same thread as outer ExecutionEngine.run()
        # which may already hold this lock. Using RLock prevents deadlock.
        self._state_lock = threading.RLock()

        # Thread pool for parallel execution — uses pool.settings.max_workers (default 10)
        try:
            max_workers = pool.settings.max_workers
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
        except Exception as e:
            logger.debug(f"Parallel executor initialization failed (non-critical): {e}")
            self.executor = None

    def resize_executor(self, max_workers: int) -> bool:
        """Resize the ThreadPoolExecutor to a new worker count.
        
        Shuts down the old executor and creates a new one with the specified number
        of workers. Any tasks still running on the old executor will be lost — this
        is intended for configuration changes between sessions, not during active work.
        
        All submitted-but-not-started tasks are cancelled. Running tasks are NOT
        interrupted (ThreadPoolExecutor.shutdown(wait=False) default behavior).

        Args:
            max_workers: New maximum number of worker threads.

        Returns:
            True if resize succeeded, False if executor was None or exception occurred.
        """
        if self.executor is None:
            return False

        old_executor = self.executor
        try:
            # Cancel pending tasks (don't wait for running ones to finish)
            old_executor.shutdown(wait=False, cancel_futures=True)
            self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
            logger.info(f"[THREAD_POOL] Resized executor: {max_workers} workers (was {old_executor._max_workers})")
            return True
        except Exception as e:
            # Restore old executor if resize fails
            logger.error(f"[THREAD_POOL] Failed to resize executor to {max_workers}: {e}")
            try:
                old_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self.executor = None
            return False

    def has_active_tasks(self, instance_name: str) -> bool:
        """Check if there are active parallel tasks for a given instance (caller)."""
        return any(caller == instance_name for _, caller, _ in self.active_tasks.values())

    def count_by_class(self, agent_class: str) -> int:
        """Count active instances of a given class. Thread-safe via _state_lock."""
        with self._state_lock:
            return sum(1 for (name, _depth) in self.active_stack if self.pool.get_instance(name) and
                       self.pool.get_instance(name).agent_class.lower() == agent_class.lower())

    def _acquire_slot(self, agent_class: str, instance_name: str):
        """Acquire an endpoint scheduling slot. Returns a release callback or None for unlimited endpoints."""

        if not hasattr(self.pool, 'api_router') or not self.pool.api_router:
            return None

        router = self.pool.api_router
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
            return router.scheduler.acquire(api_base, concurrency_limit)
        except Exception as e:
            logger.error(f"Failed to acquire endpoint slot for {instance_name}: {e}")
            raise

    def submit_task(self, agent_class, instance_name, args, history, caller, nest_depth: int = 0, call_id: str = None):
        """Submit an agent to run in the background thread pool. Returns immediately.
        
        Item 13: Acquires endpoint scheduling slot BEFORE submitting to thread pool,
        with proper release in finally block when the task completes.

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
            call_id: Unique identifier for this async call (generated by caller for tracking).
        """
        logger.debug(
            f"[CALL_AGENT_DEBUG] submit_task ENTRY — target={instance_name}, class={agent_class}, "
            f"caller={caller}, nest_depth={nest_depth}, call_id={call_id}"
        )

        if not self.executor:
            logger.error(f"[CALL_AGENT_DEBUG] submit_task EXIT (early) — target={instance_name}, reason=no_executor")
            return f"[Agent '{instance_name}' requested in parallel mode but no thread pool available.]"

        # Acquire endpoint slot before submitting (blocks if at capacity)
        try:
            endpoint_release = self._acquire_slot(agent_class, instance_name)
            logger.debug(f"[CALL_AGENT_DEBUG] submit_task — acquired endpoint slot for {instance_name}")
        except Exception as e:
            logger.error(f"[CALL_AGENT_DEBUG] submit_task EXIT (early) — target={instance_name}, reason=slot_acquisition_failed, error={e}")
            return f"[Agent '{instance_name}' failed to acquire endpoint slot: {e}]"

        # Deep copy history for thread safety
        import copy as _copy
        safe_history = _copy.deepcopy(history)

        def task_wrapper():
            logger.debug(
                f"[CALL_AGENT_DEBUG] task_wrapper START — target={instance_name}, class={agent_class}, "
                f"caller={caller}, nest_depth={nest_depth}"
            )
            try:
                from agent_cascade.execution_engine import ExecutionEngine
                from agent_cascade.compression.helpers import extract_instance_output

                # NOTE: This creates a NEW ExecutionEngine instance per parallel task.
                # This is intentional — each thread gets its own engine to avoid shared state issues.
                engine = ExecutionEngine(self.pool)
                logger.debug(
                    f"[CALL_AGENT_DEBUG] task_wrapper — created new ExecutionEngine for {instance_name}, "
                    f"engine_id={id(engine)}"
                )

                # Use shared helper for agent creation and execution
                # _create_and_run_agent handles active_stack append/cleanup in its finally block
                logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper — calling engine._create_and_run_agent for {instance_name}")
                inst, conv = engine._create_and_run_agent(agent_class, instance_name, args, caller, nest_depth)

                # Check return values — Bug #1 check
                if inst is None or conv is None:
                    logger.error(
                        f"[CALL_AGENT_DEBUG] BUG DETECTED in task_wrapper — _create_and_run_agent returned None for {instance_name}: "
                        f"inst={inst}, conv_type={type(conv).__name__}"
                    )
                    error_msg = f"[Parallel Agent '{instance_name}' Failed]: Internal error — agent creation returned None."
                    self.pool.send_message(instance_name, caller, error_msg)
                    return

                logger.debug(
                    f"[CALL_AGENT_DEBUG] task_wrapper — _create_and_run_agent returned for {instance_name}: "
                    f"inst_type={type(inst).__name__}, conv_len={len(conv)}"
                )

                # Notify caller via async message queue
                result = extract_instance_output(conv, instance_name)
                if not result:
                    logger.warning(
                        f"[CALL_AGENT_DEBUG] task_wrapper — extract_instance_output returned empty for {instance_name}"
                    )
                logger.debug(
                    f"[CALL_AGENT_DEBUG] task_wrapper — extract_instance_output for {instance_name}: "
                    f"result_preview={str(result)[:200]}"
                )
                completion_msg = f"[Parallel Agent '{instance_name}' Finished]:\n{result}"

                logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper — sending completion message to caller {caller}")
                
                # Add result to async results buffer for the caller
                self.pool.add_async_result(caller, completion_msg)
                
                # Complete the async call in the registry using the passed-through call_id
                if call_id:
                    self.pool.complete_async_call(caller, call_id)
                    logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper — completed async call {call_id} for caller {caller}")
                else:
                    logger.warning(f"[CALL_AGENT_DEBUG] task_wrapper — no call_id provided for {instance_name}")
                
                self.pool.send_message(instance_name, caller, completion_msg)

            except Exception as e:
                logger.error(
                    f"[CALL_AGENT_DEBUG] task_wrapper EXCEPTION — target={instance_name}, "
                    f"error_type={type(e).__name__}, error={e}"
                )
                error_msg = f"[Parallel Agent '{instance_name}' Failed]:\n{str(e)}"
                
                # Add error to async results buffer for the caller
                self.pool.add_async_result(caller, error_msg)
                
                # Complete the async call in the registry using the passed-through call_id
                if call_id:
                    self.pool.complete_async_call(caller, call_id)
                    logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper — completed async call {call_id} for caller {caller}")
                else:
                    logger.warning(f"[CALL_AGENT_DEBUG] task_wrapper — no call_id provided for {instance_name}")
                
                self.pool.send_message(instance_name, caller, error_msg)
            finally:
                # Release endpoint slot when agent completes
                if endpoint_release is not None:
                    try:
                        endpoint_release()
                        logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper — released endpoint slot for {instance_name}")
                    except Exception as e:
                        logger.error(f"Failed to release endpoint slot for {instance_name}: {e}")

                self.pool._mark_activity(instance_name)
                logger.debug(f"[CALL_AGENT_DEBUG] task_wrapper EXIT — target={instance_name}")

        future = self.executor.submit(task_wrapper)
        logger.debug(
            f"[CALL_AGENT_DEBUG] submit_task EXIT (success) — target={instance_name}, "
            f"future_id={id(future)}"
        )

        return f"[Started agent '{instance_name}' in parallel. You will be notified when it finishes.]"


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
        self._loggers: Dict[str, Any] = {}  # instance_name → logger instance
        self._lock = threading.Lock()  # Protects _loggers dict access

    def get_logger(self, instance_name: str, agent_class: str):
        """Get or create a real AgentInstanceLogger for an instance."""
        with self._lock:
            if instance_name not in self._loggers:
                from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
                self._loggers[instance_name] = AgentInstanceLogger(
                    agent_class=agent_class,
                    instance_name=instance_name,
                    log_dir=str(self.log_dir),
                )
            return self._loggers[instance_name]

    def create_new_session(self, instance_name: str, agent_class: str) -> None:
        """Replace the logger for an instance with a fresh one (new timestamp = new JSONL file).
        
        Used by "New Session" to start writing to a new log file instead of appending.
        Closes the old logger's file handle before replacing it.
        """
        with self._lock:
            # Close old logger's file handle if present
            if instance_name in self._loggers:
                try:
                    self._loggers[instance_name].close()
                except Exception as e:
                    logger.debug(f"Logger close during reinit failed for {instance_name} (non-critical): {e}")
            from agent_cascade.logger.agent_instance_logger import AgentInstanceLogger
            self._loggers[instance_name] = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(self.log_dir),
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
        """Signal the checker to stop and wait for it to exit."""
        if self._checker_thread is not None and self._checker_thread.is_alive():
            self._stop_event.set()
            timeout = self.pool.settings.idle_check_interval + 5.0
            self._checker_thread.join(timeout=timeout)
            if self._checker_thread.is_alive():
                logger.warning("Idle checker thread did not exit in time, forcing shutdown.")
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

        # Must NOT be actively running
        with self.pool._execution._state_lock:
            if any(n == instance_name for n, _depth in self.pool._execution.active_stack):
                return False

        # Must NOT be halted (halted agents are intentionally paused, e.g. during compression)
        if self.pool.is_instance_halted(instance_name):
            return False

        # Must have exceeded the idle timeout threshold
        idle_secs = time.monotonic() - inst.last_activity
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