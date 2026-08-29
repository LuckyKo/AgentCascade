"""Unified compress_context() function — the single entry point for all compression."""
import logging
import threading
from agent_cascade.compression.result import CompressResult
from agent_cascade.compression.helpers import (
    _refine_tool_call_boundary,
    compute_discard_count,
    build_marker_message,
    extract_summary_from_marker,
    get_message_role,
    select_markers_for_consolidation,
)
from agent_cascade.compression.agent_invoker import invoke_compression_agent
from agent_cascade.utils.utils import extract_text_from_message, strip_base64_from_images
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.llm.schema import FUNCTION, Message
from agent_cascade.settings import (
    CHARS_PER_TOKEN_ESTIMATE,
    COMPRESSION_DEFAULT_FRACTION,
    COMPRESSION_MAX_CONSOLIDATION_TOKENS,
)
from agent_cascade.prompts.dna import COMPRESSION_PROMPT

logger = logging.getLogger(__name__)

# Module-level recursion guard for hierarchical consolidation
_consolidating_agents: set[str] = set()
_consolidation_lock = threading.Lock()


def _compression_failure(error: str, mode: str) -> CompressResult:
    return CompressResult(
        success=False,
        summary_text=None,
        marker_message=None,
        messages_discarded=0,
        tail_count=0,
        error=error,
        mode=mode,
    )


