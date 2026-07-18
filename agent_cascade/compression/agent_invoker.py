"""Compression Agent invocation wrapper.

Uses the call_agent pattern (via _stream_sub_agent_call) when an orchestrator
reference is available, providing session tracking, WebUI visibility, and
consistent error handling. Falls back to direct comp_agent.run() when called
outside the orchestrator context (e.g., from API server forced compression).
"""
import logging
import time as _time
from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.llm.schema import SYSTEM, USER
from agent_cascade.utils.thinking_block import strip_thinking_blocks
from agent_cascade.utils.utils import extract_text_from_message
from agent_cascade.compression.helpers import get_role
from agent_cascade.compression.constants import (
    COMPRESSION_AGENT_TIMEOUT,
    SUMMARY_PREFIXES_TO_STRIP,
)

logger = logging.getLogger(__name__)


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
    orchestrator=None,   # Optional: the orchestrator instance (for call_agent pattern)
):
    """
    Invoke the Compression Agent to generate a summary of target messages.

    When an orchestrator is provided and has _stream_sub_agent_call, uses the
    call_agent pattern for session tracking, WebUI visibility, and consistent
    error handling. When no orchestrator is available (e.g., forced compression
    from API server), falls back to direct agent.run().

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.
        orchestrator: Optional orchestrator instance for call_agent pattern.

    Returns:
        The raw summary string (with thinking blocks stripped).

    Raises:
        RuntimeError: If the compression agent fails or returns an empty summary.
    """
    if not agent_pool:
        raise RuntimeError("agent_pool not connected")

    # 1. Ensure the compression agent is loaded
    # Agent type identifier: 'Compressor' (renamed from 'compression_agent')
    if not agent_pool.get_agent('Compressor'):
        try:
            agent_pool.load_agent('Compressor')
        except Exception as e:
            raise RuntimeError(f"Could not load Compressor: {e}") from e

    comp_agent = agent_pool.get_agent('Compressor')
    if not comp_agent:
        raise RuntimeError("Compressor is None after loading")

    comp_state_key = 'Compressor'

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
    # FIX-3: Initialize subagent_return_value BEFORE the if/else split so it's always defined
    # This prevents UnboundLocalError if an exception occurs in the else branch and
    # the code at line 244 references this variable
    subagent_return_value = None
    try:
        if orchestrator is not None and hasattr(orchestrator, '_stream_sub_agent_call'):
            # ── call_agent pattern via _stream_sub_agent_call ──
            # Build tool_args matching what call_agent expects
            tool_args = {
                'instance_name': comp_state_key,
                'agent_class': 'Compressor',
                'task': summary_prompt,
                'context': None,
            }
            # Use the orchestrator's _stream_sub_agent_call for full lifecycle management.
            # Pass an empty current_response and manager_history since compression is a
            # self-contained LLM call without tool use or async message injection needs.
            current_response = []
            manager_history = comp_history

            final_msgs = []
            # subagent_return_value already initialized above (FIX-3)
            start_time = _time.monotonic()   # Monotonic clock — immune to NTP adjustments
            max_poll_time = COMPRESSION_AGENT_TIMEOUT
            poll_count = 0

            try:
                gen = orchestrator._stream_sub_agent_call(
                    'call_agent', tool_args, current_response, manager_history
                )
                # Iterate the generator synchronously (same pattern as yield from).
                # Each LLM streaming chunk produces one yield through the chain:
                #   _original_call_llm → hooked_call_llm → _run → run() → _stream_sub_agent_call
                # For large compression tasks (60K+ tokens of input), 1000+ chunks are common.
                # We use a time-based timeout instead of iteration count to handle any task size.
                while True:
                    yielded = next(gen)
                    poll_count += 1

                    # Time-based check — adapts to any streaming chunk rate
                    elapsed = _time.monotonic() - start_time
                    if elapsed > max_poll_time:
                        raise RuntimeError(
                            f"Compression agent timed out after {elapsed:.0f}s "
                            f"({poll_count} iterations) — further processing may have been incomplete"
                        )

                    # The generator yields intermediate state for WebUI — capture final messages
                    if comp_state_key in agent_pool.sub_agent_state:
                        msgs = agent_pool.sub_agent_state[comp_state_key].get('messages', [])
                        if msgs:
                            final_msgs = list(msgs)
            except StopIteration as e:
                # Normal termination of the generator — capture return value as fallback
                if hasattr(e, 'value') and e.value is not None:
                    subagent_return_value = e.value

        else:
            # ── Fallback: direct comp_agent.run() when no orchestrator or method available ──
            logger.info(
                "Compression agent invoked via direct run() — "
                "no orchestrator reference for call_agent pattern"
            )

            # Initialize sub-agent state for WebUI visibility (direct path)
            agent_pool.sub_agent_state[comp_state_key] = {
                'active': True,
                'agent_name': f"Compressor",
                'messages': list(comp_history),
            }
            if comp_state_key not in agent_pool.active_stack:
                agent_pool.active_stack.append(comp_state_key)
            agent_pool.instance_conversations[comp_state_key] = list(comp_history)

            final_msgs = []
            start_time = _time.monotonic()   # Monotonic clock for timeout (same as call_agent path)
            max_poll_time = COMPRESSION_AGENT_TIMEOUT
            poll_count = 0

            for partial in comp_agent.run(comp_history, agent_instance_name=comp_state_key):
                final_msgs = partial
                # Note: poll_count here counts run() yields (~1 per LLM turn), not token chunks
                poll_count += 1

                # Time-based check — adapts to any streaming chunk rate (same as call_agent path)
                elapsed = _time.monotonic() - start_time
                if elapsed > max_poll_time:
                    raise RuntimeError(
                        f"Compression agent timed out after {elapsed:.0f}s "
                        f"({poll_count} iterations in direct run path)"
                    )

                # Update sub_agent_state during streaming so WebUI reflects progress
                agent_pool.sub_agent_state[comp_state_key]['messages'] = (
                    list(comp_history) + (list(final_msgs) if isinstance(final_msgs, list) else [final_msgs])
                )
                agent_pool.instance_conversations[comp_state_key] = list(
                    agent_pool.sub_agent_state[comp_state_key]['messages']
                )

        # 2. Extract the summary from the last assistant message
        if final_msgs:
            for msg_obj in reversed(final_msgs):
                role = get_role(msg_obj)
                if role == 'assistant':
                    content = extract_text_from_message(msg_obj, add_upload_info=False)
                    summary = strip_thinking_blocks(content)
                    break

            # Strip conversational filler prefixes
            lower_summary = summary.lower()
            for prefix in SUMMARY_PREFIXES_TO_STRIP:
                if lower_summary.startswith(prefix):
                    summary = summary[len(prefix):].strip()
                    summary = summary.lstrip(':\n \t')
                    lower_summary = summary.lower()

        # Fallback: if final_msgs was empty but we got a return value from the generator,
        # try to extract the summary from that string. This handles edge cases where the
        # sub_agent_state wasn't populated during streaming.
        if not summary.strip() and subagent_return_value:
            if isinstance(subagent_return_value, str):
                logger.info("Using subagent return value as fallback for summary extraction")
                rv = subagent_return_value
                # Strip the "[instance_name's output]:\n" wrapper added by _stream_sub_agent_call
                if ']:' in rv and '\n' in rv:
                    rv = rv.split(']:\n', 1)[1]
                summary = strip_thinking_blocks(rv)

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
        if comp_state_key in agent_pool.sub_agent_state:
            agent_pool.sub_agent_state[comp_state_key]['active'] = False
            if comp_state_key in agent_pool.active_stack:
                agent_pool.active_stack.remove(comp_state_key)