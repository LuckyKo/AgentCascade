"""Helper functions for the compression system."""
import copy
from typing import Any, Dict, List
from agent_cascade.prompts.dna import (
    COMPRESSION_BASELINE_TEMPLATE,
    COMPRESSION_NOTICE_TEMPLATE,
)
from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION, ROLE, Message
from agent_cascade.utils.utils import extract_text_from_message


def compute_discard_count(active_set, fraction, force):
    """
    Calculate how many messages to discard from the active set.

    Algorithm:
    1. Start with fraction-based count: int(len(active_set) * fraction)
    2. If not force: keep at least 2 tail messages (clamp discard to len-2)
    3. If force: ensure at least 1 message is discarded

    Args:
        active_set: List of active (uncompressed) messages eligible for compression.
        fraction: Fraction of the active set to discard (0.0 to 1.0).
        force: If True, bypass the "keep 2 tail" guard and compress at least 1 message.

    Returns:
        Number of messages to discard from the active set.
    """
    discard = int(len(active_set) * fraction)
    if not force:
        # Keep at least 2 tail messages for agent continuity
        discard = max(0, min(discard, len(active_set) - 2))
    else:
        # Force mode: compress at least 1 message even from small sets
        discard = max(1, discard)
    return discard


def build_marker_message(summary_text, fraction):
    """
    Wrap a raw summary in the COMPRESSION_BASELINE_TEMPLATE to create a marker message.

    Args:
        summary_text: The raw summary text (before template wrapping).
        fraction: Fraction of history that was discarded (e.g., 0.5 for 50%).

    Returns:
        A Message object (USER role) with the formatted compression marker.
    """
    pct = int(fraction * 100)
    header = f"{pct}% of history summarized"
    compression_notice = COMPRESSION_NOTICE_TEMPLATE.format(fraction=pct)

    content = COMPRESSION_BASELINE_TEMPLATE.format(
        header=header,
        summary=summary_text,
        compression_notice=compression_notice,
    )
    return Message(role=USER, content=str(content))


def rebuild_working_set(
    messages_list: list[Any],
    agent_pool: Any,
    agent_name: str,
) -> None:
    """
    Rebuild a caller's working set from pool state after compression.

    With clean trim, the pool is already compact — we just replace the
    caller's list with a deepcopy of the current pool content.

    Mutates messages_list in-place (caller passes their own list reference).

    Args:
        messages_list: The caller's mutable message list (will be cleared and re-populated).
        agent_pool: The AgentPool instance (single source of truth).
        agent_name: The agent instance name whose conversation to rebuild.
    """
    compressed = agent_pool.get_conversation(agent_name)
    if not compressed:
        return

    # With clean trim, the pool is already sliced — no need for slice_history_for_llm
    messages_list.clear()
    messages_list.extend(copy.deepcopy(compressed))
    # deepcopy ensures callers don't accidentally mutate pool state through their references


def extract_instance_output(messages: List[Dict], instance_name: str) -> str:
    """
    Extracts text output from agent instance messages.
    Only includes text generated AFTER the last tool call ended.
    
    Args:
        messages: List of message dicts or Message objects from the agent's conversation.
        instance_name: Name of the agent instance (used in error/warning messages).
    
    Returns:
        The extracted text output, or a warning/fallback message if no text was found.
    """
    last_tool_idx = -1
    for i, msg in enumerate(messages):
        # Handle both dict and Message object types
        if isinstance(msg, dict):
            role_check = msg.get(ROLE) == FUNCTION or msg.get('function_call')
        else:
            role_check = getattr(msg, 'role', None) == FUNCTION or getattr(msg, 'function_call', None)

        if role_check:
            last_tool_idx = i

    relevant_msgs = messages[last_tool_idx + 1:] if last_tool_idx != -1 else messages

    collected_text = []
    for msg in relevant_msgs:
        if isinstance(msg, dict):
            msg_role = msg.get('role', '')
        else:
            msg_role = getattr(msg, 'role', '')

        if msg_role == ASSISTANT:
            text = extract_text_from_message(msg, add_upload_info=False)
            if text:
                collected_text.append(text)

    result_str = "\n\n".join(collected_text).strip()

    if not result_str:
        if last_tool_idx != -1:
            return f"WARNING: Sub-agent {instance_name} performed tool calls but provided no final summary."
        return f"Sub-agent {instance_name} finished but provided no text output."

    return result_str