def _consolidate_markers(
    agent_pool,
    target_agent_name: str,
) -> None:
    """Post-compression hierarchical consolidation.

    Called after successful compress_context when marker count >= threshold.
    Takes oldest N-1 markers (all except newest), consolidates them into one,
    replaces marker-0 position with the new L2 marker, removes intermediate markers.
    Preserves all raw message segments between markers.

    Thread-safety:
        - Uses _consolidation_lock for cross-agent recursion guard.
        - Acquires target instance's _compression_lock during pool read+validation and again
          during pool write to prevent races with concurrent compression/rollback operations.
        - The LLM call runs outside the lock (long-running).

    Non-fatal: If anything fails, normal compression still succeeded.
    """
    from agent_cascade.agent_pool import AgentPool
    from agent_cascade.settings import COMPRESSION_CONSOLIDATION_THRESHOLD

    # Recursion guard: prevent re-entry for this agent
    with _consolidation_lock:
        if target_agent_name in _consolidating_agents:
            logger.debug(
                f"Consolidation already in progress for '{target_agent_name}' — skipping (recursion guard)"
            )
            return
        _consolidating_agents.add(target_agent_name)

    # Get the target instance to access its compression lock
    target_inst = agent_pool.get_instance(target_agent_name)
    if target_inst is None:
        logger.warning(f"Instance '{target_agent_name}' not found — skipping consolidation")
        with _consolidation_lock:
            _consolidating_agents.discard(target_agent_name)
        return

    try:
        # ── Phase 1: Read + validate under lock, extract summaries for LLM call ──
        history = None
        marker_indices = []
        summaries_to_consolidate = []
        consolidate_indices = []
        first_consolidate_idx = -1
        remove_indices = set()

        with target_inst._compression_lock:
            history = list(target_inst.conversation)
            marker_indices = AgentPool.find_all_marker_indices(history)

            # Guard: need at least threshold markers to consolidate
            if len(marker_indices) < COMPRESSION_CONSOLIDATION_THRESHOLD:
                logger.debug(
                    f"Consolidation skipped for '{target_agent_name}': "
                    f"only {len(marker_indices)} markers (< {COMPRESSION_CONSOLIDATION_THRESHOLD})"
                )
                return

            # Use shared marker selection strategy
            consolidate_indices, keep_index = select_markers_for_consolidation(marker_indices)

            num_to_consolidate = len(consolidate_indices)
            logger.info(
                f"Consolidating {num_to_consolidate} markers for '{target_agent_name}' "
                f"(indices {consolidate_indices}, keeping newest at index {keep_index})"
            )

            # Extract summary texts from markers being consolidated using shared helper
            for idx in consolidate_indices:
                summary_text = extract_summary_from_marker(history[idx])
                if summary_text:
                    summaries_to_consolidate.append(summary_text)
                else:
                    logger.warning(
                        f"Could not extract valid summary from marker at index {idx} — skipping"
                    )

            if not summaries_to_consolidate:
                logger.error(
                    f"No valid summaries extracted from {num_to_consolidate} markers for '{target_agent_name}' — "
                    f"aborting consolidation to avoid data loss"
                )
                return

            # Token size check before invoking compressor
            try:
                total_summary_tokens = sum(qwen_count(s) for s in summaries_to_consolidate)
                if total_summary_tokens > COMPRESSION_MAX_CONSOLIDATION_TOKENS:
                    logger.warning(
                        f"Consolidation input too large for '{target_agent_name}': "
                        f"{total_summary_tokens} tokens > {COMPRESSION_MAX_CONSOLIDATION_TOKENS} limit. "
                        f"Aborting to prevent compressor failure."
                    )
                    return
            except Exception as e:
                logger.debug(f"Token count check for consolidation skipped (non-fatal): {e}")

            # Capture indices for later use
            first_consolidate_idx = consolidate_indices[0]
            remove_indices = set(consolidate_indices[1:])

        # ── Phase 2: LLM call OUTSIDE lock (long-running) ──
        from agent_cascade.compression.agent_invoker import invoke_consolidation_agent
        try:
            consolidated_summary, _consolidation_caption = invoke_consolidation_agent(
                agent_pool=agent_pool,
                marker_summaries=summaries_to_consolidate,
                caller_name=target_agent_name,
            )
        except RuntimeError as e:
            logger.error(f"Consolidation agent failed for '{target_agent_name}': {e}")
            return  # Non-fatal; normal compression succeeded

        if not consolidated_summary or not consolidated_summary.strip():
            logger.error(f"Empty consolidation result for '{target_agent_name}' — aborting")
            return

        # Build new L2 marker
        from agent_cascade.compression.helpers import build_consolidation_marker_message
        new_marker = build_consolidation_marker_message(consolidated_summary, len(summaries_to_consolidate))

        logger.info(
            f"Consolidation summary generated for '{target_agent_name}': "
            f"{len(consolidated_summary)} chars from {len(summaries_to_consolidate)} summaries"
        )

        # ── Phase 3: Re-read + rebuild under lock (defensive against concurrent changes) ──
        with target_inst._compression_lock:
            current_history = list(target_inst.conversation)
            current_markers = AgentPool.find_all_marker_indices(current_history)

            # Defensive re-check: markers may have changed since Phase 1
            if len(current_markers) < COMPRESSION_CONSOLIDATION_THRESHOLD:
                logger.warning(
                    f"Consolidation aborted for '{target_agent_name}': marker count dropped below threshold "
                    f"during LLM call ({len(current_markers)} < {COMPRESSION_CONSOLIDATION_THRESHOLD})"
                )
                return

            # Re-select markers to consolidate based on current state
            current_consolidate_indices = current_markers[:-1]
            current_first_idx = current_consolidate_indices[0]
            current_remove_indices = set(current_consolidate_indices[1:])

            # Pool mutation: replace M0 position with new marker, remove M1..M6 only.
            # CRITICAL: Preserve all raw message segments between markers.
            new_history = []
            for i, msg in enumerate(current_history):
                if i == current_first_idx:
                    new_history.append(new_marker)
                elif i not in current_remove_indices:
                    new_history.append(msg)

            logger.info(
                f"Consolidation pool mutation for '{target_agent_name}': "
                f"{len(current_history)} → {len(new_history)} messages, "
                f"removed {len(current_remove_indices)} markers (indices {sorted(current_remove_indices)}), "
                f"replaced marker at index {current_first_idx}"
            )

            # Atomic pool update via rebuild_conversation which holds _compression_lock (already held here)
            try:
                target_inst.rebuild_conversation(new_history)
            except Exception as e:
                logger.error(f"Pool mutation during consolidation failed for '{target_agent_name}': {e}")
                return

        # ── Phase 4: Sync logger (outside lock, non-fatal) ──
        try:
            log_inst = agent_pool.get_logger(target_agent_name, target_inst.agent_class)
            success = log_inst._consolidate_markers_in_jsonl(
                new_pool_state=new_history,
            )
            if not success:
                logger.warning(
                    f"JSONL consolidation sync failed for '{target_agent_name}' — "
                    f"pool is authoritative; JSONL will be corrected on next compression."
                )
        except Exception as e:
            logger.error(f"Logger sync during consolidation failed for '{target_agent_name}': {e}")
            # Non-fatal: pool is correct

    finally:
        # Always clear recursion guard
        with _consolidation_lock:
            _consolidating_agents.discard(target_agent_name)


