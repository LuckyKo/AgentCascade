"""Unified compress_context() function — the single entry point for all compression.

Architecture:
    compress_context() [orchestrator, ~60 lines]
    ├── _prepare_compression() [validation & calculation, ~80 lines] → returns CompressionPreparation
    └── _apply_compression_phase() [atomic application, ~60 lines] → returns CompressResult

    Summary generation is handled inline in the orchestrator (~20 lines)
    since it doesn't warrant a separate phase function.
"""
import logging
from agent_cascade.prompts.dna import COMPRESSION_MARKER
from agent_cascade.compression.result import CompressResult, CompressionPreparation
from agent_cascade.compression.helpers import (
    compute_discard_count,
    build_marker_message,
    get_role,
    count_active_tokens,
)
from agent_cascade.compression.constants import (
    DEFAULT_COMPRESSION_FRACTION,
    MIN_MESSAGES_TO_COMPRESS,
    MIN_TOKENS_TO_COMPRESS,
    COMPRESSION_INPUT_FRACTION,
    ESTIMATED_TOKENS_PER_MESSAGE,
)
from agent_cascade.compression.agent_invoker import invoke_compression_agent
from agent_cascade.utils.utils import extract_text_from_message
from agent_cascade.llm.schema import SYSTEM

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
    
    ── FINAL SYSTEM MESSAGE VALIDATION GATE ──
    This is the single source of truth for SYSTEM message validation.
    All compression paths converge here, so we validate once and fail fast.
    
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
            'content': f"[SYSTEM INFO: Forced compression started, compressing messages for agent '{target_agent_name}'.']"
        }
    
    try:
        # Validate insert_pos is within bounds to prevent silent data loss
        if insert_pos > len(history):
            logger.error(
                f"Invalid insert_pos={insert_pos} for history length {len(history)} "
                f"in agent '{target_agent_name}' — pool state may be corrupted"
            )
            return False
        
        # FIX: Ensure system message is preserved even if active_start_idx=0
        # When active_start_idx == 0, history[:0] returns empty list, dropping the system message.
        # We calculate prefix_len to always include at least the system message when it exists.
        prefix_len = active_start_idx
        if history and get_role(history[0]) == SYSTEM and prefix_len < 1:
            prefix_len = 1
        
        # Build new history atomically: prefix + force_marker (optional) + summary_marker + tail
        if include_force_marker:
            new_history = history[:prefix_len] + [force_marker, marker_message] + history[insert_pos:]
        else:
            new_history = history[:prefix_len] + [marker_message] + history[insert_pos:]
        
        # ── FINAL SYSTEM MESSAGE VALIDATION GATE ──
        # All compression paths converge here. Validate once and fail fast.
        if new_history:
            first_role = get_role(new_history[0])
            if first_role != SYSTEM:
                logger.error(
                    f"[COMPRESSION BUG] System message missing from new_history for '{target_agent_name}'. "
                    f"First role: {first_role}. active_start_idx={active_start_idx}, insert_pos={insert_pos}"
                )
                return False  # Prevent applying corrupted history to pool
        
        # LOG FIRST: Update the log file before touching the pool
        # This ensures if log update fails, pool remains untouched (no divergence)
        try:
            if target_agent_name in agent_pool.instance_loggers:
                logger_inst = agent_pool.instance_loggers[target_agent_name]
                if hasattr(logger_inst, 'reset_history'):
                    # Use tail-offset insertion method to preserve all existing log entries
                    # Instead of passing truncated history (which destroys discarded messages),
                    # we calculate the insertion offset from the tail and insert markers there.
                    # This mirrors what update_history() does during sync operations.
                    log_history = logger_inst.data["history"]  # Read-only reference
                    
                    # Calculate insert offset from tail — preserves all existing log entries
                    log_insert_pos = len(log_history) - tail_count
                    
                    # Safety: never insert before SYSTEM message (index 0)
                    first_log_role = get_role(log_history[0])
                    if log_insert_pos == 0 and log_history and first_log_role == SYSTEM:
                        log_insert_pos = 1
                    
                    # Clamp to valid range (both lower and upper bounds)
                    # Prevents negative indices (when tail_count > len) and out-of-range errors
                    log_insert_pos = max(0, min(log_insert_pos, len(log_history)))
                    
                    # CRITICAL: Build new list via copy — don't mutate log_history in-place!
                    # If we mutate log_history directly (which is logger_inst.data["history"]),
                    # and then reset_history() fails, internal state diverges from disk.
                    new_log_history = list(log_history)  # Shallow copy
                    
                    # Build formatted markers using logger's formatting (adds timestamps)
                    formatted_marker = logger_inst._format_message(marker_message)
                    
                    if include_force_marker:
                        # Insert force marker first, then summary marker into the COPY
                        formatted_force = logger_inst._format_message(force_marker)
                        new_log_history.insert(log_insert_pos, formatted_force)
                        new_log_history.insert(log_insert_pos + 1, formatted_marker)
                    else:
                        # Just insert the summary marker into the COPY
                        new_log_history.insert(log_insert_pos, formatted_marker)
                    
                    # Rewrite the entire file since we inserted in the middle
                    log_write_success = logger_inst.reset_history(new_log_history, rewrite=True)
                    if not log_write_success:
                        logger.error(
                            f"Log write failed for '{target_agent_name}' — pool update skipped to prevent divergence"
                        )
                        return False
                    
                    # Update internal state only after successful write
                    # (reset_history already does this, but explicit assignment ensures consistency)
                    logger_inst.data["history"] = new_log_history
                else:
                    logger.error(f"Logger for '{target_agent_name}' lacks reset_history — log not updated")
                    return False
            
            # POOL SECOND: Only update pool after log succeeded (both in same try block for atomicity)
            agent_pool.instance_conversations[target_agent_name] = new_history
            
            # Output structured marker format for visibility after compression
            if hasattr(agent_pool, 'output_structured_marker_format'):
                try:
                    history = agent_pool.get_conversation(target_agent_name)
                    structure_output = agent_pool.output_structured_marker_format(target_agent_name, history)
                    if structure_output:
                        logger.info(f"[COMPRESSION STRUCTURE] {target_agent_name}:\n{structure_output}")
                except Exception as e:
                    # Graceful degradation - structured marker output should never break compression
                    logger.warning(f"Structured marker format output failed for '{target_agent_name}': {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Atomic compression application failed for '{target_agent_name}': {e}")
            return False
        
    except Exception as e:
        logger.error(f"Atomic compression application failed for '{target_agent_name}': {e}")
        return False


