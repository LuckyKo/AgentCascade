"""Helper functions for the compression system."""
import copy
from typing import Any
from agent_cascade.prompts.dna import COMPRESSION_BASELINE_TEMPLATE
from agent_cascade.llm.schema import USER, ASSISTANT, FUNCTION, Message
from agent_cascade.utils.utils import extract_text_from_message


def _has_pending_tool_calls(msg) -> bool:
    """
    Check if an assistant message has pending tool/function calls.

    Handles three detection modes:
    1. Legacy mode: function_call attribute on the message
    2. Native OpenAI streaming: tool_index in extra dict (set by oai.py)
    3. Standard OpenAI format: tool_calls array (direct attribute or dict key)
    
    Works with Message objects, dicts, and any object with role attributes.

    Args:
        msg: A Message object, dict, or any object with role attributes.

    Returns:
        True if the message has tool calls that expect FUNCTION responses.
    """
    if msg is None:
        return False

    # Support both dict-style and attribute-style access
    is_dict = isinstance(msg, dict)

    # Legacy mode: check function_call attribute / key
    fc = msg.get('function_call') if is_dict else getattr(msg, 'function_call', None)
    if fc is not None:
        return True

    # Standard OpenAI format: check tool_calls array (direct attribute or dict key)
    tc = msg.get('tool_calls') if is_dict else getattr(msg, 'tool_calls', None)
    if isinstance(tc, list) and len(tc) > 0:
        return True

    # Native OpenAI streaming mode: check for tool_index in extra dict
    # (set by oai.py when parsing streaming responses)
    extra = msg.get('extra') if is_dict else getattr(msg, 'extra', None)
    if isinstance(extra, dict):
        if 'tool_index' in extra:
            return True

    return False


def _get_function_call_ids(msg):
    """
    Extract function_id(s) from an ASSISTANT message's tool call.

    Returns a list of function IDs (strings). Empty list if no tool calls found.
    Supports legacy mode (function_call + extra.function_id), native streaming
    (extra.tool_index with extra.function_id), and standard OpenAI tool_calls array.
    """
    if msg is None:
        return []

    is_dict = isinstance(msg, dict)
    ids = []

    # Standard OpenAI format: check tool_calls array
    tc = msg.get('tool_calls') if is_dict else getattr(msg, 'tool_calls', None)
    if isinstance(tc, list):
        for call in tc:
            cid = call.get('id') if isinstance(call, dict) else getattr(call, 'id', None)
            if cid:
                ids.append(cid)
        return ids

    # Legacy/streaming mode: check extra.function_id or construct from tool_index
    fc = msg.get('function_call') if is_dict else getattr(msg, 'function_call', None)
    if fc is not None:
        extra = msg.get('extra') if is_dict else getattr(msg, 'extra', None)
        if isinstance(extra, dict):
            fid = extra.get('function_id')
            if fid:
                return [fid]
            # Fallback: use tool_index as ID if function_id missing
            tidx = extra.get('tool_index')
            if tidx is not None:
                return [str(tidx)]
        # Synthetic fallback: no extra dict — use function_call name as ID
        fc_name = fc.get('name', 'unknown') if isinstance(fc, dict) else getattr(fc, 'name', 'unknown')
        return [fc_name]

    return []


def _get_function_result_id(msg):
    """
    Extract the function_id from a FUNCTION result message.

    Returns the function ID string, or None if not found.
    The function_id is stored in extra['function_id'] per OpenAI spec.
    """
    if msg is None:
        return None

    is_dict = isinstance(msg, dict)
    extra = msg.get('extra') if is_dict else getattr(msg, 'extra', None)
    if isinstance(extra, dict):
        fid = extra.get('function_id')
        if fid:
            return str(fid)

    # Fallback: check name attribute (some messages use name as ID)
    name = msg.get('name') if is_dict else getattr(msg, 'name', None)
    return name


def _count_tool_responses(msg) -> int:
    """
    Count how many FUNCTION response messages follow an ASSISTANT's tool calls.

    For legacy mode (single function_call), returns 1.
    For native tool_calls array, returns len(tool_calls).
    For streaming mode (tool_index in extra), returns 1 per call.
    Returns 0 if the message has no tool calls.

    Args:
        msg: A Message object, dict, or any object with role attributes.

    Returns:
        Number of FUNCTION response messages expected for this ASSISTANT's tool calls.
    """
    if msg is None:
        return 0

    is_dict = isinstance(msg, dict)

    # Standard OpenAI format: check tool_calls array (direct attribute or dict key)
    tc = msg.get('tool_calls') if is_dict else getattr(msg, 'tool_calls', None)
    if isinstance(tc, list) and len(tc) > 0:
        return len(tc)

    # Legacy mode: single function_call → exactly 1 FUNCTION response
    fc = msg.get('function_call') if is_dict else getattr(msg, 'function_call', None)
    if fc is not None:
        return 1

    # Native OpenAI streaming mode: tool_index in extra dict (set by oai.py)
    extra = msg.get('extra') if is_dict else getattr(msg, 'extra', None)
    if isinstance(extra, dict):
        if 'tool_index' in extra:
            return 1

