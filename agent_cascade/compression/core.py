"""Unified compress_context() function — the single entry point for all compression."""
import logging
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.helpers import (
    compute_discard_count,
    build_marker_message,
)
from agent_cascade.compression.agent_invoker import invoke_compression_agent
from agent_cascade.utils.utils import extract_text_from_message

logger = logging.getLogger(__name__)


def apply_compression(
    agent_pool,
    target_agent_name: str,
    marker_message,          # The summary marker message (already built)
    insert_pos: int,         # Where to insert the marker in the pool history
    active_start_idx: int,   # Start of active set
    messages_discarded: int, # How many were discarded
    tail_count: int,         # How many remain after marker
    include_force_marker: bool = False,  # Whether to include force compression marker
) -> bool:
    """
    Apply compression atomically to both pool and log.
    
    This unified function ensures pool and log are always modified together,
    eliminating divergence between them. All compression triggers should use
    this function for the final mutation step.
    
    CRITICAL ORDER: Log is updated FIRST, then pool. If log update fails,
    pool remains untouched — preventing the exact divergence we're eliminating.
    
    Args:
        agent_pool: The AgentPool instance.
        target_agent_name: The agent instance name whose context to compress.
        marker_message: The compression summary marker (already built).
        insert_pos: Position in pool history where marker should be inserted.
        active_start_idx: Start index of the active set in pool history.
        messages_discarded: Number of messages being discarded.
        tail_count: Number of messages remaining after the marker.
        include_force_marker: If True, inject a force compression marker before summary.
    
    Returns:
        True if compression was applied successfully, False otherwise.
    """
    history = agent_pool.get_conversation(target_agent_name)
    
    # Build force marker if needed (for forced compression tracking)
    force_marker = None
    if include_force_marker:
        force_marker = {
            'role': 'user',
            'content': f"[SYSTEM INFO: Forced compression started, compressing messages for agent '{target_agent_name}'.]"
        }
    
    try:
        # Validate insert_pos is within bounds to prevent silent data loss
        if insert_pos > len(history):
            logger.error(
                f"Invalid insert_pos={insert_pos} for history length {len(history)} "
                f"in agent '{target_agent_name}' — pool state may be corrupted"
            )
            return False
        
        # Build new history atomically: prefix + force_marker (optional) + summary_marker + tail
        if include_force_marker:
            new_history = history[:active_start_idx] + [force_marker, marker_message] + history[insert_pos:]
        else:
            new_history = history[:active_start_idx] + [marker_message] + history[insert_pos:]
        
        # LOG FIRST: Update the log file before touching the pool
        # This ensures if log update fails, pool remains untouched (no divergence)
        if target_agent_name in agent_pool.instance_loggers:
            logger_inst = agent_pool.instance_loggers[target_agent_name]
            if hasattr(logger_inst, 'reset_history'):
                logger_inst.reset_history(new_history, rewrite=True)
            else:
                logger.error(f"Logger for '{target_agent_name}' lacks reset_history — log not updated")
                return False
        
        # POOL SECOND: Only update pool after log succeeded
        agent_pool.instance_conversations[target_agent_name] = new_history
        
        return True
        
    except Exception as e:
        logger.error(f"Atomic compression application failed for '{target_agent_name}': {e}")
        return False


def compress_context(
    agent_pool,
    target_agent_name: str,        # Which agent's context to compress
    fraction: float = 0.5,         # Fraction of active history to discard
    mode: str = "auto",            # "auto" (LLM generates) or "manual" (summary provided)
    summary_text: str | None = None,  # Required when mode == "manual"
    force: bool = False,           # Bypass validation guards (forced compression at >95%)
    justification: str = "",       # Human-readable reason for this compression
    orchestrator=None,             # Optional: orchestrator instance for call_agent pattern
    dry_run: bool = False,         # If True, generate summary but don't mutate pool
    precomputed_summary: str | None = None,  # Pre-generated summary to skip LLM call
) -> CompressResult:
    """
    Unified compression function. Handles ALL compression triggers:

    - Forced (>95% context usage): orchestrator calls with force=True
    - Agent-triggered (agent calls compress_context tool): normal mode
    - Manual (user provides summary text): mode="manual" with summary_text

    Synchronous — uses generator iteration to invoke the Compression Agent
    (matching the existing _stream_sub_agent_call pattern).

    Fail-safe: if compression fails at any point, pool is untouched.

    Args:
        agent_pool: The AgentPool instance (single source of truth).
        target_agent_name: The agent instance name whose context to compress.
        fraction: Fraction of active history to discard (0.3 to 1.0).
        mode: "auto" for LLM-generated summary, "manual" for provided summary.
        summary_text: Required when mode == "manual". Raw summary text.
        force: If True, bypass the "not enough messages" guard.
        justification: Human-readable reason (logged for debugging).
        orchestrator: Optional orchestrator instance for call_agent pattern invocation.
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
    
    # Safety clamp: ensure we don't try to discard more messages than exist in active set
    target_discard_count = min(target_discard_count, len(active_set))

    # ── 3a. Flag for forced compression marker injection (done in apply_compression) ──
    # This ensures forced compression has the same message structure as agent-triggered compression
    # (which already has a tool call message in the stack). The force marker is injected atomically
    # in apply_compression() along with the summary marker, preventing bloat accumulation.
    include_force_marker = False

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

    # ── 4. Guard: Not enough to compress (unless force=True) ──
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
                orchestrator=orchestrator,
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

    # ── 10. Apply compression atomically to pool and log ──
    # Use the unified apply_compression() function which handles both pool mutation and log update
    # in a single atomic operation, eliminating divergence between them.
    # For forced compression, include_force_marker=True injects the force marker atomically.
    
    tail_count = len(active_set) - target_discard_count
    
    # Adjust active_start_idx if force marker will be injected (it shifts indices by +1)
    adjusted_active_start_idx = active_start_idx + (1 if force and not dry_run else 0)
    insert_pos = adjusted_active_start_idx + target_discard_count
    
    try:
        success = apply_compression(
            agent_pool=agent_pool,
            target_agent_name=target_agent_name,
            marker_message=marker_message,
            insert_pos=insert_pos,
            active_start_idx=adjusted_active_start_idx,
            messages_discarded=target_discard_count,
            tail_count=tail_count,
            include_force_marker=force and not dry_run,  # Include force marker for forced compression
        )
        
        if not success:
            logger.error(f"Atomic compression application failed for '{target_agent_name}'")
            return CompressResult(
                success=False,
                summary_text=generated_summary,
                marker_message=None,
                messages_discarded=0,
                tail_count=0,
                error="Failed to apply compression atomically to pool and log",
                mode=mode,
            )
            
    except Exception as e:
        # Fail-safe: atomic application failed — this shouldn't happen but protect against it
        logger.error(f"Atomic compression application exception for '{target_agent_name}': {e}")
        return CompressResult(
            success=False,
            summary_text=generated_summary,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error=f"Atomic compression failed: {e}",
            mode=mode,
        )

    # ── 11. Log the successful compression event ──
    logger.info(
        f"Clean-trim compression: Discarded {target_discard_count} messages "
        f"for agent '{target_agent_name}'. Tail count: {tail_count}. "
        f"Justification: {justification}"
    )

    return CompressResult(
        success=True,
        summary_text=generated_summary,
        marker_message=marker_message,
        messages_discarded=target_discard_count,
        tail_count=tail_count,
        error=None,
        mode=mode,
    )