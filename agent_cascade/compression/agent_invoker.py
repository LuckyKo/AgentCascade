"""Compression Agent invocation wrapper.

Routes LLM calls through the API router (api_router.call_with_fallback) for
multi-endpoint failover, concurrency enforcement, and token limit adherence.
Also acquires endpoint scheduling slots via _acquire_slot() and protects shared
state writes with the execution state lock.

Called both from the orchestrator context and from the API server forced compression path.
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
):
    """
    Invoke the Compression Agent to generate a summary of target messages.

    Routes LLM calls through api_router.call_with_fallback() for multi-endpoint
    failover, concurrency enforcement, and token limit adherence (Fix #1).
    Acquires endpoint scheduling slot via _acquire_slot() before execution (Fix #3).
    Protects shared state writes with _state_lock (Fix #4).

    Called both from the orchestrator context and from the API server forced compression path.

    Args:
        agent_pool: The AgentPool instance (provides agent loading and state management).
        target_messages: List of messages to summarize.
        existing_summary: Optional previous summary text to compound onto.

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
    endpoint_release = None  # Fix #3: endpoint slot release callback

    try:
        # ── Fix #3: Acquire endpoint scheduling slot before execution ──
        if hasattr(agent_pool, '_execution') and hasattr(agent_pool._execution, '_acquire_slot'):
            try:
                endpoint_release = agent_pool._execution._acquire_slot('compression_agent', comp_state_key)
            except Exception as e:
                logger.warning(f"Failed to acquire endpoint slot for compression_agent: {e}")

        # ── Fix #4: Thread-safe state initialization via _state_lock ──
        with agent_pool._execution._state_lock:
            agent_pool.instance_state[comp_state_key] = {
                'active': True,
                'agent_name': f"Compression Agent (compression_agent)",
                'messages': list(comp_history),
            }
            if not any(n == comp_state_key for n, _depth in agent_pool._execution.active_stack):
                agent_pool._execution.active_stack.append((comp_state_key, 1))
            # Phase 3: Write to instance.conversation if real instance exists, otherwise skip
            inst = agent_pool.get_instance(comp_state_key)
            if inst:
                with inst._compression_lock:
                    inst.conversation = list(comp_history)
                    # Invalidate token count cache — conversation was replaced (Fix #2)
                    inst._last_token_count_conversation_length = -1

        # ── Fix #1: Route LLM call through API router instead of direct comp_agent.run() ──
        logger.info("Compression agent invoked via API router (call_with_fallback)")

        api_router = getattr(agent_pool, 'api_router', None)

        if api_router and hasattr(api_router, 'call_with_fallback'):
            # Build the LLM call function that mirrors what comp_agent._call_llm() does
            def _compression_llm_call(llm_cfg: dict):
                """Execute the compression agent's LLM call with the given config."""
                # Merge configs in same order as Agent._call_llm(): extra_generate_cfg → generate_cfg → router config
                merged_cfg = {}
                if hasattr(comp_agent, 'extra_generate_cfg') and comp_agent.extra_generate_cfg:
                    merged_cfg.update(comp_agent.extra_generate_cfg)
                if hasattr(comp_agent.llm, 'generate_cfg'):
                    merged_cfg.update(comp_agent.llm.generate_cfg)
                merged_cfg.update(llm_cfg)
                merged_cfg['agent_name'] = comp_agent.name

                return comp_agent.llm.chat(
                    messages=comp_history,
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=merged_cfg,
                )

            # call_with_fallback handles retries, per-endpoint concurrency semaphores, and failover
            output_stream = api_router.call_with_fallback('compression_agent', _compression_llm_call)
        else:
            # Fallback: direct LLM call if no router available (preserves old behavior)
            logger.warning("API router unavailable — using direct LLM call for compression agent")
            fallback_cfg = {}
            if hasattr(comp_agent, 'extra_generate_cfg') and comp_agent.extra_generate_cfg:
                fallback_cfg.update(comp_agent.extra_generate_cfg)
            if hasattr(comp_agent.llm, 'generate_cfg'):
                fallback_cfg.update(comp_agent.llm.generate_cfg)
            fallback_cfg['agent_name'] = comp_agent.name
            output_stream = comp_agent.llm.chat(
                messages=comp_history,
                stream=True,
                delta_stream=False,
                extra_generate_cfg=fallback_cfg,
            )

        final_msgs = []
        start_time = _time.monotonic()   # Monotonic clock for timeout
        max_poll_time = 300            # 5-minute timeout for large compression tasks
        poll_count = 0

        for partial in output_stream:
            final_msgs = partial
            poll_count += 1

            # Time-based check — adapts to any streaming chunk rate
            elapsed = _time.monotonic() - start_time
            if elapsed > max_poll_time:
                raise RuntimeError(
                    f"Compression agent timed out after {elapsed:.0f}s "
                    f"({poll_count} iterations)"
                )

            # Fix #4: Update instance_state during streaming with lock protection
            with agent_pool._execution._state_lock:
                agent_pool.instance_state[comp_state_key]['messages'] = (
                    list(comp_history) + list(final_msgs) if isinstance(final_msgs, list) else list(comp_history) + [final_msgs]
                )
                # Phase 3: Write to instance.conversation if real instance exists, otherwise skip
                inst = agent_pool.get_instance(comp_state_key)
                if inst:
                    with inst._compression_lock:
                        inst.conversation = list(final_msgs) if isinstance(final_msgs, list) else [final_msgs]
                        # Invalidate token count cache — conversation was replaced (Fix #2)
                        inst._last_token_count_conversation_length = -1

        # Normalize final_msgs to a list for downstream consumers (after the loop ends)
        if not isinstance(final_msgs, list):
            final_msgs = [final_msgs] if final_msgs else []

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
        # Always clean up compression agent state when done (Fix #4: thread-safe)
        with agent_pool._execution._state_lock:
            if comp_state_key in agent_pool.instance_state:
                agent_pool.instance_state[comp_state_key]['active'] = False
            if any(n == comp_state_key for n, _depth in agent_pool._execution.active_stack):
                for i, (n, _depth) in enumerate(agent_pool._execution.active_stack):
                    if n == comp_state_key:
                        agent_pool._execution.active_stack.pop(i)
                        break

        # Fix #3: Release endpoint slot when done
        if endpoint_release is not None:
            try:
                endpoint_release()
            except Exception as e:
                logger.warning(f"Failed to release endpoint slot for compression_agent: {e}")