def compute_discard_count(active_set, fraction, force):
    """
    Calculate how many messages to discard from the active set.

    Algorithm:
    1. Start with fraction-based count: int(len(active_set) * fraction)
    2. If not force: keep at least 2 tail messages (clamp discard to len-2)
    3. If force: ensure at least 1 message is discarded AND keep at least 2 tail messages
    4. Refine: scan forward from cut point to avoid splitting ASSISTANT→FUNCTION pairs

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
        # Force mode: compress at least 1 but keep at least 2 tail messages to prevent over-compression
        discard = max(1, min(discard, len(active_set) - 2))

    # ── Refine: avoid splitting ASSISTANT(tool_call) → FUNCTION(result) pairs ──
    # Scan forward from the cut point. For each ASSISTANT with tool calls, collect
    # its function_id(s). Then skip ahead until all matching FUNCTION responses found.
    # This handles ANY pattern: [A,F,A,F], [A,A,A,F,F,F], or mixed interleaved/batched.
    max_discard = max(0, len(active_set) - 2 if not force else len(active_set) - 1)

    pending_ids = []
    while discard < max_discard:
        msg = active_set[discard]
        role = msg.get('role', '') if isinstance(msg, dict) else getattr(msg, 'role', '')

        # Collect function_id from ASSISTANT messages with tool calls
        fc_ids = _get_function_call_ids(msg)
        if fc_ids:
            pending_ids.extend(fc_ids)
            discard += 1
            # Advance past matching FUNCTION responses for these tool calls
            while len(pending_ids) > 0 and discard < max_discard:
                fn_msg = active_set[discard]
                fn_role = fn_msg.get('role', '') if isinstance(fn_msg, dict) else getattr(fn_msg, 'role', '')
                # Also collect IDs from any new ASSISTANTs we encounter (batched pattern [A,A,F,F])
                more_ids = _get_function_call_ids(fn_msg)
                if more_ids:
                    pending_ids.extend(more_ids)
                    discard += 1
                elif fn_role == FUNCTION:
                    fn_id = _get_function_result_id(fn_msg)
                    if fn_id and fn_id in pending_ids:
                        pending_ids.remove(fn_id)
                        discard += 1
                    else:
                        break  # Orphaned FUNCTION result — stop scanning
                else:
                    break  # Non-FUNCTION, non-ASSISTANT message — stop scanning
        else:
            break

    return min(discard, max_discard)


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

    Returns the content of the last message in the conversation, which represents
    the agent's final output for the current invocation.

    Args:
        messages: List of Message objects or dicts (mixed types).
        instance_name: The agent instance name (used in fallback warnings).
        was_terminated: If True, the agent was terminated by user — return a termination message instead of generic warning.

    Returns:
        The extracted text, or a warning message if no output was found.
    """

    if not messages:
        if was_terminated:
            return f"Sub-agent {instance_name} was terminated by user."
        return f"Sub-agent {instance_name} finished but provided no text output."

    # Get the last message in the conversation
    last_msg = messages[-1]

    if isinstance(last_msg, dict):
        msg_role = last_msg.get('role', '')
    else:
        msg_role = getattr(last_msg, 'role', '')

    # Guard: if the last message is a tool result (function role), the agent
    # likely terminated incorrectly without producing a final text response.
    if msg_role == FUNCTION:
        return (f"WARNING: Sub-agent {instance_name} terminated with a tool result "
                f"(no final text output). Check log for details: "
                f"{instance_name}.log")

    result_str = extract_text_from_message(last_msg, add_upload_info=False).strip()

    if not result_str:
        if was_terminated:
            return (f"Sub-agent {instance_name} was terminated by user. "
                    f"Check log for details: {instance_name}.log")
        return f"WARNING: Sub-agent {instance_name} produced no text output in its final message (role={msg_role})."

    return result_str


# ── Message Pool Validation (Phase 2 Task M3) ────────────────────────────────────
# Note: validate_message_pool has been moved to utils/pool_validation.py to avoid circular imports.
# This import provides backward compatibility for any code still importing from here.
from agent_cascade.utils.pool_validation import validate_message_pool  # noqa: F401