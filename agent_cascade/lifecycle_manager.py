"""
Agent Lifecycle Manager — Phase 4.1 of the AgentCascade Architecture Rewrite.

Manages agent instance lifecycle: creation, reuse logic, settings propagation.
Extracted from ExecutionEngine to reduce God Object complexity.

See DESIGN_REWRITE.md §4.1 for design rationale.
"""

import time
from typing import Tuple, Optional, TYPE_CHECKING

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.llm.schema import Message, SYSTEM, USER
from agent_cascade.log import logger


if TYPE_CHECKING:
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.execution_engine import ExecutionEngine


# ── Module-level helper functions (FIX #4 - reviewer) ────────────────────────
def _msg_role(msg: dict | Message) -> str:  # FIX #3 + tighter type annotation
    """Get role from message dict or object."""
    return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')


def _msg_content(msg: dict | Message) -> str:  # FIX #3 + tighter type annotation
    """Get content from message dict or object."""
    return msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')


def _inject_metadata_into_message(sys_msg: Message, pool: 'AgentPool', instance: AgentInstance) -> None:
    """Inject Session Metadata block into system message if not already present.
    
    This is called before logging to ensure sub-agent log files contain the metadata block.
    The existing injection in execution_engine._setup_turn() is preserved for runtime updates.
    
    Args:
        sys_msg: System Message object to modify in-place
        pool: AgentPool instance (needed for _build_session_metadata)
        instance: AgentInstance whose metadata should be injected
    """
    from agent_cascade.execution_engine import _build_session_metadata
    
    # Defensive guard for empty content
    if not sys_msg.content or not sys_msg.content.strip():
        return
    
    if '## Session Metadata' not in sys_msg.content:
        meta_block = _build_session_metadata(pool, instance)
        if meta_block:
            content_lines = sys_msg.content.split('\n')
            # Insert after identity line; skip extra blank/comment lines (matches execution_engine.py line 943)
            insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith("#") else 1
            for i, ml in enumerate(meta_block.split('\n')):
                content_lines.insert(insert_pos + i, ml)
            sys_msg.content = '\n'.join(content_lines)


