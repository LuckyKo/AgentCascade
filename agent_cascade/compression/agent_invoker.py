"""Compression Agent invocation wrapper.

Uses direct comp_agent.run() for session tracking, WebUI visibility, and
consistent error handling. Called both from the orchestrator context and
from the API server forced compression path.
"""
import logging
import time as _time
from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message

logger = logging.getLogger(__name__)

# Conversational filler prefixes to strip from summaries
_SUMMARY_PREFIXES = [
    "here is a summary", "here is the summary", "summary:",
    "in summary,", "here's a summary", "**summary**:",
]


def _format_messages_for_summary(target_messages):
    """
    Format a list of messages into plain text for the compression prompt.

    Handles both dict and Message objects, including multi-modal content lists.

    Args:
        target_messages: List of messages (dicts or Message objects) to format.

    Returns:
        A single string with role-prefixed message contents.
    """
    history_text = ""
    for msg in target_messages:
        if isinstance(msg, dict):
            role = msg.get('role', 'unknown').upper()
            content = msg.get('content', '')
        else:
            role = getattr(msg, 'role', 'unknown').upper()
            content = getattr(msg, 'content', '')

        # Handle multi-modal content (list of items)
        # Only include text parts; skip images and other non-text items to avoid "None" strings
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text', '') or ''
                    if text:
                        text_parts.append(str(text))
                else:
                    text = getattr(item, 'text', None)
                    if text:
                        text_parts.append(str(text))
            content = " ".join(text_parts)
        history_text += f"{role}: {content}\n\n"
    return history_text


def invoke_compression_agent(
    agent_pool,
    target_messages,
    existing_summary=None,
    orchestrator=None,   # Unused — retained for backward compatibility
):
    """
    Invoke the Compression Agent to generate a summary of target messages.

    Uses direct comp_agent.run() for session tracking, WebUI visibility, and
    consistent error handling. Called both from the orchestrator context and
    from the API server forced compression path.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.
        orchestrator: Unused — retained for backward compatibility.

    Returns:
        The raw summary string (with thinking blocks stripped).

    Raises:
        RuntimeError: If the compression agent fails or returns an empty summary.
    """
    if not agent_pool:
        raise RuntimeError("agent_pool not connected")

    # 1. Ensure the compression agent is loaded
    if not agent_pool.get_agent('compression_agent'):
        try:
            agent_pool.load_agent('compression_agent')
        except Exception as e:
            raise RuntimeError(f"Could not load compression_agent: {e}") from e

    comp_agent = agent_pool.get_agent('compression_agent')
    if not comp_agent:
        raise RuntimeError("compression_agent is None after loading")

    comp_state_key = 'compression_agent'

    # Build the history text for the summary prompt
    history_text = _format_messages_for_summary(target_messages)

    # If there's an existing summary, prepend it as context
    if existing_summary:
        history_text = (
            f"EXISTING SUMMARY:\n{existing_summary}\n\n"
            f"NEW CONVERSATION TO SUMMARIZE:\n{history_text}"
        )

    summary_prompt = COMPRESSION_PROMPT.format(history_text=history_text)
    comp_history = [
        {'role': SYSTEM, 'content': (
            'You are a context compression specialist. Your job is to summarize older '
            'conversation history to free up context space while preserving key '
            'information like decisions, facts, task state, and progress.'
        )},
        {'role': USER, 'content': summary_prompt},
    ]

    summary = ""
    try:
        # ── Direct comp_agent.run() path (always) ──
        logger.info(
            "Compression agent invoked via direct run()"
        )

        # Initialize agent instance state for WebUI visibility
        agent_pool.instance_state[comp_state_key] = {
            'active': True,
            'agent_name': f"Compression Agent (compression_agent)",
            'messages': list(comp_history),
        }
        if comp_state_key not in agent_pool.active_stack:
            agent_pool.active_stack_append(comp_state_key)
        agent_pool.instance_conversations[comp_state_key] = list(comp_history)

        final_msgs = []
        start_time = _time.monotonic()   # Monotonic clock for timeout
        max_poll_time = 300            # 5-minute timeout for large compression tasks
        poll_count = 0

        for partial in comp_agent.run(comp_history, agent_instance_name=comp_state_key):
            final_msgs = partial
            # Note: poll_count here counts run() yields (~1 per LLM turn), not token chunks
            poll_count += 1

            # Time-based check — adapts to any streaming chunk rate
            elapsed = _time.monotonic() - start_time
            if elapsed > max_poll_time:
                raise RuntimeError(
                    f"Compression agent timed out after {elapsed:.0f}s "
                    f"({poll_count} iterations in direct run path)"
                )

            # Update instance_state during streaming so WebUI reflects progress
            agent_pool.instance_state[comp_state_key]['messages'] = (
                list(comp_history) + (list(final_msgs) if isinstance(final_msgs, list) else [final_msgs])
            )
            agent_pool.instance_conversations[comp_state_key] = list(
                agent_pool.instance_state[comp_state_key]['messages']
            )

        # 2. Extract the summary from the last assistant message
        if final_msgs:
            for msg_obj in reversed(final_msgs):
                role = (msg_obj.get('role', '') if isinstance(msg_obj, dict)
                        else getattr(msg_obj, 'role', ''))
                if role == 'assistant':
                    content = extract_text_from_message(msg_obj, add_upload_info=False)
                    summary = strip_thinking_blocks(content)
                    break

            # Strip conversational filler prefixes
            lower_summary = summary.lower()
            for prefix in _SUMMARY_PREFIXES:
                if lower_summary.startswith(prefix):
                    summary = summary[len(prefix):].strip()
                    summary = summary.lstrip(':\n \t')
                    lower_summary = summary.lower()

        # Validate we got a usable summary
        if not summary.strip():
            raise RuntimeError("Compression Agent returned an empty summary")

        return summary.strip()

    except RuntimeError:
        # Re-raise our own errors as-is
        raise
    except Exception as e:
        raise RuntimeError(f"Exception occurred while generating summary: {e}") from e
    finally:
        # Always clean up compression agent state when done
        if comp_state_key in agent_pool.instance_state:
            agent_pool.instance_state[comp_state_key]['active'] = False
            if comp_state_key in agent_pool.active_stack:
                agent_pool.active_stack_remove(comp_state_key)