# ── Phase 1: Preparation ──

def _prepare_compression(
    agent_pool,
    target_agent_name: str,
    fraction: float,
    force: bool,
) -> CompressionPreparation:
    """
    Phase 1 of compression: Validate inputs and calculate what to compress.
    
    Returns:
        CompressionPreparation dataclass with validation result and calculated values.
        
    If early_exit is not None, return it immediately (validation failed).
    Otherwise, use the other fields for Phase 2 (summary generation in orchestrator).
    """
    # ── Validate fraction range ──
    if not 0.0 <= fraction <= 1.0:
        return CompressionPreparation(
            early_exit=CompressResult(
                success=False, summary_text=None, marker_message=None,
                messages_discarded=0, tail_count=0,
                error="fraction must be between 0.0 and 1.0", mode="auto",
            ),
            discard_count=0,
            active_start_idx=0,
            target_messages=[],
            existing_summary=None,
        )
    
    # ── Get active set from pool ──
    active_start_idx, active_set, latest_summary_idx = (
        agent_pool.get_compression_target_set(target_agent_name)
    )
    
    if not active_set:
        return CompressionPreparation(
            early_exit=CompressResult(
                success=False, summary_text=None, marker_message=None,
                messages_discarded=0, tail_count=0,
                error="No active messages to compress", mode="auto",
            ),
            discard_count=0,
            active_start_idx=0,
            target_messages=[],
            existing_summary=None,
        )
    
    # ── Calculate token count (advisory) ──
    total_tokens = count_active_tokens(active_set)
    
    # ── Calculate discard count ──
    target_discard_count = compute_discard_count(active_set, fraction, force)
    target_discard_count = min(target_discard_count, len(active_set))
    
    # ── Cap by compression agent's context window ──
    try:
        comp_agent = agent_pool.get_agent('Compressor')
        if comp_agent:
            max_tokens = None
            if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
            elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                max_tokens = comp_agent.llm.cfg.get('max_input_tokens')
            
            if max_tokens:
                available_for_messages = int(max_tokens * COMPRESSION_INPUT_FRACTION)
                max_discardable = available_for_messages // ESTIMATED_TOKENS_PER_MESSAGE
                target_discard_count = min(target_discard_count, max_discardable)
    except Exception:
        pass  # If we can't determine the limit, proceed with original count
    
    # ── Guard: Not enough to compress ──
    if not force:
        if (len(active_set) < MIN_MESSAGES_TO_COMPRESS and total_tokens < MIN_TOKENS_TO_COMPRESS) or target_discard_count <= 0:
            return CompressionPreparation(
                early_exit=CompressResult(
                    success=False, summary_text=None, marker_message=None,
                    messages_discarded=0, tail_count=len(active_set),
                    error="Not enough messages to compress; deferring until more accumulate", mode="auto",
                ),
                discard_count=0,
                active_start_idx=0,
                target_messages=[],
                existing_summary=None,
            )
    
    # ── Determine target messages for summary ──
    if latest_summary_idx != -1:
        history = agent_pool.get_conversation(target_agent_name)
        target_messages = history[latest_summary_idx : latest_summary_idx + 1 + target_discard_count]
    else:
        target_messages = active_set[:target_discard_count]
    
    # ── Extract existing summary for compounding ──
    existing_summary = None
    if latest_summary_idx != -1:
        history = agent_pool.get_conversation(target_agent_name)
        summary_msg = history[latest_summary_idx]
        
        from agent_cascade.llm.schema import Message
        
        if isinstance(summary_msg, dict):
            wrapped_msg = Message(**summary_msg)
        else:
            wrapped_msg = summary_msg
        
        raw_content = extract_text_from_message(wrapped_msg, add_upload_info=True)
        
        if '<context_summary>' in raw_content:
            try:
                existing_summary = raw_content.split('<context_summary>')[1].split('</context_summary>')[0].strip()
            except (IndexError, AttributeError):
                pass
    
    # Return preparation result (no early exit, proceed to summary generation)
    return CompressionPreparation(
        early_exit=None,
        discard_count=target_discard_count,
        active_start_idx=active_start_idx,
        target_messages=target_messages,
        existing_summary=existing_summary,
    )


