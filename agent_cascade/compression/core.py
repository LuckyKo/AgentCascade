"""Unified compress_context() function — the single entry point for all compression."""
import logging
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.helpers import (
    compute_discard_count,
    build_marker_message,
)
from agent_cascade.llm.schema import FUNCTION
from agent_cascade.compression.agent_invoker import invoke_compression_agent
from agent_cascade.utils.utils import extract_text_from_message

logger = logging.getLogger(__name__)


def compress_context(
    agent_pool,
    target_agent_name: str,        # Which agent's context to compress
    fraction: float = 0.5,         # Fraction of active history to discard
    mode: str = "auto",            # "auto" (LLM generates) or "manual" (summary provided)
    summary_text: str | None = None,  # Required when mode == "manual"
    force: bool = False,           # Bypass validation guards (forced compression at >95%)
    dry_run: bool = False,         # If True, generate summary but don't mutate pool
    precomputed_summary: str | None = None,  # Pre-generated summary to skip LLM call in auto mode
) -> CompressResult:
    """
    Unified compression function. Handles ALL compression triggers:

    - Forced (>95% context usage): orchestrator calls with force=True
    - Agent-triggered (agent calls compress_context tool): normal mode
    - Manual (user provides summary text): mode="manual" with summary_text

    Synchronous — uses engine.run() to invoke the Compression Agent.

    Fail-safe: if compression fails at any point, pool is untouched.

    Args:
        agent_pool: The AgentPool instance (single source of truth).
        target_agent_name: The agent instance name whose context to compress.
        fraction: Fraction of active history to discard (0.3 to 1.0).
        mode: "auto" for LLM-generated summary, "manual" for provided summary.
        summary_text: Required when mode == "manual". Raw summary text.
        force: If True, bypass the "not enough messages" guard.
        dry_run: If True, generate summary but don't mutate pool (for /compress command).
        precomputed_summary: Pre-generated summary to skip LLM call in auto mode.

    Returns:
        CompressResult with success status, summary text, and metadata.
    """
    # ── 0. Validate fraction range ──
    if not 0.0 <= fraction <= 1.0:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="fraction must be between 0.0 and 1.0",
            mode=mode,
        )

    # ── 0. Validate manual mode has summary_text or precomputed_summary (before any other checks) ──
    if mode == "manual" and not summary_text and not precomputed_summary:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="Manual mode requires summary_text or precomputed_summary",
            mode=mode,
        )

    # ── 1. Get active set from pool ──
    active_start_idx, active_set, latest_summary_idx = (
        agent_pool.get_compression_target_set(target_agent_name)
    )

    if not active_set:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="No active messages to compress",
            mode=mode,
        )

    # ── 2. Guard: Already optimally compressed (<3 messages AND <200 tokens) ──
    try:
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        from agent_cascade.llm.schema import Message

        total_tokens = 0
        for msg in active_set:
            if isinstance(msg, dict):
                wrapped = Message(**msg)
            else:
                wrapped = msg
            content = extract_text_from_message(wrapped, add_upload_info=True)
            tokens = qwen_count(content)
            total_tokens += tokens
    except Exception:
        # Token counting is advisory — if it fails, skip the token guard
        total_tokens = 0

    # ── 3. Calculate discard count ──
    target_discard_count = compute_discard_count(active_set, fraction, force)

    # Check for error signal: -1 means tool chains extend past max_discard with no clean split
    if target_discard_count == -1:
        return CompressResult(
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=len(active_set),
            error="Compression not possible at this ratio — tool-call chains extend past the keep zone",
            mode=mode,
        )

    # ── 3b. Cap discard count so compression agent can actually process the messages ──
    # If the compression agent has a known context window, don't feed it more than it can handle.
    # Estimate ~500 tokens per message; reserve 60% of the agent's context for input (40% for system prompt,
    # existing summary, and output generation).
    try:
        comp_agent = agent_pool.get_agent('Compressor')
        if comp_agent:
            max_tokens = None
            if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
            elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                max_tokens = comp_agent.llm.cfg.get('max_input_tokens')

            if max_tokens:
                # Reserve ~90% of compression agent's context for input messages (10% for summary output)
                available_for_messages = int(max_tokens * 0.9)
                estimated_tokens_per_message = 500
                max_discardable = available_for_messages // estimated_tokens_per_message
                target_discard_count = min(target_discard_count, max_discardable)
    except Exception:
        pass  # If we can't determine the limit, proceed with original count

    # ── 4a. Guard: Active set too small for safe compression (any mode) ──
    # Always keep at least 3 messages in active set so compression leaves ≥2 tail messages.
    if len(active_set) < 3:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=len(active_set),
            error=f"Active set too small ({len(active_set)} messages) for safe compression. "
                  f"Need at least 3 to preserve ≥2 tail messages.",
            mode=mode,
        )

    # ── 4b. Guard: Not enough to compress (unless force=True) ──
    # Combines the "already optimally compressed" check with the "not enough to discard" check.
    # If fewer than 3 messages AND under 200 tokens, OR if nothing to discard — defer.
    if not force:
        if (len(active_set) < 3 and total_tokens < 200) or target_discard_count <= 0:
            return CompressResult(
                success=False,
                summary_text=None,
                marker_message=None,
                messages_discarded=0,
                tail_count=len(active_set),
                error="Not enough messages to compress; deferring until more accumulate",
                mode=mode,
            )

    # ── 5. Force mode guard: if discard count is 0 in force mode, fail gracefully ──
    if force and target_discard_count < 1:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=len(active_set),
            error="Force mode but compute_discard_count returned 0 — unexpected pool state",
            mode=mode,
        )

    # ── 6. Determine messages to send to the Compression Agent ──
    if latest_summary_idx != -1:
        # Include the latest summary marker + the new active messages being discarded
        history = agent_pool.get_conversation(target_agent_name)
        target_messages = history[
            latest_summary_idx : latest_summary_idx + 1 + target_discard_count
        ]
    else:
        # First compression: just send the messages we are about to compress
        target_messages = active_set[:target_discard_count]

    # ── 7. Get existing summary text from pool for compounding ──
    existing_summary = None
    if latest_summary_idx != -1:
        history = agent_pool.get_conversation(target_agent_name)
        summary_msg = history[latest_summary_idx]
        
        # Use extract_text_from_message to handle both string and multi-modal list content
        from agent_cascade.llm.schema import Message
        
        if isinstance(summary_msg, dict):
            wrapped_msg = Message(**summary_msg)
        else:
            wrapped_msg = summary_msg
        
        raw_content = extract_text_from_message(wrapped_msg, add_upload_info=True)
        
        # Extract the summary text between <context_summary> tags
        if '<context_summary>' in raw_content:
            try:
                existing_summary = raw_content.split('<context_summary>')[1].split('</context_summary>')[0].strip()
            except (IndexError, AttributeError):
                pass

    # ── 8. Generate or obtain summary ──
    if precomputed_summary:
        # Use a pre-generated summary (e.g., from /compress command after user approval)
        generated_summary = precomputed_summary.strip()
    elif mode == "manual":
        generated_summary = summary_text.strip()
    else:
        try:
            generated_summary = invoke_compression_agent(
                agent_pool=agent_pool,
                target_messages=target_messages,
                existing_summary=existing_summary,
                caller_name=target_agent_name,  # Pass actual instance name for slot management
            )
        except Exception as e:
            # Fail-safe: Compression Agent failed — pool is untouched
            logger.error(f"Compression Agent invocation failed: {e}")
            return CompressResult(
                success=False,
                summary_text=None,
                marker_message=None,
                messages_discarded=0,
                tail_count=0,
                error=f"Compression Agent failed: {e}",
                mode=mode,
            )

    # Validate we have a usable summary
    if not generated_summary:
        return CompressResult(
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="Failed to obtain a valid summary",
            mode=mode,
        )

    # ── 9. Build the marker message ──
    marker_message = build_marker_message(generated_summary, fraction)

    # ── Dry run: return early with summary but don't mutate pool ──
    if dry_run:
        logger.info(
            f"Dry-run compression for agent '{target_agent_name}': "
            f"would discard {target_discard_count} messages."
        )
        return CompressResult(
            success=True,
            summary_text=generated_summary,
            marker_message=marker_message,
            messages_discarded=target_discard_count,
            tail_count=len(active_set) - target_discard_count,
            error=None,
            mode=mode,
        )

    # ── 10. Apply to pool: trim → insert marker (atomic mutation via copy-and-replace) ──
    # NOTE: This is single-threaded by design — forced compression halts all other agents
    # before running, so no concurrent pool mutations can occur during this block.
    
    # NOTE: get_conversation() returns a copy of inst.conversation (see agent_pool.py:1124).
    history = agent_pool.get_conversation(target_agent_name)
    insert_pos = active_start_idx + target_discard_count

    # ── Defensive guard: verify cut boundary doesn't split tool-call pairs ──
    from agent_cascade.compression.helpers import (
        _has_pending_tool_calls,
        _get_function_call_ids,
        _get_function_result_id,
    )

    while insert_pos < len(history):
        msg = history[insert_pos]
        if _has_pending_tool_calls(msg):
            logger.debug(
                f"Compression boundary adjustment: including ASSISTANT with tool call "
                f"at position {insert_pos} for agent '{target_agent_name}'"
            )
            insert_pos += 1
        else:
            # Also check for orphaned FUNCTION results at the boundary.
            # If a FUNCTION message's matching ASSISTANT was already compressed,
            # skip it to avoid dangling results in the tail.
            fn_role = getattr(msg, 'role', '') if not isinstance(msg, dict) else msg.get('role', '')
            if fn_role == FUNCTION:
                fn_id = _get_function_result_id(msg)
                if fn_id:
                    # Search backwards for the matching ASSISTANT in discarded range
                    found_assistant = False
                    for j in range(insert_pos - 1, active_start_idx - 1, -1):
                        aid_list = _get_function_call_ids(history[j])
                        if fn_id in aid_list:
                            found_assistant = True
                            break
                    if not found_assistant:
                        # Orphaned FUNCTION — its ASSISTANT was already compressed
                        logger.debug(
                            f"Compression boundary adjustment: skipping orphaned "
                            f"FUNCTION result at position {insert_pos} for agent '{target_agent_name}'"
                        )
                        insert_pos += 1
                        continue
            break

    actual_discard = insert_pos - active_start_idx

    # Safety check: insert position must be after the SYSTEM message
    if insert_pos < 1:
        raise RuntimeError(
            f"Insert position {insert_pos} would overwrite or precede SYSTEM message — "
            f"pool state corrupted for agent '{target_agent_name}'"
        )

    # Atomic mutation via copy-and-replace: build new list and assign.
    try:
        new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
        agent_pool.instance_conversations[target_agent_name] = new_history
    except Exception as e:
        # Fail-safe: pool mutation failed — this shouldn't happen but protect against it
        logger.error(f"Pool mutation during compression failed: {e}")
        return CompressResult(
            success=False,
            summary_text=generated_summary,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error=f"Pool mutation failed: {e}",
            mode=mode,
        )

    # Fix #5: Re-validate conversation length after mutation to detect concurrent modification
    post_mutation_conv = agent_pool.get_conversation(target_agent_name)
    if len(post_mutation_conv) != len(new_history):
        logger.warning(
            f"Compression aborted for '{target_agent_name}': "
            f"conversation was modified during compression (race condition detected). "
            f"Expected length {len(new_history)}, got {len(post_mutation_conv)}."
        )
        return CompressResult(
            success=False,
            summary_text=generated_summary,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error="Concurrent modification detected",
            mode=mode,
        )

    # ── 11. Calculate tail count and notify logger ──
    effective_discard = actual_discard
    tail_count = len(active_set) - effective_discard
    # NOTE: Logger sync is now handled by handler.py's _sync_logger_after_compression()
    # which calls reset_history(conv, rewrite=True) for all compression paths.
    # The insert_compression_marker() method in agent_instance_logger.py is deprecated.

    # ── 12. Log the successful compression event ──
    logger.info(
        f"Clean-trim compression: Discarded {effective_discard} messages "
        f"for agent '{target_agent_name}'. Tail count: {tail_count}."
    )

    return CompressResult(
        success=True,
        summary_text=generated_summary,
        marker_message=marker_message,
        messages_discarded=effective_discard,
        tail_count=tail_count,
        error=None,
        mode=mode,
    )