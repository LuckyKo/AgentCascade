"""
ConversationMixin — conversation history, compression target sets, and slice_history_for_llm. Moved verbatim from agent_pool.py (Phase 2).
"""

from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional, Tuple
from agent_cascade.log import logger
from agent_cascade.llm.schema import FUNCTION, Message, ROLE, SYSTEM, USER
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from .conversation_map import _InstanceConversationMapping
class ConversationMixin:
    def clear_conversation(self, instance_name: str):
        """Clear an agent's conversation while keeping the instance alive.

        Used by agent_orchestrator.py at lines 1781 and 2245 for class mismatch
        cleanup and terminated instance cleanup respectively.
        """
        instance_name = instance_name.strip()
        inst = self.instances.get(instance_name)
        if inst:
            inst.reset_conversation()  # PR3: centralized API handles full reset with cache sync
    def add_message(self, instance_name: str, message: Message):
        """Append a message (thread-safe) to an agent's conversation.

        Simple append operation - no version tracking. Token count cache is
        invalidated on each append. The LLM API handles prefix caching automatically.
        
        This is the single point of truth for adding messages — all writes go
        directly to instances[name].conversation.
        """
        instance_name = instance_name.strip()
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
        instance_name = instance_name.strip()
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
            first_role = self._msg_field(conv[0], 'role')
            active_start_idx = 2 if first_role == SYSTEM else 1

        active_set = conv[active_start_idx:]
        return active_start_idx, active_set, latest_marker

    def get_compression_target_set_from_conversation(self, instance_name: str, conv: List[Message]):
        """Like get_compression_target_set but accepts a pre-fetched conversation snapshot.

        Used by compress_context() to avoid stale references when compressor agent
        adds messages to the pool between discard calculation and pool mutation."""
        if not conv:
            return 0, [], -1

        latest_marker = self.find_last_marker(conv)

        if latest_marker >= 0:
            active_start_idx = latest_marker + 1
        else:
            first_role = self._msg_field(conv[0], 'role')
            active_start_idx = 2 if first_role == SYSTEM else 1

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
        marker_indices = [
            i for i in range(len(history))
            if isinstance(self._msg_field(history[i], 'content'), str)
               and self._msg_field(history[i], 'content').startswith(COMPRESSION_MARKER)
        ]

        if not marker_indices:
            return list(history)  # No markers — nothing to slice

        # Determine where content starts (after system message, if present)
        first_role = self._msg_field(history[0], 'role')
        has_system = (first_role == SYSTEM)
        expected_start = 1 if has_system else 0

        # Per design §5.2: stacked form is [SYS][U0][COMP1][COMP2]...
        # First marker can be at index 1 (no U0) or index 2 (U0 present after SYS)
        first_marker_pos = marker_indices[0]
        last_marker_idx = marker_indices[-1]

        # Check if markers are already stacked (consecutive near the start)
        markers_stacked = (
            first_marker_pos <= expected_start + 1
            and last_marker_idx == first_marker_pos + len(marker_indices) - 1
        )

        # If first marker is at index expected_start+1, verify intervening msg is U0 (non-marker user)
        if markers_stacked and first_marker_pos == expected_start + 1:
            intervening = history[expected_start]
            int_role = self._msg_field(intervening, 'role')
            int_content = self._msg_field(intervening, 'content')
            if int_role != 'user' or (isinstance(int_content, str) and int_content.startswith(COMPRESSION_MARKER)):
                markers_stacked = False

        if markers_stacked:
            # Already culled at load time — return a copy
            return list(history)

        # Unculled data still present — apply culling now per design §5.2: [SYS][U0][COMP...][tail]
        tail = list(history[last_marker_idx + 1:])
        marker_msgs = [history[i] for i in marker_indices]

        # Find U0: first non-marker user message before the last marker
        u0 = None
        for msg in history[:last_marker_idx]:
            msg_role = self._msg_field(msg, 'role')
            msg_content = self._msg_field(msg, 'content')
            if msg_role == 'user' and not (isinstance(msg_content, str) and msg_content.startswith(COMPRESSION_MARKER)):
                u0 = msg
                break

        # Build result: [SYS][U0][markers][tail]
        result = []
        if has_system:
            result.append(history[0])
        if u0:
            result.append(u0)
        result.extend(marker_msgs)
        result.extend(tail)

        return result
