"""
Agent Pool — Manages a pool of specialized sub-agents.

Handles agent discovery, loading, instance lifecycle, conversation persistence,
context compression, and streaming state for the WebUI.
"""

import copy
import json
import os
import time
import datetime
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_cascade.agents import Assistant
from agent_cascade.log import logger
from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, FUNCTION, ROLE, SYSTEM, USER, Message,
)
from agent_cascade.settings import DEFAULT_WORKSPACE

from agent_logger import AgentInstanceLogger
from telemetry import TelemetryCollector
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.compression.helpers import get_role
from api_router import APIRouter


class AgentPool:
    """Manages a pool of specialized sub-agents."""
    
    def __init__(self, llm_cfg: dict, agents_dir: str = 'agents', workspace_dir: Optional[str] = None,
                 idle_timeout_seconds: float = 300.0, idle_check_interval: float = 60.0):
        """Initialize the AgentPool.

        Args:
            llm_cfg: LLM configuration dictionary.
            agents_dir: Path to the agents directory.
            workspace_dir: Path to the workspace directory.
            idle_timeout_seconds: Seconds of inactivity before auto-dismissing an agent (default 300 = 5 min).
            idle_check_interval: Seconds between idle-check sweeps (default 60 = 1 min).
        """
        self.llm_cfg = llm_cfg
        self.agents_dir = Path(agents_dir)
        self.workspace_dir = Path(workspace_dir) if workspace_dir else Path(DEFAULT_WORKSPACE)
        
        # Agent templates (loaded by class name)
        self.agents: Dict[str, Assistant] = {}
        self.agent_configs: Dict[str, dict] = {}
        
        # Initialize OperationManager for blocking approvals
        from operation_manager import OperationManager
        self.operation_manager = OperationManager(base_dir=str(self.workspace_dir), agent_pool=self)
        
        # Initialize Telemetry Collector for performance tracking
        telemetry_dir = str(self.workspace_dir / 'telemetry')
        self.telemetry = TelemetryCollector(log_dir=telemetry_dir)
        
        # API Router for multi-endpoint management (uses llm_cfg as default fallback)
        self.api_router = APIRouter(
            default_llm_cfg=llm_cfg,
            config_dir=str(self.workspace_dir / 'config')
        )
        
        # Persistent conversation histories for each named instance
        self.instance_conversations: Dict[str, List] = {}
        # Mapping of instance_name to its agent_class
        self.instance_classes: Dict[str, str] = {}
        
        # Mapping of instance_name to its AgentInstanceLogger
        self.instance_loggers: Dict[str, AgentInstanceLogger] = {}
        
        # Mapping of instance_name to its active compression summary
        self.instance_summaries: Dict[str, str] = {}
        
        # Live streaming state for WebUI (updated during sub-agent execution)
        self.sub_agent_state: Dict[str, dict] = {}
        
        # List of instance_names currently in an active call (stack for recursion)
        self.active_stack: List[str] = []
        
        # Caching for tool arguments to support __USE_PREV_ARG__
        # self.last_tool_args[instance_name][tool_name] = {arg_name: actual_value}
        self.last_tool_args: Dict[str, Dict[str, Dict[str, Any]]] = {}
        
        # Explicit stop flag for cancellation — backed by threading.Event
        # for proper cross-thread memory barriers (GIL alone does NOT guarantee
        # visibility ordering for plain bool attributes).
        self._stopped_event = threading.Event()
        
        # Per-instance halt flags — used during forced compression to halt specific agents
        # while allowing the compression agent itself to run.
        # Key = instance_name, Value = bool (True = halted)
        self._instance_halted: Dict[str, bool] = {}
        
        # Lock for thread-safe access to _instance_halted (used across WebSocket handler and agent threads)
        self._halt_lock = threading.Lock()
        
        # Track which instances were halted by forced compression specifically,
        # so resume_all_instances only clears those (not manual halts from /api/halt).
        self._compression_halted: set = set()
        
        self.terminated_instances = set()
        
        # Per-agent message queues for routed async injection
        # Key = instance_name (e.g. 'Maine', 'MACFixer2'), Value = list of message strings
        self.message_queues: Dict[str, List[str]] = {}
        
        # Backward compat: kept as a drain-only alias checked by legacy code
        self.async_message_queue: List[str] = []
        
        # Thread safety locks for parallel agent execution
        self._state_lock = threading.Lock()           # Protects sub_agent_state, active_stack
        self._conversation_lock = threading.Lock()    # Protects instance_conversations writes
        
        # ── Idle agent auto-dismissal ──────────────────────────────────────────
        # Configurable timeout and interval for idle agent cleanup
        self.idle_timeout_seconds = max(30.0, float(idle_timeout_seconds))  # Minimum 30s guard
        self.idle_check_interval = max(10.0, float(idle_check_interval))
        
        # Per-instance last-activity timestamp (seconds since epoch via time.monotonic)
        # Key = instance_name, Value = float (monotonic timestamp)
        self._last_activity: Dict[str, float] = {}
        
        # Lock for thread-safe access to _last_activity
        self._activity_lock = threading.Lock()
        
        # Background thread that periodically checks for and dismisses idle agents
        self._idle_checker_thread: Optional[threading.Thread] = None
        # Event used by the background thread to signal it should stop
        self._idle_checker_stop_event = threading.Event()
        
        # Initialize Parallel Agent Manager
        from agent_orchestrator import ParallelAgentManager
        self.parallel_manager = ParallelAgentManager(self, max_workers=llm_cfg.get('max_parallel_agents', 3))
        
        # Callback hooks for dismissal events (used by api_server to broadcast real-time tab removal)
        # Each callback receives: callback(instance_name: str, log_path: Optional[str])
        self._on_dismissed_callbacks: list = []
        
        # Auto-load all agents from the agents directory
        self._discover_agents()
        
        # Start background idle checker thread
        self._start_idle_checker()

    # ── stopped property (Event-backed for cross-thread visibility) ───────
    @property
    def stopped(self) -> bool:
        return self._stopped_event.is_set()
    
    @stopped.setter
    def stopped(self, value: bool):
        if value:
            self._stopped_event.set()
        else:
            self._stopped_event.clear()

    # ── Dismiss callback hooks (for real-time UI tab removal) ────────────────
    def on_dismissed(self, callback):
        """Register a callback invoked when an agent instance is dismissed.
        
        Callback signature: callback(instance_name: str, log_path: Optional[str])
        """
        self._on_dismissed_callbacks.append(callback)
    
    def _fire_on_dismissed(self, instance_name: str, log_path: Optional[str] = None):
        """Fire all registered dismissal callbacks for a dismissed agent."""
        for cb in self._on_dismissed_callbacks:
            try:
                cb(instance_name, log_path)
            except Exception as e:
                logger.error(f"Error in on_dismissed callback for {instance_name}: {e}")

    # ── Per-instance halt for forced compression ──────────────────────────
    def is_halted(self, instance_name: str) -> bool:
        """Check if a specific agent instance has been halted (e.g. during forced compression)."""
        with self._halt_lock:
            return self._instance_halted.get(instance_name, False)

    def halt_instance(self, instance_name: str):
        """Halt a specific agent instance while allowing others to continue."""
        with self._halt_lock:
            self._instance_halted[instance_name] = True

    def resume_instance(self, instance_name: str):
        """Resume a previously halted agent instance."""
        with self._halt_lock:
            self._instance_halted[instance_name] = False

    def halt_all_instances(self, except_instance: str = None, except_instances: List[str] = None):
        """Halt all active instances except the given one(s) (used before forced compression)."""
        # Build set of exceptions
        skip = set()
        if except_instance:
            skip.add(except_instance)
        if except_instances:
            skip.update(except_instances)
        
        # Get all known instance names from message_queues, active_stack, and conversations
        all_instances = set(self.message_queues.keys()) | set(self.active_stack) | set(self.instance_conversations.keys())
        for inst in all_instances:
            if inst not in skip:
                was_already_halted = self.is_halted(inst)
                self.halt_instance(inst)
                # Only track instances that weren't already halted — preserves manual halts
                if not was_already_halted:
                    self._compression_halted.add(inst)

    def resume_all_instances(self):
        """Resume only the instances that were halted by forced compression (not manual halts)."""
        for inst in self._compression_halted:
            self.resume_instance(inst)
        self._compression_halted.clear()

    def discover_instances(self):
        """Scan the logs directory and reload existing instances."""
        log_dir = self.workspace_dir / 'logs'
        if not log_dir.exists():
            return

        # Find the most recent log file for each (agent_class, instance_name)
        latest_logs = {} # (agent_class, instance_name) -> (mtime, path)
        
        for log_file in log_dir.glob('*.jsonl'):
            try:
                # Filename format: {agent_class}_{instance_name}_{timestamp}.jsonl
                parts = log_file.stem.split('_')
                if len(parts) < 3: continue
                
                # Handling names with underscores
                agent_class = parts[0].strip().lower()  # Normalize for case-insensitive lookup
                timestamp = parts[-2] + "_" + parts[-1]
                instance_name = "_".join(parts[1:-2])
                
                mtime = log_file.stat().st_mtime
                key = (agent_class, instance_name)
                if key not in latest_logs or mtime > latest_logs[key][0]:
                    latest_logs[key] = (mtime, log_file)
            except Exception:
                continue

        for (agent_class, instance_name), (mtime, path) in latest_logs.items():
            if instance_name == 'Maine': continue # Main session handled by api_server
            
            # Load the instance if it's not already in memory
            if instance_name not in self.instance_conversations:
                logger.info(f"Recovering sub-agent instance '{instance_name}' ({agent_class}) from log...")
                self.load_session_from_log(str(path), target_instance=instance_name)
                # Initialize state for UI
                self.sub_agent_state[instance_name] = {
                    'active': False,
                    'agent_name': f"{instance_name} ({agent_class})",
                    'messages': self.instance_conversations[instance_name],
                }
                self.instance_classes[instance_name] = agent_class


    # ── Per-Agent Message Queue Helpers ──────────────────────────────────────

    def enqueue_message(self, target: str, text: str):
        """Push a message into a specific agent's queue."""
        if target not in self.message_queues:
            self.message_queues[target] = []
        self.message_queues[target].append(text)
        
        # Mark activity: someone is sending messages to this agent
        if hasattr(self, '_mark_activity'):
            self._mark_activity(target)

    def drain_queue(self, target: str) -> List[str]:
        """Pop and return all pending messages for a specific agent.
        
        Deduplicates across the targeted queue and the legacy global queue
        so that if the same message text exists in both, it is only returned once.
        Preserves insertion order (first occurrence wins).
        """
        msgs = []
        # Drain targeted queue
        if target in self.message_queues and self.message_queues[target]:
            msgs = self.message_queues[target][:]
            self.message_queues[target].clear()
        # Also drain any legacy global queue messages (backward compat)
        if self.async_message_queue:
            # Deduplicate: only extend with messages not already in msgs
            seen = set(msgs)
            for m in self.async_message_queue:
                if m not in seen:
                    msgs.append(m)
                    seen.add(m)
            self.async_message_queue.clear()
        return msgs

    def has_messages(self, target: str) -> bool:
        """Check if there are pending messages for a specific agent without consuming them."""
        if target in self.message_queues and self.message_queues[target]:
            return True
        if self.async_message_queue:
            return True
        return False

    def refresh_agents(self):
        """Reload all agent souls and templates from disk."""
        self.agents.clear()
        self.agent_configs.clear()
        self._discover_agents()
        logger.info("AgentPool souls refreshed from disk.")
    
    def terminate_instance(self, instance_name: str):
        """Mark an instance for immediate termination. Only triggers global stop if the instance is currently active."""
        self.terminated_instances.add(instance_name)
        if instance_name in self.active_stack:
            self._stopped_event.set()

    def dismiss_instance(self, instance_name: str):
        """Remove an instance from the pool. If it's active, it will be stopped and cleared upon completion.
        
        NOTE: This method is called for UI-initiated termination (terminate_sub_agent WebSocket message).
        The UI handler already broadcasts state after calling this, so we do NOT fire the dismissal
        callback here — that would cause a double-broadcast. Only LLM-initiated dismissals via
        DismissAgent.call() should fire callbacks for real-time tab removal.
        """
        if instance_name in self.active_stack:
            self.terminated_instances.add(instance_name)
            self._stopped_event.set()
        else:
            self.clear_conversation(instance_name)
            # Clean up activity tracking to prevent memory leak (Issue #1)
            with self._activity_lock:
                self._last_activity.pop(instance_name, None)

    def capture_snapshots(self) -> Dict[str, int]:
        """Capture the current history lengths of all active sub-agent instances."""
        snapshots = {}
        for name, conv in self.instance_conversations.items():
            snapshots[name] = len(conv)
        return snapshots

    def rollback_to_snapshots(self, snapshots: Dict[str, int], soft: bool = False, reason: Optional[str] = None):
        """Rollback all sub-agent instances to the lengths recorded in the snapshots."""
        for name, target_len in snapshots.items():
            # Rollback history list
            if name in self.instance_conversations:
                conv = self.instance_conversations[name]
                if len(conv) > target_len:
                    del conv[target_len:]
            
            # Rollback persistent log file
            if name in self.instance_loggers:
                self.instance_loggers[name].truncate_to(target_len, soft=soft, reason=reason)
        
    def surgical_rollback(self, agent_name: str, pop_count: int, soft: bool = False, reason: Optional[str] = None):
        """Rollback a specific agent by pop_count messages."""
        if not agent_name or pop_count is None or pop_count <= 0:
            return
            
        if agent_name in self.instance_conversations:
            conv = self.instance_conversations[agent_name]
            
            # Determine the core messages that must NEVER be removed
            keep_at_least = 0
            if len(conv) > 0 and conv[0].get(ROLE) == SYSTEM:
                keep_at_least = 1
                if len(conv) > 1 and conv[1].get(ROLE) == USER:
                    keep_at_least = 2
            
            removable = len(conv) - keep_at_least
            if removable <= 0:
                return
            
            # Safety cap: never remove more than 50% of removable history in one rollback.
            # This prevents cumulative rollbacks from wiping everything.
            max_pop = max(1, removable // 2)
            if pop_count > max_pop:
                logger.warning(f"Surgical rollback for {agent_name}: capping pop_count from {pop_count} to {max_pop} (50% safety limit of {removable} removable messages).")
                pop_count = max_pop
            
            # Refine pop_count: If the first message we are removing is a tool result (FUNCTION),
            # we should also remove the preceding tool call (ASSISTANT) to avoid dangling states.
            while pop_count < removable:
                start_idx = len(conv) - pop_count
                if start_idx >= keep_at_least and conv[start_idx].get(ROLE) == FUNCTION:
                    pop_count += 1
                elif start_idx >= keep_at_least and conv[start_idx].get(ROLE) == ASSISTANT and conv[start_idx].get('function_call'):
                    # We are at a tool call boundary. Stop here.
                    break
                else:
                    break

            new_len = max(keep_at_least, len(conv) - pop_count)
            removed = len(conv) - new_len
            del conv[new_len:]
            logger.info(f"Surgically rolled back {agent_name} from {new_len + removed} to {new_len} messages (removed {removed}).")
        
        # Sync the log file to the new history length
        if agent_name in self.instance_loggers:
            new_len = len(self.instance_conversations.get(agent_name, []))
            self.instance_loggers[agent_name].truncate_to(new_len, soft=soft, reason=reason)
        
        # Clear any sub-agents that were created AFTER the snapshot?
        # (This is harder since they might be in self.agents, but they are mostly harmless)
        pass

    def get_logger(self, instance_name: str, agent_class: str, base_metadata: Optional[Dict] = None) -> AgentInstanceLogger:
        """Get or create a logger for an agent instance."""
        if instance_name not in self.instance_loggers:
            # Ensure workspace/logs exists
            log_dir = self.operation_manager.base_dir / 'logs'
            log_dir.mkdir(parents=True, exist_ok=True)
            
            meta = base_metadata or {}
            if 'working_dir' not in meta:
                meta['working_dir'] = str(self.operation_manager.base_dir)
            
            # Add extra paths to metadata
            if 'extra_paths_ro' not in meta:
                meta['extra_paths_ro'] = [str(p) for p in self.operation_manager.extra_work_folders_ro]
            if 'extra_paths_rw' not in meta:
                meta['extra_paths_rw'] = [str(p) for p in self.operation_manager.extra_work_folders_rw]
            
            self.instance_loggers[instance_name] = AgentInstanceLogger(
                agent_class=agent_class,
                instance_name=instance_name,
                log_dir=str(log_dir),
                base_metadata=meta
            )
        return self.instance_loggers[instance_name]
    
    def _discover_agents(self):
        """Find and load all agent configurations from the agents directory."""
        if not self.agents_dir.exists():
            self.agents_dir.mkdir(exist_ok=True)
            # Create a default example agent
            self._create_example_agent()
        
        # Load all *_soul.md files
        for soul_file in self.agents_dir.glob('*_soul.md'):
            agent_name = soul_file.name.replace('_soul.md', '')
            try:
                self.load_agent(agent_name)
                logger.info("[OK] Loaded agent: %s", agent_name)
            except Exception as e:
                logger.error("[ERROR] Failed to load agent %s: %s", agent_name, e)
    
    def _create_example_agent(self):
        """Create an example sub-agent."""
        soul_path = self.agents_dir / 'researcher_soul.md'
        
        soul_content = """name: Researcher
tagline: Deep research specialist

identity:
  role: Academic and technical research expert
  background: |
    You specialize in deep research, analysis, and synthesizing complex information.
    You're methodical, thorough, and love diving into technical details.
  personality_traits:
    - Analytical and detail-oriented
    - Patient and systematic
    - Loves citing sources and evidence
    - Asks clarifying questions

communication:
  tone: Professional, precise, academic
  style_notes:
    - Always cite sources when using web_search
    - Break down complex topics step by step
    - Use technical terms when appropriate
    - Summarize key findings clearly

capabilities:
  tools:
    - web_search
    - visit_website
  
  skills:
    - Literature review
    - Technical analysis
    - Fact verification
    - Source evaluation

rules:
  - Always verify information from multiple sources
  - Cite your sources explicitly
  - Distinguish between facts and opinions
  - Admit uncertainty when evidence is weak
"""
        soul_path.write_text(soul_content)
    
    def load_agent(self, agent_name: str) -> Assistant:
        """Load or reload a specific agent with file tools."""
        from agent_factory import load_sub_agent_with_tools
        
        soul_path = self.agents_dir / f'{agent_name}_soul.md'
        
        if not soul_path.exists():
            raise FileNotFoundError(f"No soul.md found for agent: {agent_name}")
        
        # Load agent with file tools
        agent = load_sub_agent_with_tools(self, agent_name, self.llm_cfg)
        
        # Normalize agent name: strip whitespace and convert to lowercase for case-insensitive lookup
        normalized_name = agent_name.strip().lower()
        self.agents[normalized_name] = agent
        self.agent_configs[normalized_name] = agent.agent_configs.get(agent_name, {})
        
        return agent
    
    def get_agent(self, agent_name: str) -> Optional[Assistant]:
        """Get an agent by name (case-insensitive)."""
        if not agent_name:
            return None
        return self.agents.get(agent_name.strip().lower())
    
    def update_llm_cfg(self, new_cfg: dict):
        """Update the global LLM config and propagate it to all loaded agents."""
        self.llm_cfg.update(new_cfg)
        
        # Keys that should NEVER be passed to sub-agent LLM chat API
        EXCLUDE_KEYS = {
            'max_auto_rollbacks', 'auto_rollback_on_loop', 'auto_continue', 
            'max_turns', 'mcpServers', 'work_access_folders', 'seed',
            'tool_result_max_chars', 'grep_char_limit', 'grep_spillover', 'shell_char_limit', 'code_char_limit'
        }
        
        for agent_name, agent in self.agents.items():
            if hasattr(agent, 'llm') and hasattr(agent.llm, 'generate_cfg'):
                # Check if this agent has a specialized routing chain in the APIRouter.
                # If so, we MUST NOT overwrite its infrastructure (model, base, key) 
                # with the General Settings, as that would bypass the Router's assignment.
                has_specialized_routing = False
                if hasattr(self, 'api_router'):
                    # We use lower() to match the runtime resolution in OrchestratorAgent._call_llm
                    agent_type = getattr(agent, 'agent_type', agent_name).lower()
                    if self.api_router.get_agent_priorities(agent_type):
                        has_specialized_routing = True

                # Clean sub-agent config of any internal keys before updating
                for key in EXCLUDE_KEYS:
                    agent.llm.generate_cfg.pop(key, None)
                # Extract max_input_tokens if it's at the top level, consistent with BaseChatModel.__init__
                update_data = copy.deepcopy(new_cfg)
                if 'max_input_tokens' in update_data and 'max_input_tokens' not in update_data.get('generate_cfg', {}):
                    if 'generate_cfg' not in update_data:
                        update_data['generate_cfg'] = {}
                    update_data['generate_cfg']['max_input_tokens'] = update_data['max_input_tokens']
                
                if 'generate_cfg' in update_data:
                    agent.llm.generate_cfg.update(update_data['generate_cfg'])
                    # Also update top-level keys in update_data that are not 'generate_cfg'
                    # but might be sampling params (some code puts them at top level)
                    for k, v in update_data.items():
                        if k not in ['generate_cfg', 'model', 'model_type', 'api_key', 'api_base', 'base_url', 'model_server']:
                            agent.llm.generate_cfg[k] = v
                else:
                    # Flat config from WebUI, update generate_cfg directly
                    # but skip top-level LLM identity keys
                    for k, v in update_data.items():
                        if k not in ['model', 'model_type', 'api_key', 'api_base', 'base_url', 'model_server']:
                            agent.llm.generate_cfg[k] = v
                
                # Also update other relevant top-level attributes IF the agent doesn't have specialized routing.
                # If it DOES have specialized routing, we let the APIRouter handle the model/base/key at runtime.
                if not has_specialized_routing:
                    for attr in ['model', 'model_type', 'api_key']:
                        if attr in update_data:
                            setattr(agent.llm, attr, update_data[attr])
                    if 'api_base' in update_data or 'base_url' in update_data or 'model_server' in update_data:
                        val = update_data.get('api_base') or update_data.get('base_url') or update_data.get('model_server')
                        if hasattr(agent.llm, 'api_base'):
                            agent.llm.api_base = val
                        if hasattr(agent.llm, 'base_url'):
                            agent.llm.base_url = val
                else:
                    logger.debug(f"Skipping infrastructure override for agent '{agent_name}' because it has specialized APIRouter priorities.")
        logger.debug("Propagated LLM config changes to all active agents in the pool.")
        
        # Keep the API Router's default fallback in sync with General Settings
        if hasattr(self, 'api_router'):
            self.api_router.update_default_llm_cfg(new_cfg)
    
    def list_agents(self) -> List[str]:
        """List all available agents."""
        return list(self.agents.keys())
    
    def get_conversation(self, instance_name: str) -> List:
        """Get or create persistent conversation history for an agent instance."""
        if instance_name not in self.instance_conversations:
            self.instance_conversations[instance_name] = []
        return self.instance_conversations[instance_name]

    @staticmethod
    def find_last_marker(history: List[Union[dict, Message]]) -> int:
        """
        Scan backwards through the history for a message whose content starts with
        COMPRESSION_MARKER (USER role). Returns the index of that message, or -1 if none.

        Shared helper used by slice_history_for_llm and get_compression_target_set
        to avoid duplicating the backward-scan logic.

        Args:
            history: List of messages (dicts or Message objects).

        Returns:
            Index of the latest compression marker message, or -1 if not found.
        """
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = msg.get(ROLE) if isinstance(msg, dict) else getattr(msg, ROLE, '')
            content = msg.get(CONTENT, '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                return i
        return -1

    def slice_history_for_llm(self, history: List[Union[dict, Message]]) -> List[Union[dict, Message]]:
        """
        Extract the 'working set' from a full conversation history.
        Preserves the first SYSTEM message and slices from the latest compression marker onwards.
        
        OPTIMIZATION: Uses O(1) check for SYSTEM presence instead of O(n) scan.
        Since markers are always inserted AFTER the system message (index 0),
        if latest_summary_idx > 0, the system message is NOT in the slice.
        """
        if not history:
            return []
        
        latest_summary_idx = self.find_last_marker(history)
                
        if latest_summary_idx == -1:
            return history
        
        # O(1) check: System message is at index 0, markers inserted after it
        # If latest_summary_idx == 0, marker IS the system message (corruption) or no system exists
        # If latest_summary_idx > 0, system message is NOT in sliced result
        has_system_in_slice = (latest_summary_idx == 0)
        
        # Check if history[0] is actually a SYSTEM message
        first_role = get_role(history[0])
        has_system_message = (first_role == SYSTEM)
        
        sliced = history[latest_summary_idx:]
        
        # Prepend system message only if it exists and is not in the slice
        if has_system_message and not has_system_in_slice:
            logger.debug(
                f"[slice_history_for_llm] Prepending system message "
                f"(latest_summary_idx={latest_summary_idx}, len(history)={len(history)})"
            )
            return [history[0]] + list(sliced)
        
        return list(sliced)

    def get_compression_target_set(self, agent_name: str) -> tuple[Optional[int], List[Union[dict, Message]], int]:
        """
        Returns the compression target set for an agent: 
        (active_start_idx, messages_to_compress, latest_summary_idx).
        Shared helper used by compress_context() in core.py.
        
        active_start_idx: The index where the active (uncompressed) set begins.
                          Always preserves system message + first user message.
        messages_to_compress: The list of active messages eligible for compression.
        latest_summary_idx: The index of the most recent compression marker (-1 if none).
        """
        history = self.get_conversation(agent_name)
        if not history:
            return None, [], -1
        
        # Determine start index — always preserve system message + first user message.
        # After multiple compressions the pool looks like: [SYS][USER_MSG_0][SUM1][SUM2]...[tail]
        start_idx = 0
        if history:
            first_role = get_role(history[0])
            if first_role == SYSTEM:
                start_idx = 1
                # Also skip the first user message if present
                if len(history) > 1 and get_role(history[1]) == USER:
                    start_idx = 2
        
        # Find the latest compression marker using shared helper
        latest_summary_idx = self.find_last_marker(history)
        
        active_start_idx = latest_summary_idx + 1 if latest_summary_idx != -1 else start_idx
        
        # FIX 2: Insurance guard — if history[0] is SYSTEM but active_start_idx would be 0,
        # force it to at least 1. This catches corruption scenarios where start_idx logic
        # was bypassed (e.g., pool sync lost system message), ensuring compression formula
        # doesn't produce empty prefix and lose the system message.
        # Kept at WARNING level because this indicates actual pool corruption.
        # Changed from "== 0" to "< 1" for a more defensive check (handles edge cases).
        if history:
            first_role = get_role(history[0])
            if first_role == SYSTEM and active_start_idx < 1:
                logger.warning(
                    f"[COMPRESSION FIX] Forced active_start_idx from {active_start_idx} to 1 for '{agent_name}' "
                    f"to preserve system message (pool may be partially corrupted)"
                )
                active_start_idx = 1
        
        messages_to_compress = history[active_start_idx:]
        
        return active_start_idx, messages_to_compress, latest_summary_idx

    def clear_conversation(self, instance_name: str):
        """Clear an agent instance's conversation history."""
        self.instance_conversations.pop(instance_name, None)
        self.instance_classes.pop(instance_name, None)
        self.instance_loggers.pop(instance_name, None)
        self.sub_agent_state.pop(instance_name, None)

    def reset(self):
        """Full reset of all sub-agent instances and persistent data."""
        # Stop the idle checker first to avoid race conditions during reset (Issue #5)
        self._stop_idle_checker()
        
        self.instance_conversations.clear()
        self.instance_classes.clear()
        self.instance_loggers.clear()
        self.sub_agent_state.clear()
        self.active_stack.clear()
        self.last_tool_args.clear()
        self.terminated_instances.clear()
        self._instance_halted.clear()
        self._compression_halted.clear()
        # Also clear activity tracking on reset
        with self._activity_lock:
            self._last_activity.clear()
        
        logger.info("AgentPool reset — all instances and loggers cleared.")
        # Restart the idle checker after reinitialization
        self._start_idle_checker()
    
    # ── Idle agent auto-dismissal ───────────────────────────────────────

    def _mark_activity(self, instance_name: str) -> None:
        """Record the current time as the last activity timestamp for an agent.
        
        Thread-safe; calls from any context (orchestrator loop, message injection, etc.)
        are safe because we hold the _activity_lock.
        """
        with self._activity_lock:
            self._last_activity[instance_name] = time.monotonic()
    
    def _get_idle_seconds(self, instance_name: str) -> Optional[float]:
        """Return how many seconds an agent has been idle (None if no record)."""
        with self._activity_lock:
            last = self._last_activity.get(instance_name)
        if last is None:
            return None
        return time.monotonic() - last
    
    def _is_agent_idle(self, instance_name: str) -> bool:
        """Determine whether an agent is idle and eligible for auto-dismissal.
        
        An agent is considered idle when ALL of the following hold:
        1. It is NOT in the active_stack (not currently executing).
        2. It has conversation history or a class mapping (i.e., it exists as a sub-agent).
        3. Its last activity was more than idle_timeout_seconds ago.
        4. It is NOT the main orchestrator ("Maine").
        5. It is NOT currently halted (halted agents are intentionally paused).
        
        Thread-safety: Reads active_stack under _state_lock and halted status under _halt_lock
        to avoid TOCTOU races where an agent starts running between checks.
        """
        # Never auto-dismiss the main orchestrator
        if instance_name == 'Maine':
            return False
        
        # Must be a sub-agent with conversation history or class mapping
        has_history = (instance_name in self.instance_conversations or 
                       instance_name in self.instance_classes)
        if not has_history:
            return False
        
        # Must NOT be actively running — check atomically under state_lock (Issue #4)
        with self._state_lock:
            is_active = instance_name in self.active_stack
        if is_active:
            return False
        
        # Must NOT be halted (halted agents are intentionally paused, e.g. during compression)
        if self.is_halted(instance_name):
            return False
        
        # Must have exceeded the idle timeout threshold
        idle_secs = self._get_idle_seconds(instance_name)
        if idle_secs is None or idle_secs < self.idle_timeout_seconds:
            return False
        
        return True
    
    def _start_idle_checker(self) -> None:
        """Start the background thread that periodically checks for idle agents."""
        # Guard against double-start: don't start a new thread if one is already alive
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            return  # Already running
        self._idle_checker_stop_event.clear()
        self._idle_checker_thread = threading.Thread(
            target=self._idle_checker_loop,
            name="IdleAgentChecker",
            daemon=True,  # Daemon so it doesn't block interpreter shutdown
        )
        self._idle_checker_thread.start()
        logger.info(
            f"Idle agent checker started: timeout={self.idle_timeout_seconds:.0f}s, "
            f"interval={self.idle_check_interval:.0f}s"
        )
    
    def _stop_idle_checker(self) -> None:
        """Signal the idle checker to stop and wait for it to exit."""
        if self._idle_checker_thread is not None and self._idle_checker_thread.is_alive():
            self._idle_checker_stop_event.set()
            self._idle_checker_thread.join(timeout=self.idle_check_interval + 5.0)
            if self._idle_checker_thread.is_alive():
                logger.warning("Idle agent checker thread did not exit in time, forcing shutdown.")
        self._idle_checker_thread = None
    
    def _idle_checker_loop(self) -> None:
        """Background loop that periodically checks for and dismisses idle agents."""
        while not self._idle_checker_stop_event.is_set():
            try:
                # Snapshot the set of known instances to avoid holding locks during check
                with self._activity_lock:
                    all_instances = set(self._last_activity.keys())
                
                dismissed_this_round = []
                
                for inst in all_instances:
                    if self._idle_checker_stop_event.is_set():
                        break
                    
                    try:
                        if self._is_agent_idle(inst):
                            self._auto_dismiss_idle_agent(inst)
                            dismissed_this_round.append(inst)
                    except Exception as e:
                        # Never let a single bad instance crash the whole checker
                        logger.error(
                            f"Idle checker error processing '{inst}': {e}", exc_info=True
                        )
                
                if dismissed_this_round:
                    logger.info(
                        f"[idle_checker] Auto-dismissed {len(dismissed_this_round)} idle agent(s): "
                        f"{', '.join(dismissed_this_round)}"
                    )
            
            except Exception as e:
                # Catch-all: a crash in the loop shouldn't bring down the system
                logger.error(f"[idle_checker] Loop error: {e}", exc_info=True)
            
            # Wait for next check interval (or until stop event fires)
            self._idle_checker_stop_event.wait(timeout=self.idle_check_interval)
    
    def _auto_dismiss_idle_agent(self, instance_name: str) -> None:
        """Dismiss a single idle agent and clean up its resources.
        
        Uses the existing clear_conversation and _fire_on_dismissed mechanism
        so UI tabs close in real-time (leveraging the callback hooks).
        """
        # Capture log path BEFORE clearing (clear_conversation removes the logger)
        log_path = None
        logger_inst = self.instance_loggers.get(instance_name)
        if logger_inst:
            log_path = getattr(logger_inst, 'log_path', None)
        
        idle_secs = self._get_idle_seconds(instance_name) or 0.0
        
        logger.info(
            f"[idle_checker] Auto-dismissing idle agent '{instance_name}' "
            f"(idle for {idle_secs:.0f}s, threshold={self.idle_timeout_seconds:.0f}s)"
        )
        
        # Clear the conversation (removes instance_conversations, instance_classes, etc.)
        self.clear_conversation(instance_name)
        
        # Clean up any pending operation backups
        if hasattr(self, 'operation_manager') and self.operation_manager:
            self.operation_manager.cleanup_backups(instance_name)
        
        # Remove from activity tracking
        with self._activity_lock:
            self._last_activity.pop(instance_name, None)
        
        # Fire dismissal callbacks for real-time UI tab removal
        if hasattr(self, '_fire_on_dismissed'):
            self._fire_on_dismissed(instance_name, log_path)
    
    def stop(self):
        """Shut down the AgentPool gracefully (including background threads)."""
        self._stop_idle_checker()

    def load_session_from_log(self, log_input: str, target_instance: Optional[str] = None) -> str:
        """
        Load session history from a log entry (JSON string) or a log file path.
        Returns a status message.
        """
        log_input = log_input.strip()
        if not log_input:
            return "Error: Empty log input."

        messages = []
        metadata = {}
        
        # Try as file path first (resolve relative paths against workspace_dir)
        potential_path = Path(log_input)
        if not potential_path.is_absolute():
            potential_path = self.workspace_dir / potential_path
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
                        except json.JSONDecodeError:
                            continue
                log_source = f"file '{potential_path.name}'"
            except Exception as e:
                return f"Error reading log file: {e}"
        else:
            # Try as JSON (single line or block)
            try:
                # Handle potential multiple JSON objects in one block (JSONL style)
                lines = log_input.split('\n')
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        if "metadata" in item:
                            metadata.update(item["metadata"])
                        elif isinstance(item, list): # Full history block
                            messages.extend(item)
                        else:
                            messages.append(item)
                    except json.JSONDecodeError:
                        # Maybe it's a single large JSON block
                        if len(lines) == 1:
                            raise # Re-raise to try full-block parse
                        continue
                log_source = "JSON input"
            except json.JSONDecodeError:
                # Try parsing the whole thing as one JSON block
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
        agent_class = (metadata.get("agent_class") or "Orchestrator").strip().lower()  # Normalize for case-insensitive lookup

        # Filter out event markers and ensure role/content exist
        cleaned_messages = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if "event" in msg: # Skip COMPRESSION markers
                continue
            if ROLE in msg and CONTENT in msg:
                cleaned_messages.append(msg)

        if not cleaned_messages:
            return "Error: No valid conversation messages found."
            
        # On session load/restore we'll read from latest summary onwards
        latest_summary_idx = -1
        for i in range(len(cleaned_messages) - 1, -1, -1):
            msg = cleaned_messages[i]
            if msg.get(ROLE) == USER and isinstance(msg.get(CONTENT, ''), str) and msg.get(CONTENT, '').startswith(COMPRESSION_MARKER):
                latest_summary_idx = i
                break
                
        if latest_summary_idx != -1:
            # Extract raw summary from markers
            summary_msg = cleaned_messages[latest_summary_idx].get(CONTENT, '')
            import re
            # Only match content INSIDE the tags
            match = re.search(r"<context_summary>[\s\n]*(.*?)[\s\n]*</context_summary>", summary_msg, re.DOTALL)
            if match:
                self.instance_summaries[instance_name] = match.group(1).strip()

            system_msg = None
            if len(cleaned_messages) > 0 and cleaned_messages[0].get(ROLE) == SYSTEM:
                system_msg = cleaned_messages[0]
                
            sliced_messages = cleaned_messages[latest_summary_idx:]
            
            # Ensure the system message remains at the top
            if system_msg and sliced_messages[0].get(ROLE) != SYSTEM:
                sliced_messages.insert(0, system_msg)
                
            # Note: We keep the full history in memory for now. 
            # slice_history_for_llm will handle the working set during execution.
            pass

        # Restore to pool — convert raw dicts from JSONL into Message objects
        restored_messages = []
        for msg_dict in cleaned_messages:
            try:
                restored_messages.append(Message(**msg_dict))
            except Exception as e:
                logger.warning(f"Failed to convert loaded message to Message object: {e}")
        self.instance_conversations[instance_name] = restored_messages
        self.instance_classes[instance_name] = agent_class
        
        # Proactively clear any existing logger for this instance so get_logger creates a fresh one
        self.instance_loggers.pop(instance_name, None)
        
        # Initialize a new logger for the continued session
        self.instance_loggers[instance_name] = self.get_logger(
            instance_name=instance_name,
            agent_class=agent_class,
            base_metadata=metadata
        )
        
        # 1. Clean up the loaded history before syncing to the new log
        self._cleanup_history(instance_name)
        cleaned_messages = self.instance_conversations[instance_name]
        
        # Sync the loaded history to the NEW log file so it's persistent
        self.instance_loggers[instance_name].update_history(cleaned_messages)

        return f"Successfully loaded {len(cleaned_messages)} messages for instance '{instance_name}' ({agent_class}) from {log_source}."
    
    def _cleanup_history(self, instance_name: str):
        """
        Ultra-robust deduplicator to prune accidental duplications from history.
        Handles adjacent repeats and repeating sequences (echoes).
        """
        messages = self.instance_conversations.get(instance_name, [])
        if not messages: return
        
        # 1. Prune adjacent identical messages (same role, content, name, AND function_call)
        new_msgs = []
        for m in messages:
            if not new_msgs:
                new_msgs.append(m)
                continue
            prev = new_msgs[-1]
            if str(prev.get(ROLE)) == str(m.get(ROLE)) and \
               str(prev.get(CONTENT)).strip() == str(m.get(CONTENT)).strip() and \
               str(prev.get('name')) == str(m.get('name')):
                # Also check function_call — parallel tool calls have identical role/content/name
                # but different function_calls; pruning them loses valid messages
                prev_fc = prev.get('function_call')
                curr_fc = m.get('function_call')
                if (prev_fc is None and curr_fc is None) or prev_fc == curr_fc:
                    logger.debug(f"Cleanup [{instance_name}]: Pruned adjacent identical message.")
                    continue
            new_msgs.append(m)
        messages = new_msgs

        # 2. Search for context summary markers and handle duplication around them
        # (Sliding window match for segments that were accidentally re-appended after compression)
        i = 0
        while i < len(messages):
            msg = messages[i]
            if isinstance(msg, dict) and str(msg.get(CONTENT, "")).startswith(COMPRESSION_MARKER):
                num_after = len(messages) - 1 - i
                if num_after > 0:
                    for length in range(num_after, 0, -1):
                        after_segment = messages[i+1 : i+1+length]
                        for start_idx in range(0, i - length + 1):
                            before_segment = messages[start_idx : start_idx + length]
                            matches = True
                            for k in range(length):
                                mb = before_segment[k]
                                ma = after_segment[k]
                                if str(mb.get(ROLE)) != str(ma.get(ROLE)) or \
                                   str(mb.get(CONTENT)).strip() != str(ma.get(CONTENT)).strip():
                                    matches = False
                                    break
                            if matches:
                                logger.debug(f"Cleanup [{instance_name}]: Pruned {length} echo messages found around summary.")
                                del messages[i+1 : i+1+length]
                                i = -1
                                break
                        if i == -1: break
            i += 1
            
        # 3. General sequence-deduplication (catch accidental turn repeats anywhere)
        # We look for any repeating sequence of length 2 or more.
        i = 0
        while i < len(messages):
            # Try sequences of length 5 down to 2
            for n in range(min(5, (len(messages)-i) // 2), 1, -1):
                seq = messages[i : i+n]
                # Look for this exact sequence immediately following it
                following = messages[i+n : i+2*n]
                
                matches = True
                for k in range(n):
                    if str(seq[k].get(ROLE)) != str(following[k].get(ROLE)) or \
                       str(seq[k].get(CONTENT)).strip() != str(following[k].get(CONTENT)).strip():
                        matches = False
                        break
                if matches:
                    logger.debug(f"Cleanup [{instance_name}]: Pruned repeated sequence of {n} messages starting at index {i+n}.")
                    del messages[i+n : i+2*n]
                    i = -1 # Restart
                    break
            if i == -1: 
                i = 0
                continue
            i += 1
        
        self.instance_conversations[instance_name] = messages
    
    def get_agent_info(self, agent_name: str) -> Optional[dict]:
        """Get info about a specific agent."""
        config = self.agent_configs.get(agent_name.strip().lower())
        if not config:
            return None
        
        return {
            'name': config.get('name', agent_name),
            'tagline': config.get('tagline', ''),
            'tools': config.get('capabilities', {}).get('tools', []),
            'description': config.get('identity', {}).get('background', ''),
        }