class AgentLifecycleManager:
    """Manages agent instance lifecycle: creation, reuse, and configuration.
    
    This class handles:
    - Finding or creating instances (with reuse logic)
    - Building system and task messages
    - Propagating settings from parent to child
    - Initializing conversations and logger state
    
    Usage:
        manager = AgentLifecycleManager(pool)
        inst, is_reuse = manager.find_or_create_instance(...)
        sys_msg = manager.build_system_message(...)
        manager.propagate_settings(inst, caller_name, agent_class)
    """
    
    def __init__(self, pool):
        """Initialize with reference to AgentPool.
        
        Args:
            pool: AgentPool instance for template lookup and state management
        """
        self.pool = pool
        self._engine = None  # Lazy initialization
    
    @property
    def engine(self) -> 'ExecutionEngine':
        """Get engine reference (raises if not set)."""
        if self._engine is None:
            raise RuntimeError("AgentLifecycleManager._engine not set. Call ExecutionEngine.initialize().")
        return self._engine
    
    def set_engine(self, engine: 'ExecutionEngine') -> None:
        """Set engine reference after ExecutionEngine construction completes.
        
        This breaks the circular dependency during __init__.
        
        Args:
            engine: ExecutionEngine instance for cross-reference
        """
        self._engine = engine
    
    def find_or_create_instance(
        self,
        agent_class: str,
        instance_name: str,
        caller: str,
        nest_depth: int,
        force_fresh: bool = False
    ) -> Tuple[AgentInstance, bool]:
        """Find existing inactive instance or create new one.
        
        Checks for an existing inactive (IDLE/TERMINATED) instance that can be reused.
        If no reusable instance exists, creates a new AgentInstance.
        
        Args:
            agent_class: Template class name
            instance_name: Unique instance identifier
            caller: Parent instance name
            nest_depth: Depth in call chain
            force_fresh: If True, always create new instance (for Security/Compressor)
            
        Returns:
            Tuple of (instance, is_reuse) where is_reuse indicates if existing was reused
        """
        now = time.monotonic()
        existing = self.pool.instances.get(instance_name)
        is_reuse = False
        inst = None  # Initialize to ensure it's always defined
        
        # Skip reuse logic if force_fresh=True (for Security/Compressor agents)
        if not force_fresh and existing is not None:
            # Reuse existing instance if it's IDLE or TERMINATED (not currently executing)
            existing_state = getattr(existing, 'state', None)
            if existing_state in (AgentState.IDLE, AgentState.TERMINATED):
                # Reuse existing inactive instance instead of creating new one
                inst = existing
                is_reuse = True
                
                # Update _nest_depth to reflect current call chain depth (Fix #1 improvement)
                inst._nest_depth = nest_depth
                
                # MAJOR FIX: Reset last_activity when reusing instance so idle timer starts from reuse event
                inst.last_activity = now
                
                logger.debug(
                    f"[INSTANCE REUSE] '{instance_name}' ({agent_class}) reusing existing inactive instance. "
                    f"Conversation history will be preserved and extended."
                )
            else:
                # Existing instance is still active (RUNNING/SLEEPING/COMPLETING), fall through to create new one
                existing = None  # Clear so we don't incorrectly log about reusing an active instance
        
        if inst is None or not is_reuse:
            # Create new instance (existing is None or still active)
            inst = AgentInstance(
                instance_name=instance_name,
                agent_class=agent_class,
                conversation=[],
                max_turns=None,  # Will be set below via settings propagation (P6)
                parent_instance=caller,
                created_at=now,
                last_activity=now,
                compression_summary=None,
                latest_marker_index=-1,
                _nest_depth=nest_depth,
            )
            
            if existing is not None:
                # Warn about overwriting an active instance
                logger.warning(
                    f"[NEW INSTANCE] '{instance_name}' ({agent_class}) replacing active instance. "
                    f"Previous instance conversation will be replaced."
                )

        # FIX #7: Only assign new instances to pool (reused instances already exist)
        if not is_reuse:
            self.pool.instances[instance_name] = inst
            logger.debug(
                "[CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for %s",
                instance_name
            )
        
        return inst, is_reuse
    
    def build_system_message(
        self,
        agent_class: str,
        instance_name: str
    ) -> Message:
        """Build system message for new agent.
        
        Retrieves template and constructs system message with injected instance name.
        Session metadata injection is handled by P7 in _setup_turn for all agents uniformly.
        
        Args:
            agent_class: Template class name
            instance_name: Instance name to inject into template
            
        Returns:
            System Message object
            
        Raises:
            ValueError: If no template found for agent_class
        """
        template = self.pool.get_template(agent_class)
        if not template:
            logger.error("NO TEMPLATE for %s/%s", agent_class, instance_name)
            raise ValueError(f"No template for agent class {agent_class}")

        sys_content = getattr(template, 'base_system_message',
                              getattr(template, 'system_message', ''))
        lines = sys_content.strip().split('\n') if sys_content else []

        # Replace identity line
        if lines and f" {instance_name}" not in lines[0]:
            lines[0] = f"You are {instance_name}."

        return Message(role=SYSTEM, content="\n".join(lines))
    
    def build_task_message(
        self,
        args: dict,
        caller: str
    ) -> Message:
        """Build task message with multimodal image propagation.
        
        Scans caller's conversation for images and includes them as multimodal content
        if referenced in the task text or present in the last user message.
        
        Args:
            args: Tool arguments (task, context)
            caller: Parent instance name to scan for images
            
        Returns:
            Task Message object (possibly with multimodal content)
        """
        task_text = args.get('task', '')
        context_text = args.get('context', '')
        if context_text:
            task_text = f"{context_text}\n\n{task_text}"

        # Item 9: Multimodal image propagation — scan caller's conversation for images
        # referenced in the task text and include them as multimodal content
        agent_msg_content: list = [{'type': 'text', 'text': task_text}]
        added_to_inst = set()

        # Get caller's conversation history to scan for images
        caller_conv = self.pool.get_conversation(caller)
        seen_images = {}
        if caller_conv:
            for msg in caller_conv:
                content = _msg_content(msg)
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == 'image':
                            img_url = item_value
                            seen_images[img_url] = img_url

        # Include images that are referenced in the task text
        for img_url in seen_images.values():
            basename = img_url.split('/')[-1].split('?')[0] if '/' in img_url else img_url
            if basename in task_text and img_url not in added_to_inst:
                agent_msg_content.append({'type': 'image', 'value': img_url})
                added_to_inst.add(img_url)

        # Also check the last user message for images even if not referenced in text
        if caller_conv:
            last_user_msg = None
            for m in reversed(caller_conv):
                if _msg_role(m) == USER:
                    last_user_msg = m
                    break
            if last_user_msg:
                content = _msg_content(last_user_msg)
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == 'image' and item_value not in added_to_inst:
                            agent_msg_content.append({'type': 'image', 'value': item_value})
                            added_to_inst.add(item_value)

        # Use multimodal content list if images found, otherwise plain text
        if len(agent_msg_content) > 1:
            return Message(role=USER, content=agent_msg_content)
        else:
            return Message(role=USER, content=task_text)
    
    def initialize_conversation(  # FIX #3 (reviewer): Renamed from initialize_instance_conversation
        self,
        instance: AgentInstance,
        sys_msg: Message,
        task_msg: Message,
        is_reuse: bool,
        instance_name: str,
        agent_class: str
    ) -> list:
        """Initialize or extend instance conversation.
        
        For reused instances: resets stale state, updates system message in-place,
        appends task message to preserved conversation, and syncs logger.
        
        For new instances: builds fresh [system, task] conversation, assigns to instance,
        and logs initial messages.
        
        Args:
            instance: AgentInstance to initialize
            sys_msg: System message
            task_msg: Task message
            is_reuse: Whether reusing existing instance
            instance_name: Instance name for logger access
            agent_class: Agent class for logger access
            
        Returns:
            Conversation list (either preserved or newly created)
        """
        # FIX #2 (reviewer): Import at function level to avoid circular import.
        # execution_engine.py imports from lifecycle_manager.py at module level,
        # and token_cache_invalidated is defined in execution_engine.py.
        # Importing here (inside method) breaks the circular dependency.
        from agent_cascade.execution_engine import token_cache_invalidated
        
        # METADATA INJECTION FIX: Inject metadata into sys_msg BEFORE logging/update_history().
        # This ensures sub-agent log files contain the "Session Metadata" block in their initial system message.
        # The existing injection in _setup_turn() is preserved for runtime updates (e.g., workspace changes).
        _inject_metadata_into_message(sys_msg, self.pool, instance)
        
        if is_reuse:
            # Thread-safe update of instance state for reuse
            with token_cache_invalidated(instance):  # FIX: Invalidate token cache for reused instances
                with instance._compression_lock:
                    # FIX #3: Reset stale state fields to prepare for new task
                    instance.compression_summary = None
                    instance.latest_marker_index = -1
                    instance._generate_cfg_override = None
                    instance.max_turns = None
                    
                    # FIX #4: Clear is_terminated flag (token cache invalidated by outer context manager)
                    instance.is_terminated = False
                    
                    # SLOT_TIMEOUT FIX: Clear _slot_release to prevent stale callback issues
                    # The reused instance will acquire a fresh slot in engine.run() at line 349
                    # This ensures no leftover release callback from previous execution interferes
                    instance._slot_release = None
                    
                    # FIX: Preserve & extend conversation
                    # Update system message in-place (first message is always system)
                    if instance.conversation and len(instance.conversation) > 0:
                        # Preserve old system message's timestamp so update_history() can match it as an update
                        # rather than appending a duplicate. The logger uses timestamps as identity markers.
                        old_sys_msg = instance.conversation[0]
                        if hasattr(old_sys_msg, 'timestamp') and old_sys_msg.timestamp:
                            try:
                                sys_msg.timestamp = old_sys_msg.timestamp
                            except AttributeError:
                                pass  # Fallback: _format_message() will generate a new timestamp
                        
                        # Update the existing system message with new template content
                        instance.conversation[0] = sys_msg
                    else:
                        # Fallback: prepend system message if conversation is empty
                        instance.conversation.insert(0, sys_msg)
                    
                    
                    # Get the preserved conversation (will be extended with task below)
                    conv = instance.conversation
            
            # FIX #2: For reused instances, append task message to preserved conversation
            # (conv already set above with system message updated in-place)
            conv.append(task_msg)
            
            # FIX #6: Use update_history() for logger synchronization on reused instances
            # This prevents duplicate log_message(task_msg) calls and properly syncs the logger
            with instance._compression_lock:
                try:
                    log_inst = self.pool.get_logger(instance_name, agent_class)
                    log_inst.update_history(conv)
                except Exception as e:
                    logger.debug(f"Logger sync via update_history for {instance_name} failed (non-critical): {e}")
        else:
            # Build conversation: [system, task] for new instances
            conv = [sys_msg, task_msg]
            with token_cache_invalidated(instance):
                with instance._compression_lock:
                    instance.conversation = conv

                # Log initial messages to agent's JSONL file (P1 continuation)
                try:
                    log_inst = self.pool.get_logger(instance_name, agent_class)
                    log_inst.log_message(sys_msg)
                    log_inst.log_message(task_msg)
                except Exception as e:
                    logger.debug(f"Logging initial messages for {instance_name} failed (non-critical): {e}")
        
        return conv
    
    def propagate_settings(
        self,
        instance: AgentInstance,
        caller: str,
        agent_class: str
    ) -> None:
        """Propagate settings from caller to child instance.
        
        Propagates max_turns, max_input_tokens, and disabled_tools from the caller
        agent's configuration to the child instance. Uses single lock scope to prevent
        race conditions where another thread reads partial state.
        
        Args:
            instance: Child instance to configure
            caller: Parent instance name
            agent_class: Child's agent class
            
        Note:
            If target template has no LLM config, max_turns is still set but
            max_input_tokens and disabled_tools propagation is skipped.
        """
        # FIX #2 (reviewer): Add debug log before silent return to mask configuration issues
        if not hasattr(self.pool, 'api_router') or not self.pool.api_router:
            logger.debug("Settings propagation skipped — no api_router on pool")
            return
            
        try:
            caller_inst = self.pool.get_instance(caller)
            if not caller_inst:
                return
                
            caller_template = self.pool.get_template(caller_inst.agent_class)
            if not caller_template or not hasattr(caller_template, 'llm'):
                return
                
            # Use caller instance's override first (has user's UI settings),
            # fall back to template's generate_cfg
            llm_cfg = getattr(caller_inst, '_generate_cfg_override', None) or getattr(caller_template.llm, 'generate_cfg', {})

            # Propagate max_turns from caller's instance directly.
            # Do NOT read from llm_cfg — it was stripped out of _generate_cfg_override
            # because 'max_turns' is in NON_LLM_KEYS and must not leak to the LLM API.
            caller_max_turns = getattr(caller_inst, 'max_turns', None)
            if not caller_max_turns:
                caller_max_turns = 50  # DEFAULT_MAX_TURNS fallback
            instance.max_turns = caller_max_turns

            target_template = self.pool.get_template(agent_class)
            if not target_template or not getattr(target_template, 'llm', None):
                # Target template has no LLM — skip settings propagation but continue execution
                logger.warning(
                    f"Target agent instance ({agent_class}) template has no LLM config — "
                    f"skipping settings propagation (max_input_tokens and disabled_tools)"
                )
                return
            
            # Query router BEFORE acquiring _state_lock to reduce lock contention
            propagated_max = llm_cfg.get('max_input_tokens')
            # Fallback: if caller's config doesn't have max_input_tokens (e.g., because 
            # initial_llm_cfg was missing it), query the API router for the target agent type's 
            # effective limit. This ensures sub-agents get proper limits even when the 
            # caller has no specific endpoint configured.
            if not propagated_max and self.pool.api_router:
                try:
                    propagated_max = self.pool.api_router.get_effective_max_tokens(
                        agent_class.lower()
                    )
                except Exception:
                    pass
            
            # FIX #1 (reviewer): Use pool's public _state_lock property instead of accessing _execution directly
            with self.pool._state_lock:
                # Propagate max_input_tokens from caller's config (context window limit) — store on instance, NOT template
                if propagated_max:
                    cfg = (target_template.llm.generate_cfg or {}).copy()
                    cfg['max_input_tokens'] = propagated_max
                    instance._generate_cfg_override = cfg

                # Propagate disabled_tools — merge with any existing instance override (not overwrite)
                caller_disabled_tools = llm_cfg.get('disabled_tools')
                if caller_disabled_tools:
                    cfg = dict(instance._generate_cfg_override or {}) if instance._generate_cfg_override else (target_template.llm.generate_cfg or {}).copy()
                    existing_disabled = cfg.get('disabled_tools', [])
                    if isinstance(existing_disabled, list):
                        # Merge: combine existing with caller's disabled tools (deduplicated)
                        cfg['disabled_tools'] = list(set(existing_disabled + list(caller_disabled_tools)))
                    else:
                        cfg['disabled_tools'] = list(caller_disabled_tools)
                    instance._generate_cfg_override = cfg
        except Exception as e:
            logger.debug(f"Settings propagation from {caller} to instance failed (non-critical): {e}")