# ── Phase 3: Application ──

def _apply_compression_phase(
    agent_pool,
    target_agent_name: str,
    generated_summary: str,
    fraction: float,
    active_start_idx: int,
    target_discard_count: int,
    active_set_len: int,
    force: bool,
    dry_run: bool,
    justification: str,
) -> CompressResult:
    """
    Phase 3 of compression: Build marker and apply to pool/log.
    
    Returns:
        CompressResult with success status and metadata.
    """
    # Build marker message
    marker_message = build_marker_message(generated_summary, fraction)
    
    # Dry run: return early without mutating
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
            tail_count=active_set_len - target_discard_count,
            error=None,
            mode="auto",
        )
    
    # Apply atomically
    tail_count = active_set_len - target_discard_count
    # Adjust active_start_idx if force marker will be injected (it shifts indices by +1)
    adjusted_active_start_idx = active_start_idx + (1 if force else 0)
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
            include_force_marker=force,
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
                mode="auto",
            )
    except Exception as e:
        logger.error(f"Atomic compression application exception for '{target_agent_name}': {e}")
        return CompressResult(
            success=False,
            summary_text=generated_summary,
            marker_message=None,
            messages_discarded=0,
            tail_count=0,
            error=f"Atomic compression failed: {e}",
            mode="auto",
        )
    
    # Log success
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
        mode="auto",
    )


