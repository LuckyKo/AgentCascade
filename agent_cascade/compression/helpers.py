"""Helper functions for the compression system."""
import copy
from typing import Any
from agent_cascade.prompts.dna import COMPRESSION_BASELINE_TEMPLATE
from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION, Message
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

    content = COMPRESSION_BASELINE_TEMPLATE.format(
        header=header,
        summary=summary_text,
    )
    return Message(role=USER, content=str(content))


def rebuild_working_set(
    messages_list: list[Any],
    agent_pool: Any,
    agent_name: str,
) -> None:
    """
    Rebuild a caller's working set from pool state after compression.

    Optimized rebuild with cache invalidation support.

    With clean trim, the pool is already compact — we just replace the
    caller's list with a deepcopy of the current pool content.
    
    Cache Invalidation:
    - Clears token count cache in AgentInstance if accessible
    - Ensures fresh preprocessing on next LLM call
    
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
    
    # Invalidate token count cache in AgentInstance (cache invalidation)
    try:
        inst = agent_pool.get_instance(agent_name)
        if inst and hasattr(inst, '_cached_token_count'):
            inst._cached_token_count = 0
            inst._last_token_count_conversation_length = -1
    except Exception:
        # Defensive: cache invalidation is optimization, not critical path
        pass


def extract_instance_output(messages: list[Any], instance_name: str, was_terminated: bool = False) -> str:
    """
    Extract text output from a sub-agent's conversation messages.

    Only includes text generated AFTER the last tool call ended — i.e., the
    agent's final summary/response that should be returned to the caller.

    Args:
        messages: List of Message objects or dicts (mixed types).
        instance_name: The agent instance name (used in fallback warnings).
        was_terminated: If True, the agent was terminated by user — return a termination message instead of generic warning.

    Returns:
        The extracted text, or a warning message if no output was found.
    """

    # Find the last tool-call (function) message index
    last_tool_idx = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            role_check = msg.get('role') == FUNCTION or msg.get('function_call')
        else:
            role_check = getattr(msg, 'role', None) == FUNCTION or getattr(msg, 'function_call', None)

        if role_check:
            last_tool_idx = i

    # Only look at messages after the last tool call
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
        if was_terminated:
            return f"Sub-agent {instance_name} was terminated by user."
        if last_tool_idx != -1:
            return f"WARNING: Sub-agent {instance_name} performed tool calls but provided no final summary."
        return f"Sub-agent {instance_name} finished but provided no text output."

    return result_str


# ── Message Pool Validation (Phase 2 Task M3) ────────────────────────────────────
# Note: validate_message_pool has been moved to utils/pool_validation.py to avoid circular imports.
# This import provides backward compatibility for any code still importing from here.
from agent_cascade.utils.pool_validation import validate_message_pool  # noqa: F401