"""
RollbackMixin — conversation snapshots and surgical rollback. Moved verbatim from agent_pool.py (Phase 2). Carries the lazy tail_sync_check import unchanged.
"""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER
from agent_cascade.prompts.dna import COMPRESSION_MARKER
class RollbackMixin:
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
    @staticmethod
    def _msg_field(msg, field, default=''):
        """Extract a field from a message (dict or Message object)."""
        return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)

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
                if len(conv) > 0 and self._msg_field(conv[0], 'role') == SYSTEM:
                    keep_at_least = 1
                    if len(conv) > 1 and self._msg_field(conv[1], 'role') == USER:
                        keep_at_least = 2

            # Refine: avoid leaving dangling FUNCTION messages at the cut boundary
            start_idx = current_len - pop_count
            if refine_function_boundary and start_idx >= keep_at_least:
                if self._msg_field(conv[start_idx], 'role') == FUNCTION:
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