# ── Orchestrator ──

def compress_context(
    agent_pool,
    target_agent_name: str,        # Which agent's context to compress
    fraction: float = DEFAULT_COMPRESSION_FRACTION,
    mode: str = "auto",            # "auto" (LLM generates) or "manual" (summary provided)
    summary_text: str | None = None,  # Required when mode == "manual"
    force: bool = False,           # Bypass validation guards (forced compression at >95%)
    justification: str = "",       # Human-readable reason for this compression
    orchestrator=None,             # Optional: orchestrator instance for call_agent pattern
    dry_run: bool = False,         # If True, generate summary but don't mutate pool
    precomputed_summary: str | None = None,  # Pre-generated summary to skip LLM call
) -> CompressResult:
    """
    Unified compression function — orchestrates two phases:
    
    Phase 1 (_prepare_compression): Validate inputs, calculate what to compress
    Phase 2 (inline): Generate or obtain summary text (~20 lines)
    Phase 3 (_apply_compression_phase): Apply compression to pool and log
    
    This is the single entry point for all compression triggers:
    - Forced (>95% context usage): orchestrator calls with force=True
    - Agent-triggered (agent calls compress_context tool): normal mode
    - Manual (user provides summary text): mode="manual" with summary_text
    
    Args:
        agent_pool: The AgentPool instance (single source of truth).
        target_agent_name: The agent instance name whose context to compress.
        fraction: Fraction of active history to discard (0.0 to 1.0).
        mode: "auto" for LLM-generated summary, "manual" for provided summary.
        summary_text: Required when mode == "manual". Raw summary text.
        force: If True, bypass the "not enough messages" guard.
        justification: Human-readable reason (logged for debugging).
        orchestrator: Optional orchestrator instance for call_agent pattern invocation.
        dry_run: If True, generate summary but don't mutate pool.
        precomputed_summary: Pre-generated summary to skip LLM call in auto mode.
    
    Returns:
        CompressResult with success status, summary text, and metadata.
    """
    # ── Validate manual mode has summary_text or precomputed_summary ──
    if mode == "manual" and not summary_text and not precomputed_summary:
        return CompressResult(
            success=False, summary_text=None, marker_message=None,
            messages_discarded=0, tail_count=0,
            error="Manual mode requires summary_text or precomputed_summary", mode=mode,
        )

    # ── Phase 1: Prepare ──
    prep = _prepare_compression(agent_pool, target_agent_name, fraction, force)
    if prep.early_exit is not None:
        return prep.early_exit
    
    # ── Phase 2: Generate Summary (inline) ──
    if precomputed_summary:
        generated_summary = precomputed_summary.strip()
    elif mode == "manual":
        generated_summary = summary_text.strip()
    else:
        # Auto mode: invoke compression agent
        try:
            generated_summary = invoke_compression_agent(
                agent_pool=agent_pool,
                target_messages=prep.target_messages,
                existing_summary=prep.existing_summary,
                orchestrator=orchestrator,
            )
        except Exception as e:
            logger.error(f"Compression Agent invocation failed: {e}")
            return CompressResult(
                success=False, summary_text=None, marker_message=None,
                messages_discarded=0, tail_count=0,
                error=f"Compression Agent failed: {e}", mode=mode,
            )
    
    # Validate we have a usable summary
    if not generated_summary:
        return CompressResult(
            success=False, summary_text=None, marker_message=None,
            messages_discarded=0, tail_count=0,
            error="Failed to obtain a valid summary", mode=mode,
        )
    
    # ── Phase 3: Apply ──
    # Get fresh active set length for tail count calculation
    _, active_set, _ = agent_pool.get_compression_target_set(target_agent_name)
    
    return _apply_compression_phase(
        agent_pool=agent_pool,
        target_agent_name=target_agent_name,
        generated_summary=generated_summary,
        fraction=fraction,
        active_start_idx=prep.active_start_idx,
        target_discard_count=prep.discard_count,
        active_set_len=len(active_set),
        force=force,
        dry_run=dry_run,
        justification=justification,
    )