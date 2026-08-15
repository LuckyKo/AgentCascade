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
from agent_cascade.constants import NON_LLM_KEYS
from agent_cascade.settings import DEFAULT_MAX_TURNS
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
            # Insert after identity line; skip extra blank/comment lines
            # (matches execution_engine.py line 943)
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
        instance_name = self.pool._resolve_instance_name(instance_name)
        now = time.monotonic()
        existing = self.pool.instances.get(instance_name)
        is_reuse = False
        inst = None  # Initialize to ensure it's always defined

        # Skip reuse logic if force_fresh=True (for Security/Compressor agents)
        if not force_fresh and existing is not None:
            # Reuse existing instance if it's IDLE or TERMINATED (not currently
            # executing)
            existing_state = getattr(existing, 'state', None)
            if existing_state in (AgentState.IDLE, AgentState.TERMINATED):
                # Reuse existing inactive instance instead of creating new one
                inst = existing
                is_reuse = True

                # Update _nest_depth to reflect current call chain depth (Fix
                inst._nest_depth = nest_depth

                # MAJOR FIX: Reset last_activity when reusing instance so idle
                # timer starts from reuse event
                inst.last_activity = now

                # Clear old child tracking — reused instances start fresh with no children
                # Track old parent for cleanup, then update to new caller (thread-safe).
                # Use _children_lock for pool.children and _state_lock for instance state.
                with self.pool._children_lock:
                    old_parent = inst.parent_instance
                with inst._state_lock:
                    inst.parent_instance = caller
                    inst._child_instances.clear()

                # Remove from old parent's tracking even if new caller is None
                if old_parent is not None:
                    self.pool._update_child_relationship(old_parent, instance_name, add=False)

                logger.debug(
                    f"[INSTANCE REUSE] '{instance_name}' ({agent_class}) reusing existing inactive instance. "
                    f"Conversation history will be preserved and extended."
                )
            else:
                # Existing instance is still active
                # (RUNNING/SLEEPING/COMPLETING), fall through to create new one
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

        # FIX
        if not is_reuse:
            self.pool.instances[instance_name] = inst
            logger.debug(
                "[CALL_AGENT_DEBUG] _create_and_run_agent — new instance registered in pool for %s",
                instance_name
            )

            # Per-instance child tracking + pool.children sync via helper (thread-safe, deduped)
            if caller is not None and caller != instance_name:
                self.pool._update_child_relationship(caller, instance_name, add=True)
        else:
            # Reuse path: update per-instance child tracking for new caller (thread-safe via helper)
            if caller is not None and caller != instance_name:
                # Remove from old parent's tracking if caller changed
                if old_parent is not None and old_parent != caller:
                    self.pool._update_child_relationship(old_parent, instance_name, add=False)
                # Add to new parent's tracking (helper handles deduplication)
                self.pool._update_child_relationship(caller, instance_name, add=True)

        # BUG FIX (Bug 2): Load session from log_file if provided
        session_was_loaded = False
        if log_file:
            # Don't dismiss all instances — we're just loading history into an existing instance
            status = self.pool.load_session_from_log(log_file, target_instance=instance_name, clear_sub_agents_before_load=False, caller_name=caller)
            if status.startswith("Error"):
                logger.warning(f"[LOG_FILE_LOAD] Failed to load session for '{instance_name}': {status}")
            else:
                # load_session_from_log creates a new instance in the pool,
                # update our reference
                inst = self.pool.instances.get(instance_name) or inst
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

    @staticmethod
    def _is_image_referenced_in_task(
        img_url: str,
        task_text: str,
        seen_images: dict
    ) -> bool:
        """Check if an image is referenced in task text by basename or alias.

        Args:
            img_url: The image URL to check
            task_text: The task text to search in
            seen_images: Dict mapping basenames/aliases to image URLs

        Returns:
            True if the image's basename or any of its aliases appear in task_text
        """
        basename = get_basename_from_url(img_url)
        if basename in task_text:
            return True
        # Check if any alias for this image appears in task text
        for alias, url in seen_images.items():
            if url == img_url and alias in task_text:
                return True
        return False

    def _collect_images_from_caller(
        self,
        caller: str,
    ) -> dict:
        """Scan caller's conversation for images and build basename/alias mapping.

        Args:
            caller: Parent instance name to scan

        Returns:
            Dict mapping basenames and aliases (e.g., 'image_0') to image URLs
        """
        seen_images = {}
        alias_counter = 0
        caller_conv = self.pool.get_conversation(caller)
        if not caller_conv:
            return seen_images

        for msg in caller_conv:
            content = msg_field(msg, 'content')
            if isinstance(content, list):
                for item in content:
                    item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                    item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                    if item_type == IMAGE:
                        img_url = item_value
                        basename = get_basename_from_url(img_url)
                        seen_images[basename] = img_url
                        seen_images[f"image_{alias_counter}"] = img_url
                        alias_counter += 1

        return seen_images

    def _propagate_images_to_task(
        self,
        task_text: str,
        caller: str,
        max_images_for_llm: int
    ) -> list:
        """Build multimodal content list by propagating relevant images from caller.

        Only includes images that are explicitly referenced in task text by basename
        or alias, respecting the max_images_for_llm limit.

        Args:
            task_text: The formatted task text (includes context and task labels)
            caller: Parent instance name to scan for images
            max_images_for_llm: Max images with base64 to propagate (-1 = keep all)

        Returns:
            List of content items: [{'text': task_text}, {IMAGE: url}, ...] or just
            [{'text': task_text}] if no images should be propagated.
        """
        agent_msg_content = [{'text': task_text}]

        seen_images = self._collect_images_from_caller(caller)
        propagated_image_urls = set()

        def _can_add_more() -> bool:
            return max_images_for_llm == -1 or len(propagated_image_urls) < max_images_for_llm

        # Include images referenced in task text (by basename or alias)
        for img_url in seen_images.values():
            if not _can_add_more():
                break
            if self._is_image_referenced_in_task(img_url, task_text, seen_images) and img_url not in propagated_image_urls:
                agent_msg_content.append({IMAGE: img_url})
                propagated_image_urls.add(img_url)

        # Also check last user message for images referenced in task text
        if _can_add_more():
            caller_conv = self.pool.get_conversation(caller)
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
                            if not _can_add_more():
                                break
                            item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                            item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                            if item_type == IMAGE and item_value not in propagated_image_urls:
                                if self._is_image_referenced_in_task(item_value, task_text, seen_images):
                                    agent_msg_content.append({IMAGE: item_value})
                                    propagated_image_urls.add(item_value)

        return agent_msg_content

    def build_task_message(
        self,
        args: dict,
        caller: str,
        agent_class: Optional[str] = None
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

        # Task 1: Include max_turns info in context when call_agent was called
        # with a custom turn budget
        # This lets the sub-agent know its allocated turn limit upfront
        max_turns_info = ''
        if 'max_turns' in args and isinstance(args['max_turns'], int) and args['max_turns'] > 0:
            max_turns_info = f"\n\nYour turn budget for this task: {args['max_turns']} turns."

        # Match main AC branch formatting behavior
        caller_prefix = f"This is a message from {caller}."
        if context_text:
            context_text = f"{caller_prefix}\n{context_text}"
        else:
            context_text = caller_prefix

        task_text = f'Context: {context_text}{max_turns_info}\n\nTask: {task_text}\n\nPlease help with this task.'

        # Read max_images_for_llm from pool config to limit image propagation
        # -1 = keep all, 0 = no images, N >= 1 = max N images
        raw_max = self.pool.llm_cfg.get('max_images_for_llm', 2)
        if isinstance(raw_max, int):
            max_images_for_llm = raw_max if raw_max >= -1 else 2
        else:
            max_images_for_llm = 2

        # Delegate image propagation to helper method (uses _collect_images_from_caller,
        # _is_image_referenced_in_task internally)
        agent_msg_content = self._propagate_images_to_task(task_text, caller, max_images_for_llm)

        # Fallback for empty message (match main AC branch behavior)
        if not task_text.strip():
            task_text = "Please proceed with your task."
            agent_msg_content[0]['text'] = task_text

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
        # METADATA INJECTION FIX: Inject metadata into sys_msg BEFORE
        # logging/update_history().
        # This ensures sub-agent log files contain the "Session Metadata" block
        # in their initial system message.
        # The existing injection in _setup_turn() is preserved for runtime
        # updates (e.g., workspace changes).
        _inject_metadata_into_message(sys_msg, self.pool, instance)

        # FIX: Initialize before if/else so it's available for both branches
        # (was only set in else branch)
        is_restored_session = False

        if is_reuse:
            # Thread-safe update of instance state for reuse
            with instance._compression_lock:
                # FIX #3: Reset stale state fields to prepare for new task
                instance.compression_summary = None
                instance.latest_marker_index = -1
                instance._generate_cfg_override = None
                instance.max_turns = None
                instance._current_turn = 0

                # FIX #4: Clear is_terminated flag
                instance.is_terminated = False

                # SLOT_TIMEOUT FIX: Clear _slot_release to prevent stale
                # callback issues
                instance._slot_release = None
                if hasattr(instance, '_slot_key'):
                    instance._slot_key = None

                # FIX: Preserve & extend conversation
                # Update system message in-place (first message is always
                # system)
                if instance.conversation and len(instance.conversation) > 0:
                    # Preserve old system message's timestamp so
                    # update_history() can match it as an update
                    old_sys_msg = instance.conversation[0]
                    if hasattr(old_sys_msg, 'timestamp') and old_sys_msg.timestamp:
                        try:
                            sys_msg.timestamp = old_sys_msg.timestamp
                        except AttributeError:
                            pass  # Fallback: generate new timestamp below

                    # FIX
                    if not getattr(sys_msg, 'timestamp', None):
                        sys_msg.timestamp = datetime.datetime.now().isoformat()

                    # Update the existing system message with new template
                    # content
                    instance.edit_message_in_place(0, sys_msg)  # PR2: centralized API handles cache sync
                else:
                    # Fallback: prepend system message if conversation is empty
                    instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync


                # Get the preserved conversation (will be extended with task
                # below)
                conv = instance.conversation

                # FIX
                # (conv already set above with system message updated in-place)
            instance.append_message(task_msg)  # PR2: centralized mutation API handles cache sync

            # FIX
            # load_session_from_log(). Just log the new task message directly
            # instead of
            # calling update_history() with the trimmed working set (~33 msgs
            # vs 63+ in log).
            # The forward-only search in update_history can miss matches
            # against the full
            # history, causing buffer insertions → duplicates.
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                log_inst.log_message(task_msg)

                # Logged task message to logger for reused instance

                # ── Tail sync check after task logging (design doc §5.2 — D1
                # fix) ──
                if getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                    from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                    with instance._compression_lock:
                        current_conv = list(instance.conversation)
                    _check_tail(instance_name, current_conv, log_inst.log_path, context="reused_instance_init")
            except Exception as e:
                logger.debug(f"Logging task message for reused {instance_name} failed (non-critical): {e}")
        else:
            # For new instances: check if session was loaded from a log file
            # (explicit parameter, not flag)
            with instance._compression_lock:
                if from_external_load:
                    # Session loaded from log file — only append task message,
                    # preserve restored conversation
                    instance.append_message(task_msg)  # Centralized mutation API handles cache sync

                    conv = instance.conversation
                    is_restored_session = True
                else:
                    # Build conversation: [system, task] for fresh instances
                    conv = [sys_msg, task_msg]
                    instance.rebuild_conversation(conv)  # PR2: centralized mutation API handles full cache invalidation

                    is_restored_session = False

            # Log messages to agent's JSONL file (outside lock — logger has its
            # own synchronization)
            # Note: Direct log_message calls are acceptable here because the
            # initialization path
            # runs single-threaded and messages are already in conversation
            # before logging.
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                if is_restored_session:
                    log_inst.log_message(task_msg)
                else:
                    log_inst.log_message(sys_msg)
                    log_inst.log_message(task_msg)

                # ── Tail sync check after session init logging (design doc
                # §5.2 — D1 fix) ──
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
        
        Propagates max_turns and disabled_tools from the caller agent's configuration
        to the child instance. Uses single lock scope to prevent race conditions where
        another thread reads partial state.
        
        Note: max_input_tokens is NOT propagated here. It is resolved dynamically
        at call time via _resolve_max_tokens(), which consults the API Router for
        live endpoint values (enabling failover). Baking it into the override would
        freeze the value at spawn time and block router updates.
        
        Args:
            instance: Child instance to configure
            caller: Parent instance name
            agent_class: Child's agent class
            call_agent_args: Optional args dict from the call_agent tool invocation.
                           If it contains 'max_turns', that value is used (capped by caller's limit).
        
        Note:
            If target template has no LLM config, max_turns is still set but
            disabled_tools propagation is skipped.
        """
        # FIX
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
            # Do NOT read from llm_cfg — it was stripped out of
            # _generate_cfg_override
            # because 'max_turns' is in NON_LLM_KEYS and must not leak to the
            # LLM API.
            caller_max_turns = getattr(caller_inst, 'max_turns', None) or DEFAULT_MAX_TURNS

            # If agent budgeting is enabled, allow per-agent max_turns
            # overrides via call_agent args.
            # Otherwise, all agents use the caller's max_turns limit regardless
            # of call_agent args.
            enable_agent_budgeting = getattr(self.pool.settings, 'enable_agent_budgeting', True) if hasattr(self.pool, 'settings') else True

            # Use provided max_turns from call_agent args if specified,
            # otherwise inherit from caller.
            # The caller's limit (UI turn limit) acts as the hard cap.
            if call_agent_args and 'max_turns' in call_agent_args and enable_agent_budgeting:
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
                # Target template has no LLM — skip settings propagation but
                # continue execution
                logger.warning(
                    f"Target agent instance ({agent_class}) template has no LLM config — "
                    f"skipping settings propagation (disabled_tools only)"
                )
                return

            # FIX: Do NOT bake max_input_tokens into _generate_cfg_override here.
            # Doing so freezes the value at spawn time and short-circuits
            # _resolve_max_tokens() from consulting the API Router for live
            # endpoint values on failover. Per-instance overrides should only
            # contain max_input_tokens when explicitly set via UI (_apply_ui_config),
            # which is already handled correctly there.
            with self.pool._state_lock:
                # Propagate non-LLM config settings from caller to child instance.
                if llm_cfg:
                    cfg = (target_template.llm.generate_cfg or {}).copy()

                    # Ensure max_input_tokens is NOT baked into the override,
                    # so _resolve_max_tokens() can consult the API Router dynamically.
                    cfg.pop('max_input_tokens', None)

                    # Merge UI-level settings from caller's config that should
                    # propagate to children.
                    # Keys in NON_LLM_KEYS are excluded so each agent uses its
                    # own model config (includes max_input_tokens, max_turns, etc.).
                    if llm_cfg:
                        for k, v in llm_cfg.items():
                            if k not in NON_LLM_KEYS and v is not None:
                                cfg[k] = v

                    instance._generate_cfg_override = cfg

                # Centralized disabled_tools resolution — see
                # agent_cascade.utils.disabled_tools
                from agent_cascade.utils.disabled_tools import (
                    resolve_disabled_tools_for_agent,
                    normalize_disabled_tools,
                    merge_disabled_tools,
                )

                # Resolve caller's full disabled set (includes class defaults
                # via resolver)
                caller_type = getattr(caller_template, 'agent_type', '') or ''
                caller_name = getattr(caller_template, 'name', '') or ''
                caller_disabled = resolve_disabled_tools_for_agent(
                    instance_override=getattr(caller_inst, '_generate_cfg_override', None),
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=caller_name,
                    agent_type=caller_type,
                )

                # Also resolve disabled tools FOR THE CHILD AGENT from the
                # caller's per-agent dict.
                # The caller's _generate_cfg_override may contain a dict like
                # {'Compressor': [...], 'Coder': [...]} — we need to look up
                # the child's entry.
                target_name = getattr(target_template, 'name', '') or agent_class
                target_type = getattr(target_template, 'agent_type', '') or ''
                # Fallback to instance's agent_class for defense-in-depth
                # (matches execution_engine.py)
                if not target_type:
                    target_type = getattr(instance, 'agent_class', '') or ''
                child_disabled_from_caller_cfg = resolve_disabled_tools_for_agent(
                    instance_override=getattr(caller_inst, '_generate_cfg_override', None),
                    template_cfg=getattr(caller_template.llm, 'generate_cfg', None),
                    agent_name=target_name,
                    agent_type=target_type,
                )

                # Propagate caller's disabled tools into child instance
                # override.
                # Merge with any existing disabled_tools already on the child
                # config.
                cfg = (copy.deepcopy(instance._generate_cfg_override)
                       if instance._generate_cfg_override
                       else (target_template.llm.generate_cfg or {}).copy())

                existing_disabled = normalize_disabled_tools(cfg.get('disabled_tools'))
                merged = merge_disabled_tools(existing_disabled, caller_disabled)
                # Also merge child-specific disabled tools extracted from
                # caller's per-agent dict.
                # This ensures entries like {'Compressor': [...]} are properly
                # applied to the child.
                merged = merge_disabled_tools(merged, child_disabled_from_caller_cfg)

                # Check live pool config for real-time tool updates.
                if self.pool and hasattr(self.pool, 'get_ui_disabled_tools_for_agent'):
                    live_disabled = self.pool.get_ui_disabled_tools_for_agent(target_name, target_type)
                    merged = merge_disabled_tools(merged, live_disabled)

                cfg['disabled_tools'] = list(merged)  # store as list for JSON serialization
                instance._generate_cfg_override = cfg

                # Defense-in-depth defaults (Security/Compressor) are applied
                # both here via the
                # child lookup above AND again at runtime during engine.run() —
                # idempotent by design.
        except Exception as e:
            logger.debug(f"Settings propagation from {caller} to instance failed (non-critical): {e}")
