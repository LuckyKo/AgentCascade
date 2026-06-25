"""
CompressionHandler — Phase 4.2 of ExecutionEngine Refactor.

Extracts all compression-related logic from ExecutionEngine into a focused class.
This includes forced compression checks, /compress command handling, and compress_context tool handling.

Design Pattern: Lazy Initialization (same as AgentLifecycleManager)
- __init__ receives pool only
- set_engine() called after ExecutionEngine construction completes
- self.engine property raises RuntimeError if accessed before initialization
"""

import json
import time
from typing import TYPE_CHECKING, Any, List, Optional

if TYPE_CHECKING:
    from agent_cascade.execution_engine import ExecutionEngine

from agent_cascade.log import logger
from agent_cascade.llm.schema import Message, USER
from agent_cascade.agent_instance import AgentInstance


# ── Helper Functions (moved from execution_engine module level) ───────────────

def _msg_role(msg: dict | Message) -> str:
    """Safely get role from dict or Message object."""
    return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')


def _msg_content(msg: dict | Message) -> str:
    """Safely get content from dict or Message object."""
    return msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')


# ── CompressionHandler Class ─────────────────────────────────────────────────

class CompressionHandler:
    """Handles all compression-related logic for agent context management.
    
    Extracted from ExecutionEngine (Phase 4.2) to reduce God Object complexity.
    Manages forced compression, cooldown checks, overfeeding detection, and
    manual /compress command handling.
    
    Usage:
        handler = CompressionHandler(pool)
        engine.compression_handler.set_engine(engine)  # Set engine reference after construction
        should_skip = handler.check_cooldown(instance, llm_messages, usage_pct)
        if not should_skip:
            success = handler.execute_force_compression(instance, messages, llm_messages, usage_pct)
    """
    
    def __init__(self, pool):
        """Initialize with pool reference only.
        
        Args:
            pool: AgentPool for state management
        """
        self.pool = pool
        self._engine = None  # Lazy initialization (see REVIEWER FINDING #1)
    
    @property
    def engine(self) -> 'ExecutionEngine':
        """Get engine reference (raises if not set)."""
        if self._engine is None:
            raise RuntimeError("CompressionHandler._engine not set. Call ExecutionEngine.initialize().")
        return self._engine
    
    def set_engine(self, engine: 'ExecutionEngine') -> None:
        """Set engine reference after ExecutionEngine construction completes."""
        self._engine = engine
    
    # ── Notification Helper with Pending Queue ────────────────────────────────
    
    def _inject_compression_notification(
        self,
        instance: 'AgentInstance',
        notification_text: str,
        inst_name: str,
    ) -> None:
        """Push a compression notification into the pending queue for in-tool-response injection.
        
        After forced compression the pool has [SYS][U0][MARKER=USER][tail...].
        A separate USER notification would create consecutive USER messages (API violation).
        The notification is queued and drained into the next tool response's FUNCTION content,
        or into the next _drain_and_inject USER message. If no such messages fire,
        the notification is simply not delivered — that's acceptable.
        
        Dedup guard: checks if the exact notification text already exists in the conversation.
        
        Args:
            instance: Agent instance with conversation pool access
            notification_text: The [SYSTEM] notification string to inject
            inst_name: Instance name for logging
        """
        with instance._compression_lock:
            # Dedup guard — check if notification appears as a complete line (avoids substring false positives)
            notification_exists = any(
                notification_text in str(_msg_content(m)).split('\n')
                for m in instance.conversation
            )
            
            # Also check pending queue for undelivered duplicates (prevents double-queue on rapid compression)
            pending_queue = getattr(instance, '_pending_notifications', None)
            if pending_queue and notification_text in pending_queue:
                notification_exists = True
            
            if not notification_exists:
                if pending_queue is None:
                    instance._pending_notifications: List[str] = []
                    pending_queue = instance._pending_notifications
                pending_queue.append(notification_text)
                logger.info(f"Compression notification queued for injection into '{inst_name}'")
            else:
                logger.debug(f"Compression notification already exists in conversation for '{inst_name}' — skipping")

    def _drain_pending_into_tool_result(
        self,
        instance: 'AgentInstance',
        tool_result: str,
    ) -> str:
        """Drain pending notifications into a tool result string.
        
        Called from execution_engine before constructing the FUNCTION message.
        If there are pending notifications, they are appended to tool_result and queue cleared.
        
        Args:
            instance: Agent instance with _pending_notifications attribute
            tool_result: The tool's raw output string (or any object convertible to str)
        Returns:
            tool_result with notifications appended (or unchanged if no pending)
        """
        # Bug 2 fix: hold lock while reading/writing shared _pending_notifications
        # Bug 6 fix: convert non-string tool results to string before draining
        with instance._compression_lock:
            pending = getattr(instance, '_pending_notifications', None)
            if pending:
                if not isinstance(tool_result, str):
                    tool_result = str(tool_result)
                notif_block = "\n\n".join(n for n in pending)
                tool_result = f"{tool_result}\n\n{notif_block}"
                instance._pending_notifications = []
        return tool_result

    def _drain_pending_into_user_message(
        self,
        instance: 'AgentInstance',
        text: str,
    ) -> str:
        """Drain pending notifications into a USER message's content string.
        
        Called from execution_engine before constructing the first USER message in _drain_and_inject.
        If there are pending notifications, they are prepended to text and queue cleared.
        
        Args:
            instance: Agent instance with _pending_notifications attribute
            text: The user message's raw content string
        Returns:
            text with notifications prepended (or unchanged if no pending)
        """
        # Bug 2 fix: hold lock while reading/writing shared _pending_notifications
        with instance._compression_lock:
            pending = getattr(instance, '_pending_notifications', None)
            if pending:
                if isinstance(text, str):
                    notif_block = "\n\n".join(n for n in pending)
                    text = f"{notif_block}\n{text}"
                # Always clear the queue — notification was "attempted" even if text is non-string
                instance._pending_notifications = []
        return text

    def _drain_tool_warnings(
        self,
        instance: 'AgentInstance',
        text: str,
        prepend: bool = False,
    ) -> str:
        """Drain generic tool warnings into a text string.

        Called from execution_engine before constructing FUNCTION or USER messages.
        If there are pending tool warnings (e.g., path resolution from extra folders),
        they are appended to the result and queue cleared.

        Args:
            instance: Agent instance with _tool_warnings attribute
            text: The raw content string (or any object convertible to str)
            prepend: If True, warnings are prepended (for USER messages).
                     If False (default), warnings are appended (for tool results).
        Returns:
            text with warnings added (or unchanged if no pending)
        """
        # Copy and clear the warnings list under lock, then do string ops outside the lock
        with instance._compression_lock:
            warnings = list(instance._tool_warnings)  # Guaranteed dataclass field (default_factory=list)
            instance._tool_warnings = []

        if warnings:
            if not isinstance(text, str):
                text = str(text)
            warning_block = "\n\n".join(str(w) for w in warnings)
            if prepend:
                text = f"[TOOL WARNINGS]\n{warning_block}\n\n{text}"
            else:
                text = f"{text}\n\n[TOOL WARNINGS]\n{warning_block}"
        return text
    
    # ── Logger Sync Helper (unified for all compression paths) ────────────────
    
    def _sync_logger_after_compression(
        self,
        instance_name: str,
        agent_class: str,
        operation_name: str
    ) -> None:
        """Sync logger state to match pool after compression/rollback operations.
        
        This is a unified helper for all compression paths (forced compression,
        compress_context tool, /compress command, /rollback command). Using
        reset_history() instead of update_history() ensures exact synchronization:
        - update_history() is ADDITIVE only (can't shrink history)
        - reset_history(rewrite=True) replaces logger state with pool state
        
        Args:
            instance_name: Name of the agent instance
            agent_class: Agent class for logger retrieval
            operation_name: Description for logging (e.g., "forced compression", "/compress command")
        """
        try:
            conv = self.pool.get_conversation(instance_name)
            log_inst = self.pool.get_logger(instance_name, agent_class)
            logger.debug(
                f"Logger sync after {operation_name} for '{instance_name}': "
                f"pool_len={len(conv) if conv else 0}, using reset_history() for full sync"
            )
            log_inst.reset_history(conv, rewrite=True)
        except Exception as e:
            logger.error(
                f"Logger sync after {operation_name} FAILED for '{instance_name}': {e}. "
                f"Pool may desync — manual intervention required."
            )
    
    # ── Forced Compression Methods (extracted from _force_compression) ────────
    
    def check_cooldown(
        self,
        instance: AgentInstance,
        llm_messages: List[Message],
        usage_pct: float
    ) -> bool:
        """Check if compression cooldown is active.
        
        Extracted from _check_compression_cooldown() - Phase 3.5
        
        Args:
            instance: Agent instance
            llm_messages: Working set for warning injection
            usage_pct: Current token usage percentage
            
        Returns:
            True if cooldown active (skip compression this cycle)
        """
        inst_name = instance.instance_name
        
        with instance._compression_lock:
            now = time.monotonic()
            cooldown = getattr(self.pool.settings, 'compression_force_cooldown', 2.0)
            elapsed = now - instance._last_force_compress_time
            
            if elapsed < cooldown:
                logger.warning(
                    f"Forced compression cooldown active for {inst_name}: "
                    f"{elapsed:.1f}s / {cooldown:.1f}s — skipping this cycle"
                )
                current_tokens = self.engine._count_history_tokens(instance.conversation, instance)
                max_tokens = self.engine._get_max_tokens(instance)
                self.engine._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens)
                return True
            
            # Mark this compression attempt (under lock for thread safety)
            instance._last_force_compress_time = now
            instance._force_compress_count += 1
        
        return False
    
   
    def check_overfeeding(
        self,
        instance: AgentInstance,
        llm_messages: List[Message],
        response: Optional[List[Message]] = None
    ) -> bool:
        """Check if overfeeding threshold exceeded.
        
        Extracted from _check_overfeeding() - Phase 3.5
        
        Args:
            instance: Agent instance
            llm_messages: Working set for notification injection
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
            
        Returns:
            True if overfeeding detected (terminate agent)
        """
        inst_name = instance.instance_name
        max_attempts = getattr(self.pool.settings, 'compression_max_attempts', 3)
        
        if instance._force_compress_count >= max_attempts:
            logger.error(
                f"Overfeeding detected for {inst_name}: "
                f"{instance._force_compress_count} forced compressions exceeded limit of {max_attempts}. "
                f"Context keeps filling faster than compression can reduce it. Terminating agent."
            )
            notification_text = f"[SYSTEM] Overfeeding — {instance._force_compress_count} compressions without relief. Terminating."
            self._inject_compression_notification(instance, notification_text, inst_name)
            self.pool.halt_instance(inst_name)
            return True
        
        return False
    
    def execute_force_compression(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        usage_pct: float,
        response: Optional[List[Message]] = None
    ) -> bool:
        """Execute forced compression and rebuild working set.
        
        Extracted from _execute_force_compression() - Phase 3.5
        
        Args:
            instance: Agent instance
            messages, llm_messages: Working message sets
            usage_pct: Current token usage percentage
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
            
        Returns:
            True if compression successful (continue loop)
        """
        inst_name = instance.instance_name
        
        # Halt other agents (exempt target, Compressor, and root agent)
        exempt = [inst_name, 'Compressor']
        if instance.parent_instance:
            exempt.append(instance.parent_instance)
        self.pool.halt_all_instances(except_instances=exempt)
        
        try:
            logger.info(
                f"Context usage at {usage_pct:.1f}% for {inst_name} — "
                f"forcing compression (attempt #{instance._force_compress_count})."
            )

            from agent_cascade.compression.core import compress_context as _compress
            result = _compress(
                agent_pool=self.pool,
                target_agent_name=inst_name,
                fraction=0.5,
                mode='auto',
                force=True,
            )

            if result.success:
                # Rebuild working set from compressed pool state (includes token cache invalidation)
                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                
                # Use summary_text directly from CompressResult (P2 fix — no fragile tag parsing)
                instance.compression_summary = result.summary_text
                # Update latest_marker_index to point to the new marker in the conversation (P2 fix)
                conv = self.pool.get_conversation(inst_name)
                if conv:
                    for idx, msg in enumerate(conv):
                        role = _msg_role(msg)
                        c = _msg_content(msg)
                        if isinstance(c, str) and '<context_summary>' in c:
                            instance.latest_marker_index = idx

                    # ── Inject compression feedback into last message (in-tool-response pattern) ──
                    # Appending to the last message's content avoids creating a separate USER message
                    # which would violate OpenAI API alternation rules (consecutive USER messages after marker).
                    notification_text = (
                        f"[SYSTEM] Context exceeded {usage_pct:.1f}%. "
                        f"Forced compression applied. Continue your work — context has been preserved."
                    )
                    self._inject_compression_notification(instance, notification_text, inst_name)

                    # Re-fetch conv after notification append so validation includes the notification message
                    conv = self.pool.get_conversation(inst_name)

                    # ── FIX: Sync logger BEFORE validation ──
                    # Ensures that if recovery is needed, the logger has clean compressed state.
                    self._sync_logger_after_compression(inst_name, instance.agent_class, "forced compression")

                    # Item 10: Validate message pool after forced compression (now includes notification)
                    from agent_cascade.utils.pool_validation import validate_message_pool
                    if not validate_message_pool(conv, inst_name):
                        logger.error(f"[MSG POOL VALIDATION] Pool invalid after forced compression for '{inst_name}'. Attempting recovery from log...")
                        # Recovery: reload from the logger's history (now synced with compressed state)
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                instance.rebuild_conversation(list(recov))
                                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov
                                # Sync logger again after recovery to reflect recovered state
                                self._sync_logger_after_compression(inst_name, instance.agent_class, "recovery")
                            else:
                                logger.error("Recovery from log also failed — message pool may be corrupted")
                                notification_text = f"[SYSTEM] Compression corrupted pool: Forced compression and recovery both failed for {inst_name}. Agent halted to prevent corruption."
                                self._inject_compression_notification(instance, notification_text, inst_name)
                                # Halt this instance to prevent further execution with corrupted state
                                self.pool.halt_instance(inst_name)
                        except Exception as e:
                            logger.error(f"Recovery attempt failed for '{inst_name}': {e}")

                    # Set cooldown flag to suppress loop detection on next turn after compression
                    instance._suppress_loop_detection_next_turn = True
                    
                    # Force immediate stream update via existing periodic push mechanism (avoids duplicate broadcasts)
                    self.engine.stream_publisher.push_periodic_update(instance.parent_instance or inst_name)
            
            else:  # Compression failed or returned error
                logger.error(f"Forced compression failed for {inst_name}: {result.error}")
                notification_text = f"[SYSTEM] Context exceeded {usage_pct:.1f}%, but automatic compression failed."
                self._inject_compression_notification(instance, notification_text, inst_name)

        except Exception as e:
            logger.error(f"Forced compression raised exception for {inst_name}: {e}")
            return True

        finally:
            self.pool.resume_all_instances()
    
    # ── Compress Context Tool Handler ────────────────────────────────────────
    
    def handle_compress_tool(
        self,
        args: Any,
        instance: AgentInstance,
        target_agent_name: str
    ) -> str:
        """Handle compress_context tool call — delegates to compression module.
        
        Extracted from _handle_compress_context() - Phase 4.2
        
        Args:
            args: Compression arguments (fraction, mode).
            instance: Agent instance for cache invalidation
            target_agent_name: Name of the agent whose context should be compressed.

        Returns:
            Result string with compression outcome.
        """
        if args is None:
            # JSON parsing failed in _resolve_placeholders — return error
            return 'Error: Invalid JSON arguments.'

        # Fix #7: Validate fraction to prevent extreme values
        fraction = max(0.1, min(0.9, args.get('fraction', 0.5)))
        mode = args.get('mode', 'auto')
        summary_text = args.get('summary_text')
        force = args.get('force', False)

        # NOTE: Do NOT wrap compress_context in _compression_lock — it internally
        # calls agent_pool.get_conversation() which acquires the same lock.
        # Holding the outer lock + inner lock = deadlock (non-reentrant Lock).
        from agent_cascade.compression.core import compress_context as _compress
        result = _compress(
            agent_pool=self.pool,
            target_agent_name=target_agent_name,
            fraction=fraction,
            mode=mode,
            summary_text=summary_text,
            force=force,
        )

        if result.success:
            from agent_cascade.execution_engine import _invalidate_token_cache
            _invalidate_token_cache(instance)  # Invalidate cache before rebuilding working set (Fix Finding #3)
            
            
            # Get current messages for rebuild
            conv = self.pool.get_conversation(target_agent_name)
            if conv:
                messages_list = list(conv)
                llm_messages_list = list(self.pool.slice_history_for_llm(conv))
                self.engine._rebuild_working_set(messages_list, llm_messages_list, target_agent_name)
            
            # Set cooldown flag to suppress loop detection on next turn after compression
            instance._suppress_loop_detection_next_turn = True

            # Sync logger state to match pool after compress_context tool execution
            self._sync_logger_after_compression(target_agent_name, instance.agent_class, "compress_context tool")

            # Force immediate stream update via existing periodic push mechanism (avoids duplicate broadcasts)
            self.engine.stream_publisher.push_periodic_update(instance.parent_instance or instance.instance_name)

            return (f"Compression successful. Discarded {result.messages_discarded} messages. "
                    f"Tail count: {result.tail_count}.")
        else:
            return f"Compression failed: {result.error}"
    
    # ── /compress Command Handler Methods ────────────────────────────────────
    
    def detect_and_parse_compress_command(
        self,
        instance: AgentInstance,
        messages: List[Message]
    ) -> Optional[float]:
        """Detect /compress command and parse fraction parameter.
        
        Extracted from _detect_and_parse_compress_command() - Phase 3.7
        
        Scans the last user message for /compress command pattern.
        
        Args:
            instance: Current agent instance
            messages: Working message list
            
        Returns:
            Fraction value (0.1-0.9) if command detected, None otherwise.
        """
        inst_name = instance.instance_name
        
        # Find the last USER message
        last_user = None
        for msg in reversed(messages):
            role = _msg_role(msg)
            if role == USER:
                last_user = msg
                break
        
        if last_user is None:
            return None
        
        content = _msg_content(last_user)
        if not isinstance(content, str):
            return None
        
        stripped_content = content.strip()
        if not stripped_content.startswith('/compress'):
            return None
        
        # Guard against re-detection of notification messages containing "/compress"
        # When a notification is appended with "\n\n{notification_text}", any text starting
        # with "/compress" will have "\n/compress" in the content. This catches such cases.
        if '\n/compress' in content:
            return None  # Skip embedded /compress references (e.g., in notifications)
        
        # Parse fraction from command before modifying content - default 0.5
        parts = content.strip().split()
        fraction = 0.5
        if len(parts) > 1:
            try:
                fraction = float(parts[1])
            except ValueError as e:
                logger.warning(f"Invalid fraction in /compress command for {inst_name}: {e}")
        
        # Clamp fraction to valid range
        fraction = max(0.1, min(0.9, fraction))
        
        # Replace the /compress command with a descriptive system message to prevent re-detection
        # Convert decimal fraction to percentage (e.g., 0.5 → "50%")
        percentage = int(round(fraction * 100))
        system_message = f"[SYSTEM] Compressing {percentage}% of context..."
        if isinstance(last_user, dict):
            last_user['content'] = system_message
        else:
            last_user.content = system_message
        
        return fraction
    
    def generate_compression_preview(
        self,
        instance: AgentInstance,
        messages: List[Message],
        fraction: float
    ) -> Optional[tuple]:
        """Generate compression preview in dry_run mode.
        
        Extracted from _generate_compression_preview() - Phase 3.7
        
        Args:
            instance: Current agent instance
            messages: Working message list
            fraction: Compression fraction
            
        Returns:
            Tuple of (summary, reason) if successful; (None, reason) on failure.
            reason is one of: 'success', 'tool_unavailable', 'preview_failed', 'exception'
        """
        inst_name = instance.instance_name
        
        # Get compress_context tool from template
        template = self.pool.get_template(instance.agent_class)
        if not template or 'compress_context' not in getattr(template, 'function_map', {}):
            logger.warning(f"/compress command but compress_context tool unavailable for {inst_name}")
            return (None, 'tool_unavailable')
        
        compress_tool = template.function_map['compress_context']
        
        # Generate preview summary (dry_run)
        try:
            preview_params = json.dumps({
                'fraction': fraction,
                'mode': 'auto',
            })
            summary = compress_tool.call(
                preview_params,
                messages=messages,
                agent_instance_name=inst_name,
                agent_obj=instance,  # Pass instance so tool can resolve agent_pool via template
                dry_run=True,  # Don't mutate pool yet
            )
        except Exception as e:
            logger.error(f"Preview compression failed for {inst_name}: {e}")
            return (None, 'exception')
        
        if not summary or str(summary).startswith("ERROR"):
            logger.warning(f"/compress preview failed for {inst_name}: {summary}")
            return (None, 'preview_failed')
        
        return (summary, 'success')
    
    def request_user_approval(
        self,
        messages: List[Message],
        inst_name: str,
        fraction: float,
        summary: str,
        instance: Optional[AgentInstance] = None,
        llm_messages: Optional[List[Message]] = None,
        response: Optional[List[Message]] = None
    ) -> bool:
        """Request user approval for compression via UI.
        
        Extracted from _request_user_approval() - Phase 3.7
        
        Args:
            messages: Working message list for notifications
            inst_name: Instance name for logging
            fraction: Compression fraction
            summary: Preview summary text
            instance: Agent instance (needed for lock/cache invalidation)
            llm_messages: LLM working set (for notification append)
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
            
        Returns:
            True if approved, False if rejected.
        """
        if self.pool.operation_manager:
            try:
                approved, rejection_reason = self.pool.operation_manager.request_user_approval(
                    agent_name=inst_name,
                    tool_name='compress_context',
                    tool_args={'fraction': fraction, 'summary': summary},
                    description=f"Proposed Compression Summary ({int(round(fraction * 100))}% of history)",
                )
            except Exception as e:
                logger.error(f"User approval request failed for {inst_name}: {e}")
                if instance is not None and llm_messages is not None:
                    notification_text = f"[SYSTEM] Compression command failed: Compression approval request failed: {e}"
                    self._inject_compression_notification(instance, notification_text, inst_name)
                return False
        else:
            # No operation_manager — auto-approve (standalone mode)
            approved = True
        
        if not approved:
            logger.info(f"/compress rejected by user for {inst_name}: {rejection_reason}")
            if instance is not None and llm_messages is not None:
                notification_text = f"[SYSTEM] Compression cancelled: Compression cancelled by user. Reason: {rejection_reason}"
                self._inject_compression_notification(instance, notification_text, inst_name)
        
        return approved  # FIX Bug #2: Return actual approval status
    
    def apply_approved_compression(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        fraction: float,
        summary: str,
        compress_tool,
        response: Optional[List[Message]] = None
    ) -> bool:
        """Apply compression with validation and recovery.
        
        Extracted from _apply_approved_compression() - Phase 3.7
        
        Executes actual compression, validates message pool, attempts recovery if needed,
        syncs logger history, and sets loop detection cooldown flag.
        
        Args:
            instance: Current agent instance
            messages, llm_messages: Working message sets
            fraction: Compression fraction
            summary: Precomputed preview summary
            
        Returns:
            True if compression succeeded (continue loop), False otherwise.
        """
        inst_name = instance.instance_name
        
        try:
            apply_params = json.dumps({
                'fraction': fraction,
                'mode': 'auto',
            })
            result = compress_tool.call(
                apply_params,
                messages=messages,
                agent_instance_name=inst_name,
                agent_obj=instance,  # Pass instance for proper pool resolution
                precomputed_summary=summary,  # Skip LLM summary generation
            )
            logger.info(f"/compress applied for {inst_name}: {result}")
            
            # Check if compression succeeded (tool returns string, not CompressResult)
            result_str = str(result) if result else ""
            if result_str.startswith("Compression failed"):
                logger.warning(f"/compress silently failed for {inst_name}: {result}")
                notification_text = f"[SYSTEM] Compression command failed: Compression failed for {inst_name}: {result}"
                self._inject_compression_notification(instance, notification_text, inst_name)
                return True
            
            # Validate message pool after compression (Item 10)
            conv = self.pool.get_conversation(inst_name)

            # ── FIX: Sync logger BEFORE validation ──
            # Ensures that if recovery is needed, the logger has clean compressed state.
            self._sync_logger_after_compression(inst_name, instance.agent_class, "/compress command")

            working_set_rebuilt = False
            from agent_cascade.utils.pool_validation import validate_message_pool
            if conv and not validate_message_pool(conv, inst_name):
                logger.error(f"[MSG POOL VALIDATION] Pool invalid after /compress for '{inst_name}'. Attempting recovery...")
                try:
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))  # PR2: centralized API handles full cache sync
                        logger.info(f"Recovered message pool after /compress for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                        working_set_rebuilt = True
                        # FIX 5: Sync logger after recovery to reflect recovered state
                        self._sync_logger_after_compression(inst_name, instance.agent_class, "/compress recovery")
                    else:
                        notification_text = f"[SYSTEM] Compression corrupted pool: Compression applied but message pool validation failed and recovery unsuccessful."
                        notif_msg = Message(role=USER, content=notification_text)
                        instance.append_message(notif_msg)
                        if response is not None:
                            response.append(notif_msg)
                except Exception as e:
                    logger.error(f"Recovery after /compress failed for '{inst_name}': {e}")
            
            # Rebuild working set after successful compression (if not already rebuilt in recovery path)
            conv = self.pool.get_conversation(inst_name)
            if conv and validate_message_pool(conv, inst_name) and not working_set_rebuilt:
                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
            
            # Set cooldown flag to suppress loop detection on next turn after compression
            instance._suppress_loop_detection_next_turn = True
            
            # Fix Issue #2: Explicit token cache invalidation to handle edge cases
            # where rebuild_working_set is not called (e.g., conv is None or validation fails)
            from agent_cascade.execution_engine import _invalidate_token_cache
            _invalidate_token_cache(instance)
            
            notification_text = f"[SYSTEM] Compression applied successfully for {inst_name}."
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
            
            # Force immediate stream update via existing periodic push mechanism (avoids duplicate broadcasts)
            self.engine.stream_publisher.push_periodic_update(instance.parent_instance or inst_name)

            return True
            
        except Exception as e:
            logger.error(f"/compress apply failed for {inst_name}: {e}")
            notification_text = f"[SYSTEM] Compression command failed: Compression apply failed for {inst_name}: {e}"
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
            return True
    
    def handle_compress_command(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: Optional[List[Message]] = None
    ) -> bool:
        """Detect and handle /compress [fraction] user command.

        Extracted from _handle_compress_command() - Phase 3.7
        
        Args:
            instance: Current agent instance
            messages, llm_messages: Working message sets
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
        
        Returns True if the command was handled (whether approved or not).
        """
        inst_name = instance.instance_name
        
        # Step 1: Detect and parse command
        fraction = self.detect_and_parse_compress_command(instance, messages)
        if fraction is None:
            return False
        
        # Step 2: Generate preview (Fix Issue #1: distinguish failure modes)
        result = self.generate_compression_preview(instance, messages, fraction)
        summary, reason = result if result else (None, None)
        if not summary:
            if reason == 'tool_unavailable':
                notification_text = f"[SYSTEM] Compression tool unavailable: Compression command issued but compress_context tool is unavailable for {inst_name}."
            elif reason == 'preview_failed':
                notification_text = f"[SYSTEM] Compression command failed: Compression preview failed for {inst_name}. Cannot compress."
            else:  # exception or unknown
                notification_text = f"[SYSTEM] Compression command failed: Compression preview encountered an error for {inst_name}. Cannot compress."
            
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
            return True
        
        # Step 3: Apply compression (skip user approval — proceed directly like WebSocket path)
        template = self.pool.get_template(instance.agent_class)
        compress_tool = template.function_map['compress_context']
        
        return self.apply_approved_compression(instance, messages, llm_messages, fraction, summary, compress_tool, response)

    # ── /rollback Command Handler Methods ────────────────────────────────────

    def detect_and_parse_rollback_command(
        self,
        instance: AgentInstance,
        messages: List[Message]
    ) -> Optional[int]:
        """Detect /rollback command and parse count parameter.
        
        Scans the last user message for /rollback command pattern.
        Also replaces the command content with a descriptive system message to prevent re-detection.
        
        Args:
            instance: Current agent instance
            messages: Working message list
            
        Returns:
            Rollback count (positive integer) if command detected, None otherwise.
        """
        inst_name = instance.instance_name
        
        # Find the last USER message
        last_user = None
        for msg in reversed(messages):
            role = _msg_role(msg)
            if role == USER:
                last_user = msg
                break
        
        if last_user is None:
            return None
        
        content = _msg_content(last_user)
        if not isinstance(content, str):
            return None
        
        stripped_content = content.strip()
        if not stripped_content.startswith('/rollback'):
            return None
        
        # Guard against re-detection of notification messages containing "/rollback"
        if '\n/rollback' in content:
            return None  # Skip embedded /rollback references (e.g., in notifications)
        
        # Parse count from command before modifying content - default 1
        parts = content.strip().split()
        count = 1
        if len(parts) > 1:
            try:
                count = int(parts[1])
                if count < 1:
                    count = 1
            except ValueError as e:
                logger.warning(f"Invalid count in /rollback command for {inst_name}: {e}")
                count = 1
        
        # Clamp count to reasonable range (1-50) to prevent catastrophic rollbacks
        max_rollback_count = getattr(self.pool.settings, 'rollback_max_count', 50)
        count = max(1, min(max_rollback_count, count))
        
        # Replace the /rollback command with a descriptive system message to prevent re-detection
        system_message = f"[SYSTEM] Rolling back {count} {'message' if count == 1 else 'messages'}..."
        if isinstance(last_user, dict):
            last_user['content'] = system_message
        else:
            last_user.content = system_message
        
        return count

    def handle_rollback_command(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: Optional[List[Message]] = None
    ) -> bool:
        """Detect and handle /rollback [count] user command.
        
        Args:
            instance: Current agent instance
            messages, llm_messages: Working message sets
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
        
        Returns True if the command was handled (whether successful or not).
        """
        inst_name = instance.instance_name
        
        # Step 1: Detect and parse command (also replaces content with descriptive message)
        count = self.detect_and_parse_rollback_command(instance, messages)
        if count is None:
            return False
        
        # Step 2: Apply rollback using pool's surgical_rollback (unified path)
        # Note: surgical_rollback() already handles cache invalidation internally (sets _last_token_count_conversation_length = -1)
        try:
            actual_count = self.pool.surgical_rollback(inst_name, count, reason="Manual /rollback command")
            
            # Validate message pool after rollback (Item 10 - same as compress handler lines 602-626)
            conv = self.pool.get_conversation(inst_name)

            # ── FIX: Sync logger BEFORE validation ──
            # Ensures that if recovery is needed, the logger has clean compressed state.
            self._sync_logger_after_compression(inst_name, instance.agent_class, "/rollback command")

            working_set_rebuilt = False
            from agent_cascade.utils.pool_validation import validate_message_pool
            if conv and not validate_message_pool(conv, inst_name):
                logger.error(f"[MSG POOL VALIDATION] Pool invalid after /rollback for '{inst_name}'. Attempting recovery...")
                try:
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        instance.rebuild_conversation(list(recov))  # PR2: centralized API handles full cache sync
                        logger.info(f"Recovered message pool after /rollback for '{inst_name}' ({len(recov)} messages)")
                        self.engine._rebuild_working_set(messages, llm_messages, inst_name)
                        working_set_rebuilt = True
                        # FIX 5: Sync logger after recovery to reflect recovered state
                        self._sync_logger_after_compression(inst_name, instance.agent_class, "/rollback recovery")
                    else:
                        notification_text = f"[SYSTEM] Rollback corrupted pool: Rollback applied but message pool validation failed and recovery unsuccessful."
                        notif_msg = Message(role=USER, content=notification_text)
                        instance.append_message(notif_msg)
                        if response is not None:
                            response.append(notif_msg)
                except Exception as e:
                    logger.error(f"Recovery after /rollback failed for '{inst_name}': {e}")
            
            # Rebuild working set after successful validation (if not already rebuilt in recovery path)
            if conv and validate_message_pool(conv, inst_name) and not working_set_rebuilt:
                self.engine._rebuild_working_set(messages, llm_messages, inst_name)
            
            # Explicit token cache invalidation to handle edge cases where rebuild_working_set is not called
            # (e.g., conv is None or validation fails). Matches compress handler pattern at lines 644-647.
            from agent_cascade.execution_engine import _invalidate_token_cache
            _invalidate_token_cache(instance)
            
            # Suppress loop detection on next turn to prevent false positives from abrupt state change
            instance._suppress_loop_detection_next_turn = True
            
            notification_text = f"[SYSTEM] Rollback applied: Rolled back {actual_count} message(s) for {inst_name}."
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)
            if response is not None:
                response.append(notif_msg)
            
            logger.info(f"/rollback command executed for {inst_name}: rolled back {actual_count} message(s)")
            return True
            
        except Exception as e:
            logger.error(f"/rollback apply failed for {inst_name}: {e}")
            # Append notification as a new Message object (not mutating last message content)
            notification_text = f"[SYSTEM] Rollback command failed: Rollback failed for {inst_name}: {e}"
            notif_msg = Message(role=USER, content=notification_text)
            instance.append_message(notif_msg)  # Updates conversation + _cached_messages (= messages/llm_messages) atomically
            if response is not None:
                response.append(notif_msg)  # Keep local list update for streaming
            return True