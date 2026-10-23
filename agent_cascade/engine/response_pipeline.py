"""Response normalization, auto-continue, tool handoff, and JSONL sync (Phase 4 split).

``ResponsePipelineMixin`` owns the response-processing cluster moved from
:mod:`agent_cascade.engine.core`. ``_process_response`` now delegates the
continue-merge and the truncation/retry branches to the private helpers below.
"""

from __future__ import annotations

import copy
from typing import List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.helpers import (_check_message_truncation, _extract_text_content,
                                          _is_incomplete_state, _normalize_gemma_thought_tags,
                                          _normalize_thinking_blocks)
from agent_cascade.engine.telegram_stream import _tg_stream_push
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, Message
from agent_cascade.log import logger
from agent_cascade.settings import (MAX_AUTO_CONTINUE_ATTEMPTS, REASONING_ONLY_CONTINUE_ATTEMPTS,
                                    SOFT_CONTINUE_NUDGE_ENABLED)
from agent_cascade.skills.selector import classify_completion
from agent_cascade.utils.utils import extract_text_from_message, msg_field, msg_set


def _last_assistant_text(turn_output: List[Message]) -> str:
    """Return the last assistant message's text content, ``''`` if there is none.

    Deliberately NOT reusing _is_incomplete_state: that walks the whole list to classify a
    shape, this only extracts the text the classifier needs to read.
    """
    for msg in reversed(turn_output):
        if msg_field(msg, 'role', '') != ASSISTANT:
            continue
        return _extract_text_content(msg)
    return ''


