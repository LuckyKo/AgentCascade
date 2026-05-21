"""
Agent Pool — Manages a pool of specialized sub-agents.

Handles agent discovery, loading, instance lifecycle, conversation persistence,
context compression, and streaming state for the WebUI.
"""

import copy
import json
import os
import datetime
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from agent_cascade.agents import Assistant
from agent_cascade.log import logger
from agent_cascade.llm.schema import (
    ASSISTANT, CONTENT, FUNCTION, ROLE, SYSTEM, USER, Message,
)
from agent_cascade.utils.utils import extract_text_from_message
from agent_cascade.settings import DEFAULT_WORKSPACE

from agent_logger import AgentInstanceLogger
from telemetry import TelemetryCollector
from agent_cascade.prompts.dna import (
    COMPRESSION_BASELINE_TEMPLATE, 
    COMPRESSION_MARKER,
    COMPRESSION_NOTICE_TEMPLATE
)
from api_router import APIRouter

# Minimum length for a valid compression summary (below this, skip insertion)
MIN_SUMMARY_LENGTH = 10


class AgentPool:
    """Manages a pool of specialized sub-agents."""
    
    def __init__(self, llm_cfg: dict, agents_dir: str = 'agents', workspace_dir: Optional[str] = None):
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
        
        # Initialize Parallel Agent Manager
        from agent_orchestrator import ParallelAgentManager
        self.parallel_manager = ParallelAgentManager(self, max_workers=llm_cfg.get('max_parallel_agents', 3))
        
        # Auto-load all agents from the agents directory
        self._discover_agents()

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

    # ── Per-instance halt for forced compression ──────────────────────────
    def is_halted(self, instance_name: str) -> bool:
        """Check if a specific agent instance has been halted (e.g. during forced compression)."""
        return self._instance_halted.get(instance_name, False)

    def halt_instance(self, instance_name: str):
        """Halt a specific agent instance while allowing others to continue."""
        self._instance_halted[instance_name] = True

    def resume_instance(self, instance_name: str):
        """Resume a previously halted agent instance."""
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
                was_already_halted = self._instance_halted.get(inst, False)
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
        """Remove an instance from the pool. If it's active, it will be stopped and cleared upon completion."""
        if instance_name in self.active_stack:
            self.terminated_instances.add(instance_name)
            self._stopped_event.set()
        else:
            self.clear_conversation(instance_name)

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
        
        self.agents[agent_name] = agent
        self.agent_configs[agent_name] = agent.agent_configs.get(agent_name, {})
        
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
            'tool_result_max_chars', 'grep_char_limit', 'shell_char_limit', 'code_char_limit'
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

    def slice_history_for_llm(self, history: List[Union[dict, Message]]) -> List[Union[dict, Message]]:
        """
        Extract the 'working set' from a full conversation history.
        Preserves the first SYSTEM message and slices from the latest compression marker onwards.
        """
        if not history:
            return []
            
        latest_summary_idx = -1
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            role = msg.get(ROLE) if isinstance(msg, dict) else getattr(msg, 'role', '')
            content = msg.get(CONTENT, '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if role == USER and isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                latest_summary_idx = i
                break
                
        if latest_summary_idx == -1:
            return history
            
        system_msg = None
        first_role = history[0].get(ROLE) if isinstance(history[0], dict) else getattr(history[0], 'role', '')
        if first_role == SYSTEM:
            system_msg = history[0]
            
        sliced = history[latest_summary_idx:]
        
        # Ensure system message is at the top
        if system_msg and (sliced[0].get(ROLE) if isinstance(sliced[0], dict) else getattr(sliced[0], 'role', '')) != SYSTEM:
            return [system_msg] + list(sliced)
            
        return list(sliced)

    def get_compression_target_set(self, agent_name: str) -> tuple[Optional[int], List[Union[dict, Message]], int]:
        """
        Returns the compression target set for an agent: 
        (active_start_idx, messages_to_compress, latest_summary_idx).
        Shared helper used by both the compress_context tool and _apply_context_compression.
        
        active_start_idx: The index where the active (uncompressed) set begins.
        messages_to_compress: The list of active messages eligible for compression.
        latest_summary_idx: The index of the most recent compression marker (-1 if none).
        """
        history = self.get_conversation(agent_name)
        if not history:
            return None, [], -1
        
        # Determine start index (skip system message if present)
        start_idx = 1 if (history[0].get('role') == SYSTEM if isinstance(history[0], dict) else getattr(history[0], 'role', '') == SYSTEM) else 0
        
        # Find the latest compression marker to identify the active set boundary
        latest_summary_idx = -1
        for i in range(len(history) - 1, -1, -1):
            msg = history[i]
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if isinstance(content, str) and content.startswith(COMPRESSION_MARKER):
                latest_summary_idx = i
                break
        
        active_start_idx = latest_summary_idx + 1 if latest_summary_idx != -1 else start_idx
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
        self.instance_conversations.clear()
        self.instance_classes.clear()
        self.instance_loggers.clear()
        self.sub_agent_state.clear()
        self.active_stack.clear()
        self.last_tool_args.clear()
        self.terminated_instances.clear()
        self._instance_halted.clear()
        self._compression_halted.clear()
        logger.info("AgentPool reset — all instances and loggers cleared.")
    
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
    
    def _apply_context_compression(self, agent_name: str, summary: str, fraction: float, target_discard_count: Optional[int] = None, agent_obj=None):
        """
        Actually replace the oldest messages in an agent's history with a summary.
        Called by OperationManager after approval.
        
        If target_discard_count is provided, used as-is (subject to safety clamps).
        If None, calculated from fraction.
        """
        # Use shared helper to determine the active compression set
        active_start_idx, messages_to_compress, latest_summary_idx = self.get_compression_target_set(agent_name)
        if not messages_to_compress:
            logger.warning(f"No messages to compress for agent '{agent_name}': empty conversation history")
            return
        
        # Keep the first message if it's SYSTEM
        system_msg = None
        start_idx = 0
        history = self.get_conversation(agent_name)
        first_role = history[0].get('role') if isinstance(history[0], dict) else getattr(history[0], 'role', '')
        if first_role == SYSTEM:
            system_msg = history[0]
            start_idx = 1
        
        # DEBUG: verify last summary content in pool (only at debug level)
        _pool_last_summary_preview = ""
        _pool_post_summary_preview = ""
        if latest_summary_idx != -1:
            _sm = history[latest_summary_idx]
            _sc = _sm.get('content', '') if isinstance(_sm, dict) else getattr(_sm, 'content', '')
            _pool_last_summary_preview = str(_sc)[:300]
            # Also preview the message right AFTER the summary (first active message)
            if latest_summary_idx + 1 < len(history):
                _pm = history[latest_summary_idx + 1]
                _pc = _pm.get('content', '') if isinstance(_pm, dict) else getattr(_pm, 'content', '')
                _pool_post_summary_preview = str(_pc)[:300]
        
        from agent_cascade.utils.tokenization_qwen import count_tokens
        
        if target_discard_count is None:
            # Fallback: Calculate total tokens to find the actual fraction of content to compress
            total_tokens = 0
            token_counts = []
            for msg in messages_to_compress:
                tokens = agent_obj._count_message_tokens(msg) if agent_obj and hasattr(agent_obj, '_count_message_tokens') else 0
                if not tokens:
                    # Fallback if agent_obj isn't provided
                    from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
                    if isinstance(msg, dict):
                        role = msg.get('role', '')
                        function_call = msg.get('function_call')
                        if role == ASSISTANT and function_call:
                            tokens = qwen_count(f'{function_call}')
                        else:
                            content = extract_text_from_message(Message(**msg), add_upload_info=True)
                            tokens = qwen_count(content)
                    else:
                        if msg.role == ASSISTANT and msg.function_call:
                            tokens = qwen_count(f'{msg.function_call}')
                        else:
                            content = extract_text_from_message(msg, add_upload_info=True)
                            tokens = qwen_count(content)
                            
                token_counts.append(tokens)
                total_tokens += tokens
                
            target_tokens = int(total_tokens * fraction)
            
            tokens_seen = 0
            target_discard_count = 0
            for count in token_counts:
                tokens_seen += count
                target_discard_count += 1
                if tokens_seen >= target_tokens and target_discard_count < len(messages_to_compress) - 1:
                    break
                
        # Not enough messages to compress meaningfully - need at least 3 to keep 2 tail messages
        if len(messages_to_compress) <= 2:
            logger.warning(f"Not enough active messages to compress for agent '{agent_name}': only {len(messages_to_compress)} available")
            return
        
        # Safety clamp: don't compress more messages than exist in the active set.
        # Also ensure at least 2 messages remain after the summary marker for agent continuity.
        target_discard_count = min(target_discard_count, len(messages_to_compress))
        if len(messages_to_compress) > 2:
            target_discard_count = min(target_discard_count, len(messages_to_compress) - 2)
        
        # Safety floor: ensure at least 1 message is compressed if we reach this point.
        # For normal tool flow, this is unreachable (tool rejects <= 0).
        # For forced compression or direct callers, this prevents no-op compression events.
        # NOTE: Applied AFTER clamps so the clamp doesn't defeat the floor.
        if target_discard_count <= 0:
            logger.warning(f"target_discard_count was 0 for agent '{agent_name}', forcing minimum of 1")
            target_discard_count = 1
        
        # The compression summary marker itself acts as a safe starting point 
        # for the active context, so we don't need to scan for non-FUNCTION roles.
        # This ensures the exact fraction requested by the user is respected.
        pass
            
        # Guard: if the summary is empty or trivially short (likely generated from no content), skip insertion.
        # This prevents inserting meaningless markers that confuse the agent and slice_history_for_llm.
        if not summary or len(summary.strip()) < MIN_SUMMARY_LENGTH:
            logger.warning(f"Empty or trivial summary for agent '{agent_name}' (len={len(summary) if summary else 0}), skipping compression insertion")
            return
        
        # Create the summary text using the notice template from dna.py
        compression_notice = COMPRESSION_NOTICE_TEMPLATE.format(fraction=int(fraction * 100))
        summary_text = COMPRESSION_BASELINE_TEMPLATE.format(
            header=f"{int(fraction * 100)}% of history summarized",
            summary=summary,
            compression_notice=compression_notice
        )
        
        # New history baseline marker
        is_dict = isinstance(system_msg, dict) if system_msg else isinstance(messages_to_compress[0], dict)
        summary_msg = {'role': USER, 'content': str(summary_text)} if is_dict else Message(role=USER, content=str(summary_text))
        
        # 1. CUMULATIVE: Insert summary marker at the boundary position.
        #    The boundary is right after the summarized messages (active_start_idx + target_discard_count).
        #    We do NOT pop the summarized messages to provide full visibility in the UI.
        #    The slice_history_for_llm method handles actual LLM truncation by scanning
        #    for the COMPRESSION_MARKER.
        insert_pos = active_start_idx + target_discard_count
        history.insert(insert_pos, summary_msg)
        
        # 2. Track the active summary
        self.instance_summaries[agent_name] = summary
        
        # 3. Notify the logger that a compression event happened.
        #    Pass tail_count (number of messages AFTER the summary marker in the pool)
        #    so the logger can compute insert_pos = len(log_history) - tail_count.
        #    This avoids problems from pool/log divergence: the log may have extra
        #    incrementally-logged messages that shift absolute positions, but the
        #    number of tail messages is stable and comparable.
        tail_count = len(messages_to_compress) - target_discard_count
        logger.debug(f"[COMPRESSION DEBUG] Agent={agent_name}, pool_summary_idx={latest_summary_idx}, active_start={active_start_idx}, active_set={len(messages_to_compress)}, target_discard_count={target_discard_count}, insert_pos={insert_pos}, tail={tail_count}")
        logger.debug(f"[COMPRESSION DEBUG] Pool summary preview: {_pool_last_summary_preview[:100]}")
        logger.debug(f"[COMPRESSION DEBUG] Pool post-summary preview: {_pool_post_summary_preview[:100]}")
        
        if agent_name in self.instance_loggers:
            self.instance_loggers[agent_name].insert_compression_marker(
                summary_msg, tail_count
            )
        
        logger.info(f"Cumulative compression: Inserted summary after {target_discard_count} messages for agent '{agent_name}'. Full history preserved in memory.")
    
    def get_agent_info(self, agent_name: str) -> Optional[dict]:
        """Get info about a specific agent."""
        config = self.agent_configs.get(agent_name)
        if not config:
            return None
        
        return {
            'name': config.get('name', agent_name),
            'tagline': config.get('tagline', ''),
            'tools': config.get('capabilities', {}).get('tools', []),
            'description': config.get('identity', {}).get('background', ''),
        }
