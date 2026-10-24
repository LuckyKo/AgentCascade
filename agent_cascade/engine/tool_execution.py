"""Tool dispatch / execution cluster for the execution engine (Phase 1 module-split).

``ToolExecMixin`` holds the tool-execution and image-handling methods. Method bodies
are moved VERBATIM from ``agent_cascade/execution_engine.py``; only the class wrapper
and the imports needed by these methods were added. The mixin is composed into
``ExecutionEngine`` in :mod:`agent_cascade.engine.core`.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import List, Optional

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.error_reporting import TB_DEDUP, format_crash
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.llm.schema import FUNCTION, Message
from agent_cascade.log import logger
from agent_cascade.operation_manager import clear_current_instance_name, set_current_instance_name
from agent_cascade.settings import DEFAULT_TOOL_RESULT_MAX_CHARS, DEFAULT_WILD_READ_TRUNCATION_CHARS
from agent_cascade.utils.pool_validation import validate_message_pool


class ToolExecMixin:
    """Mixin providing tool-execution and image-handling methods."""

    def _execute_detected_tools(self, instance: AgentInstance, inst_name: str, turn_output: List[Message],
                                messages: List[Message], llm_messages: List[Message], response: List[Message]) -> bool:
        """Execute tools detected in turn output.

        Scans turn_output for tool calls, executes them with telemetry tracking,
        handles truncation and error detection, adds FUNCTION result messages to
        all working sets, and handles orphaned tool calls from early breaks.

        Args:
            instance: AgentInstance for execution context
            inst_name: Instance name for logging and halt checks
            turn_output: Messages to scan for tool calls
            messages: Full message set to append FUNCTION results
            llm_messages: LLM-formatted message set to append FUNCTION results
            response: Response list for streaming to UI

        Returns:
            True if any tools were executed, False otherwise
        """
        used_any_tool = False
        executed_tools = []  # Track which tools were actually executed (for orphan handling)

        # ── Resolve tool policy once (template, disabled tools, function map) ────────
        # Outside the loop. Template and disabled tool list don't change during the loop.
        # Shared with orphan handling via _resolve_tool_policy.
        _primary_template, _primary_disabled_tools, _primary_function_map = self._resolve_tool_policy(instance)

        # ── Parallel-call_agent / media_gen batch detection (single source of truth) ───────
        # A parent may issue several call_agent (or media_gen) tool calls in ONE turn_output.
        # Their parent context is identical across the batch (no LLM generation between tool
        # calls), so only the FIRST needs to arm the parent save and only the LAST needs to swap
        # the parent model back in. turn_output is the complete, ordered tool-call list for this
        # turn, already in memory, so the batch structure is known STATICALLY — no runtime
        # in-flight counter. _scan_tool_batch_indices uses the SAME _detect_tool the loop uses
        # so the detector cannot diverge from what actually runs. is_first/is_last flags are
        # threaded per-call (never shared instance state) so nested/parallel agents stay correct.
        ca_indices, mg_indices = self._scan_tool_batch_indices(turn_output)

        # Whether any call_agent actually ran in this batch — gates the safety net below so it never
        # fires for a batch that contained no call_agent (nothing was ever deferred).
        ran_ca = False
        # Control-flow marker (NOT a deferral flag): did the loop reach and process the batch's LAST
        # call_agent? If it did, the correct end-of-batch handling already happened (a SYNC last child ran
        # its full restore; an ASYNC last child intentionally leaves the label to self-heal at the next
        # _setup_turn). The safety net must therefore fire ONLY when this stayed False — i.e. the loop exited
        # early (terminal-stop break / AgentTerminatedError raise) before the last call_agent.
        reached_last_ca = False
        # Media-gen counterparts (same polarity as the call_agent markers above).
        ran_mg = False
        reached_last_mg = False

        # The whole loop is wrapped in try/finally so the deferred restore is guaranteed to run on
        # EVERY exit path — including the terminal-stop break and the AgentTerminatedError raise, both
        # of which bypass the orphan-handling block that follows the loop.
        try:
            for _out_idx, out in enumerate(turn_output):
                use_tool, tool_name, tool_args, _ = self._detect_tool(out)
                if not use_tool:
                    continue

                # Cooperatively wait if paused — don't skip tool execution, just wait
                while self.pool.is_paused():
                    if self._is_terminal_stop(inst_name):
                        break  # Terminal stop — exit pause-wait, will break at next _is_stopped check
                    self.pool.wait_if_paused(timeout=1.0)

                # Stop/halt check BEFORE tool execution (check before setting
                # used_any_tool)
                if self._is_terminal_stop(inst_name):
                    break
                elif self._is_suspended_by_compression(inst_name):
                    # Compression-halt is a suspension, not termination — wait, then retry this tool.
                    logger.debug('tool exec suspended by compression - %s', inst_name)
                    if not self._wait_for_compression_to_clear(inst_name):
                        break  # Terminal stop during wait
                    continue  # Resumed, re-enter tool dispatch loop with current tool

                # Additional check: raise AgentTerminatedError if this instance was terminated.
                # This allows sync children to propagate the signal up to their parent rather
                # than completing long-running tools after termination.
                inst = self.pool.get_instance(inst_name)
                if inst and inst.is_terminated:
                    raise AgentTerminatedError(inst_name)

                # ── Disabled/Inexistent Tool Auto-Deny
                # ────────────────────────────
                # Defense-in-depth: check if tool is disabled BEFORE execution.
                # Disabled tools are still in function_map (only filtered from
                # active functions sent to LLM). If an agent generates a call for a
                # disabled tool, it gets auto-denied here.
                deny_reason = self._auto_deny_reason(
                    _primary_template, _primary_disabled_tools, _primary_function_map, tool_name)
                if deny_reason is not None:
                    self._auto_deny_tool_call(
                        instance, inst_name, out, tool_name, deny_reason, response, executed_tools)

                    used_any_tool = True
                    continue  # Skip actual tool execution

                used_any_tool = True

                # Track tool success/failure — needed for function_id matching and
                # frontend isToolFailure()
                _tool_success = True
                _tool_error = ''

                # Telemetry: record tool call start (non-blocking)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_tool_call_start(inst_name, tool_name)
                    except Exception:
                        pass

                # Extract function_id from the assistant message that had the tool
                # call BEFORE executing
                # This is critical — without it, the LLM API can't match tool
                # results to tool calls
                extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                function_id = extra_data.get('function_id')

                # P8-4: bind tool_result before the try. The finally block references tool_result
                # unconditionally (the _postprocess_tool_result call in the finally); on the AgentTerminatedError re-raise
                # path execute_tool never binds it, so an UnboundLocalError there would mask the
                # AgentTerminatedError. An empty string is a safe, post-processable default.
                tool_result = ''

                try:
                    # Set current instance name in thread-local for _resolve_path
                    # warnings
                    set_current_instance_name(inst_name)

                    try:
                        # Phase 4.3: Delegate to ToolDispatcher
                        # Parallel-call_agent batch flags: for a call_agent, derive its batch position from
                        # the pre-scanned ca_indices (single source of truth — never recomputed elsewhere).
                        # A single call_agent → both True → byte-identical to today. Non-call_agent tools get
                        # the safe defaults (True/True) so their behavior is unchanged.
                        # Parallel-media_gen batch flags: same static batch detection via mg_indices. A single
                        # media_gen → both True → byte-identical to today; non-media_gen tools get True/True.
                        if tool_name == 'call_agent':
                            ran_ca = True
                            is_first_ca, is_last_ca = self._batch_position(_out_idx, ca_indices)
                            if is_last_ca:
                                reached_last_ca = True
                            is_first_vram = is_last_vram = True
                        elif tool_name == 'media_gen':
                            ran_mg = True
                            is_first_vram, is_last_vram = self._batch_position(_out_idx, mg_indices)
                            if is_last_vram:
                                reached_last_mg = True
                            is_first_ca = is_last_ca = True
                        else:
                            is_first_ca = is_last_ca = True
                            is_first_vram = is_last_vram = True
                        tool_result = self.tool_dispatcher.execute_tool(instance,
                                                                        tool_name,
                                                                        tool_args,
                                                                        llm_messages,
                                                                        function_id=function_id,
                                                                        is_first_ca=is_first_ca,
                                                                        is_last_ca=is_last_ca,
                                                                        is_first_vram=is_first_vram,
                                                                        is_last_vram=is_last_vram)
                    except AgentTerminatedError:
                        # Clean abort from a stop-check during tool execution. Record the termination
                        # in telemetry (success=False, error='terminated') so the finally's
                        # record_tool_call_end reflects a failed call rather than a spurious success,
                        # then re-raise for the caller.
                        _tool_success = False
                        _tool_error = 'terminated'
                        raise
                    except Exception as e:
                        # R3: present dispatcher/plumbing crashes with the SAME shape as a tool crash
                        # (agent.py:_call_tool) — one compact root-cause line at ERROR, full traceback
                        # demoted to DEBUG and deduped per (root type+msg). The LLM-facing result stays
                        # compact (no multi-frame stack) for consistency with the tool-crash path.
                        tool_result = self._format_tool_dispatch_error(e, tool_name, inst_name)
                        _tool_success = False
                        _tool_error = str(e)
                    # Cache full output (if exceeds threshold)
                    if isinstance(tool_result, str):
                        self._cache_tool_output(inst_name,
                                                tool_name,
                                                tool_result,
                                                threshold=self.pool.settings.cache_threshold_chars)

                    # ── Post-execution success detection
                    # ────────────────────────────────
                    # Many tools return an error message as a string instead of
                    # raising an exception.
                    # NOTE: This uses first-line heuristics — false positives are
                    # possible for tools
                    # that return structured error-like output. Only affects
                    # telemetry metrics and
                    # frontend tool status display, not execution flow.
                    if _tool_success and isinstance(tool_result, str):
                        _tool_success, _tool_error = self._detect_tool_result_failure(tool_result)

                finally:
                    # Telemetry: record tool call end (non-blocking, always called)
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_tool_call_end(
                                inst_name,
                                tool_name,
                                success=_tool_success,
                                result_chars=len(tool_result) if isinstance(tool_result, str) else 0,
                                error=_tool_error,
                                is_call_agent=(tool_name == 'call_agent'),
                            )
                        except Exception:
                            pass

                    # Assemble final tool result with consistent layout of warnings,
                    # output, and truncation (always runs even on exceptions).
                    if self.compression_handler:
                        # Only post-process when a compression handler is configured. tool_result is
                        # always bound here (initialized to '' before the try), so this reference is
                        # safe even on the AgentTerminatedError re-raise path. The post-processing body
                        # lives in _postprocess_tool_result.
                        tool_result = self._postprocess_tool_result(instance, inst_name, tool_name, tool_result)

                    # Clear thread-local instance name after draining to prevent
                    # stale references across concurrent calls
                    clear_current_instance_name()

                # Track compress_context execution and record telemetry
                if tool_name == 'compress_context':
                    inst = self.pool.get_instance(inst_name)
                    if inst:
                        self._rebuild_working_set(messages, llm_messages, inst_name)
                    # Item 10: Validate message pool after agent-triggered
                    # compression
                    conv = self.pool.get_conversation(inst_name)
                    if conv and not validate_message_pool(conv, inst_name):
                        logger.error(
                            f"[MSG POOL VALIDATION] Pool invalid after agent-triggered compression for '{inst_name}'")

                    # Build function result message — include function_id and
                    # tool_success per OpenAI spec
                # function_id was extracted BEFORE _execute_tool call above
                fn_msg = Message(
                    role=FUNCTION,
                    name=tool_name,
                    content=tool_result,
                    extra={
                        'function_id': function_id or '1',
                        'tool_success': _tool_success,
                    },
                )
                self._append_and_log(instance, fn_msg)
                response.append(fn_msg)  # Stream tool result to UI (separate list for streaming)

                # Track executed tool for orphan handling
                executed_tools.append(tool_name)

        finally:
            # Guarantee a still-pending deferred parent/KV restore is NOT lost when the batch
            # loop exits early (terminal-stop break / compression break / AgentTerminatedError raise),
            # all of which bypass the orphan-handling block below. The call_agent net runs FIRST, then
            # the media_gen net; both gate on their ran_*/reached_last_* markers plus a still-pending
            # _state_label. Full gate rationale lives in _run_deferred_state_restore_safety_nets.
            self._run_deferred_state_restore_safety_nets(
                instance, inst_name,
                ran_ca=ran_ca, reached_last_ca=reached_last_ca,
                ran_mg=ran_mg, reached_last_mg=reached_last_mg)

        # ── Handle orphaned tool calls from early break
        # ───────────────────────────────
        # If halt/stop was detected mid-loop, remaining tools in turn_output
        # don't have FUNCTION results. Add placeholder FUNCTION messages to prevent
        # API Error 400 (orphaned tool_call_id's). Runs only when the instance is
        # stopped; the placeholder batch is owned by _handle_orphaned_tool_calls.
        if self._is_stopped(inst_name):
            self._handle_orphaned_tool_calls(
                instance, inst_name, turn_output, executed_tools, response)

        if used_any_tool:
            self._proactive_compression_check(instance, messages, llm_messages, response, check_label='post-tool')

        return used_any_tool

    def _resolve_tool_policy(self, instance: AgentInstance) -> tuple[object, set[str], dict]:
        """Resolve ``(template, disabled_tools, function_map)`` for an instance.

        Shared by the primary tool loop and orphan handling. The disabled-tools
        resolver is imported lazily *inside* this helper so the existing patch target
        ``agent_cascade.utils.disabled_tools.resolve_disabled_tools_for_agent`` keeps
        intercepting. Returns ``(None, set(), {})`` when the agent has no template.
        """
        from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

        template = self.pool.get_template(instance.agent_class)
        disabled_tools: set[str] = set()
        function_map: dict = {}
        if template:
            # Use the centralized resolver instead of duplicating inline logic
            agent_name = getattr(template, 'name', '') or ''
            agent_type = getattr(template, 'agent_type', '') or ''
            instance_override = (getattr(instance, '_generate_cfg_override', None) if hasattr(
                instance, '_generate_cfg_override') else None)
            template_cfg = (getattr(template.llm, 'generate_cfg', None) if getattr(
                template, 'llm', None) is not None else {})

            disabled_tools = resolve_disabled_tools_for_agent(
                instance_override=instance_override,
                template_cfg=template_cfg,
                agent_name=agent_name,
                agent_type=agent_type,
            )

            function_map = getattr(template, 'function_map', {})
        return template, disabled_tools, function_map

    def _handle_orphaned_tool_calls(self, instance: AgentInstance, inst_name: str,
                                    turn_output: List[Message], executed_tools: List[str],
                                    response: List[Message]) -> None:
        """Add placeholder FUNCTION results for tool calls left orphaned by an early
        halt/stop, preventing API Error 400 from orphaned tool_call_ids.

        Called only when the instance is stopped (guarded by the caller). Holds
        ``instance._compression_lock`` for the whole placeholder batch (single acquisition
        around the loop AND the count warning), skips already-executed tool names, applies
        the same disabled/inexistent denial rules via ``_auto_deny_reason``, appends with
        ``_append_and_log(..., lock_held=True)``, streams to the UI ``response`` list,
        tracks each tool in ``executed_tools``, and logs the count of placeholders added.
        """
        executed_set = set(executed_tools)  # Convert to set for O(1) lookup
        tools_processed = 0

        # Resolve tool policy (shared with the primary loop) outside the
        # compression lock — template/disabled list don't change during the loop.
        _orphan_template, _orphan_disabled_tools, _orphan_function_map = self._resolve_tool_policy(instance)

        with instance._compression_lock:  # FIX #1: Batch lock acquisition for all placeholder appends

            for out in turn_output:
                use_tool, tool_name, tool_args, _ = self._detect_tool(out)
                if not use_tool:
                    continue

                # Only add placeholder for tools that were NOT executed
                if tool_name in executed_set:
                    continue

                # ── Disabled/Inexistent Tool Auto-Deny (orphan handling)
                # ────────
                # For unexecuted tools due to halt, check if they're disabled/inexistent.
                # If so, give proper denial message instead of generic "skipped" message.
                deny_reason = self._auto_deny_reason(
                    _orphan_template, _orphan_disabled_tools, _orphan_function_map, tool_name)
                if deny_reason is not None:
                    # Log the denial (matching primary loop pattern)
                    logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")

                # Extract function_id from the assistant message that had the tool call
                extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                _orphan_function_id = extra_data.get('function_id')  # Use same pattern as primary loop

                # Add placeholder FUNCTION result for unexecuted tool.
                # Use denial message if tool is disabled/inexistent, otherwise skip message.
                if deny_reason:
                    fn_content = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."
                else:
                    fn_content = f"Tool execution skipped: instance {inst_name} was halted/stopped"

                # Telemetry: record tool call start and end (matching primary loop pattern)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_tool_call_start(inst_name, tool_name)
                        tel.record_tool_call_end(
                            inst_name,
                            tool_name,
                            success=False,
                            result_chars=len(fn_content),
                            truncated=False,
                            error=f"Tool {deny_reason}" if deny_reason else 'Skipped (halt/stop)',
                            is_call_agent=(tool_name == 'call_agent'),
                        )
                    except Exception:
                        pass

                fn_msg = Message(
                    role=FUNCTION,
                    name=tool_name,
                    content=fn_content,
                    extra={
                        'function_id': _orphan_function_id or '1',  # Use same pattern as primary loop
                        'tool_success': False,
                    },
                )
                self._append_and_log(instance, fn_msg, lock_held=True)
                response.append(fn_msg)  # Stream to UI (separate list for streaming)

                # Track as executed for consistency (matching primary loop pattern)
                executed_tools.append(tool_name)

                tools_processed += 1

            if tools_processed > 0:
                logger.warning(
                    f"Added {tools_processed} placeholder FUNCTION messages for unexecuted tools in {inst_name}")

    def _run_deferred_state_restore_safety_nets(self, instance: AgentInstance, inst_name: str,
                                               ran_ca: bool, reached_last_ca: bool,
                                               ran_mg: bool, reached_last_mg: bool) -> None:
        """Run the deferred state-restore safety nets for a tool batch.

        Called from the loop's ``finally`` (after the loop, before orphan handling) so a
        still-pending deferred restore is NOT lost when the batch loop exits early. The
        call_agent net runs FIRST, then the media_gen net (ordering matters: the call_agent
        net clears the shared ``_state_label`` on success, so a mixed batch performs exactly
        one restore).

        Bare-instance safety: neither net calls ``reacquire_after_slot_yield`` /
        ``_restore_held_slot_state`` unless its ``ran_*`` marker is true, so a batch that
        contained no call_agent / media_gen never touches slot machinery.
        """
        # Safety net (plan §10.4): guarantee a still-pending deferred parent restore is NOT LOST when the
        # batch loop exits BEFORE processing the last call_agent — the terminal-stop break, the
        # compression-wait break, and the AgentTerminatedError raise, all of which bypass the
        # orphan-handling block below. Two conditions, both required:
        #   • reached_last_ca is False — the loop exited BEFORE processing the batch's last call_agent.
        #     If it DID reach it, the correct end-of-batch handling already ran (a SYNC last child did its
        #     full restore; an ASYNC last child intentionally leaves the label to self-heal at the next
        #     _setup_turn — forcing a restore here would be the wasted swap-in §10.5 rules out).
        #   • a still-pending _state_label — the detector for "no full restore has run yet". On the
        #     normal SYNC path the last child's restore already cleared it, so this no-ops.
        # tolerate_failure=True swallows a SlotCancelled/TimeoutError so a clean stop is not corrupted by a
        # raise from finally; respect_toggle=False matches the sync-child finally (the toggle branch would
        # clear the label and skip the restore). The forced call hits reacquire_for's already-held fast-path,
        # so it cannot double-acquire.
        if ran_ca and not reached_last_ca:
            parent_inst = self.pool.get_instance(inst_name) or instance
            if parent_inst is not None and getattr(parent_inst, '_state_label', None) is not None:
                logger.debug(
                    f"[STATE_RESTORE_SAFETY_NET] {inst_name} — batch exited before the last call_agent; "
                    f"forcing the deferred parent restore")
                self.reacquire_after_slot_yield(
                    parent_inst, inst_name, 'parallel-call-agent safety net', tolerate_failure=True,
                    respect_toggle=False)

        # Media-gen safety net (same polarity as the call_agent net above): a media_gen batch that saved
        # on its first call but exited before processing the last one (terminal-stop break / compression
        # break / AgentTerminatedError raise) would otherwise leak a pending _state_label and cold-reload.
        # The media_gen seam never releases the engine slot, so there is no reacquire to do — only the
        # deferred KV restore. restore_held_slot_state is defensive (never raises) and targets the held
        # endpoint live; passing the stable label makes it independent of a system agent (e.g. an auto-ask
        # security advisor) consuming ``_state_label`` inside an approval window — see the label contract
        # documented on state_ops.restore_held_slot_state.
        # GATE POLARITY: for the media net the LABEL gate is load-bearing — the last call's own restore
        # clears _state_label on every success/skip path, so the net no-ops on a completed batch. The
        # flow-marker gate (not reached_last_mg) is belt-and-suspenders (unlike the call_agent net, where
        # an ASYNC last child intentionally leaves the label and the flow marker is the only guard).
        # ORDERING: this net runs AFTER the call_agent net above, which clears the shared _state_label on
        # success. In a mixed batch where the call_agent net already forced the restore, the _state_label
        # gate here is None and this net correctly no-ops (one restore total).
        if ran_mg and not reached_last_mg:
            mg_inst = self.pool.get_instance(inst_name) or instance
            if mg_inst is not None and getattr(mg_inst, '_state_label', None) is not None:
                logger.debug(
                    f"[STATE_RESTORE_SAFETY_NET] {inst_name} — batch exited before the last media_gen; "
                    f"forcing the deferred KV restore")
                self._restore_held_slot_state(mg_inst, inst_name, label=inst_name)

    def _format_tool_dispatch_error(self, e: Exception, tool_name: str, inst_name: str) -> str:
        """Log/dedupe a dispatcher/plumbing crash and return the compact ``Error: ...`` string.

        R3: present dispatcher/plumbing crashes with the SAME shape as a tool crash
        (agent.py:_call_tool) — one compact root-cause line at ERROR, full traceback
        demoted to DEBUG and deduped per (root type+msg). The LLM-facing result stays
        compact (no multi-frame stack) for consistency with the tool-crash path.
        ``traceback.format_exc()`` still resolves the active exception because this is
        called from within the caller's ``except`` handler scope.
        """
        summary = format_crash(e)
        logger.error(f"Tool {tool_name} failed for {inst_name}: {summary}")
        if TB_DEDUP.should_log_full_tb(TB_DEDUP.get_tb_key(e)):
            logger.debug(f"Tool {tool_name} failed for {inst_name} — full traceback:\n"
                         f"{traceback.format_exc()}")
        return f"Error: {summary}"

    def _detect_tool_result_failure(self, tool_result: str) -> tuple[bool, str]:
        """Return ``(tool_success, tool_error)`` from the first-line error heuristic.

        Pure decision helper — does NOT log, cache, or mutate ``tool_result``. Scans the
        first non-blank line of the result; if it starts with a known error indicator
        or contains ``failed to``, reports the result as failed with a 500-char excerpt.
        A clean result returns ``(True, '')``.
        """
        first_line = ''
        for line in tool_result.split('\n'):
            stripped = line.strip()
            if stripped:
                first_line = stripped.lower()
                break
        error_indicators = [
            'error:', 'rejected by user:', 'rejected:', 'failed:', 'invalid:', 'permission denied:',
            'an error occurred', 'does not exist'
        ]
        if any(first_line.startswith(ind) for ind in error_indicators) or 'failed to' in first_line:
            return False, tool_result[:500]
        return True, ''

    def _postprocess_tool_result(self, instance: AgentInstance, inst_name: str, tool_name: str,
                                 tool_result: object) -> object:
        """Assemble the final tool result with a consistent layout of warnings, output,
        and truncation (truncation/drain post-processing).

        Called ONLY when a compression handler is present (the caller guards that check).
        ``tool_result`` is always bound at the call site (initialized to '' before the per-tool
        try), so this runs safely even on the AgentTerminatedError / early-raise paths. Resolves
        truncation params from config, gets ``base_dir`` from ``pool.operation_manager``, and
        uses agent-specific endpoint config (``api_router.get_llm_config`` with a ``pool.llm_cfg``
        fallback) for the vision capability check. Assembles via ``_assemble_tool_result``; on failure,
        logs and falls back to a thin drain chain (``_legacy_drain_tool_result``), coercing to a string so the
        result is never None. Returns the final result.
        """
        try:
            # Determine truncation params from config.
            # char_threshold: when to trigger (Wild Read Threshold)
            # char_limit: how much to keep after triggering (Wild Read Truncation)
            _llm_cfg = self.pool.llm_cfg or {}
            char_threshold = _llm_cfg.get('tool_result_max_chars', DEFAULT_TOOL_RESULT_MAX_CHARS)
            char_limit = _llm_cfg.get('wild_read_truncation_chars', DEFAULT_WILD_READ_TRUNCATION_CHARS)

            # Get base_dir from operation_manager for spillover path resolution
            om = getattr(self.pool, 'operation_manager', None)
            base_dir = getattr(om, 'base_dir', Path('.')) if om else Path('.')

            # Use agent-specific endpoint config for vision capability check.
            # self.pool.llm_cfg lacks model_type, causing ContentItem lists
            # to be stringified instead of properly rendered as images.
            if self.pool.api_router:
                agent_llm_cfg = self.pool.api_router.get_llm_config(instance.agent_class)
            else:
                agent_llm_cfg = self.pool.llm_cfg or {}

            tool_result = self.compression_handler._assemble_tool_result(
                instance,
                tool_result,
                char_limit=char_limit,
                instance_name=inst_name,
                tool_name=tool_name,
                base_dir=base_dir,
                llm_cfg=agent_llm_cfg,
                char_threshold=char_threshold,
            )
        except Exception as e:
            # Log the failure (was previously silent), then fall back to a thin drain
            # chain that still ensures warnings and notifications are delivered.
            logger.error(f"assemble_tool_result failed for '{inst_name}' (tool={tool_name}): {e}")
            try:
                tool_result = self.compression_handler._legacy_drain_tool_result(instance, tool_result)
            except Exception as drain_err:
                logger.error(f"Legacy drain also failed for '{inst_name}' (tool={tool_name}): {drain_err}")
                # Ensure we have something non-None to return
                if not isinstance(tool_result, str):
                    tool_result = str(tool_result) if tool_result is not None else ''
        return tool_result

    def _scan_tool_batch_indices(self, turn_output: List[Message]) -> tuple[List[int], List[int]]:
        """Return ``(ca_indices, mg_indices)`` — ordered indices of call_agent
        and media_gen tool calls in ``turn_output``.

        Static parallel-batch detection (single source of truth): ``turn_output`` is
        the complete, ordered tool-call list for this turn, already in memory, so the
        batch structure is known up-front — no runtime in-flight counter. Uses the
        SAME ``_detect_tool`` the execution loop uses so the detector cannot diverge
        from what actually runs. One ``_detect_tool`` call per element, in order.
        """
        ca_indices = []
        mg_indices = []
        for _idx, _out in enumerate(turn_output):
            _use, _name, _, _ = self._detect_tool(_out)
            if _use and _name == 'call_agent':
                ca_indices.append(_idx)
            elif _use and _name == 'media_gen':
                mg_indices.append(_idx)
        return ca_indices, mg_indices

    def _batch_position(self, out_idx: int, indices: List[int]) -> tuple[bool, bool]:
        """Return ``(is_first, is_last)`` for ``out_idx`` within a batch's index list.

        Called only from inside the matching call_agent/media_gen branch, where ``indices``
        is non-empty by construction of the scan (the current tool's own index was just
        appended) — never call with an empty list (would raise IndexError).
        """
        return (out_idx == indices[0]), (out_idx == indices[-1])

    def _auto_deny_reason(self, template: object, disabled_tools: set[str], function_map: dict,
                          tool_name: str) -> Optional[str]:
        """Return the auto-deny reason for a tool call, or ``None`` if it is allowed.

        Encapsulates the FULL guard (``template`` present AND the tool is disabled or
        absent from ``function_map``) plus the three-branch reason chain in the existing
        order. Returns ``None`` unless the guard matches — callers branch on the returned
        reason; do NOT treat this as only the inner chain, or every tool absent from an
        empty ``function_map`` would be denied.
        """
        if not (template and (tool_name in disabled_tools or tool_name not in function_map)):
            return None
        if tool_name in disabled_tools and tool_name not in function_map:
            return 'disabled and does not exist'
        elif tool_name in disabled_tools:
            return 'disabled'
        else:
            return 'does not exist'

    def _auto_deny_tool_call(self, instance: AgentInstance, inst_name: str, out: Message, tool_name: str,
                             deny_reason: str, response: List[Message],
                             executed_tools: List[str]) -> None:
        """Build and append the auto-deny FUNCTION result for a disabled/absent tool.

        Records telemetry (start+end, success=False), builds the denial message with
        the caller's ``function_id``, appends via ``_append_and_log``, streams to the
        UI ``response`` list, and tracks the tool as processed for orphan handling.
        ``used_any_tool`` is set by the CALLER after this returns (post-append timing).
        """
        logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")
        tool_result = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."

        # Extract function_id from the assistant message that had the tool call
        extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
        function_id = extra_data.get('function_id')

        # Telemetry: record tool call start and end for auto-denied tools
        if (tel := self._telemetry()) is not None:
            try:
                tel.record_tool_call_start(inst_name, tool_name)
                tel.record_tool_call_end(
                    inst_name,
                    tool_name,
                    success=False,
                    result_chars=len(tool_result),
                    truncated=False,
                    error=f"Tool {deny_reason}",
                    is_call_agent=(tool_name == 'call_agent'),
                )
            except Exception:
                pass

        # Build function result message with denial — include function_id per OpenAI spec
        fn_msg = Message(
            role=FUNCTION,
            name=tool_name,
            content=tool_result,
            extra={
                'function_id': function_id or '1',
                'tool_success': False,
            },
        )
        self._append_and_log(instance, fn_msg)
        response.append(fn_msg)  # Stream denial to UI (separate list for streaming)

        # Track as executed for orphan handling (it was processed, just denied)
        executed_tools.append(tool_name)

    @staticmethod
    def _has_images(messages):
        """Check if any message in the list contains image content."""
        for msg in messages:
            items = msg.content if isinstance(msg.content, list) else []
            for item in items:
                img_val = item.get('image') if isinstance(item, dict) else getattr(item, 'image', None)
                if img_val:
                    return True
        return False

    def _ensure_image_captions(self, messages, agent_type=None, instance_name=None):
        """Generate captions for uncaptioned images using any vision-capable endpoint.

        Captions are stored as metadata on ContentItem so they survive when falling
        back to text-only endpoints. This is called before LLM calls that may route
        through non-vision endpoints.

        Args:
            instance_name: Owning instance name (sticky slot plan change #12): enables
                side-call slot participation (acquire-or-keep, never drop) and the
                autoloader KV guard inside caption_images().
        """
        router = getattr(self.pool, 'api_router', None)
        if router and hasattr(router, 'caption_images'):
            return router.caption_images(messages, agent_type=agent_type, instance_name=instance_name)
        return messages
