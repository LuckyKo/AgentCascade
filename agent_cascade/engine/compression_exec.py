"""Engine-side compression triggering / slicing / rollback (Phase 1 module-split).

``CompressionExecMixin`` holds the fallback-compression methods of the execution
engine. Method bodies are moved VERBATIM from ``agent_cascade/execution_engine.py``;
only the class wrapper, the fallback-compression constants, and the imports needed
by these methods were added. The mixin is composed into ``ExecutionEngine`` in
:mod:`agent_cascade.engine.core`.

The bare-global call to ``compute_discard_count`` inside ``_find_compression_slice``
is preserved as-is (v3 decision: patch targets point at this true home module).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import List, Optional, Tuple

from agent_cascade.settings import (
    CHARS_PER_TOKEN_ESTIMATE,
    COMPRESSION_RECOUNT_THRESHOLD,
    DEFAULT_MAX_INPUT_TOKENS,
)
from agent_cascade.llm.schema import USER, Message
from agent_cascade.log import logger
from agent_cascade.exceptions import ContextWindowExceeded
from agent_cascade.compression.helpers import compute_discard_count
from agent_cascade.prompts.dna import COMPRESSION_PROMPT
from agent_cascade.utils.utils import extract_text_from_message
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count

from agent_cascade.engine.helpers import _invalidate_token_cache

# ── Fallback Compression Constants ───────────────────────────────────────────
# These control the iterative "smart slice-first" compression used when an agent
# hits context window limits even on fallback endpoints. The algorithm tests
# whether a slice of history fits the compressor's window before attempting
# compression, halving the fraction until it does.

FALLBACK_COMPRESSION_MAX_ROUNDS = 5       # Maximum outer loop iterations before giving up
FALLBACK_COMPRESSION_INITIAL_FRACTION = 0.70  # Start by discarding 70% of active history
FALLBACK_COMPRESSION_MIN_SLICE_FRACTION = 0.05  # Minimum slice: prevents degenerate case where a single massive message cannot be compressed

# Compressor window safety factor: we reserve 15% of the compressor's max tokens
# for system prompt, compression prompt template overhead, and tokenization variance.
# This prevents edge-case failures when the estimated payload size is close to the limit.
_COMPRESSOR_WINDOW_SAFETY_FACTOR = 0.85


class CompressionExecMixin:
    """Mixin providing engine-side fallback-compression methods."""

    def _check_and_trigger_compression(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: Optional[List[Message]] = None
    ) -> bool:
        """Calculate usage percentage and trigger force compression if needed.

        Extracted from _pre_llm_checks() - Phase 3.8

        Uses cached ground-truth token count from last LLM call as baseline,
        then estimates delta tokens for messages added since then (tool results,
        user messages, async injections). Falls back to full recount on first
        turn. Injects warning at lower thresholds.

        Hardening behavior:
        - Always uses fresh max_tokens for threshold comparison (not stale cache)
        - Forces full recount when delta unavailable AND near threshold
        - Overrides cooldown at force threshold to prevent silent truncation
        - Raises ContextWindowExceeded on max-attempts exceeded at force threshold
        - Acquires _compression_lock around token count to prevent race with async drains

        Args:
            instance: Current agent instance
            messages, llm_messages: Working message sets
            response: Optional list to append notifications for yielding (fixes compress feedback bug)

        Returns:
            True if compression was triggered (skip LLM call), False otherwise.

        Raises:
            ContextWindowExceeded: When max-attempts exceeded at force-threshold usage (overflow is loud, not silent)
        """
        force_threshold = self.pool.settings.compression_force_threshold
        reserve_tokens = self.pool.settings.compression_context_reserve_tokens

        # Always use fresh max_tokens — do not trust stale cache near/at threshold.
        # If resolution fails, fall back to instance cache, then the configured default.
        try:
            max_tokens_fresh = self._get_max_tokens(instance)
        except Exception:
            max_tokens_fresh = instance._allocated_max_input_tokens or DEFAULT_MAX_INPUT_TOKENS

        actual_tokens = instance._last_actual_token_count
        allocated_max = instance._allocated_max_input_tokens

        if actual_tokens > 0 and allocated_max > 0:
            # Calculate delta: messages added since last token count
            delta_start = instance._last_token_count_conversation_length
            delta_tokens = 0

            if delta_start >= 0 and len(messages) > delta_start:
                # Use a fresh dummy object to avoid corrupting the instance's
                # token count cache (_count_history_tokens updates
                # _last_token_count_conversation_length to len(messages))
                dummy = SimpleNamespace(
                    _last_token_count_conversation_length=-1,
                    _cached_token_count=0
                )
                with instance._compression_lock:
                    delta_tokens = self._count_history_tokens(messages[delta_start:], dummy)
            elif delta_start < 0 and actual_tokens > allocated_max * COMPRESSION_RECOUNT_THRESHOLD:
                # Cache was invalidated by appends (compression, rollback), so delta is
                # unavailable. If already near threshold, force a full recount instead
                # of trusting potentially stale estimates.
                logger.debug(
                    f"[{instance.instance_name}] Token cache invalidated near threshold "
                    f"(delta_start={delta_start}, actual={actual_tokens}/{allocated_max}), forcing recount"
                )
                with instance._compression_lock:
                    actual_tokens = self._count_history_tokens(messages, instance)
                delta_tokens = 0

            current_tokens = actual_tokens + delta_tokens
            max_tokens_for_check = max_tokens_fresh
        else:
            # Fallback: first turn or no cached data yet
            with instance._compression_lock:
                current_tokens = self._count_history_tokens(messages, instance)
            max_tokens_for_check = max_tokens_fresh

        # Reserve tokens for LLM call overhead (system prompt, function schemas, reasoning)
        effective_limit = max_tokens_for_check - reserve_tokens
        if effective_limit <= 0:
            effective_limit = max_tokens_for_check

        usage_pct = (current_tokens / effective_limit * 100) if effective_limit > 0 else 0

        if usage_pct > force_threshold:
            inst_name = instance.instance_name
            max_attempts = self.pool.settings.compression_max_attempts

            # At >95%, if max-attempts exceeded, raise instead of silent truncation
            if instance._force_compress_count >= max_attempts:
                raise ContextWindowExceeded(
                    f"Max compression attempts ({max_attempts}) exceeded at {usage_pct:.1f}% usage "
                    f"({current_tokens}/{effective_limit} tokens). Context keeps filling faster than "
                    f"compression can reduce it."
                )

            # Cooldown check + override decision + counter update in a single lock
            # acquisition to prevent a race between read and write (TOCTOU).
            with instance._compression_lock:
                now = time.monotonic()
                cooldown = self.pool.settings.compression_force_cooldown
                elapsed = now - instance._last_force_compress_time

                if elapsed < cooldown and usage_pct > force_threshold:
                    # Override cooldown at critical level — truncation risk > compression cost
                    logger.warning(
                        f"[{inst_name}] Overriding compression cooldown at {usage_pct:.1f}% usage "
                        f"(elapsed={elapsed:.1f}s/{cooldown:.1f}s). Critical threshold reached — must compress."
                    )
                    instance._last_force_compress_time = now
                    instance._force_compress_count += 1

            # Call _force_compression outside the lock to avoid deadlock.
            return self._force_compression(instance, messages, llm_messages, usage_pct, response)

        # Warning injection at >90% (configurable via compression_warning_threshold)
        if usage_pct > self.pool.settings.compression_warning_threshold:
            self._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens_for_check)

        return False


    def _force_compression(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], usage_pct: float,
        response: Optional[List[Message]] = None
    ) -> bool:
        """Force compress when token usage exceeds critical threshold. Returns True (continue loop)."""
        inst_name = instance.instance_name

        # Phase 4.2: Delegate to compression_handler (pass response for
        # notification feedback)
        if self.compression_handler.check_cooldown(instance, llm_messages, usage_pct):
            return True

        if self.compression_handler.check_overfeeding(instance, llm_messages, response):
            return True

        return self.compression_handler.execute_force_compression(instance, messages, llm_messages, usage_pct, response)


    def _proactive_compression_check(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: Optional[List[Message]] = None,
        check_label: str = "proactive"
    ) -> None:
        """Check context usage after post-tool / async-drain appends and trigger compression if needed.

        Best-effort check — all exceptions are caught internally and never escape
        to callers. If compression is skipped due to cooldown/max-attempts, logs a
        warning; the pre-LLM hard guard (_check_and_trigger_compression) catches
        overflow as final backstop.

        Args:
            instance: Agent instance to check
            messages: Current message list (used for token counting)
            llm_messages: LLM working set (passed through to _force_compression for
                warning injection and working-set rebuild after compression)
            response: Optional response list for streaming notifications
            check_label: Label for log messages ("post-tool", "async-drain", etc.)
        """
        try:
            inst_name = instance.instance_name
            proactive_threshold = self.pool.settings.compression_proactive_threshold
            reserve_tokens = self.pool.settings.compression_context_reserve_tokens

            max_tokens = self._get_max_tokens(instance)

            # Reserve headroom for LLM call overhead (system prompt, function schemas)
            effective_limit = max_tokens - reserve_tokens
            if effective_limit <= 0:
                effective_limit = max_tokens

            with instance._compression_lock:
                current_tokens = self._count_history_tokens(messages, instance)

            usage_pct = (current_tokens / effective_limit * 100) if effective_limit > 0 else 0

            if usage_pct > proactive_threshold:
                logger.info(
                    f"[{inst_name}] {check_label} proactive check: context at {usage_pct:.1f}% "
                    f"(threshold {proactive_threshold}%), triggering compression"
                )
                result = self._force_compression(instance, messages, llm_messages, usage_pct, response)
                if not result:
                    logger.warning(
                        f"[{inst_name}] {check_label} compression skipped (cooldown/max-attempts) "
                        f"at {usage_pct:.1f}% — pre-LLM guard will catch overflow"
                    )
        except Exception as e:
            # Never let this fail the caller's path (tool execution or async drain)
            logger.debug(f"[{instance.instance_name}] {check_label} proactive compression check failed (non-fatal): {e}")


    def _inject_compression_warning(
        self, llm_messages: List[Message], usage_pct: float,
        current_tokens: int, max_tokens: int
    ):
        """Inject a warning message when context is approaching limit.

        Note: This warning goes directly to the LLM's working set without being persisted
        to conversation pool or yielded (it's an inline hint, not a system notification).
        Uses _append_system_notification for simplicity since it doesn't need UI feedback.
        """
        warning = (
            f"[SYSTEM WARNING: Context window at {usage_pct:.1f}% capacity "
            f"({current_tokens}/{max_tokens} tokens). "
            f"Consider using compress_context to free space.]"
        )
        self._append_system_notification(llm_messages, "[SYSTEM WARNING: Context", warning)


    def _inline_rollback_and_hint(
        self, instance: AgentInstance, inst_name: str,
        pop_count: int, reason: str,
        messages: List[Message], llm_messages: List[Message],
        response: List[Message],
    ) -> None:
        """Rollback conversation and inject a hint message inline (no exception).

        Steps:
          1. Pop N messages from instance.conversation via pool._rollback_instance
             (this also clears working set caches and syncs the logger).
          2. Append ONE USER hint message to guide the agent toward a new approach.
          3. Rebuild local working sets (messages, llm_messages) so the next turn
             uses the rolled-back state instead of stale copies.

        Args:
            instance: The AgentInstance being executed.
            inst_name: Instance name string.
            pop_count: Number of messages to remove from end.
            reason: Human-readable loop detection reason.
            messages, llm_messages, response: Working lists mutated in-place.
        """
        # Step 1: Rollback — pops N msgs, clears caches, syncs logger
        self.pool._rollback_instance(inst_name, pop_count=pop_count)

        # Step 2: Append hint message (goes to conversation + logger
        # atomically)
        hint_msg = Message(
            role=USER,
            content=(
                f"[SYSTEM]: You appear to be stuck in a loop — {reason}. "
                f"Try a different approach to break the pattern."
            ),
        )
        self._append_and_log(instance, hint_msg)

        # Step 3: Rebuild local working sets so the next turn sees fresh state.
        # The response list is also cleared since we're starting a new turn.
        self._rebuild_working_set(messages, llm_messages, inst_name)
        response.clear()


    def _rebuild_working_set(
        self, messages: List[Message], llm_messages: List[Message], inst_name: str
    ):
        """Rebuild both working sets from pool state after compression.

        Optimized rebuild with proper cache invalidation.

        With clean-trim model (DESIGN_REWRITE §2.4), the pool is already compact —
        we just replace our references with deepcopies of the current pool content.

        Cache Invalidation Strategy:
        - Clears token count cache in AgentInstance
        - Signals LLM to clear preprocessing cache if available

        Args:
            messages: Full conversation working set (mutated in-place)
            llm_messages: Sliced working set for LLM (mutated in-place)
            inst_name: Agent instance name to rebuild for
        """
        # Get instance for cache invalidation
        inst = self.pool.get_instance(inst_name)

        # Rebuild messages (full conversation) using shared helper
        from agent_cascade.compression.helpers import rebuild_working_set as _rws
        _rws(messages, self.pool, inst_name)

        # Rebuild llm_messages (sliced working set) — apply
        # slice_history_for_llm
        conv = self.pool.get_conversation(inst_name)
        if not conv:
            return

        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages.clear()
        llm_messages.extend(list(sliced))  # Already a new list from slice_history_for_llm

        # ── Cache Invalidation (after rebuild)
        # ────────────────────────────────────
        # Invalidate token count cache so next _count_history_tokens does fresh
        # count
        if inst:
            inst._cached_token_count = 0
            _invalidate_token_cache(inst)  # Critical: invalidates ALL cache fields including _last_actual_token_count

        # Invalidate LLM preprocessing cache for this instance's template
        if inst:
            self._clear_llm_preprocess_cache(inst, inst_name)

        # Sync the instance caches (Fix LLM Reprocessing)
        if inst:
            inst._cached_messages = messages
            inst._cached_llm_messages = llm_messages
            inst._last_config_version = self.pool._config_version

        logger.debug(
            f"Rebuilt working sets for {inst_name}: "
            f"messages={len(messages)}, llm_messages={len(llm_messages)}"
        )


    def _find_compression_slice(
        self,
        active_set: List[Message],
        history: List[Message],
        active_start_idx: int,
        latest_summary_idx: int,
        compressor_window: Optional[int],
        min_fraction: float,
    ) -> Optional[Tuple[float, int, List[Message]]]:
        """Find a slice of active history that fits within the compressor's context window.

        Iteratively halves the discard fraction starting from INITIAL_FRACTION until
        either (a) the estimated token count fits the compressor window, or
        (b) the fraction drops below min_fraction.

        Uses pre-computed cumulative token counts for O(1) slice estimation.

        Args:
            active_set: Messages eligible for compression (from get_compression_target_set_from_conversation).
            history: Full conversation history (needed when latest_summary_idx == -1 to include u0).
            active_start_idx: Index in history where active_set begins.
            latest_summary_idx: Index of last summary marker (-1 if none).
            compressor_window: Available tokens for messages in the compressor, or None to skip window check.
            min_fraction: Minimum allowed fraction (stops halving below this).

        Returns:
            Tuple of (fraction_used, discard_count, target_messages) if a valid slice is found,
            or None if no slice fits within constraints.
        """
        # Pre-compute cumulative token counts for active_set — each slice test becomes O(1).
        cum_tokens = [0] * len(active_set)
        running = 0
        for i, msg in enumerate(active_set):
            content = extract_text_from_message(msg, add_upload_info=False)
            running += qwen_count(content)
            cum_tokens[i] = running

        # Estimate prompt overhead (system prompt + compression template).
        comp_agent = self.pool.get_agent('Compressor')
        sys_prompt_tokens = 50  # reasonable default
        if comp_agent and hasattr(comp_agent, 'system_message'):
            sys_prompt_tokens = len(str(comp_agent.system_message)) // CHARS_PER_TOKEN_ESTIMATE

        prompt_template_chars = len(COMPRESSION_PROMPT.format(history_text=""))
        prompt_overhead_tokens = sys_prompt_tokens + (prompt_template_chars // CHARS_PER_TOKEN_ESTIMATE)

        # Halve fraction iteratively until slice fits or we hit the minimum.
        target_fraction = FALLBACK_COMPRESSION_INITIAL_FRACTION
        for _ in range(10):  # 10 halvings: 0.7 → ~0.0007 is more than enough headroom
            if target_fraction < min_fraction:
                logger.warning(
                    f"[FALLBACK_COMPRESSION] Fraction {target_fraction:.4f} below minimum "
                    f"{min_fraction}. Cannot find slice that fits compressor window."
                )
                return None

            discard_count = compute_discard_count(active_set, target_fraction, force=True)
            if discard_count <= 0:
                logger.debug(
                    f"[FALLBACK_COMPRESSION] discard_count=0 at fraction={target_fraction:.4f}, halving..."
                )
                target_fraction *= 0.5
                continue

            # Build target_messages for this fraction.
            if latest_summary_idx != -1:
                target_messages = active_set[:discard_count]
            else:
                u0_index = active_start_idx - 1
                target_messages = [history[u0_index]] + list(active_set[:discard_count])

            # Estimate tokens using cumulative array for the active_set portion.
            target_token_count = cum_tokens[discard_count - 1] if discard_count > 0 else 0
            # If u0 was prepended, count its tokens separately.
            if latest_summary_idx == -1 and len(target_messages) > discard_count:
                u0_content = extract_text_from_message(target_messages[0], add_upload_info=False)
                target_token_count += qwen_count(u0_content)

            total_estimated = target_token_count + prompt_overhead_tokens

            # Test against compressor window (if known).
            if compressor_window is not None and total_estimated > compressor_window:
                logger.debug(
                    f"[FALLBACK_COMPRESSION] Slice test FAILED at fraction={target_fraction:.4f}: "
                    f"~{total_estimated} tokens vs ~{compressor_window} available. Halving..."
                )
                target_fraction *= 0.5
                continue

            # Test passed — this slice fits the compressor's window.
            logger.debug(
                f"[FALLBACK_COMPRESSION] Slice test PASSED at fraction={target_fraction:.4f}: "
                f"~{total_estimated} tokens (discard {discard_count} messages). Proceeding."
            )
            return (target_fraction, discard_count, target_messages)

        # Should not reach here under normal conditions.
        logger.warning("[FALLBACK_COMPRESSION] Exhausted slice attempts without finding a fit.")
        return None

