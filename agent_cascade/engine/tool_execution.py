"""Tool dispatch / execution cluster for the execution engine (Phase 1 module-split).

``ToolExecMixin`` holds the tool-execution and image-handling methods. Method bodies
are moved VERBATIM from ``agent_cascade/execution_engine.py``; only the class wrapper
and the imports needed by these methods were added. The mixin is composed into
``ExecutionEngine`` in :mod:`agent_cascade.engine.core`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.settings import DEFAULT_TOOL_RESULT_MAX_CHARS
from agent_cascade.llm.schema import FUNCTION, Message
from agent_cascade.log import logger
from agent_cascade.exceptions import AgentTerminatedError
from agent_cascade.utils.pool_validation import validate_message_pool
from agent_cascade.operation_manager import (
    set_current_instance_name,
    clear_current_instance_name,
)


class ToolExecMixin:
    """Mixin providing tool-execution and image-handling methods."""

    def _execute_detected_tools(
        self,
        instance: AgentInstance,
        inst_name: str,
        turn_output: List[Message],
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
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

        # ── Hoist template lookup outside loop (performance optimization)
        # ────────
        # Template and disabled tool list don't change during the loop.
        # Centralized disabled_tools resolution — see
        # agent_cascade.utils.disabled_tools
        from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

        _primary_template = self.pool.get_template(instance.agent_class)
        _primary_disabled_tools: set[str] = set()
        _primary_function_map: dict = {}
        if _primary_template:
            # Use the centralized resolver instead of duplicating inline logic
            agent_name = getattr(_primary_template, 'name', '') or ''
            agent_type = getattr(_primary_template, 'agent_type', '') or ''
            instance_override = (getattr(instance, '_generate_cfg_override', None)
                                if hasattr(instance, '_generate_cfg_override') else None)
            template_cfg = (getattr(_primary_template.llm, 'generate_cfg', None)
                            if getattr(_primary_template, 'llm', None) is not None else {})

            _primary_disabled_tools = resolve_disabled_tools_for_agent(
                instance_override=instance_override,
                template_cfg=template_cfg,
                agent_name=agent_name,
                agent_type=agent_type,
            )

            _primary_function_map = getattr(_primary_template, 'function_map', {})

        for out in turn_output:
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
                logger.debug("tool exec suspended by compression - %s", inst_name)
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
            # active functions sent to LLM).
            # If an agent generates a call for a disabled tool, it gets
            # auto-denied here.
            if _primary_template and (tool_name in _primary_disabled_tools or tool_name not in _primary_function_map):
                # Determine deny reason with same logic as legacy
                # implementation
                if tool_name in _primary_disabled_tools and tool_name not in _primary_function_map:
                    deny_reason = "disabled and does not exist"
                elif tool_name in _primary_disabled_tools:
                    deny_reason = "disabled"
                else:
                    deny_reason = "does not exist"

                logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")
                tool_result = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."

                # Extract function_id from the assistant message that had the
                # tool call
                extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                function_id = extra_data.get('function_id')

                # Telemetry: record tool call start and end for auto-denied
                # tools
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_tool_call_start(inst_name, tool_name)
                        tel.record_tool_call_end(
                            inst_name, tool_name,
                            success=False,
                            result_chars=len(tool_result),
                            truncated=False,
                            error=f"Tool {deny_reason}",
                            is_call_agent=(tool_name == 'call_agent'),
                        )
                    except Exception:
                        pass

                # Build function result message with denial — include
                # function_id per OpenAI spec
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

                # Track as executed for orphan handling (it was processed, just
                # denied)
                executed_tools.append(tool_name)

                used_any_tool = True
                continue  # Skip actual tool execution

            used_any_tool = True

            # Track tool success/failure — needed for function_id matching and
            # frontend isToolFailure()
            _tool_success = True
            _tool_error = ""

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

            try:
                # Set current instance name in thread-local for _resolve_path
                # warnings
                set_current_instance_name(inst_name)

                try:
                    # Phase 4.3: Delegate to ToolDispatcher
                    tool_result = self.tool_dispatcher.execute_tool(
                        instance, tool_name, tool_args, llm_messages, function_id=function_id
                    )
                except AgentTerminatedError:
                    # Clean abort from stop-check during tool execution — re-raise for caller
                    raise
                except Exception as e:
                    logger.error(f"Tool {tool_name} failed for {inst_name}: {e}")
                    tool_result = f"Error: {e}"
                    _tool_success = False
                    _tool_error = str(e)
                # Cache full output (if exceeds threshold)
                if isinstance(tool_result, str):
                    self._cache_tool_output(
                        inst_name, tool_name, tool_result,
                        threshold=self.pool.settings.cache_threshold_chars
                    )

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
                    first_line = ''
                    for line in tool_result.split('\n'):
                        stripped = line.strip()
                        if stripped:
                            first_line = stripped.lower()
                            break
                    error_indicators = [
                        'error:', 'rejected by user:', 'rejected:', 'failed:', 'invalid:',
                        'permission denied:', 'an error occurred', 'does not exist'
                    ]
                    if any(first_line.startswith(ind) for ind in error_indicators) or 'failed to' in first_line:
                        _tool_success = False
                        _tool_error = tool_result[:500]

            finally:
                # Telemetry: record tool call end (non-blocking, always called)
                if (tel := self._telemetry()) is not None:
                    try:
                        tel.record_tool_call_end(
                            inst_name, tool_name,
                            success=_tool_success,
                            result_chars=len(tool_result) if isinstance(tool_result, str) else 0,
                            error=_tool_error,
                            is_call_agent=(tool_name == 'call_agent'),
                        )
                    except Exception:
                        pass

                # Assemble final tool result with consistent layout of warnings,
                # output, and truncation (always runs even on exceptions).
                # Only assemble when tool_result is defined to avoid errors from
                # early failures.
                if self.compression_handler:
                    try:
                        # Determine char_limit from config or default
                        char_limit = (self.pool.llm_cfg or {}).get(
                            'tool_result_max_chars', DEFAULT_TOOL_RESULT_MAX_CHARS
                        )

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
                        )
                    except Exception as e:
                        # Log the failure (was previously silent), then fall back
                        # to a thin drain chain that still ensures warnings and
                        # notifications are delivered.
                        logger.error(
                            f"assemble_tool_result failed for '{inst_name}' (tool={tool_name}): {e}"
                        )
                        try:
                            tool_result = self.compression_handler._legacy_drain_tool_result(
                                instance, tool_result
                            )
                        except Exception as drain_err:
                            logger.error(
                                f"Legacy drain also failed for '{inst_name}' (tool={tool_name}): {drain_err}"
                            )
                            # Ensure we have something non-None to return
                            if not isinstance(tool_result, str):
                                tool_result = str(tool_result) if tool_result is not None else ""

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
                    logger.error(f"[MSG POOL VALIDATION] Pool invalid after agent-triggered compression for '{inst_name}'")

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

        # ── Handle orphaned tool calls from early break
        # ───────────────────────────────
        # If halt/stop was detected mid-loop, remaining tools in turn_output
        # don't have FUNCTION results.
        # Add placeholder FUNCTION messages to prevent API Error 400 (orphaned
        # tool_call_id's).
        if self._is_stopped(inst_name):
            executed_set = set(executed_tools)  # Convert to set for O(1) lookup
            tools_processed = 0

            # ── Hoist template lookup outside compression lock
            # ──────────────────
            # Template and disabled tool list don't change during the loop.
            # Centralized disabled_tools resolution — see
            # agent_cascade.utils.disabled_tools
            from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

            _orphan_template = self.pool.get_template(instance.agent_class)
            _orphan_disabled_tools: set[str] = set()
            _orphan_function_map: dict = {}
            if _orphan_template:
                # Use the centralized resolver instead of duplicating inline
                # logic
                agent_name = getattr(_orphan_template, 'name', '') or ''
                agent_type = getattr(_orphan_template, 'agent_type', '') or ''
                instance_override = (getattr(instance, '_generate_cfg_override', None)
                                    if hasattr(instance, '_generate_cfg_override') else None)
                template_cfg = (getattr(_orphan_template.llm, 'generate_cfg', None)
                                if getattr(_orphan_template, 'llm', None) is not None else {})

                _orphan_disabled_tools = resolve_disabled_tools_for_agent(
                    instance_override=instance_override,
                    template_cfg=template_cfg,
                    agent_name=agent_name,
                    agent_type=agent_type,
                )

                _orphan_function_map = getattr(_orphan_template, 'function_map', {})

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
                    # For unexecuted tools due to halt, check if they're
                    # disabled/inexistent.
                    # If so, give proper denial message instead of generic
                    # "skipped" message.
                    deny_reason = None
                    # Template guard for consistency with primary loop
                    if _orphan_template and (tool_name in _orphan_disabled_tools or tool_name not in _orphan_function_map):
                        # Determine deny reason with same logic as legacy
                        # implementation
                        if tool_name in _orphan_disabled_tools and tool_name not in _orphan_function_map:
                            deny_reason = "disabled and does not exist"
                        elif tool_name in _orphan_disabled_tools:
                            deny_reason = "disabled"
                        else:
                            deny_reason = "does not exist"

                        # Log the denial (matching primary loop pattern)
                        logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")

                    # Extract function_id from the assistant message that had
                    # the tool call
                    extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                    _orphan_function_id = extra_data.get('function_id')  # Use same pattern as primary loop

                    # Add placeholder FUNCTION result for unexecuted tool
                    # Use denial message if tool is disabled/inexistent,
                    # otherwise use skip message
                    if deny_reason:
                        fn_content = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."
                    else:
                        fn_content = f"Tool execution skipped: instance {inst_name} was halted/stopped"

                    # Telemetry: record tool call start and end (matching
                    # primary loop pattern)
                    if (tel := self._telemetry()) is not None:
                        try:
                            tel.record_tool_call_start(inst_name, tool_name)
                            tel.record_tool_call_end(
                                inst_name, tool_name,
                                success=False,
                                result_chars=len(fn_content),
                                truncated=False,
                                error=f"Tool {deny_reason}" if deny_reason else "Skipped (halt/stop)",
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

                    # Track as executed for consistency (matching primary loop
                    # pattern)
                    executed_tools.append(tool_name)

                    tools_processed += 1

                if tools_processed > 0:
                    logger.warning(f"Added {tools_processed} placeholder FUNCTION messages for unexecuted tools in {inst_name}")

        if used_any_tool:
            self._proactive_compression_check(instance, messages, llm_messages, response, check_label="post-tool")

        return used_any_tool


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


    def _ensure_image_captions(self, messages, agent_type=None):
        """Generate captions for uncaptioned images using any vision-capable endpoint.

        Captions are stored as metadata on ContentItem so they survive when falling
        back to text-only endpoints. This is called before LLM calls that may route
        through non-vision endpoints.
        """
        router = getattr(self.pool, 'api_router', None)
        if router and hasattr(router, 'caption_images'):
            return router.caption_images(messages, agent_type=agent_type)
        return messages