def compress_context(
    agent_pool,
    target_agent_name: str,        # Which agent's context to compress
    fraction: float = COMPRESSION_DEFAULT_FRACTION,  # Fraction of active history to discard
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

    # ── 1. Snapshot pool state (single source of truth for entire compression) ──
    # Fetch history once here and reuse throughout. The compressor agent
    # adds messages to the pool during preview/apply, so fetching fresh history
    # at step 10 would cause insert_pos to point to wrong messages (desync bug).
    history = agent_pool.get_conversation(target_agent_name)
    active_start_idx, active_set, latest_summary_idx = (
        agent_pool.get_compression_target_set_from_conversation(target_agent_name, history)
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
            success=False,
            summary_text=None,
            marker_message=None,
            messages_discarded=0,
            tail_count=len(active_set),
            error="Compression not possible at this ratio — tool-call chains extend past the keep zone",
            mode=mode,
        )

    # ── 3b. Determine compressor context window limit (for overfeeding check later) ──
    available_for_messages = None
    max_compressor_tokens = None
    try:
        # Find the largest context window among all compressor endpoints in the fallback chain
        if agent_pool.api_router:
            comp_chain = agent_pool.api_router.get_endpoint_chain('Compressor')
            for cfg in comp_chain:
                ep_limit = cfg.get('max_input_tokens', 0)
                if ep_limit and (max_compressor_tokens is None or ep_limit > max_compressor_tokens):
                    max_compressor_tokens = ep_limit

        # Use the largest available endpoint's context window
        if max_compressor_tokens:
            available_for_messages = int(max_compressor_tokens * 0.85)  # Reserve ~85% for input messages
        else:
            # Fallback: check compressor agent config directly (old behavior)
            comp_agent = agent_pool.get_agent('Compressor')
            if comp_agent:
                max_tokens = None
                if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                    max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
                elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                    max_tokens = comp_agent.llm.cfg.get('max_input_tokens')
                if max_tokens:
                    available_for_messages = int(max_tokens * 0.85)

        # Cap discard count so compressor can handle the messages (~500 tokens/msg estimate)
        if available_for_messages is not None:
            max_discardable = available_for_messages // 500
            target_discard_count = min(target_discard_count, max_discardable)
    except Exception:
        pass  # If we can't determine the limit, proceed with original count

    # Re-check boundary after capping (the cap can push us back to a dirty position)
    if target_discard_count < len(active_set):
        tail_keep = 2
        max_discard = len(active_set) - tail_keep
        refined = _refine_tool_call_boundary(active_set, target_discard_count, max_discard)
        if refined > max_discard:
            return CompressResult(
                success=False,
                summary_text=None,
                marker_message=None,
                messages_discarded=0,
                tail_count=len(active_set),
                error="Compression not possible at this ratio — tool-call chains extend past the keep zone",
                mode=mode,
            )
        target_discard_count = refined

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
    # Reuse `history` snapshot from step 1 (single source of truth).
    
    if latest_summary_idx != -1:
        # Include the new active messages being discarded (NOT the marker — its summary
        # is already extracted separately below and passed as existing_summary).
        # Including both would duplicate the last marker's content in what the compressor sees.
        target_messages = active_set[:target_discard_count]
    else:
        # First compression: include U0 (first user message) so the summary captures
        # the initial prompt/context, not just the messages being discarded.
        # U0 is at index 1 if SYS exists (active_start_idx=2), or index 0 otherwise (active_start_idx=1).
        u0_index = active_start_idx - 1
        target_messages = [history[u0_index]] + list(active_set[:target_discard_count])

    # ── Strip all base64 image data before compression ────────────────────────
    # The compressor only needs text content. _format_messages_for_summary() already
    # converts image items to text placeholders, but this ensures no base64 leaks
    # through string content or edge cases. Use max_images=0 to strip all base64.
    target_messages = strip_base64_from_images(target_messages, max_images=0)

    # ── 6b. ACTUAL token count check: verify target messages fit in compressor's context window ──
    # This is the TRUE overfeeding detection — counts real tokens instead of using rough estimates.
    if available_for_messages is not None:
        try:
            target_token_count = 0
            for msg in target_messages:
                if isinstance(msg, dict):
                    wrapped = Message(**msg)
                else:
                    wrapped = msg
                content = extract_text_from_message(wrapped, add_upload_info=False)
                tokens = qwen_count(content)
                target_token_count += tokens

            # Estimate system message overhead from compressor agent config using CHARS_PER_TOKEN_ESTIMATE.
            comp_agent = agent_pool.get_agent('Compressor')
            if comp_agent and hasattr(comp_agent, 'system_message'):
                sys_prompt_tokens = len(str(comp_agent.system_message)) // CHARS_PER_TOKEN_ESTIMATE
            else:
                sys_prompt_tokens = 50  # fallback estimate

            # Actual COMPRESSION_PROMPT template size (with {history_text} replaced by empty string)
            # since the history_text portion maps to target_messages which we already counted.
            prompt_template_chars = len(COMPRESSION_PROMPT.format(history_text="", end_instruction=""))
            prompt_template_tokens = prompt_template_chars // CHARS_PER_TOKEN_ESTIMATE

            prompt_overhead_tokens = sys_prompt_tokens + prompt_template_tokens

            # Note: for first compression U0 is included; for subsequent compressions the
            # existing summary text (extracted at step 7) is prepended by agent_invoker.py.
            total_estimated = target_token_count + prompt_overhead_tokens
            if total_estimated > available_for_messages:
                return CompressResult(
                    success=False,
                    summary_text=None,
                    marker_message=None,
                    messages_discarded=0,
                    tail_count=len(active_set),
                    error=(
                        f"Compression payload ({target_token_count} tokens + ~{prompt_overhead_tokens} overhead = "
                        f"~{total_estimated} total) exceeds compressor context window (~{available_for_messages} tokens). "
                        f"Try compressing with a lower ratio or use a larger-context endpoint."
                    ),
                    mode=mode,
                )
        except Exception as e:
            logger.debug(f"Token counting for overfeeding check failed (non-fatal): {e}")

    # ── 7. Get existing summary text from pool for compounding ──
    # Reuse the history reference from step 6 instead of refetching it.
    existing_summary = None
    if latest_summary_idx != -1:
        summary_msg = history[latest_summary_idx]
        
        # Use extract_text_from_message to handle both string and multi-modal list content
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

    # ── 8. Generate or obtain summary (+ optional session caption) ──
    # The caption is parsed out of the compressor output (never placed in the marker
    # body) and stored in log metadata for UI display. Manual/precomputed paths have
    # no caption — they produce a plain summary with an empty caption.
    generated_caption = ""
    if precomputed_summary:
        # Use a pre-generated summary (e.g., from /compress command after user approval)
        generated_summary = precomputed_summary.strip()
    elif mode == "manual":
        generated_summary = summary_text.strip()
    else:
        try:
            # Only request a caption if one doesn't already exist (first-wins).
            _want_caption = False
            if not dry_run:
                try:
                    _cap_inst = agent_pool.get_instance(target_agent_name)
                    _cap_class = getattr(_cap_inst, 'agent_class', None) or target_agent_name
                    _cap_logger = agent_pool.get_logger(target_agent_name, _cap_class)
                    _want_caption = not _cap_logger.data["metadata"].get("caption")
                except Exception:
                    pass  # Non-fatal: default to no caption if logger unavailable

            # invoke_compression_agent() handles retries internally (reuses the same
            # compressor instance and resends the prompt on retryable validation failures).
            generated_summary, generated_caption = invoke_compression_agent(
                agent_pool=agent_pool,
                target_messages=target_messages,
                existing_summary=existing_summary,
                caller_name=target_agent_name,  # Pass actual instance name for slot management
                want_caption=_want_caption,
            )
        except RuntimeError as e:
            logger.error(f"Compression Agent failed: {e}")
            return _compression_failure(f"Compression Agent failed: {e}", mode=mode)
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
    # NOTE: the marker body contains ONLY the clean summary — the caption is parsed out
    # of the compressor output above and must NEVER leak into this <context_summary>
    # body (it would otherwise re-enter model context on future turns).
    marker_message = build_marker_message(generated_summary, fraction)

    # ── 9b. Persist the session caption to log metadata (first meaningful one wins) ──
    # In-memory only: set_caption() updates data["metadata"]["caption"] and the existing
    # compression rewrite path (reset_history(rewrite=True) → _sync_marker_single_write,
    # triggered by handler._sync_logger_after_compression) re-emits line 1 with it. No new
    # full-file rewrite is added here. Skipped for dry-run (no pool/logger mutation).
    if generated_caption and not dry_run:
        try:
            _cap_inst = agent_pool.get_instance(target_agent_name)
            _agent_class = getattr(_cap_inst, 'agent_class', None) or target_agent_name
            _log_inst = agent_pool.get_logger(target_agent_name, _agent_class)
            _log_inst.set_caption(generated_caption)
        except Exception as e:
            logger.debug(f"Failed to set session caption in metadata (non-fatal): {e}")

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
            tokens_before=total_tokens,
            tokens_after=0,  # dry_run: no actual mutation happened
        )

    # ── 10. Apply to pool: trim → insert marker (atomic mutation via copy-and-replace) ──
    # NOTE: This is single-threaded by design — forced compression halts all other agents
    # before running, so no concurrent pool mutations can occur during this block.
    
    # Reuse `history` snapshot from step 1 (single source of truth).
    insert_pos = active_start_idx + target_discard_count

    # Safety check: insert position must be after the SYSTEM message
    if insert_pos < 1:
        raise RuntimeError(
            f"Insert position {insert_pos} would overwrite or precede SYSTEM message — "
            f"pool state corrupted for agent '{target_agent_name}'"
        )

    # Safety check: verify first kept message is not a FUNCTION response (orphaned from its A).
    # This catches desync between pool and active_set snapshot taken at step 1.
    if insert_pos < len(history):
        first_kept = history[insert_pos]
        role = get_message_role(first_kept)
        if role == FUNCTION:
            return CompressResult(
                success=False,
                summary_text=None,
                marker_message=None,
                messages_discarded=0,
                tail_count=len(active_set),
                error=f"Compression marker would be inserted before a FUNCTION response at position "
                      f"{insert_pos} — pool/active-set desync detected. "
                      f"Discard count={target_discard_count}, active_start_idx={active_start_idx}, "
                      f"history_len={len(history)}",
                mode=mode,
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
    tail_count = len(active_set) - target_discard_count
    # NOTE: Logger sync is now handled by handler.py's _sync_logger_after_compression()
    # which calls reset_history(conv, rewrite=True) for all compression paths.
    # The insert_compression_marker() method in agent_instance_logger.py is deprecated.

    # ── 12. Calculate post-compression token count for telemetry (BUG 6 fix) ──
    # Estimate tokens_after by counting tokens of the summary marker message
    # plus the remaining tail messages (total_tokens - discarded_tokens + summary_tokens)
    try:
        # Count tokens in discarded messages
        discarded_tokens = 0
        for msg in active_set[:target_discard_count]:
            if isinstance(msg, dict):
                wrapped = Message(**msg)
            else:
                wrapped = msg
            content = extract_text_from_message(wrapped, add_upload_info=True)
            discarded_tokens += qwen_count(content)
        # Count tokens in the marker/summary message that replaces them
        summary_content = extract_text_from_message(marker_message, add_upload_info=True) if marker_message else ""
        summary_tokens = qwen_count(summary_content) if summary_content else 0
        tokens_after = max(total_tokens - discarded_tokens + summary_tokens, 0)
    except Exception:
        # Token counting is advisory — fall back to total_tokens estimate
        tokens_after = total_tokens

    # ── 13. Log the successful compression event ──
    logger.info(
        f"Clean-trim compression: Discarded {target_discard_count} messages "
        f"for agent '{target_agent_name}'. Tail count: {tail_count}. "
        f"Tokens: {total_tokens} -> {tokens_after}."
    )

    # ── 14. Post-compression hierarchical consolidation check ──
    if not dry_run:
        try:
            from agent_cascade.agent_pool import AgentPool
            from agent_cascade.settings import COMPRESSION_CONSOLIDATION_THRESHOLD

            post_history = agent_pool.get_conversation(target_agent_name)
            marker_count = AgentPool.count_markers(post_history)

            if marker_count >= COMPRESSION_CONSOLIDATION_THRESHOLD:
                logger.info(
                    f"Triggering hierarchical consolidation for '{target_agent_name}': "
                    f"{marker_count} markers present, will consolidate oldest {marker_count - 1}"
                )
                _consolidate_markers(agent_pool, target_agent_name)
        except Exception as e:
            logger.error(
                f"Hierarchical consolidation failed for '{target_agent_name}' (non-fatal): {e}. "
                f"Normal compression succeeded; markers will be consolidated on next cycle."
            )

    return CompressResult(
        success=True,
        summary_text=generated_summary,
        marker_message=marker_message,
        messages_discarded=target_discard_count,
        tail_count=tail_count,
        error=None,
        mode=mode,
        tokens_before=total_tokens,
        tokens_after=tokens_after,
    )