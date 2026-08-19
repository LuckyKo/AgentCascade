"""
AgentPool — thin coordinator for all agent state. Composes the Phase-2 mixins (lifecycle, conversation, message queue, slots, config persistence, rollback, session I/O). __init__, properties, and small helper methods live here.
"""

from __future__ import annotations
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.agents import Assistant
from agent_cascade.instance_id import get_instance_id, make_instance_dir
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from ..agent_instance import AgentInstance, PoolSettings, AgentState, ACTIVE_STATES
from ..async_tools import AsyncToolRegistry

from .conversation_map import _InstanceConversationMapping
from .parallel_manager import ParallelAgentManager
from .logger_mgr import LoggerManager
from .idle_manager import IdleManager
from .lifecycle import LifecycleMixin
from .conversation import ConversationMixin
from .message_queue import MessageQueueMixin
from .slots import SlotsMixin
from .config_persist import ConfigPersistMixin
from .rollback import RollbackMixin
from .session_io import SessionIOMixin
class AgentPool(LifecycleMixin, ConversationMixin, MessageQueueMixin,
                  SlotsMixin, ConfigPersistMixin, RollbackMixin, SessionIOMixin):
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
            # API config lives in the project root config/ dir, not workspace.
            # This file lives in pool/ (one level deeper than the original
            # agent_pool.py), so it needs an extra .parent to reach project root.
            project_root = Path(__file__).resolve().parent.parent.parent
            config_dir = str(project_root / 'config')
            self.api_router = APIRouter(
                default_llm_cfg=llm_cfg,
                config_dir=config_dir
            )
            # Back-reference so api_router can check terminated_instances during retries
            self.api_router._pool = self
        self.telemetry = telemetry
        self.operation_manager = operation_manager

        # ── Core registries (owned directly) ─────────────────────────────────
        self.instances: Dict[str, AgentInstance] = {}  # instance_name → AgentInstance
        self.templates: Dict[str, Assistant] = {}      # agent_class → template

        # ── Configuration ───────────────────────────────────────────────────
        self.llm_cfg = llm_cfg                          # fallback LLM config when no api_router
        self.settings = PoolSettings()                  # configurable thresholds and timeouts

        # ── Defaults for attributes that can be overridden by persisted settings ──
        self._enable_async_shell_console_window = False  # default OFF; _load_pool_settings may override

        # ── PoolSettings persistence ────────────────────────────────────────
        instance_id = get_instance_id()
        if instance_id:
            self._pool_settings_path = self.api_router._config_dir / f"pool_settings_{instance_id}.json"
        else:
            self._pool_settings_path = self.api_router._config_dir / 'pool_settings.json'
        self._settings_save_lock = threading.Lock()     # guards concurrent save operations
        self._loaded_auto_security = None               # persisted auto-security toggle (None = not loaded)
        self._load_pool_settings()                      # load persisted values, overriding defaults
        self._apply_pending_config()                    # apply work folders/workspace that need operation_manager

        # ── Focused managers (delegation targets) ───────────────────────────
        # Only LoggerManager and IdleManager get their own files — they have
        # distinct lifecycles (file I/O, background thread). Halt state and
        # message routing are simple data structures that belong on the pool.
        self._execution = ParallelAgentManager(self)       # parallel execution + active_stack
        self._logger = LoggerManager(self, workspace_dir)  # logger lifecycle + recovery
        self._idle = IdleManager(self)                      # idle detection + auto-dismissal

        # ── Simple state (owned directly by pool, no separate manager) ───────
        self._paused = threading.Event()                   # global pause flag; set=resumed, clear=paused
        self._paused.set()                                  # start in resumed state
        self._halted_instances: set = set()                # per-instance halt state (legacy, kept for compat)
        self._compression_halted: set = set()              # halted by forced compression (not manual)
        self.terminated_instances: set = set()             # marked for immediate termination
        self._instance_threads: Dict[str, threading.Thread] = {}  # instance_name -> execution thread (join on dismissal)
        self._instance_threads_lock = threading.Lock()            # guards _instance_threads access
        self._pool_lock = threading.RLock()                  # guards instances dict + terminated_instances set
        self.children: Dict[str, List[str]] = {}           # parent_name -> [child_names] for cascade termination
        self._children_lock = threading.RLock()            # guards pool.children + _child_instances

        # Lock hierarchy (for future reference — never nest locks in reverse order):
        #   _pool_lock → _state_lock → _instance_threads_lock / _children_lock
        # Current code uses these locks separately (no nesting), but if nested locking is added,
        # always acquire in the above order to prevent deadlocks.

        # ── Run generation counter (prevents resume race condition) ───────────
        # Each time a new execution thread starts, this is incremented. Old threads
        # check their captured generation value to detect they've been superseded.
        self._run_generation = 0                           # monotonically increasing run ID

        # ── Attributes required by api_server.py and agent_invoker.py ──
        # These bridge the new unified model with existing call patterns.
        self.instance_summaries: Dict[str, str] = {}         # per-instance compression summaries
        self._ws_loop = None                                 # asyncio event loop ref (set by api_server at runtime)

        # instance_state bridges the old WebUI state pattern with the new unified model.
        # Maintained for agent_invoker.py and session rename patterns.
        self.instance_state: Dict[str, dict] = {}
        self.message_queues: Dict[str, List[str]] = {}     # per-agent message queues
        self._queue_lock = threading.Lock()                # Protects message_queues mutations
        self._message_condition = threading.Condition(self._queue_lock)  # For __wait blocking support

        # ── Async Tools Infrastructure (SLEEPING state support) ─────────────
        # These attributes support the SLEEPING state guard for async background tools.
        # _async_registry: tracks pending async tool calls by instance name
        self._async_registry: AsyncToolRegistry = AsyncToolRegistry(pool=self)

        # ── Async Shell Infrastructure (background shell_cmd support) ────────
        from agent_cascade.async_shell import AsyncShellTracker
        self._async_shell_tracker = AsyncShellTracker(pool=self)

        # ── Global state ─────────────────────────────────────────────────────
        self._stopped_event = threading.Event()         # M3 fix: stopped flag for emergency shutdown

        # ── Version counter for lazy sync of instance_conversations (Fix #3) ──
        self._instances_version = 0                        # increments on create/remove/dismiss/reset
        self._mapping_synced_to_version = -1              # tracks last version instance_conversations was synced to

        # ── Configuration Version (Fix LLM Reprocessing) ─────────────────────
        # Incremented when global config changes (workspace dir, extra folders, refresh_agents).
        # Used by ExecutionEngine._setup_turn() to detect if system prompt needs rebuild.
        self._config_version = 0

        # ── Live UI disabled_tools cache (real-time tool assignment) ─────────
        # Stores the current per-agent disabled_tools dict from the UI settings panel.
        # All agent instances read from this during each turn for real-time updates.
        self._ui_disabled_tools: Dict[str, Any] = {}
        self._ui_disabled_tools_lock = threading.RLock()

        # Dismissal callbacks (used by api_server to broadcast real-time tab removal)
        self._on_dismissed_callbacks: list = []

        # ── Skills System: Initialize SkillManager and discover skills ────────
        from agent_cascade.skills import SkillManager
        self.skill_manager = SkillManager()

        # Discover skills from .qwen/skills/ directory (relative to project root).
        # This file lives in pool/ (one level deeper than the original agent_pool.py),
        # so it needs an extra .parent to reach project root.
        _project_root = Path(__file__).resolve().parent.parent.parent
        _skills_dir = _project_root / '.qwen' / 'skills'
        if _skills_dir.exists():
            self.skill_manager.discover([_skills_dir])

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
            # Restart async registry executor via the thread-safe resize path.
            # This recreates a fresh executor sized from settings (old one drains).
            try:
                if self._async_registry is not None and self._async_registry.resize_executor(
                        self.settings.max_workers):
                    logger.debug("Async registry executor resized on resume")
                else:
                    logger.debug("Async registry resize skipped (missing or failed, non-critical)")
            except Exception as e:
                logger.debug(f"Async registry restart (non-critical): {e}")
            logger.debug("Stopped flag cleared — ready for new execution")
    def _update_child_relationship(self, parent_name: str, child_name: str, add: bool = True) -> None:
        """Update both pool.children and parent's _child_instances.
        
        Uses _children_lock for pool.children dict and _state_lock for instance._child_instances list.
        Locks are acquired separately (not nested) to avoid deadlock.
        
        Args:
            parent_name: Name of the parent instance.
            child_name: Name of the child instance.
            add: If True, add the relationship; if False, remove it.
        """
        # Update pool.children under _children_lock
        with self._children_lock:
            if add:
                if parent_name not in self.children:
                    self.children[parent_name] = []
                if child_name not in self.children[parent_name]:
                    self.children[parent_name].append(child_name)
            else:
                if child_name in self.children.get(parent_name, []):
                    self.children[parent_name].remove(child_name)

        # Update parent instance's _child_instances under its _state_lock
        parent_inst = self.get_instance(parent_name)
        if parent_inst:
            try:
                with parent_inst._state_lock:
                    if add:
                        if child_name not in parent_inst._child_instances:
                            parent_inst._child_instances.append(child_name)
                    else:
                        if child_name in parent_inst._child_instances:
                            parent_inst._child_instances.remove(child_name)
            except Exception as e:
                logger.debug(f"Updating _child_instances for {parent_name} failed (non-critical): {e}")
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
    def _clear_all_state_dicts(self):
        """Clear all per-instance state dictionaries."""
        self.instance_state.clear()
        with self._pool_lock:
            self.terminated_instances.clear()
        with self._children_lock:
            self.children.clear()
        self.instance_summaries.clear()
        self._halted_instances.clear()
        self._compression_halted.clear()
        if hasattr(self, '_instance_conversations'):
            self._instance_conversations.clear()

    @staticmethod
    def find_last_marker(history: List[Message]) -> int:
        """Find the index of the last COMPRESSION_MARKER message in a conversation.

        Only considers messages with role=USER (compression markers are user messages).
        Returns -1 if no marker is found.
        """
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = AgentPool._msg_field(msg, 'role')
            content = AgentPool._msg_field(msg, 'content')
            # Only consider USER messages (compression markers are always user role)
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    @staticmethod
    def count_markers(history: List[Message]) -> int:
        """Count valid compression markers in conversation.

        Detection criteria and rationale:
        - role=USER: Compression markers are injected as user messages to signal context
          boundaries without interfering with assistant flow.
        - starts with COMPRESSION_MARKER prefix: Identifies the message as a system-generated
          marker rather than regular user input.
        - contains <context_summary> tags: Required structural element that holds the actual
          summary content; prevents false positives from arbitrary user messages that might
          coincidentally start with the marker prefix string.

        Args:
            history: Conversation history (list of Message objects or dicts).

        Returns:
            Number of valid compression markers found.
        """
        count = 0
        for msg in history:
            role = AgentPool._msg_field(msg, 'role')
            content = AgentPool._msg_field(msg, 'content')
            if (role == USER and isinstance(content, str)
                    and content.startswith(COMPRESSION_MARKER)
                    and '<context_summary>' in content):
                count += 1
        return count

    @staticmethod
    def find_all_marker_indices(history: List[Message]) -> List[int]:
        """Return indices of all valid compression markers in chronological order.

        Detection criteria and rationale:
        - role=USER: Compression markers are injected as user messages to signal context
          boundaries without interfering with assistant flow.
        - starts with COMPRESSION_MARKER prefix: Identifies the message as a system-generated
          marker rather than regular user input.
        - contains <context_summary> tags: Required structural element that holds the actual
          summary content; prevents false positives from arbitrary user messages that might
          coincidentally start with the marker prefix string.

        Used by consolidation logic to determine which markers to merge hierarchically.

        Args:
            history: Conversation history (list of Message objects or dicts).

        Returns:
            List of indices where valid compression markers appear, from earliest to latest.
        """
        indices = []
        for i, msg in enumerate(history):
            role = AgentPool._msg_field(msg, 'role')
            content = AgentPool._msg_field(msg, 'content')
            if (role == USER and isinstance(content, str)
                    and content.startswith(COMPRESSION_MARKER)
                    and '<context_summary>' in content):
                indices.append(i)
        return indices

