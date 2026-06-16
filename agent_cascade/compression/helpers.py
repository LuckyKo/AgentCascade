"""Helper functions for the compression system."""
import copy
from typing import Any
from agent_cascade.prompts.dna import (
    COMPRESSION_BASELINE_TEMPLATE,
    COMPRESSION_NOTICE_TEMPLATE,
)
from agent_cascade.llm.schema import USER, Message


def get_role(msg) -> str:
    """
    Safely extract the role from a message, handling both dict and Message objects.

    This is the single source of truth for role extraction across the compression system.

    Args:
        msg: A message (dict or Message object).

    Returns:
        The role string (e.g., 'system', 'user', 'assistant'), or empty string if not found.
    """
    if isinstance(msg, dict):
        return msg.get('role', '')
    else:
        return getattr(msg, 'role', '')


def get_content(msg) -> str:
    """
    Safely extract the content from a message, handling both dict and Message objects.

    Args:
        msg: A message (dict or Message object).

    Returns:
        The content string, or empty string if not found.
    """
    if isinstance(msg, dict):
        return msg.get('content', '')
    else:
        return getattr(msg, 'content', '')


def count_active_tokens(active_set) -> int:
    """
    Count total tokens in an active message set.

    This extracts the inline token counting logic from compress_context()
    to make it reusable and testable.

    Args:
        active_set: List of messages (dicts or Message objects).

    Returns:
        Total token count across all messages, or 0 if counting fails.
    """
    try:
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        from agent_cascade.utils.utils import extract_text_from_message

        total_tokens = 0
        for msg in active_set:
            if isinstance(msg, dict):
                wrapped = Message(**msg)
            else:
                wrapped = msg
            content = extract_text_from_message(wrapped, add_upload_info=True)
            total_tokens += qwen_count(content)
        return total_tokens
    except Exception:
        # Token counting is advisory — if it fails, return 0
        return 0


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

    # FIX 2: Use slice_history_for_llm to extract the proper working set from the pool.
    # The pool stores the full post-compression history (system + prefix + marker + tail),
    # but callers need only the system message + messages from the latest marker onward.
    # slice_history_for_llm extracts this sliced view while preserving the system message.
    sliced = agent_pool.slice_history_for_llm(compressed)
    messages_list.clear()
    if sliced:
        messages_list.extend(copy.deepcopy(sliced))
    # deepcopy ensures callers don't accidentally mutate pool state through their references