"""
Agent Lifecycle Manager — Phase 4.1 of the AgentCascade Architecture Rewrite.

Manages agent instance lifecycle: creation, reuse logic, settings propagation.
Extracted from ExecutionEngine to reduce God Object complexity.

See DESIGN_REWRITE.md §4.1 for design rationale.
"""

import copy
import datetime
import time
from typing import Tuple, Optional, TYPE_CHECKING

from agent_cascade.agent_instance import AgentInstance, AgentState
from agent_cascade.llm.schema import Message, SYSTEM, USER, IMAGE
from agent_cascade.log import logger
from agent_cascade.utils.utils import get_basename_from_url, msg_field


if TYPE_CHECKING:
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.execution_engine import ExecutionEngine


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
        inst, is_reuse, session_was_loaded = manager.find_or_create_instance(...)
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
        force_fresh: bool = False,
        log_file: Optional[str] = None
    ) -> Tuple[AgentInstance, bool, bool]:
        """Find existing inactive instance or create new one.
        
        Checks for an existing inactive (IDLE/TERMINATED) instance that can be reused.
        If no reusable instance exists, creates a new AgentInstance.
        
        Args:
            agent_class: Template class name
            instance_name: Unique instance identifier
            caller: Parent instance name
            nest_depth: Depth in call chain
            force_fresh: If True, always create new instance (for Security/Compressor)
            log_file: Optional path to a JSONL log file to load session history from
            
        Returns:
            Tuple of (instance, is_reuse, session_was_loaded) where is_reuse indicates
            if existing was reused and session_was_loaded indicates that conversation
            history was loaded from a log file
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

        # BUG FIX (Bug 2): Load session from log_file if provided
        session_was_loaded = False
        if log_file:
            status = self.pool.load_session_from_log(log_file, target_instance=instance_name)
            if status.startswith("Error"):
                logger.warning(f"[LOG_FILE_LOAD] Failed to load session for '{instance_name}': {status}")
            else:
                logger.info(f"[LOG_FILE_LOAD] Loaded session for '{instance_name}': {status}")
                session_was_loaded = True

        return inst, is_reuse, session_was_loaded
    
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
        
        Matches main AC branch formatting: always wraps context and task with labeled sections,
        adds caller prefix to context section, and includes a closing instruction.
        
        Args:
            args: Tool arguments (task, context)
            caller: Parent instance name to scan for images
            
        Returns:
            Task Message object (possibly with multimodal content)
        """
        task_text = args.get('task', '')
        context_text = args.get('context', '')
        
        # Match main AC branch formatting behavior
        caller_prefix = f"This is a message from {caller}."
        if context_text:
            context_text = f"{caller_prefix}\n{context_text}"
        else:
            context_text = caller_prefix
        
        task_text = f'Context: {context_text}\n\nTask: {task_text}\n\nPlease help with this task.'

        # Item 9: Multimodal image propagation — scan caller's conversation for images
        # referenced in the task text and include them as multimodal content
        # Match main AC branch format: single-key dicts
        agent_msg_content: list = [{'text': task_text}]
        added_to_inst = set()

        # Get caller's conversation history to scan for images
        caller_conv = self.pool.get_conversation(caller)
        seen_images = {}
        if caller_conv:
            for msg in caller_conv:
                content = msg_field(msg, 'content')
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == IMAGE:  # Issue 1 fix: use constant instead of hardcoded string
                            img_url = item_value
                            # Match main AC branch behavior: generate aliases for image references
                            basename = get_basename_from_url(img_url)  # Issue 2 fix: use utility function
                            seen_images[basename] = img_url
                            idx = len([v for k, v in seen_images.items() if not k.startswith("image_")]) - 1
                            seen_images[f"image_{idx}"] = img_url

        # Include images that are referenced in the task text (by basename or alias)
        for img_url in seen_images.values():
            basename = get_basename_from_url(img_url)  # Issue 2 fix: use utility function
            if basename in task_text and img_url not in added_to_inst:
                agent_msg_content.append({IMAGE: img_url})  # Match main AC branch format
                added_to_inst.add(img_url)

        # Also check the last user message for images even if not referenced in text
        # Note: No tool_name guard needed here since build_task_message() is exclusively called from call_agent path
        if caller_conv:
            last_user_msg = None
            for m in reversed(caller_conv):
                if msg_field(m, 'role') == USER:
                    last_user_msg = m
                    break
            if last_user_msg:
                content = msg_field(last_user_msg, 'content')
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == IMAGE and item_value not in added_to_inst:  # Issue 1 fix: use constant
                            agent_msg_content.append({IMAGE: item_value})  # Match main AC branch format
                            added_to_inst.add(item_value)

        # Fallback for empty message (match main AC branch behavior)
        # Note: This is technically dead code since the formatted string above always contains
        # non-empty text (caller prefix + labels), but kept as a safety net for consistency.
        if not task_text.strip():
            task_text = "Please proceed with your task."
            agent_msg_content[0]['text'] = task_text  # Issue 3 fix: removed redundant guard
        
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
        agent_class: str,
        from_external_load: bool = False
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
            from_external_load: If True, conversation was loaded from a log file and should be preserved
            
        Returns:
            Conversation list (either preserved or newly created)
        """
        # METADATA INJECTION FIX: Inject metadata into sys_msg BEFORE logging/update_history().
        # This ensures sub-agent log files contain the "Session Metadata" block in their initial system message.
        # The existing injection in _setup_turn() is preserved for runtime updates (e.g., workspace changes).
        _inject_metadata_into_message(sys_msg, self.pool, instance)
        
        # FIX: Initialize before if/else so it's available for both branches (was only set in else branch)
        is_restored_session = False

        if is_reuse:
            # Thread-safe update of instance state for reuse
            with instance._compression_lock:
                # FIX #3: Reset stale state fields to prepare for new task
                instance.compression_summary = None
                instance.latest_marker_index = -1
                instance._generate_cfg_override = None
                instance.max_turns = None
                
                # FIX #4: Clear is_terminated flag
                instance.is_terminated = False
                
                # SLOT_TIMEOUT FIX: Clear _slot_release to prevent stale callback issues
                instance._slot_release = None
                
                # FIX: Preserve & extend conversation
                # Update system message in-place (first message is always system)
                if instance.conversation and len(instance.conversation) > 0:
                    # Preserve old system message's timestamp so update_history() can match it as an update
                    old_sys_msg = instance.conversation[0]
                    if hasattr(old_sys_msg, 'timestamp') and old_sys_msg.timestamp:
                        try:
                            sys_msg.timestamp = old_sys_msg.timestamp
                        except AttributeError:
                            pass  # Fallback: generate new timestamp below
                    
                    # FIX #3: Ensure sys_msg.timestamp is ALWAYS set (never None)
                    if not getattr(sys_msg, 'timestamp', None):
                        sys_msg.timestamp = datetime.datetime.now().isoformat()
                    
                    # Update the existing system message with new template content
                    instance.edit_message_in_place(0, sys_msg)  # PR2: centralized API handles cache sync
                else:
                    # Fallback: prepend system message if conversation is empty
                    instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync
                
                
                # Get the preserved conversation (will be extended with task below)
                conv = instance.conversation
                
                # FIX #2: For reused instances, append task message to preserved conversation
                # (conv already set above with system message updated in-place)
            instance.append_message(task_msg)  # PR2: centralized mutation API handles cache sync
            
            # FIX #6: Use update_history() for logger synchronization on reused instances
            # This prevents duplicate log_message(task_msg) calls and properly syncs the logger
            with instance._compression_lock:
                try:
                    log_inst = self.pool.get_logger(instance_name, agent_class)
                    log_inst.update_history(conv)
                    log_inst._file_history_synced = True
                    
                    # ── Tail sync check after update_history (design doc §5.2 — D1 fix) ──
                    if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                        from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                        _check_tail(instance_name, conv, log_inst.log_path, context="update_history")
                except Exception as e:
                    logger.debug(f"Logger sync via update_history for {instance_name} failed (non-critical): {e}")
        else:
            # For new instances: check if session was loaded from a log file (explicit parameter, not flag)
            with instance._compression_lock:
                if from_external_load:
                    # Session loaded from log file — only append task message, preserve restored conversation
                    instance.append_message(task_msg)  # Centralized mutation API handles cache sync
                    
                    conv = instance.conversation
                    is_restored_session = True
                else:
                    # Build conversation: [system, task] for fresh instances
                    conv = [sys_msg, task_msg]
                    instance.rebuild_conversation(conv)  # PR2: centralized mutation API handles full cache invalidation
                    
                    is_restored_session = False

            # Log messages to agent's JSONL file (outside lock — logger has its own synchronization)
            # Note: Direct log_message calls are acceptable here because the initialization path
            # runs single-threaded and messages are already in conversation before logging.
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                if is_restored_session:
                    log_inst.log_message(task_msg)
                else:
                    log_inst.log_message(sys_msg)
                    log_inst.log_message(task_msg)
                
                # ── Tail sync check after session init logging (design doc §5.2 — D1 fix) ──
                if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                    from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                    with instance._compression_lock:
                        conv = list(instance.conversation)
                    _check_tail(instance_name, conv, log_inst.log_path, context="session_init")
            except Exception as e:
                logger.debug(f"Logging messages for {instance_name} failed (non-critical): {e}")
        
        return conv
    
    def propagate_settings(
        self,
        instance: AgentInstance,
        caller: str,
        agent_class: str,
        call_agent_args: dict = None,
    ) -> None:
        """Propagate settings from caller to child instance.
        
        Propagates max_turns, max_input_tokens, and disabled_tools from the caller
        agent's configuration to the child instance. Uses single lock scope to prevent
        race conditions where another thread reads partial state.
        
        Args:
            instance: Child instance to configure
            caller: Parent instance name
            agent_class: Child's agent class
            call_agent_args: Optional args dict from the call_agent tool invocation.
                           If it contains 'max_turns', that value is used (capped by caller's limit).
            
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

            # Use provided max_turns from call_agent args if specified, otherwise inherit from caller.
            # The caller's limit (UI turn limit) acts as the hard cap.
            if call_agent_args and 'max_turns' in call_agent_args:
                requested_max = call_agent_args['max_turns']
                # Validate: must be a positive integer
                if not isinstance(requested_max, int) or requested_max < 1:
                    logger.debug(f"Invalid max_turns={requested_max} for {instance.instance_name}, using caller's limit")
                    instance.max_turns = caller_max_turns
                else:
                    instance.max_turns = min(requested_max, caller_max_turns)
            else:
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

                # Centralized disabled_tools resolution — see agent_cascade.utils.disabled_tools
                from agent_cascade.utils.disabled_tools import (
                    resolve_disabled_tools_for_agent,
                    normalize_disabled_tools,
                    merge_disabled_tools,
                )

                # Resolve caller's full disabled set (includes class defaults via resolver)
                caller_type = getattr(caller_template, 'agent_type', '') or ''
                caller_name = getattr(caller_template, 'name', '') or ''
                caller_disabled = resolve_disabled_tools_for_agent(
                    instance_override=getattr(caller_inst, '_generate_cfg_override', None),
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=caller_name,
                    agent_type=caller_type,
                )

                # Also resolve disabled tools FOR THE CHILD AGENT from the caller's per-agent dict.
                # The caller's _generate_cfg_override may contain a dict like
                # {'Compressor': [...], 'Coder': [...]} — we need to look up the child's entry.
                target_name = getattr(target_template, 'name', '') or agent_class
                target_type = getattr(target_template, 'agent_type', '') or ''
                # Fallback to instance's agent_class for defense-in-depth (matches execution_engine.py)
                if not target_type:
                    target_type = getattr(instance, 'agent_class', '') or ''
                child_disabled_from_caller_cfg = resolve_disabled_tools_for_agent(
                    instance_override=getattr(caller_inst, '_generate_cfg_override', None),
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=target_name,
                    agent_type=target_type,
                )

                # Propagate caller's disabled tools into child instance override.
                # Merge with any existing disabled_tools already on the child config.
                cfg = (copy.deepcopy(instance._generate_cfg_override)
                       if instance._generate_cfg_override
                       else (target_template.llm.generate_cfg or {}).copy())

                existing_disabled = normalize_disabled_tools(cfg.get('disabled_tools'))
                merged = merge_disabled_tools(existing_disabled, caller_disabled)
                # Also merge child-specific disabled tools extracted from caller's per-agent dict.
                # This ensures entries like {'Compressor': [...]} are properly applied to the child.
                merged = merge_disabled_tools(merged, child_disabled_from_caller_cfg)

                # Check live pool config for real-time tool updates.
                if self.pool and hasattr(self.pool, 'get_ui_disabled_tools_for_agent'):
                    live_disabled = self.pool.get_ui_disabled_tools_for_agent(target_name, target_type)
                    merged = merge_disabled_tools(merged, live_disabled)

                cfg['disabled_tools'] = list(merged)  # store as list for JSON serialization
                instance._generate_cfg_override = cfg

                # Defense-in-depth defaults (Security/Compressor) are applied both here via the
                # child lookup above AND again at runtime during engine.run() — idempotent by design.
        except Exception as e:
            logger.debug(f"Settings propagation from {caller} to instance failed (non-critical): {e}")