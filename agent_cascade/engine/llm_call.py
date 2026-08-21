"""LLM-call cluster for the execution engine (Phase 1 module-split).

``LLMCallMixin`` holds the LLM invocation / retry / error-classification methods.
Method bodies are moved VERBATIM from ``agent_cascade/execution_engine.py``; only
the class wrapper and the imports needed by these methods were added. The mixin is
composed into ``ExecutionEngine`` in :mod:`agent_cascade.engine.core`.

The bare-global call to ``_canonical_detect_loop`` inside ``_pre_llm_checks`` is
preserved as-is (v3 decision: patch targets point at this true home module).
"""

from __future__ import annotations

import sys
import time
from typing import Iterator, List, Optional

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.settings import (
    STREAM_MAX_SILENCE_SECONDS,
    STREAM_MAX_TOTAL_SECONDS,
    TOKEN_ESTIMATE_CHAR_DIVISOR,
)
from agent_cascade.retry_policy import classify_error, calculate_backoff, RetryPolicy
from agent_cascade.llm.schema import ASSISTANT, USER, Message
from agent_cascade.log import logger
from agent_cascade.exceptions import (
    CharacterRunDetected,
    ContextWindowExceeded,
    FallbackCompressionRequired,
    MaxTokenExceeded,
    AgentTerminatedError,
)
from agent_cascade.utils.utils import extract_text_from_message, msg_field
from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
from agent_cascade.inner_loop_detect import InnerLoopDetector, save_loop_sample
from agent_cascade.settings import InnerLoopSettings as _InnerLoopSettings
from agent_cascade.loop_detection import detect_loop as _canonical_detect_loop

from agent_cascade.engine.compression_exec import (
    FALLBACK_COMPRESSION_MAX_ROUNDS,
    FALLBACK_COMPRESSION_MIN_SLICE_FRACTION,
    _COMPRESSOR_WINDOW_SAFETY_FACTOR,
)
from agent_cascade.engine.helpers import (
    _get_active_functions_from_template,
    _make_token_count_callback,
    _make_usage_callback,
)

# Sampling & limit parameters to strip when custom sampling is disabled for an
# endpoint. This constant lived at module scope in the original
# execution_engine.py; it is used by _build_merged_cfg (moved here), so its true
# home is this sub-module. core.py re-imports it from here (core already imports
# LLMCallMixin from llm_call, so no circular import is introduced).
SAMPLING_AND_LIMIT_KEYS = frozenset({
    'temperature', 'top_p', 'top_k', 'min_p',
    'repeat_penalty', 'repetition_penalty', 'repeatPenalty',
    'presence_penalty', 'frequency_penalty', 'max_tokens',
})