class ResponsePipelineMixin:
    """Normalize responses, handle auto-continue/truncation, and sync logs."""

    def _update_streaming_responses(self, instance: AgentInstance, last_output: List[Message]):
        """Update streaming responses only when content actually changes (performance optimization).

        Compares both message count AND content to detect meaningful changes.
        This prevents unnecessary deep copies while ensuring UI gets fresh data.

        Args:
            instance: The AgentInstance whose _streaming_responses to update
            last_output: The accumulated LLM output (list of Messages)
        """
        if last_output is None or len(last_output) == 0:
            return

        # Check if we need to update by comparing message count and content
        needs_update = False

        if len(last_output) != len(instance._streaming_responses):
            # Message count changed — definitely need update
            needs_update = True
        elif len(last_output) == len(instance._streaming_responses) and len(last_output) > 0:
            # Count is same — check if any message content changed
            for old_msg, new_msg in zip(instance._streaming_responses, last_output):
                # FIX: Also check reasoning_content and function_call to catch
                # all changes. extra.function_id is compared too so a tool-call batch whose
                # identity changed (e.g. an id assigned after the first delta, or a merge/
                # re-split of parallel calls) is always detected even when name+args are equal.
                if (getattr(old_msg, 'content', None) != getattr(new_msg, 'content', None) or
                        getattr(old_msg, 'reasoning_content', None) != getattr(new_msg, 'reasoning_content', None) or
                        getattr(old_msg, 'function_call', None) != getattr(new_msg, 'function_call', None) or
                        (getattr(old_msg, 'extra', None) or {}).get('function_id') !=
                        (getattr(new_msg, 'extra', None) or {}).get('function_id')):
                    needs_update = True
                    break

        if needs_update:
            instance._streaming_responses = copy.deepcopy(last_output)

    def _make_retrying_message(self,
                               instance: AgentInstance,
                               attempt: int,
                               max_retries: int,
                               delay: float,
                               error=None) -> Message:
        """Create [RETRYING] notification message for UI.

        Extracted from _call_llm_with_injection() - Phase 3.6

        Args:
            instance: Agent instance
            attempt: Current retry attempt number
            max_retries: Maximum retries allowed
            delay: Seconds until next retry
            error: Optional exception that triggered the retry. When provided, the message
                names the actual cause (e.g. "server error (HTTP 502)") via
                classify_endpoint_failure; when None (legacy/other callers) it keeps the exact
                original text "Connection lost" (plan §3.5).

        Returns:
            Transient Message object (not added to conversation history)
        """
        from agent_cascade.error_reporting import classify_endpoint_failure
        reason = classify_endpoint_failure(error) if error is not None else 'Connection lost'
        return Message(role=ASSISTANT,
                       content=f"[RETRYING] {reason}, retrying ({attempt}/{max_retries}) in {delay:.1f}s...")

    def _normalize_turn_output(self, turn_output: List[Message]) -> None:
        """Normalize messages in-place (Gemma tags, thinking blocks).

        Normalizes each message in turn_output by:
        - Removing Gemma thought tags
        - Stripping thinking blocks from reasoning_content
        - Cleaning thinking blocks from function call arguments

        Args:
            turn_output: List of messages to normalize (modified in-place)

        Note:
            This method only normalizes. Truncation detection is now handled
            separately by _check_message_truncation() called before normalization.
        """
        for msg in turn_output:
            # P4: Gemma thought tag normalization — prevent history pollution
            _normalize_gemma_thought_tags(msg)

            # Strip thinking blocks from reasoning_content to prevent tag
            # pollution in history
            reasoning_content = msg_field(msg, 'reasoning_content')
            if isinstance(reasoning_content, str):
                msg_set(msg, 'reasoning_content', _normalize_thinking_blocks(reasoning_content))

    def _log_messages_to_jsonl(self, instance: AgentInstance, inst_name: str, turn_output: List[Message]) -> None:
        """Persist messages to JSONL log file.

        Single clean pass: compare logger history length with conversation length,
        then log only the delta (new messages not yet in the log). Treats ALL message
        types uniformly — no special cases for FUNCTION role.

        Design principle: Logging is a count-based delta sync. The logger's data["history"]
        list should always match instance.conversation in both order and content.
        Any message added to conv via append_message will
        be picked up by this delta on the next call.

        Args:
            instance: AgentInstance for logger lookup and conversation access
            inst_name: Instance name for logging
            turn_output: Messages from LLM to log (always logged regardless of conv sync)
        """
        log_inst = self.pool.get_logger(inst_name, instance.agent_class)

        already_logged_count = len(log_inst.data.get('history', []))
        with instance._compression_lock:
            conv = list(instance.conversation)

        # Log delta: any messages in conv that aren't yet in the logger
        # history.
        # This covers ALL message types uniformly (system, user, assistant,
        # function).
        # turn_output is already in conv by the time this runs (appended before
        # logging),
        # so it's naturally included in the delta — no separate loop needed.
        try:
            wrote_any = False
            if already_logged_count < len(conv):
                for msg in conv[already_logged_count:]:
                    # Check both text content and function_call to avoid
                    # skipping
                    # assistant messages that have tool calls but empty
                    # content.
                    # Skipping such messages breaks the count-based delta sync,
                    # causing duplicate entries on the next logging pass.
                    has_content = bool(str(msg_field(msg, 'content', '')).strip())
                    has_function_call = bool(msg_field(msg, 'function_call'))
                    if has_content or has_function_call:
                        log_inst.log_message(msg)
                        wrote_any = True

            # ── Tail sync check after write (design doc §5.2 — D1 fix) ──
            # Lightweight length-only verification that pool tail matches JSONL
            # tail.
            if wrote_any and getattr(self.pool.settings, 'tail_sync_check_enabled', True):
                from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
                _check_tail(inst_name, conv, log_inst.log_path, context='log_messages')
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")

    def _check_and_handle_truncation(self, is_truncated: bool, turn_output: List[Message], instance: AgentInstance,
                                     inst_name: str, messages: List[Message], llm_messages: List[Message],
                                     response: List[Message]) -> bool:
        """Check for truncation or incomplete state and inject a continue message.

        Args:
            is_truncated: Pre-computed truncation flag from caller.
            turn_output: Messages from LLM.
            instance: AgentInstance for conversation access and state tracking.
            inst_name: Instance name for logging and halt checks.
            messages: Full message set to append continue message.
            llm_messages: LLM-formatted message set to append continue message.
            response: Response buffer to clear on rollback.

        Returns:
            True if truncation or incomplete state detected and continue injected,
            False otherwise.
        """
        is_incomplete = _is_incomplete_state(turn_output)
        # BUG_0048: a text-bearing, tool-call-free turn is structurally ambiguous - the
        # structural detector cannot tell "final answer" from "announced more work".
        # Ask the decision model; an 'incomplete' verdict becomes an opaque tag that lands
        # in the EXISTING generic retry path below (rollback + rebuild + clear). Gate on BOTH
        # toggles before the call so either being off costs zero HTTP and is byte-identical
        # to the pre-fix path. getattr default True: the feature is on by default.
        if (is_incomplete is None and self.pool.settings.auto_continue
                and getattr(self.pool.settings, 'completion_classifier_enabled', True)):
            verdict = classify_completion(self.pool, _last_assistant_text(turn_output))
            if verdict == 'incomplete':
                is_incomplete = 'text-only-incomplete'
        if (is_truncated or
                is_incomplete) and not self._is_terminal_stop(inst_name) and self.pool.settings.auto_continue:
            instance._auto_continue_count = getattr(instance, '_auto_continue_count', 0) + 1
            if instance._auto_continue_count >= MAX_AUTO_CONTINUE_ATTEMPTS:
                # cap-hit reset (site a): clear ALL counters, give up entirely
                self._reset_auto_continue_state(instance)
                return False

            # NEW: reasoning-only gets a SOFT continue first (up to N times). Guarded by the
            # BUDGET counter, which never resets mid-episode, so once we fall through to full
            # retry this path is permanently closed for the episode (N3).
            if is_incomplete == 'reasoning-only' and instance._reasoning_only_soft_attempts < REASONING_ONLY_CONTINUE_ATTEMPTS:
                return self._handle_reasoning_only_soft_continue(instance, inst_name, messages, llm_messages, response)

            # existing full-retry path (truncation / broken-json / empty-output / reasoning-only after N soft tries)
            return self._handle_full_auto_continue_retry(instance, inst_name, is_truncated, is_incomplete,
                                                         turn_output, messages, llm_messages, response)

        # normal completion -> reset both reasoning-only counters (site b)
        self._reset_auto_continue_state(instance)
        return False

    def _handle_reasoning_only_soft_continue(self, instance: AgentInstance, inst_name: str,
                                             messages: List[Message], llm_messages: List[Message],
                                             response: List[Message]) -> bool:
        """Soft-continue branch: resend the same history, optionally with an escalating nudge."""
        instance._reasoning_only_soft_attempts += 1  # budget: how many soft tries this episode
        reason = f"incomplete state (reasoning-only, soft continue {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS})"
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_auto_continue(inst_name, reason=reason)
            except Exception:
                pass
        logger.info(
            f"Detected incomplete state (reasoning-only) for {inst_name}. Soft continue attempt {instance._reasoning_only_soft_attempts}/{REASONING_ONLY_CONTINUE_ATTEMPTS}."
        )
        # Default: pure resend - re-call on the SAME history (reasoning msg stays in place); nothing is popped or appended.
        if SOFT_CONTINUE_NUDGE_ENABLED:
            # (Deferred feature - OFF by default.) Inject an escalating USER nudge so the
            # model gets an explicit instruction instead of a bare resend. When enabled, a
            # nudge is now in context, so track it for rollback accounting on the later full retry.
            instance._reasoning_only_pending_nudges += 1
            self._inject_soft_continue_nudge(instance, inst_name, messages, llm_messages, response)
        return True

    def _handle_full_auto_continue_retry(self, instance: AgentInstance, inst_name: str, is_truncated: bool,
                                         is_incomplete, turn_output: List[Message], messages: List[Message],
                                         llm_messages: List[Message], response: List[Message]) -> bool:
        """Full-retry branch: roll back the incomplete turn, rebuild, and re-call."""
        reason = 'truncation' if is_truncated else f"incomplete state ({is_incomplete})"
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_auto_continue(inst_name, reason=reason)
            except Exception:
                pass
        logger.info(f"Detected {reason} for {inst_name}. Auto-continuing.")
        pop_count = len(turn_output)
        if getattr(instance, '_continue_fallback_append', False):
            pop_count += 1
        # F1 fix: for a reasoning-only full retry, also remove the soft-continue nudges injected
        # this episode (they live in conversation/llm_messages/response, NOT in turn_output).
        # Guarded to reasoning-only so a broken-json/empty-output/truncation retry never pops
        # unrelated nudges (N2); with nudge OFF the counter is 0, so this is a no-op.
        if is_incomplete == 'reasoning-only':
            pop_count += instance._reasoning_only_pending_nudges
        if pop_count > 0:
            self.pool._rollback_instance(inst_name, pop_count=pop_count)
            self._rebuild_working_set(messages, llm_messages, inst_name)
            response.clear()
        # N1 fix: the nudges were just popped above, so they no longer exist - zero ONLY the
        # pending-nudge counter. Do NOT touch _reasoning_only_soft_attempts (it must stay at N
        # so the soft path stays closed).
        instance._reasoning_only_pending_nudges = 0
        return True

    def _reset_auto_continue_state(self, instance: AgentInstance) -> None:
        """Clear all auto-continue episode counters (cap-hit and normal completion)."""
        instance._auto_continue_count = 0
        instance._continue_fallback_append = False
        instance._reasoning_only_soft_attempts = 0
        instance._reasoning_only_pending_nudges = 0

    @staticmethod
    def _reasoning_only_continue_text(attempt: int) -> str:
        """Deterministic USER nudge text for a reasoning-only soft continue (F4).

        Escalates on the second+ attempt so an identical repeat doesn't just echo back
        reasoning. Function of ``attempt`` only, keeping tests stable. Only used when
        SOFT_CONTINUE_NUDGE_ENABLED is True.

        # attempt=1   -> "Your last turn contained only reasoning/thinking and no output or tool call. ..."
        # attempt>=2  -> "You have produced reasoning-only output again with no visible result. STOP thinking ..."
        """
        if attempt >= 2:
            return ('You have produced reasoning-only output again with no visible result. '
                    'STOP thinking and MUST produce either a final answer or a concrete tool call '
                    'on this turn — do not emit another reasoning-only message.')
        return ('Your last turn contained only reasoning/thinking and no output or tool call. '
                'Please continue — produce your response text or make the next tool call now.')

    def _tail_sync_conversation(self, instance: AgentInstance, inst_name: str, context: str) -> None:
        """Run the best-effort tail-sync JSONL check after a conversation append.

        Callers own activity marking and exception handling; this helper only performs
        the optional tail-sync check so the shape lives in one place.
        """
        if not getattr(self.pool.settings, 'tail_sync_check_enabled', True):
            return
        from agent_cascade.logger.tail_sync_check import check_and_log as _check_tail
        with instance._compression_lock:
            conv = instance.conversation
        log_inst = self.pool.get_logger(inst_name, instance.agent_class)
        _check_tail(inst_name, conv, log_inst.log_path, context=context)

    def _sync_conversation_log(self, instance: AgentInstance, inst_name: str, context: str) -> None:
        """Mark activity + run the tail-sync JSONL check after a conversation append (house pattern).

        Keeps the JSONL log in sync with ``instance.conversation`` on any path that appends to it.
        Best-effort: failures are logged at debug level and never propagate.
        """
        try:
            self.pool._mark_activity(inst_name)
            self._tail_sync_conversation(instance, inst_name, context)
        except Exception as e:
            logger.debug(f"Conversation log sync failed for {inst_name} (non-critical): {e}")

    def _inject_soft_continue_nudge(self, instance: AgentInstance, inst_name: str, messages: List[Message],
                                    llm_messages: List[Message], response: List[Message]) -> None:
        """Inject an escalating USER nudge for a reasoning-only soft continue (F3).

        Only called when SOFT_CONTINUE_NUDGE_ENABLED is True. Mirrors the urgent-message
        injection path (``_drain_and_inject``) so it is thread-safe and keeps all working
        lists in sync: appends to messages + llm_messages + response, then conversation +
        JSONL log atomically under ``instance._compression_lock``. Does NOT roll back or
        clear the response — the reasoning message stays in place; the next LLM call sees
        the reasoning-only assistant message followed by this USER nudge.
        """
        n = instance._reasoning_only_soft_attempts  # 1-based attempt number (for escalating text)
        text = self._reasoning_only_continue_text(n)
        msg = self._make_user_message(text)
        with instance._compression_lock:
            messages.append(msg)  # full working set
            response.append(msg)  # local accumulator (UI visibility)
            self._append_and_log_to_llm(instance, msg, llm_messages, lock_held=True)  # conversation + JSONL log + LLM view (exactly once)
        # Keep the JSONL log in sync with the appended nudge (house pattern).
        self._sync_conversation_log(instance, inst_name, 'reasoning_soft_continue')

    def _merge_continue_saved_message(self, instance: AgentInstance, turn_output: List[Message],
                                      inst_name: str) -> None:
        """Merge the manually saved Continue message into the new assistant turn."""
        if instance._continue_saved_msg is not None:
            with instance._compression_lock:
                saved = instance._continue_saved_msg
                # Clear immediately under lock to prevent another thread from
                # setting it again
                instance._continue_saved_msg = None

            if saved:
                # Find the last assistant message in turn_output (most recent
                # generation) and merge content
                merged = False
                for msg in reversed(turn_output):
                    role = msg_field(msg, 'role', '')
                    if role == ASSISTANT:
                        old_content = msg_field(saved, 'content', '') or ''
                        new_content = msg_field(msg, 'content', '') or ''
                        merged_content = old_content + new_content

                        # Update the message with merged content (handle both
                        # dict and Message object)
                        msg_set(msg, 'content', merged_content)

                        logger.debug(
                            f"[CONTINUE_FIX] Merged continue-saved assistant message ({len(old_content)} chars) with new response ({len(new_content)} chars)"
                        )
                        merged = True
                        break

                if not merged:
                    # Fallback: saved message was popped from conversation by
                    # continue handler.
                    # If we can't merge it, re-append it to prevent data loss.
                    logger.warning(f"[CONTINUE_FIX] Could not merge continue-saved message for {inst_name}: "
                                   f"no assistant message found in turn_output. Re-appending as separate message.")
                    self._append_and_log(instance, saved)
                    instance._continue_fallback_append = True

    def _process_response(self, instance: AgentInstance, turn_output: List[Message], messages: List[Message],
                          llm_messages: List[Message], response: List[Message]) -> bool:
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.

        Returns True if processing should continue to next iteration (tool was used or truncated).
        """
        inst_name = instance.instance_name

        # FIX
        is_truncated = any(_check_message_truncation(msg) for msg in turn_output)

        # Extracted to _normalize_turn_output() - Phase 3.3 (FIX
        self._normalize_turn_output(turn_output)

        # Commit + clear ATOMIC under _compression_lock so no concurrent serialization can
        # observe "committed but not cleared" (the commit-race window that let a stale
        # in-flight partial be appended as a near-duplicate alongside the committed final).
        # _append_and_log_batch(lock_held=True) reuses this lock (RLock, reentrant) and logs
        # each msg exactly once - identical logging to the previous non-locked call.
        with instance._compression_lock:
            self._append_and_log_batch(instance, turn_output, lock_held=True)
            response.extend(turn_output)  # Separate list for streaming/accumulation
            # Streaming UI Content Update Fix: Clear _streaming_responses in the SAME locked
            # section as the commit above so the two are atomic w.r.t. serialization.
            instance._streaming_responses = []

        # TG-STREAM F1: push the run's FIRST text output at commit time -
        # the turn is now durably committed, so its assistant text is safe to deliver.
        # Best-effort (never raises); no-op for sub-agents / tool-call-only turns.
        _tg_stream_push(instance, turn_output, self.pool)

        # FIX: Option B - Merge continue-saved assistant message if present.
        # When Continue is clicked, the last assistant message was popped from
        # conversation in api_server.py continue handler and stored as _continue_saved_msg.
        # We now merge it with the newly generated assistant message to create a single
        # concatenated message.
        self._merge_continue_saved_message(instance, turn_output, inst_name)

        # Extracted to _check_and_handle_truncation() - Phase 3.3
        if self._check_and_handle_truncation(is_truncated, turn_output, instance, inst_name, messages, llm_messages,
                                             response):
            return True  # Continue to next LLM call

        # Extracted to _execute_detected_tools() - Phase 3.3
        used_any_tool = self._execute_detected_tools(instance, inst_name, turn_output, messages, llm_messages, response)

        # Log ALL messages (turn_output + fn_msgs from tools) in a single delta
        # pass.
        # Called AFTER tool execution so FUNCTION results are already in conv
        # and get picked up by the count-based delta sync - no risk of duplicate
        # logging.
        self._log_messages_to_jsonl(instance, inst_name, turn_output)

        # -- Post-tool urgent injection -----------------------------------
        # Inject urgent messages AFTER all tools complete to avoid orphaned
        # tool_call_id's
        if self._drain_and_inject(
                instance,
                inst_name,
                messages,
                llm_messages,
                response,
        ):
            return True  # Continue to next LLM call for urgent message processing

        # FIX
        # the turn is complete (no continue message injected), so return False
        # to avoid infinite loop.
        return used_any_tool

    def _check_for_tool_calls_in_output(self, instance: AgentInstance, response: List[Message]) -> bool:
        """Scan last assistant messages for unexecuted tool calls.

        Extracted from _post_turn_checks() - Phase 3.9

        If tool calls found, they will be executed in the next turn loop iteration.

        Args:
            instance: Current agent instance
            response: Accumulated response messages

        Returns:
            True if tool calls were found (continue looping), False if no more tools to execute.
        """
        # Check if last assistant message had a tool call (still working)
        has_tool_call = False
        # Collect all executed tool names from FUNCTION results (lowercase for case-insensitive comparison)
        executed_tools = set()
        for msg in response:
            if msg_field(msg, 'role', '') == FUNCTION:
                name = msg_field(msg, 'name', '')
                if name:
                    executed_tools.add(name.lower())

        for idx in range(len(response) - 1, -1, -1):
            msg = response[idx]
            role = msg_field(msg, 'role', '')
            if role == ASSISTANT:
                fc = msg_field(msg, 'function_call')
                if fc is not None:
                    has_tool_call = True
                else:
                    # Also scan reasoning_content and content for embedded tool calls
                    use_tool, tool_name, tool_args, _ = self._detect_tool(msg)
                    if use_tool and tool_name.lower() not in executed_tools:
                        has_tool_call = True
                break

        return has_tool_call

    def _detect_pure_thinking_turn(self, instance: AgentInstance, response: List[Message]) -> bool:
        """Check if last turn was reasoning-only without real content.

        Extracted from _post_turn_checks() - Phase 3.9

        Detects when the LLM produces only thinking blocks with no substantive output,
        which indicates a stalled agent that should be interrupted.

        Args:
            instance: Current agent instance
            response: Accumulated response messages

        Returns:
            True if pure thinking detected (should break), False otherwise.
        """
        inst_name = instance.instance_name

        # Check for real content vs pure thinking
        last_msgs = [m for m in response[-3:] if m.get('role') != FUNCTION]
        has_real_content = any(
            extract_text_from_message(m, add_upload_info=False).strip()
            for m in last_msgs
            if (m.get('role') == ASSISTANT or getattr(m, 'role', '') == ASSISTANT))

        has_thinking = any(m.get('thought') or m.get('reasoning_content') for m in response[-3:])

        # Pure thinking turn — continue to next turn
        if not has_real_content and has_thinking:
            logger.info(f"Pure reasoning turn detected for {inst_name}. Continuing.")
            return True

        return False

    def _is_genuine_completion(self, instance: AgentInstance, response: List[Message]) -> bool:
        """True only when a Phase-5 break is a GENUINE natural completion (a real final
        answer), as opposed to an excluded exit.

        ``_post_turn_checks`` returns False via four distinct paths that all collapse into
        the same ``if not completed:`` in run(): terminal stop, terminal stop during the
        compression wait, pure-thinking stall, and genuine completion. The auto-skill
        reflection must fire only on the last one — a stopped or stalled agent has not
        delivered a real answer to reflect on. This helper mirrors exactly the two
        predicates ``_post_turn_checks`` uses for its non-completion break paths
        (``_is_terminal_stop`` and ``_detect_pure_thinking_turn``) so the gating cannot
        drift from the method it guards.

        Args:
            instance: The agent being executed.
            response: Response list from the just-finished turn.

        Returns:
            True only when NOT terminal-stop AND NOT a pure-thinking stall.
        """
        if self._is_terminal_stop(instance.instance_name):
            return False  # terminal stop (or stop during compression wait) — excluded
        if self._detect_pure_thinking_turn(instance, response):
            return False  # pure reasoning turn — agent stalled, not completed
        return True

    def _drain_post_generation_messages(self, instance: AgentInstance, inst_name: str, messages: List[Message],
                                        llm_messages: List[Message], response: List[Message]) -> bool:
        """Drain queued messages that arrived after turn completion.

        Extracted from _post_turn_checks() - Phase 3.9

        Also performs safety drain for race conditions.

        Args:
            instance: Current agent instance
            inst_name: Instance name for logging
            messages, llm_messages, response: Working message sets

        Returns:
            True if any messages were drained (continue looping), False otherwise.
        """
        # Post-generation queue drain
        if self.pool.has_messages(inst_name):
            logger.info(f"Queued messages for {inst_name} after turn completion. Looping back.")
            return True  # Loop back to process injected messages

        # Safety drain: catch any messages from fast-completing children that
        # completed between register_async_call() and the has_pending() check above
        try:
            if self._drain_and_inject(
                    instance,
                    inst_name,
                    messages,
                    llm_messages,
                    response,
            ):
                return True  # Continue loop to process drained results
        except Exception as e:
            logger.error('Safety drain failed for %s: %s', inst_name, e)

        return False