class LLMCallMixin:
    """Mixin providing the LLM-call / retry / error-classification methods."""

    def _pre_llm_checks(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], response: List[Message],
        turns_wrapper: List[int],  # mutable wrapper around turns_available
    ) -> bool:
        """Phase 2: Stop/halt checks, async injection, compression check, loop detection.

        Returns True if processing should continue to next iteration (yield + continue).
        Handles: stop/halt guard, async message drain, forced compression with rebuild,
        and loop detection (inline rollback + hint if found).
        When a "real cycle" occurs (rollback, compression, async message injection),
        decrements turns_wrapper[0] so max_turns acts as a backstop.
        """
        inst_name = instance.instance_name

        # 1. Stop/halt checks
        if self._check_stop_conditions(instance):
            logger.debug(f"[PRE_LLM] Stop/halt condition met for {inst_name}")
            return True  # Skip LLM call, yield and continue loop

        # 2. Async message injection
        if self._inject_async_messages(instance, messages, llm_messages, response):
            turns_wrapper[0] -= 1  # R2: async injection is a real cycle
            return True  # Yield and continue loop to process new messages

        # 3. Rollback command check (delegated to compression_handler)
        # Pass response so notification messages get yielded (fixes compress
        # feedback bug)
        if self.compression_handler.handle_rollback_command(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Rollback command handled for {inst_name}")
            turns_wrapper[0] -= 1  # R3: user rollback command is a real cycle
            return True  # Command handled — yield and continue

        # 4. Compress command check (Phase 4.2: delegated to
        # compression_handler)
        # Pass response so notification messages get yielded (fixes compress
        # feedback bug)
        if self.compression_handler.handle_compress_command(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Compress command handled for {inst_name}")
            turns_wrapper[0] -= 1  # R4: user compress command is a real cycle
            return True  # Command handled — yield and continue

        # 5. Compression trigger (pass response for notification feedback)
        if self._check_and_trigger_compression(instance, messages, llm_messages, response):
            logger.debug(f"[PRE_LLM] Compression triggered for {inst_name}")
            turns_wrapper[0] -= 1  # R5: forced compression is a real cycle
            return True  # Compression triggered — yield and continue

        # 6. Loop detection (with post-compression cooldown)
        # ───────────────────
        # After compression, the conversation state has concentrated patterns
        # that
        # can trigger false-positive loop detection. Skip detection on the turn
        # immediately following compression via
        # _suppress_loop_detection_next_turn flag.
        # Thread safety: Python GIL ensures atomic reads/writes for simple
        # boolean attributes.
        if not getattr(instance, '_suppress_loop_detection_next_turn', False):
            loop_info = _canonical_detect_loop(messages)
            if loop_info:
                reason, pop_count = loop_info
                logger.debug(
                    f"[LOOP_DETECTED] {inst_name}: pattern={reason}, "
                    f"pop_count={pop_count}, messages={len(messages)}"
                )

                # ── Respect auto_rollback_on_loop toggle ──────────────────────
                if not self.pool.settings.auto_rollback_on_loop:
                    logger.info(
                        f"[LOOP_DETECTED_NO_ROLLBACK] {inst_name}: loop detected "
                        f"(pattern={reason}) but auto_rollback_on_loop=False. "
                        f"Continuing to LLM call."
                    )
                    # Telemetry for observability
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_loop_detected(
                                inst_name, reason=reason, auto_rolled_back=False, pop_count=pop_count, loop_type="outer",
                            )
                        except Exception:
                            pass
                    # Return False → proceed to LLM call with current context.
                    # No turn consumed here; normal turn decrement at line 1343 applies.
                    return False

                # ── Inline rollback + hint (only when toggle is True) ─────────
                rollbacks = getattr(instance, '_loop_rollback_count', 0) + 1
                instance._loop_rollback_count = rollbacks

                self._inline_rollback_and_hint(
                    instance, inst_name, pop_count, reason,
                    messages, llm_messages, response,
                )

                # ── Enforce configured max_auto_rollbacks limit ───────────────
                max_rb = self.pool.settings.max_auto_rollbacks
                if max_rb == -1:
                    effective_limit = sys.maxsize  # unlimited; max_turns still backstops
                else:
                    effective_limit = max_rb

                if rollbacks > effective_limit:
                    logger.warning(
                        f"Loop recovery for {inst_name}: exceeded configured limit "
                        f"(rolled back {rollbacks} times, max={max_rb}). Terminating."
                    )
                    # Append clear failure message for caller visibility
                    fail_msg = Message(
                        role=USER,
                        content=(
                            f"[SYSTEM]: Loop recovery failed — the agent exceeded the maximum "
                            f"allowed loop recoveries ({max_rb if max_rb != -1 else 'unlimited'}). "
                            f"The detected pattern was: {reason}. Please adjust your prompt or task."
                        ),
                    )
                    self._append_and_log(instance, fail_msg)
                    # Terminate this instance (and its children). Use set_global_stopped=False
                    # so other agents are unaffected.
                    self.pool.terminate_instance(inst_name, set_global_stopped=False)
                    # Turn consumed for this rollback cycle
                    turns_wrapper[0] -= 1
                    return True  # Caller will break on _check_stop_conditions next iteration
                elif rollbacks >= 3 and rollbacks < effective_limit:
                    # Warn at ≥3rd rollback only if we still have headroom before limit
                    logger.warning(
                        f"Loop recovery for {inst_name}: rolled back "
                        f"{rollbacks} times without success. Continuing."
                    )

                # Telemetry: record loop detection (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_loop_detected(
                            inst_name, reason=reason, auto_rolled_back=True, pop_count=pop_count, loop_type="outer",
                        )
                    except Exception:
                        pass

                # Turn consumed for this rollback cycle (Change 2 integration)
                turns_wrapper[0] -= 1  # R6: loop rollback is a real cycle
                return True  # Continue loop with fresh state
        else:
            # Clear the cooldown flag now that we've skipped loop detection
            # this turn.
            # Next turn will run normal loop detection (no more suppression).
            instance._suppress_loop_detection_next_turn = False

            # Also reset rollback counter after compression (conversation state
            # changed)
            if hasattr(instance, '_loop_rollback_count'):
                instance._loop_rollback_count = 0

        return False  # Continue to LLM call normally


    def _execute_llm_call_with_retry(
        self,
        instance: AgentInstance,
        llm_messages: List[Message],
        template,
        active_functions
    ) -> Iterator[Message]:
        """Execute LLM call with retry logic and streaming injection.

        Extracted from _call_llm_with_injection() - Phase 3.6

        This method handles:
        - Retry loop with exponential backoff
        - Error classification (timeout, network, API error)
        - Streaming response handling
        - [RETRYING] message injection for UI

        Args:
            instance: Agent instance making the call
            llm_messages: Messages to send to LLM
            template: Template with LLM configuration
            active_functions: Active tool schemas

        Yields:
            Message objects or None for progress updates
        """
        inst_name = instance.instance_name
        last_output = None
        retry_count = 0
        loop_retry_count = 0       # Dedicated counter for inner-loop retries (gated by pool.settings.retry_max_attempts)
        error_already_yielded = False
        _max_attempts = self.pool.settings.retry_max_attempts  # At least 1 retry to avoid instant failure

        # Build centralized retry policy from pool settings (Phase 4a)
        _retry_policy = RetryPolicy(
            retry_max_attempts=getattr(self.pool.settings, 'retry_max_attempts', 3),
            base_delay=getattr(self.pool.settings, 'retry_base_delay', 1.0),
            max_delay=getattr(self.pool.settings, 'retry_max_delay', 8.0),
        )

        # Estimate input tokens for telemetry (rough char-based estimate)
        _input_tokens_est = sum(len(m.content or '') for m in llm_messages) // TOKEN_ESTIMATE_CHAR_DIVISOR

        # Ensure images have captions before any LLM call — this enables
        # graceful fallback
        # to text-only endpoints by replacing image data with text
        # descriptions.
        # Captions are generated ONCE and cached on ContentItem objects, so
        # subsequent retries reuse them.
        if self._has_images(llm_messages):
            agent_type = instance.agent_class.lower() if hasattr(instance, 'agent_class') else 'generalist'
            llm_messages = self._ensure_image_captions(llm_messages, agent_type=agent_type)

        try:
            while retry_count < _max_attempts:
                try:
                    # Telemetry: record LLM call start (non-blocking)
                    self._record_telemetry_event(
                        inst_name, 'start',
                        input_tokens_est=_input_tokens_est,
                        model=getattr(template.llm, 'model', '') or '',
                    )

                    # Streaming UI Content Update Fix: Track partial LLM content
                    # for UI updates every ~100ms
                    last_streaming_update_time = time.monotonic()

                    # Inner-loop detector: fresh instance per retry attempt to
                    # catch generation loops mid-stream. Uses char_run + max_chars
                    # (last line of defense) and optional two-phase semantic detection.
                    _ps = self.pool.settings
                    _inner_settings = _InnerLoopSettings(
                        default_min_chars=getattr(_ps, 'loop_min_chars', 4000),
                        default_max_chars=getattr(_ps, 'loop_max_chars', 40960),
                        char_run_enabled=getattr(_ps, 'loop_char_run_enabled', True),
                        char_run_limit=getattr(_ps, 'loop_char_run_limit', 129),
                        loop_max_chars_enabled=getattr(_ps, 'loop_max_chars_enabled', True),
                        loop_two_phase_enabled=getattr(_ps, 'loop_two_phase_enabled', False),
                        loop_suspicion_threshold=getattr(_ps, 'loop_suspicion_threshold', 7),
                        loop_confirm_required=getattr(_ps, 'loop_confirm_required', 3),
                        loop_cooldown_feeds=getattr(_ps, 'loop_cooldown_feeds', 50),
                    )
                    _inner_detector = InnerLoopDetector(settings=_inner_settings)
                    _prev_text_len = 0  # Tracks accumulated text length for delta extraction (delta_stream=False)

                    # Max-output-token guard: safety net against LLMs exceeding
                    # their token budget
                    # Resolve the output token limit from generate_cfg override,
                    # template config, or default
                    _max_output_tokens = 8192  # Default cap per single response
                    _gen_override = getattr(instance, '_generate_cfg_override', None)
                    if _gen_override and isinstance(_gen_override, dict):
                        _mt = _gen_override.get('max_tokens') or _gen_override.get('max_output_tokens') or _gen_override.get('max_input_tokens')
                        if isinstance(_mt, int) and _mt > 0:
                            _max_output_tokens = _mt
                    # Also check template LLM generate_cfg as fallback
                    if _max_output_tokens == 8192:
                        _llm_cfg = getattr(getattr(template, 'llm', None), 'generate_cfg', None) or {}
                        _mt = _llm_cfg.get('max_tokens') or _llm_cfg.get('max_output_tokens') or _llm_cfg.get('max_input_tokens')
                        if isinstance(_mt, int) and _mt > 0:
                            _max_output_tokens = _mt

                    _token_guard_triggered = False  # Prevent double-trigger within same iteration

                    gen = self._execute_llm_call(instance, template, llm_messages, active_functions)
                    _first_token_received = False  # Flag to ensure TTFT is recorded only once per call

                    # --- Interrupt helper (shared by inner-loop and max-token
                    # guards) ---
                    # Defined ONCE per retry attempt before the loop to avoid
                    # recreating on every iteration.
                    # Counter increments happen BEFORE yield so they update even if
                    # consumer drops the iterator.
                    def _abort_stream(reason_msg):
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        # Clean up async tasks (same as stop condition handler)
                        if hasattr(self.pool, '_async_registry'):
                            try:
                                self.pool._async_registry.clear_pending(inst_name)
                            except Exception:
                                pass
                        # Record telemetry end for aborted call
                        self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)
                        try:
                            gen.close()
                        except RuntimeError:
                            pass  # Already closed/exhausted
                        logger.info(f"[STREAM_GUARD] {reason_msg} for {inst_name}. Retrying…")
                        # Increment counters BEFORE yield — ensures update even if
                        # consumer drops iterator mid-yield
                        nonlocal last_output, retry_count, loop_retry_count
                        last_output = None
                        retry_count += 1
                        loop_retry_count += 1
                        yield None  # Signal UI

                    # Engine-level streaming watchdog: detect mid-stream stalls at the
                    # execution engine layer (last defense if backend didn't raise).
                    # Read configured values from pool settings, falling back to module defaults.
                    _engine_max_silence = getattr(
                        self.pool.settings, 'stream_max_silence_seconds', STREAM_MAX_SILENCE_SECONDS)
                    _engine_max_total = getattr(
                        self.pool.settings, 'stream_max_total_seconds', STREAM_MAX_TOTAL_SECONDS)
                    _engine_stream_start = time.monotonic()
                    _engine_first_output = True
                    _engine_last_output_time = None

                    for output in gen:
                        # Watchdog check on each consumed output
                        _now = time.monotonic()
                        # Silence check only after first output; slow reasoning models may take >120s to produce first token
                        if not _engine_first_output and _engine_last_output_time is not None:
                            if (_now - _engine_last_output_time) > _engine_max_silence:
                                logger.info(
                                    f"[STREAM_WATCHDOG] {inst_name}: silence exceeded "
                                    f"{_engine_max_silence:.0f}s (actual={_now - _engine_last_output_time:.1f}s)"
                                )
                                _abort_stream("Engine watchdog: stream_stalled")
                                break  # Exit loop after aborting; will retry in outer while
                        # Total timeout applies from stream start regardless of first output timing
                        if (_now - _engine_stream_start) > _engine_max_total:
                            logger.info(
                                f"[STREAM_WATCHDOG] {inst_name}: total duration exceeded "
                                f"{_engine_max_total:.0f}s (actual={_now - _engine_stream_start:.1f}s)"
                            )
                            _abort_stream("Engine watchdog: stream_stalled")
                            break  # Exit loop after aborting; will retry in outer while
                        if _engine_first_output:
                            _engine_first_output = False
                        _engine_last_output_time = _now

                        last_output = output

                        # Feed delta text to inner-loop detector (extracts new
                        # content from accumulated response)
                        try:
                            _last_msg = output[-1] if output else None
                            if _last_msg is not None:
                                _content = msg_field(_last_msg, 'content', '') or ''
                                _reasoning = msg_field(_last_msg, 'reasoning_content') or ''
                                # Handle multimodal content (list of items) for
                                # both fields
                                if isinstance(_content, list):
                                    _content = ' '.join(str(c) for c in _content if isinstance(c, str))
                                else:
                                    _content = str(_content)
                                if isinstance(_reasoning, list):
                                    _reasoning = ' '.join(str(c) for c in _reasoning if isinstance(c, str))
                                else:
                                    _reasoning = str(_reasoning)
                                _total_text = _reasoning + _content

                                # Also feed function_call data so tool-call streaming
                                # (where content is empty but payload is in name+arguments)
                                # is detected by the inner-loop detector.
                                _fc = msg_field(_last_msg, 'function_call')
                                if _fc:
                                    if isinstance(_fc, dict):
                                        _fc_name = _fc.get('name', '') or ''
                                        _fc_args = _fc.get('arguments', '') or ''
                                    else:
                                        _fc_name = getattr(_fc, 'name', '') or ''
                                        _fc_args = getattr(_fc, 'arguments', '') or ''
                                    if _fc_name or _fc_args:
                                        _total_text += f"\n{_fc_name}: {_fc_args}"

                                # Generator is append-only (delta_stream=False), so
                                # slicing by previous length gives the new delta
                                _delta_text = _total_text[_prev_text_len:]
                                _prev_text_len = len(_total_text)

                                if _delta_text:
                                    # Inner-loop detection (gated by pool settings
                                    # toggle)
                                    if getattr(self.pool.settings, 'inner_loop_detect_enabled', False):
                                        _ev = _inner_detector.feed(_delta_text)
                                        if _ev:  # Loop detected mid-stream
                                            _sample_path = save_loop_sample(
                                                text=_total_text[:4000],
                                                reason=f"inner_loop ({_ev['reason']}, score={_ev['score']})",
                                                instance_name=inst_name,
                                            )
                                            yield from _abort_stream(
                                                f"Detected generation loop: {_ev['reason']} (score={_ev['score']})"
                                            )
                                            if _sample_path:
                                                logger.debug(f"  [LOOP_SAMPLE] Saved to {_sample_path}")
                                            # Check dedicated loop retry budget
                                            # (gated by _max_attempts from pool.settings.retry_max_attempts)
                                            if loop_retry_count >= _max_attempts:
                                                raise CharacterRunDetected(
                                                    f"inner_loop_exhausted: retried {_max_attempts} times, "
                                                    f"giving up — last reason: {_ev['reason']}",
                                                    detection_reason=_ev['reason'],
                                                )
                                            raise CharacterRunDetected(
                                                f"inner_loop: {_ev['reason']}",
                                                detection_reason=_ev['reason'],
                                            )

                                # Max-output-token guard: safety net — if LLM
                                # exceeds token budget it's likely looping
                                if not _token_guard_triggered:
                                    _est_tokens = len(_total_text) // TOKEN_ESTIMATE_CHAR_DIVISOR
                                    if _est_tokens > _max_output_tokens:
                                        _sample_path = save_loop_sample(
                                            text=_total_text[:4000],
                                            reason=f"max_output_exceeded ({_est_tokens}/{_max_output_tokens} est. tokens)",
                                            instance_name=inst_name,
                                        )
                                        yield from _abort_stream(
                                            f"Output token budget exceeded: ~{_est_tokens} tokens (limit {_max_output_tokens})"
                                        )
                                        if _sample_path:
                                            logger.debug(f"  [LOOP_SAMPLE] Saved to {_sample_path}")
                                        _token_guard_triggered = True
                                        raise MaxTokenExceeded(f"max_tokens: ~{_est_tokens} tokens")
                        except Exception as e:
                            logger.debug(f"[INNER_LOOP] Detection error for {inst_name}: {e}")
                            # Re-raise if this is an explicit inner-loop or
                            # max-tokens detection exception
                            if isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
                                raise

                        # Telemetry: record Time To First Token (TTFT) on the first streaming chunk
                        if not _first_token_received:
                            self._record_telemetry_event(inst_name, 'first_token')
                            _first_token_received = True

                        # Check stop/halt mid-stream FIRST (before any work) —
                        # ensures fastest response to stop.
                        # Also checks generation change (old run superseded by
                        # newer one on resume).
                        # This is defense-in-depth: _check_stop_conditions() runs
                        # before the LLM call, but stop
                        # can also be triggered DURING the streaming call itself
                        # (while chunks are arriving).
                        if self._is_terminal_stop(inst_name):
                            # Telemetry: record LLM call end for mid-stream stop (non-blocking)
                            self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)
                            with instance._compression_lock:
                                instance._streaming_responses = []
                            # ── Fix TODO
                            if hasattr(self.pool, '_async_registry'):
                                try:
                                    self.pool._async_registry.clear_pending(inst_name)
                                except Exception:
                                    pass  # Non-critical cleanup
                            try:
                                gen.close()  # Explicitly close generator → triggers finally blocks → releases HTTP connection immediately
                            except RuntimeError:
                                pass  # Already closed/exhausted
                            yield None  # Signal UI that stop was detected mid-stream
                            break
                        # Compression-halt during streaming: let the stream complete, handle at Site 3

                        # Update _streaming_responses every ~100ms with deep copy
                        # of partial content
                        current_time = time.monotonic()
                        if current_time - last_streaming_update_time >= 0.1:
                            with instance._compression_lock:
                                self._update_streaming_responses(instance, last_output)
                                last_streaming_update_time = current_time

                        # Re-check stop/halt after UI update (defense in depth —
                        # catches stop during slow streaming)
                        if self._is_terminal_stop(inst_name):
                            # Telemetry: record LLM call end for mid-stream stop (non-blocking)
                            self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)
                            with instance._compression_lock:
                                instance._streaming_responses = []
                            # ── Fix TODO
                            if hasattr(self.pool, '_async_registry'):
                                try:
                                    self.pool._async_registry.clear_pending(inst_name)
                                except Exception:
                                    pass  # Non-critical cleanup
                            try:
                                gen.close()  # Explicitly close generator → triggers finally blocks → releases HTTP connection immediately
                            except RuntimeError:
                                pass  # Already closed/exhausted
                            yield None  # Signal UI that stop was detected mid-stream
                            break

                        # Yield partial content for UI update (after both checks
                        # pass)
                        yield None

                    if last_output is not None:
                        # Token counts captured at streaming layer via _on_usage callback.
                        # record_llm_call_end uses ground-truth values if available, falls back to char-count estimate of last_output.
                        self._record_telemetry_event(inst_name, 'end', output_tokens_est=0, last_output=last_output)
                        break

                    # Telemetry: record LLM call end for empty response before retrying (non-blocking)
                    self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)

                except FallbackCompressionRequired as fcr:
                    # Context window exceeded during fallback to smaller endpoint.
                    # Use SMART SLICE-FIRST iterative compression: before each compression,
                    # test whether the slice fits the compressor's window. If not, halve
                    # the fraction and retest. Only compress when we know it will succeed.

                    inst_name = fcr.instance_name

                    # Get instance from pool
                    instance = self.pool.get_instance(inst_name)
                    if not instance:
                        logger.error(
                            f"[FALLBACK_COMPRESSION] Instance {inst_name} not found in pool. "
                            f"Cannot compress after context-exceeded on '{fcr.failed_endpoint}'."
                        )
                        retry_count += 1
                        continue

                    # Clear streaming responses under lock (matching existing pattern)
                    with instance._compression_lock:
                        instance._streaming_responses = []

                    logger.info(
                        f"[FALLBACK_COMPRESSION] Starting smart slice-first iterative compression "
                        f"for {inst_name} after context-exceeded on '{fcr.failed_endpoint}'."
                    )

                    agent_type = fcr.agent_type

                    for round_num in range(1, FALLBACK_COMPRESSION_MAX_ROUNDS + 1):
                        logger.debug(
                            f"[FALLBACK_COMPRESSION] === Round {round_num}/{FALLBACK_COMPRESSION_MAX_ROUNDS} "
                            f"for {inst_name} ==="
                        )

                        # Check overfeeding before each round
                        conv = self.pool.get_conversation(inst_name)
                        if not conv:
                            logger.error(f"[FALLBACK_COMPRESSION] No conversation found for {inst_name}")
                            break

                        messages = []
                        llm_messages = []
                        self._rebuild_working_set(messages, llm_messages, inst_name)

                        if not llm_messages:
                            logger.error(f"[FALLBACK_COMPRESSION] Empty working set for {inst_name}")
                            break

                        if self.compression_handler.check_overfeeding(instance, llm_messages):
                            logger.warning(
                                f"[FALLBACK_COMPRESSION] Overfeeding detected for {inst_name} "
                                f"at round {round_num}. Raising ContextWindowExceeded."
                            )
                            raise ContextWindowExceeded(
                                f"Overfeeding detected during fallback compression for {inst_name} "
                                f"(context exceeded on '{fcr.failed_endpoint}')"
                            ) from fcr

                        # Get compressor's available window (same logic as core.py lines 131-163)
                        available_for_messages = None
                        try:
                            comp_chain = self.pool.api_router.get_endpoint_chain('Compressor')
                            max_compressor_tokens = 0
                            for cfg in comp_chain:
                                ep_limit = cfg.get('max_input_tokens', 0)
                                if ep_limit and ep_limit > max_compressor_tokens:
                                    max_compressor_tokens = ep_limit

                            # Fallback: check compressor agent config directly if endpoint chain lookup fails
                            if not max_compressor_tokens:
                                comp_agent = self.pool.get_agent('Compressor')
                                if comp_agent:
                                    max_tokens = None
                                    if hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'generate_cfg'):
                                        max_tokens = comp_agent.llm.generate_cfg.get('max_input_tokens')
                                    elif hasattr(comp_agent, 'llm') and hasattr(comp_agent.llm, 'cfg'):
                                        max_tokens = comp_agent.llm.cfg.get('max_input_tokens')
                                    if max_tokens:
                                        max_compressor_tokens = max_tokens

                            if max_compressor_tokens:
                                available_for_messages = int(
                                    max_compressor_tokens * _COMPRESSOR_WINDOW_SAFETY_FACTOR
                                )
                        except Exception as e:
                            logger.debug(f"[FALLBACK_COMPRESSION] Could not determine compressor window: {e}")

                        # Get active set for slicing
                        history = self.pool.get_conversation(inst_name)
                        active_start_idx, active_set, latest_summary_idx = (
                            self.pool.get_compression_target_set_from_conversation(inst_name, history)
                        )

                        if not active_set or len(active_set) < 3:
                            logger.warning(
                                f"[FALLBACK_COMPRESSION] Active set too small ({len(active_set) if active_set else 0}) "
                                f"for safe compression at round {round_num}."
                            )
                            break

                        # Use helper to find a slice that fits the compressor window.
                        slice_result = self._find_compression_slice(
                            active_set=active_set,
                            history=history,
                            active_start_idx=active_start_idx,
                            latest_summary_idx=latest_summary_idx,
                            compressor_window=available_for_messages,
                            min_fraction=FALLBACK_COMPRESSION_MIN_SLICE_FRACTION,
                        )

                        if slice_result is None:
                            logger.error(
                                f"[FALLBACK_COMPRESSION] Could not find a slice that fits compressor window "
                                f"for {inst_name} at round {round_num}. Giving up."
                            )
                            raise ContextWindowExceeded(
                                f"Smart slicing failed for {inst_name}: no slice of active history "
                                f"fits the compressor's context window. Cannot compress further."
                            ) from fcr

                        final_fraction, discard_count, _target_messages = slice_result

                        # Invoke compression with the validated fraction.
                        # Lazy import to avoid circular dependency (see module-level comment).
                        try:
                            from agent_cascade.compression.core import compress_context as _compress_local
                        
                            result = _compress_local(
                                agent_pool=self.pool,
                                target_agent_name=inst_name,
                                fraction=final_fraction,
                                mode='auto',
                                force=True,
                            )

                            if not result.success:
                                logger.warning(
                                    f"[FALLBACK_COMPRESSION] Round {round_num} compression failed for "
                                    f"{inst_name}: {result.error}. Trying next round."
                                )
                                continue

                            # Compression succeeded — rebuild working set from compressed pool state
                            self._rebuild_working_set(messages, llm_messages, inst_name)

                            # Update instance metadata (matching execute_force_compression pattern)
                            instance.compression_summary = result.summary_text
                            conv = self.pool.get_conversation(inst_name)
                            if conv:
                                for idx, msg in enumerate(conv):
                                    c = msg_field(msg, 'content', '')
                                    if isinstance(c, str) and '<context_summary>' in c:
                                        instance.latest_marker_index = idx

                            logger.debug(
                                f"[FALLBACK_COMPRESSION] Round {round_num} succeeded for {inst_name}: "
                                f"fraction={final_fraction:.4f}, discarded {result.messages_discarded} messages, "
                                f"tokens {result.tokens_before} → {result.tokens_after}"
                            )

                            # ── Sync JSONL logger to match the compressed pool (root-cause fix) ──
                            # compress_context() rewrote instance.conversation (the pool) but does NOT
                            # touch the JSONL logger — that is the caller's responsibility. The forced
                            # path calls _sync_logger_after_compression(); the fallback path previously
                            # did not, so after a successful round the pool was compressed while the
                            # log still held the OLD large history. Any path reading back from the log
                            # (recovery at handler.py:140-149, session reload) then re-inflated the pool
                            # → context-exceeded re-fired next turn → infinite loop.
                            #
                            # Order matters (see handler._sync_logger_after_compression docstring): sync
                            # BEFORE appending the notification below — reset_history(rewrite=True) would
                            # otherwise double-log it. A sync failure must NOT abort the retry loop.
                            try:
                                # Unconditional call: self.compression_handler is always set on the engine
                                # (core.py constructor) and a None handler would be caught by this try/except.
                                self.compression_handler._sync_logger_after_compression(
                                    inst_name,
                                    instance.agent_class,
                                    "fallback compression",
                                    instance,
                                )
                            except Exception as sync_err:
                                # Best-effort: reset_history(rewrite=True) is a single non-atomic write, so on
                                # failure the JSONL may be left half-written / out of sync with the pool. We do
                                # not make it atomic (larger, higher-risk change); log loudly instead so operators
                                # know recovery/reload could re-inflate. The retry loop continues regardless.
                                logger.warning(
                                    f"[FALLBACK_COMPRESSION] Logger sync after compression FAILED for "
                                    f"{inst_name}: {sync_err}. The JSONL log may now be out of sync with the "
                                    f"compressed pool (possibly half-written) — recovery/reload could "
                                    f"re-inflate it. Continuing retry."
                                )

                            # Post-compression check: does compressed payload fit next endpoint?
                            try:
                                chain = self.pool.api_router.get_endpoint_chain(
                                    agent_type, instance_name=inst_name
                                )
                                if chain:
                                    # `or 0` tolerates a literal None (e.g. "max_input_tokens": null in config).
                                    next_limit = chain[0].get('max_input_tokens') or 0

                                    # FIX (trigger b): when the router chain reports no limit
                                    # (max_input_tokens == 0, e.g. no endpoint assigned / default cfg),
                                    # fall back to the context size dynamically detected on the agent's
                                    # LLM instance (llm/oai.py sets generate_cfg['max_input_tokens']).
                                    # This mirrors the compressor-window lookup above and avoids the old
                                    # "no limit → assume it fits" path that never verified against the
                                    # real server limit. Only when BOTH are unavailable do we assume fit.
                                    if next_limit <= 0:
                                        detected_limit = None
                                        try:
                                            _llm = getattr(instance, 'llm', None)
                                            if _llm is not None and hasattr(_llm, 'generate_cfg'):
                                                detected_limit = _llm.generate_cfg.get('max_input_tokens')
                                            elif _llm is not None and hasattr(_llm, 'cfg'):
                                                detected_limit = _llm.cfg.get('max_input_tokens')
                                        except Exception:
                                            detected_limit = None
                                        if detected_limit and detected_limit > 0:
                                            next_limit = int(detected_limit)
                                            logger.debug(
                                                f"[FALLBACK_COMPRESSION] Next endpoint has no max_input_tokens; "
                                                f"using LLM-instance detected limit {next_limit} for {inst_name}."
                                            )

                                    if next_limit > 0:
                                        # Estimate tokens of compressed payload using actual token counting
                                        estimated = 0
                                        for msg in llm_messages:
                                            content = extract_text_from_message(msg, add_upload_info=False)
                                            estimated += qwen_count(content)

                                        logger.debug(
                                            f"[FALLBACK_COMPRESSION] Post-compression check for {inst_name}: "
                                            f"estimated ~{estimated} tokens vs next endpoint limit {next_limit}"
                                        )

                                        if estimated <= next_limit * 0.95:  # 5% safety margin
                                            # Payload fits — inject notification and resume agent
                                            notif_msg = Message(
                                                role=USER,
                                                content=(
                                                    f"[SYSTEM] Context exceeded on endpoint '{fcr.failed_endpoint}'. "
                                                    f"Compression applied ({round_num} round(s)), full context preserved in JSONL log. Continue."
                                                )
                                            )
                                            self._append_and_log(instance, notif_msg)

                                            # Resume all instances (compression may have halted them)
                                            try:
                                                self.pool.resume_all_instances()
                                            except Exception:
                                                pass

                                            logger.info(
                                                f"[FALLBACK_COMPRESSION] Payload fits next endpoint after "
                                                f"{round_num} compression round(s). Resuming {inst_name}."
                                            )

                                            # Continue outer retry loop with compressed messages
                                            break
                                        else:
                                            logger.warning(
                                                f"[FALLBACK_COMPRESSION] Compressed payload (~{estimated} tokens) "
                                                f"still exceeds next endpoint limit ({next_limit}). "
                                                f"Continuing to round {round_num + 1}..."
                                            )
                                    else:
                                        # No limit from the router chain AND no detected limit on the LLM
                                        # instance — we cannot verify the payload against any real server
                                        # limit. Do NOT silently "assume it fits" here: that was exactly the
                                        # escape hatch that caused the original infinite loop (the payload
                                        # was accepted without ever being checked, then re-fired context-
                                        # exceeded on the next turn). Instead log a WARNING and do NOT break —
                                        # fall through to the outer retry loop's `continue` so the LLM call is
                                        # retried with the now-compressed payload. If it still exceeds the real
                                        # (unknown) limit, FallbackCompressionRequired re-fires and the existing
                                        # context-exceeded guard drives another compression round. This path is
                                        # therefore bounded by FALLBACK_COMPRESSION_MAX_ROUNDS — no infinite loop.
                                        logger.warning(
                                            f"[FALLBACK_COMPRESSION] Next endpoint has no max_input_tokens configured "
                                            f"and no detected limit on the LLM instance for {inst_name}. Cannot verify "
                                            f"compressed payload size; NOT assuming it fits. Retrying so the "
                                            f"context-exceeded guard re-checks against the real limit."
                                        )
                            except Exception as chain_err:
                                # Non-fatal — continue retry anyway
                                logger.debug(
                                    f"[FALLBACK_COMPRESSION] Could not verify next endpoint limit for {inst_name}: "
                                    f"{chain_err}. Continuing retry."
                                )
                                break

                        except ContextWindowExceeded:
                            raise
                        except Exception as comp_err:
                            logger.error(
                                f"[FALLBACK_COMPRESSION] Round {round_num} raised exception for {inst_name}: "
                                f"{comp_err}", exc_info=True
                            )
                            # Continue to next round

                        # After each round, check if we should trigger automatic forced compression
                        # via the normal pre-LLM checks (usage_pct > 95%). The retry loop will
                        # naturally hit _pre_llm_checks on the next iteration if needed.

                    else:
                        # Exhausted all compression rounds without success
                        logger.error(
                            f"[FALLBACK_COMPRESSION] Exhausted {FALLBACK_COMPRESSION_MAX_ROUNDS} compression rounds "
                            f"for {inst_name}. Raising ContextWindowExceeded."
                        )
                        raise ContextWindowExceeded(
                            f"Iterative compression exhausted ({FALLBACK_COMPRESSION_MAX_ROUNDS} rounds) for {inst_name}. "
                            f"Context still exceeds available endpoint limits after aggressive compression. "
                            f"Original error: context exceeded on '{fcr.failed_endpoint}'."
                        ) from fcr

                    # If we got here, compression succeeded and payload fits — continue retry loop
                    # llm_messages has been updated in-place by _rebuild_working_set
                    continue

                except Exception as e:
                    with instance._compression_lock:
                        instance._streaming_responses = []

                    # Handle AgentTerminatedError — clean abort signal from stop-checks during blocking ops.
                    # This is NOT an error; exit cleanly without retry or error message.
                    if isinstance(e, AgentTerminatedError):
                        logger.debug(f"[DISMISSAL] Aborting LLM call for {inst_name}: instance terminated")
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        break

                    # Check if this is a termination-abort error from api_router — exit cleanly without retrying.
                    _is_termination_abort = (
                        isinstance(e, RuntimeError) and 
                        len(e.args) >= 1 and 
                        e.args[0] and 
                        "has been terminated" in str(e.args[0])
                    )
                
                    if _is_termination_abort:
                        # Instance was terminated — abort LLM call cleanly without retry or error message.
                        logger.debug(f"[TERMINATION] Aborting LLM call for {inst_name} due to instance termination")
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        break

                    if retry_count > _max_attempts:
                        # Telemetry: record LLM call end for exhausted retries (non-blocking)
                        self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)
                        error_msg = str(e).split('\n')[0] if e else "Unknown error"
                        # Give clearer message for loop detection failures
                        if isinstance(e, CharacterRunDetected) and 'inner_loop_exhausted' in error_msg:
                            display_msg = f"LLM generation loop detected (exceeded {_max_attempts} max attempts)"
                        elif isinstance(e, CharacterRunDetected):
                            display_msg = f"LLM generation loop detected (tried {_max_attempts} times)"
                        elif isinstance(e, MaxTokenExceeded):
                            display_msg = f"LLM exceeded token limit (tried {_max_attempts} times)"
                        elif isinstance(e, ContextWindowExceeded):
                            display_msg = f"LLM context window exceeded (tried {_max_attempts} times)"
                        else:
                            display_msg = f"LLM call failed after {_max_attempts} retry attempts — {error_msg}"
                        logger.error(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} after {_max_attempts} retry attempts: {e}")
                        yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {display_msg}]")
                        error_already_yielded = True
                        break

                    # _abort_stream already increments retry_count before raising.
                    # For regular exceptions, increment if we haven't yet
                    # (retry_count==0).
                    if retry_count == 0:
                        retry_count += 1
                    else:
                        # Already incremented by _abort_stream OR previous round —
                        # check if we need another increment (only if _abort_stream
                        # didn't do it).
                        # We know _abort_stream incremented if the error message
                        # contains our markers.
                        if not isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
                            retry_count += 1

                        # Handle inner-loop detection: budget check and endpoint advancement
                        if isinstance(e, (CharacterRunDetected, MaxTokenExceeded, ContextWindowExceeded)):
                            self._handle_inner_loop_detection(instance, e, retry_count, loop_retry_count, _max_attempts)

                    # Classify error type using centralized policy (Phase 4a)
                    error_type = classify_error(e)

                    # Telemetry: record LLM call end for failed retry attempt before continuing (non-blocking)
                    # Skip for fatal errors — they have their own end call below
                    if error_type != 'fatal':
                        self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)

                    if error_type == 'fatal':
                        # Telemetry: record LLM call end for fatal error (non-blocking)
                        self._record_telemetry_event(inst_name, 'end', output_tokens_est=0)
                        error_msg = str(e).split('\n')[0] if e else "Unknown error"
                        logger.warning(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} with non-retryable error: {e}")
                        yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed — {error_msg}]")
                        error_already_yielded = True
                        break

                    # Calculate exponential backoff delay with jitter using centralized policy (Phase 4a)
                    backoff = calculate_backoff(retry_count, _retry_policy)

                    # Only claim "new endpoint" when cursor was actually advanced.
                    # Matches _handle_inner_loop_detection logic: character-run and max-token
                    # advance the cursor; other detections (sentence, ngram, block, entropy)
                    # retry the same endpoint. Non-inner-loop errors also retry same endpoint.
                    _det_reason = getattr(e, 'detection_reason', '')
                    advancing_endpoint = isinstance(e, (MaxTokenExceeded, ContextWindowExceeded)) or (
                        isinstance(e, CharacterRunDetected) and _det_reason.startswith('character run')
                    )
                    endpoint_str = " with new endpoint" if advancing_endpoint else ""

                    logger.warning(
                        f"[ENDPOINT_RETRY] LLM call failed for {inst_name}, retry {retry_count}/{_max_attempts}. "
                        f"Retrying in {backoff:.1f}s{endpoint_str}... Error: {e}"
                    )

                    # Signal retry to UI before blocking on sleep
                    yield self._make_retrying_message(instance, retry_count, _max_attempts, backoff)
                    time.sleep(backoff)
                    yield None

            # Final update before yielding results
            if last_output is not None:
                with instance._compression_lock:
                    self._update_streaming_responses(instance, last_output)


            if not last_output or (isinstance(last_output, list) and len(last_output) == 0):
                if not error_already_yielded:
                    yield Message(role=ASSISTANT, content="[SYSTEM ERROR: Empty LLM response]")
            else:
                for msg in last_output:
                    yield msg
        finally:
            # Turn-boundary cursor reset: the endpoint cursor is a transient
            # per-turn failover mechanism. Clear it unconditionally so the next
            # turn starts from position 0, regardless of how this turn ended.
            if hasattr(self.pool.api_router, 'reset_instance_endpoint'):
                self.pool.api_router.reset_instance_endpoint(inst_name)

    # ═══════════════════════════════════════════════════════════════════════

    def _call_llm_with_injection(
        self, instance: AgentInstance, llm_messages: List[Message]
    ) -> Iterator[Message]:
        """Delegate to retry logic — now ~15 lines.

        Extracted core logic to _execute_llm_call_with_retry() - Phase 3.6

        Args:
            instance: Agent instance making the call
            llm_messages: Messages to send to LLM

        Yields:
            Message objects from LLM response
        """
        inst_name = instance.instance_name
        template = self.pool.get_template(instance.agent_class)
        if not template:
            yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: No template for {instance.agent_class}]")
            return

        # Get active functions (tool schemas) from template
        active_functions = _get_active_functions_from_template(template, instance, pool=self.pool)

        # Delegate to extracted method - Phase 3.6
        yield from self._execute_llm_call_with_retry(instance, llm_messages, template, active_functions)


    @staticmethod
    def _build_merged_cfg(llm, instance, endpoint_cfg: dict = None) -> dict:
        """Merge config layers: template defaults → user override → endpoint sampler override.

        When an endpoint has custom sampling enabled (use_custom_sampling=True), its
        sampler parameters take final precedence over the global UI settings so that
        per-endpoint sampling values are not silently overwritten.

        When custom sampling is DISABLED for the used endpoint, stale sampling params
        from lower layers (template defaults / UI overrides) are stripped out to prevent
        them from leaking into the LLM call.
        """
        merged = {}
        if getattr(llm, 'generate_cfg', None):
            merged.update(llm.generate_cfg)              # Layer 1: template defaults
        override = getattr(instance, '_generate_cfg_override', None)
        if override is not None:
            merged.update(override)                       # Layer 2: user override

        # Inject pool-level settings that are read by LLM preprocessing but not sent to API.
        # These live in pool.llm_cfg (set via config handlers) and must be available in
        # generate_cfg for _preprocess_messages to consume them.
        pool = getattr(instance, '_pool', None) or getattr(instance, 'pool', None)
        if pool and hasattr(pool, 'llm_cfg'):
            for key in ('max_images_for_llm',):
                if key in pool.llm_cfg:
                    merged[key] = pool.llm_cfg[key]

        if endpoint_cfg:
            # Strip stale params when custom sampling disabled
            use_custom = endpoint_cfg.get('_use_custom_sampling', True)
            if not use_custom:
                merged = {k: v for k, v in merged.items() if k not in SAMPLING_AND_LIMIT_KEYS}
            merged.update(endpoint_cfg)                   # Layer 3: endpoint config (sampler params win)

        return merged


    @staticmethod
    def _store_allocated_max_input_tokens(instance, cfg: dict) -> None:
        """Store validated max_input_tokens in instance for compression tracking."""
        val = cfg.get('max_input_tokens')
        if isinstance(val, int) and val > 0:
            instance._allocated_max_input_tokens = val

    def _execute_llm_call(self, instance: AgentInstance, template, messages: List[Message], active_functions) -> Iterator[List[Message]]:
        """Execute the actual LLM API call via api_router with failover.

        Returns an iterator of List[Message] (each item is the accumulated response).
        """
        # Defensive: template.llm may be None for templates without LLM config
        llm = getattr(template, 'llm', None)
        if llm is None:
            def _empty_iter():
                yield [Message(role=ASSISTANT, content=f"[SYSTEM ERROR: Template '{getattr(template, 'name', instance.agent_class)}' has no LLM configured]")]
            return _empty_iter()

        if self.pool.api_router and hasattr(self.pool.api_router, 'call_with_fallback'):
            # Route through API router for multi-endpoint failover

            # Derive agent type once for the entire method (used by token
            # resolution + router call)
            agent_type = instance.agent_class.lower() if hasattr(instance, 'agent_class') else 'agent'

            # Dynamic endpoint selection based on agent's actual token
            # requirements
            allocated_tokens = None
            override = getattr(instance, '_generate_cfg_override', None)
            if override is not None and 'max_input_tokens' in override:
                val = override['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    allocated_tokens = val

            # Prefer live API router data over stale template config (fixes
            # max_tokens not updating on live config changes)
            if allocated_tokens is None:
                try:
                    val = self.pool.api_router.get_effective_max_tokens(agent_type)
                    if isinstance(val, int) and val > 0:
                        allocated_tokens = val

                        # Log endpoint allocation with stats for observability
                        prev_tokens = getattr(instance, '_allocated_max_input_tokens', 0)

                        # Try to get the active endpoint details from priority
                        # list
                        priority_ids = self.pool.api_router.get_agent_priorities(agent_type)
                        if priority_ids:
                            first_ep_id = priority_ids[0]
                            ep = self.pool.api_router.get_endpoint(first_ep_id)
                            if ep:
                                endpoint_info = {
                                    'endpoint': ep.name or first_ep_id,
                                    'api_base': ep.api_base,
                                    'model': ep.model,
                                    'max_input_tokens': val,
                                    'rate_limit_rpm': getattr(ep, 'rate_limit_rpm', 0),
                                    'concurrency_limit': getattr(ep, 'concurrency_limit', 0),
                                }
                                if prev_tokens != val:
                                    endpoint_info['prev_max_input_tokens'] = prev_tokens
                                    logger.info(
                                        f"Endpoint allocation updated for {agent_type}: "
                                        f"{endpoint_info}"
                                    )
                                else:
                                    pass  # Normal path — no need to log every successful resolution
                            else:
                                logger.debug(
                                    f"No endpoint found by ID '{first_ep_id}' for {agent_type}, "
                                    f"max_input_tokens={val}"
                                )
                        else:
                            logger.debug(
                                f"No priorities configured for {agent_type}, "
                                f"max_input_tokens={val}"
                            )
                except (KeyError, AttributeError, ValueError):
                    pass  # Fall through to template fallback below
                except Exception:
                    logger.warning(f"Failed to resolve max_tokens for {agent_type}, falling back to template", exc_info=True)

            # Template config as last resort (only used if no override and
            # router unavailable/empty)
            if allocated_tokens is None and getattr(llm, 'generate_cfg', None) and 'max_input_tokens' in llm.generate_cfg:
                val = llm.generate_cfg['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    allocated_tokens = val
                    logger.debug(
                        f"Template fallback for {agent_type}: "
                        f"max_input_tokens={val}"
                    )

            def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
                # Config merge priority (lowest → highest):
                #   1. Template LLM generate_cfg     – base defaults
                # 2. Per-instance override – user-specified values via UI
                # 3. Endpoint config from fallback chain – sampler params (when
                # use_custom_sampling=True) win
                merged_cfg = self._build_merged_cfg(llm, instance, endpoint_cfg=llm_cfg)
                merged_cfg['agent_name'] = template.name
# Cache endpoint config for state save/restore decisions.
                # This is the actual endpoint being used (may differ from template
                # due to fallbacks/load balancing), so state_ops uses this instead
                # of doing a fresh router lookup by agent_class.
                api_base = llm_cfg.get('api_base', '') or ''
                model = llm_cfg.get('model', '') or ''
                with instance._state_lock:
                    instance._last_endpoint_config = {
                        'api_base': api_base,
                        'model': model,
                        'state_save_enabled': llm_cfg.get('state_save_enabled', False)
                    }
                # Store allocated max_input_tokens in instance for compression
                # check (ground-truth tracking)
                self._store_allocated_max_input_tokens(instance, merged_cfg)

                # Register token count callback to capture actual token usage
                # from LLM (ground-truth tracking)
                merged_cfg['_on_token_count'] = _make_token_count_callback(instance)

                # Register usage callback to capture response tokens at streaming layer (ground-truth tracking)
                _telemetry_collector = getattr(self.pool, 'telemetry', None)  # Same pattern as self._telemetry() helper
                if _telemetry_collector is not None:
                    merged_cfg['_on_usage'] = _make_usage_callback(instance, _telemetry_collector)

                return llm.chat(
                    messages=messages,
                    functions=active_functions,
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=merged_cfg,
                )

            # Pass _do_call directly — call_with_fallback handles generator
            # lifecycle via finally blocks. Also pass instance_name so the router
            # can apply per-instance cursor rotation (kick to next endpoint).
            return self.pool.api_router.call_with_fallback(
                agent_type, _do_call, allocated_tokens=allocated_tokens,
                agent_instance_name=instance.instance_name,
            )
        else:
            # Direct call without router — same merge priority as fallback
            # path:
            merged_cfg = self._build_merged_cfg(llm, instance)  # no endpoint config layer in direct call
            merged_cfg['agent_name'] = template.name

            # Cache endpoint config from template LLM for state save/restore decisions.
            # In direct call mode there's no router to select endpoints dynamically,
            # so we use the template's configured api_base/model. State save only works
            # if this happens to be an autoloader endpoint with state_save_enabled=True.
            api_base = getattr(llm, 'api_base', '') or ''
            model = getattr(llm, 'model', '') or ''
            with instance._state_lock:
                instance._last_endpoint_config = {
                    'api_base': api_base,
                    'model': model,
                    'state_save_enabled': False  # Direct call mode doesn't support state save config from router
                }

            # Store allocated max_input_tokens in instance for compression
            # check (ground-truth tracking)
            self._store_allocated_max_input_tokens(instance, merged_cfg)

            # Register token count callback to capture actual token usage from
            # LLM (ground-truth tracking)
            merged_cfg['_on_token_count'] = _make_token_count_callback(instance)

            # Register usage callback to capture response tokens at streaming layer (ground-truth tracking)
            _telemetry_collector = getattr(self.pool, 'telemetry', None)  # Same pattern as self._telemetry() helper
            if _telemetry_collector is not None:
                merged_cfg['_on_usage'] = _make_usage_callback(instance, _telemetry_collector)

            return llm.chat(
                messages=messages,
                functions=active_functions,
                stream=True,
                delta_stream=False,
                extra_generate_cfg=merged_cfg,
            )
