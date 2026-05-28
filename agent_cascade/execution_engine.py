"""
Execution Engine — Phase 1 of the AgentCascade Architecture Rewrite.

Stateless execution coordinator that drives ALL agent instances through a single
unified loop. Replaces both api_server.run_agent_thread() and
OrchestratorAgent._stream_sub_agent_call() — eliminating the structural duality.

See DESIGN_REWRITE.md §3.1 for design rationale.

Key design principle: Engine is stateless. It receives AgentInstance as a parameter
and orchestrates phases. Each phase method (~20-60 lines) is independently testable.
"""

import json
import time
from typing import Any, Iterator, List, Tuple, Union

from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, SYSTEM, USER, Message,
)
from agent_cascade.log import logger
from agent_cascade.utils.utils import extract_text_from_message

from .agent_instance import AgentInstance, LoopDetectedError


class ExecutionEngine:
    """
    Coordinates execution of an AgentInstance through its turn loop.

    Stateless in terms of turn-level state — receives AgentInstance as parameter.
    Holds a pool reference for coordination (halt checks, tool delegation) but
    tracks no per-turn variables between calls. This makes testing straightforward:
    create an instance, set up state, call run(), inspect yields.

    Every agent (including the root/top-level agent) goes through this same engine.
    There is no separate execution path for any agent type.
    """

    def __init__(self, pool):
        """Initialize with a reference to the AgentPool.

        Args:
            pool: The AgentPool instance that manages all agent state.
        """
        self.pool = pool

    def run(self, instance: AgentInstance) -> Iterator[List[Message]]:
        """Execute the agent's turn loop as a generator yielding state updates.

        This is THE execution entry point for ALL agents. No separate paths
        for any agent type. The root agent is just the first instance
        created in the pool.

        Args:
            instance: The AgentInstance to execute.

        Yields:
            List[Message]: Current conversation state after each phase.
        """
        instance.is_active = True  # Mark active before execution starts
        try:
            # ── Phase 1: Setup ─────────────────────────────────────────────
            messages, llm_messages, response = self._setup_turn(instance)
            if not messages:
                return  # Manual command handled or error

            max_turns = instance.max_turns or 50
            turns_available = max_turns

            while turns_available > 0:
                # ── Phase 2: Pre-LLM Checks ────────────────────────────────
                # Stop/halt checks, async message injection, compression check/force, loop detection
                if self._pre_llm_checks(instance, messages, llm_messages, turns_available):
                    yield response
                    continue

                turns_available -= 1

                # ── Phase 3: LLM Call with Injection Points ────────────────
                turn_output = list(self._call_llm_with_injection(instance, llm_messages))

                if self.pool.stopped or self.pool.is_instance_halted(instance.instance_name):
                    yield response
                    continue

                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    yield response
                    continue

                # ── Phase 5: Post-Turn Checks ───────────────────────────────
                if not self._post_turn_checks(instance, messages):
                    break

            # ── Cleanup: Turn limit reached ────────────────────────────────
            if turns_available <= 0:
                msg = Message(
                    role=ASSISTANT,
                    content="\n\n[SYSTEM: Turn limit reached. Ask me to continue if incomplete.]",
                )
                response.append(msg)
                yield response

        except LoopDetectedError:
            # Propagate to consumer-level recovery wrapper (DESIGN_REWRITE §7.2)
            raise
        except Exception as e:
            # C4 fix: Catch unhandled exceptions — log and yield error state
            logger.error(f"ExecutionEngine.run() failed for {instance.instance_name}: {e}")
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — mark inactive regardless of how we exit
            instance.is_active = False

    # ═══════════════════════════════════════════════════════════════════════
    #  Phase Methods — each ~20-60 lines, independently testable
    # ═══════════════════════════════════════════════════════════════════════

    def _setup_turn(self, instance: AgentInstance) -> tuple:
        """Phase 1: Prepare messages and LLM input for the turn loop.

        Builds the system message from template (for main agent), loads conversation history,
        applies slice_history_for_llm to get working set, and sets up the response accumulator.

        Returns:
            Tuple of (messages, llm_messages, response) or (None, None, None) on error.
        """
        inst_name = instance.instance_name

        # Load conversation from pool (single source of truth)
        with instance._compression_lock:
            conv = list(instance.conversation)
        if not conv:
            return None, None, None

        # P7: System prompt injection for root agent (first/top-level agent in the pool)
        # Inject identity, session metadata, available resources, and argument reuse instructions
        if instance.parent_instance is None and len(conv) > 0:
            m0 = conv[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')
            if m0_role == SYSTEM:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str):
                    import re as _re
                    
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are\s+\w+\."
                    if _re.search(pattern, m0_content):
                        m0_content = _re.sub(pattern, f"You are {inst_name}.", m0_content, count=1)
                    
                    # 2. Insert Session Metadata section (if not present)
                    if '## Session Metadata' not in m0_content:
                        meta_lines = [
                            "## Session Metadata",
                            f"- Supervisor: User",  # Root agent's supervisor is always the user
                        ]

                        # Try to get logger metadata for paths; fall back to defaults on failure
                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            working_dir = log_inst.data['metadata'].get('working_dir', 'Unknown')
                            log_path = getattr(log_inst, 'log_path', 'Unknown')
                            extra_ro = log_inst.data['metadata'].get('extra_paths_ro', [])
                            extra_rw = log_inst.data['metadata'].get('extra_paths_rw', [])
                        except Exception:
                            working_dir = os.getcwd()
                            log_path = "N/A"
                            extra_ro = []
                            extra_rw = []

                        meta_lines.append(f"- Working Dir: {working_dir}")
                        meta_lines.append(f"- Log Path: {log_path}")
                        if extra_ro:
                            meta_lines.append(f"- Extra Paths (Read-Only): {', '.join(extra_ro)}")
                        if extra_rw:
                            meta_lines.append(f"- Extra Paths (Read-Write): {', '.join(extra_rw)}")
                        meta_lines.append("Use your logs to recall details from turns that were compressed.")

                        content_lines = m0_content.split('\n')
                        insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith("#") else 1
                        for i, ml in enumerate(meta_lines):
                            content_lines.insert(insert_pos + i, ml)
                        m0_content = '\n'.join(content_lines)
                    # 3. Inject available resources (other agent types and enabled tools)
                    if '--- CURRENT AVAILABLE RESOURCES' not in m0_content:
                        res_append = "\n\n--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---\n"
                        res_append += "\nAvailable Agent Types (call via call_agent):\n"
                        
                        # List available templates
                        has_agents = False
                        for name in sorted(self.pool.templates.keys()):
                            if name.lower() != inst_name.lower():
                                agent_obj = self.pool.templates[name]
                                tagline = getattr(agent_obj, 'description', 'No description provided')
                                res_append += f"- **{name}**: {tagline}\n"
                                has_agents = True
                        if not has_agents:
                            res_append += "- None currently available.\n"
                        
                        # List enabled tools (excluding disabled_tools)
                        res_append += "\nEnabled Tools (can change per interaction):\n"
                        template = self.pool.templates.get(instance.agent_class)
                        if template and hasattr(template, 'function_map'):
                            disabled_tools = getattr(template.llm, 'generate_cfg', {}).get('disabled_tools', [])
                            for t_name in sorted(template.function_map.keys()):
                                if t_name in disabled_tools:
                                    continue
                                desc = getattr(template.function_map[t_name], 'description', 'No description provided')
                                res_append += f"- **{t_name}**: {desc}\n"
                        else:
                            res_append += "- None currently enabled.\n"
                        m0_content += res_append
                    
                    # 4. Inject Argument Reuse instructions (static version for caching)
                    if '### Advanced Feature: Argument Reuse' not in m0_content:
                        m0_content += (
                            "\n\n### Advanced Feature: Argument Reuse\n"
                            "To reuse a LARGE argument value (like full file content or path) from any previous successful tool call in this session, "
                            'use the exact placeholder: "__USE_PREV_ARG__". This saves tokens and processing time.'
                        )
                    
                    # Update the message
                    if isinstance(m0, dict):
                        m0['content'] = m0_content
                    else:
                        m0.content = m0_content

        # messages = full working set; llm_messages = what actually goes to LLM
        # Apply slice to extract system + post-marker tail if markers exist
        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages = list(sliced) if sliced else list(conv)
        response: List[Message] = []

        return conv, llm_messages, response

    def _pre_llm_checks(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], turns_available: int
    ) -> bool:
        """Phase 2: Stop/halt checks, async injection, compression check, loop detection.

        Returns True if processing should continue to next iteration (yield + continue).
        Handles: stop/halt guard, async message drain, forced compression with rebuild,
        and loop detection (raises LoopDetectedError if found).
        """
        inst_name = instance.instance_name

        # ── Stop/halt guard ────────────────────────────────────────────────
        if self.pool.stopped or self.pool.is_instance_halted(inst_name):
            return True  # Skip LLM call, yield and continue loop

        # ── Async message injection (drain queue) ──────────────────────────
        pending = self.pool.drain_queue(inst_name)
        if pending:
            for async_msg_text in pending:
                if not async_msg_text.strip():
                    continue  # Skip empty messages
                async_msg = Message(role=USER, content=async_msg_text)
                messages.append(async_msg)
                llm_messages.append(async_msg)
                with instance._compression_lock:
                    instance.conversation.append(async_msg)
            return True  # Yield and continue loop to process new messages

        # ── /compress manual command handling (Item 7) ──────────────────────
        if self._handle_compress_command(instance, messages):
            return True  # Command handled — yield and continue

        # ── COMPRESSION CHECK (critical for long-running agents) ────────────
        max_tokens = self._get_max_tokens(instance)
        current_tokens = self._count_history_tokens(llm_messages)
        usage_pct = (current_tokens / max_tokens * 100) if max_tokens > 0 else 0

        # Forced compression at >95% — halts other agents, compresses, rebuilds
        if usage_pct > self.pool.settings.compression_force_threshold:
            return self._force_compression(instance, messages, llm_messages, usage_pct)

        # Warning injection at >85%
        if usage_pct > self.pool.settings.compression_warning_threshold:
            self._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens)

        # ── Loop detection ────────────────────────────────────────────────
        loop_info = self._detect_loop(messages)
        if loop_info:
            reason, pop_count = loop_info
            logger.warning(f"Loop detected for {inst_name}: {reason}")
            raise LoopDetectedError(reason=reason, pop_count=pop_count)

        return False  # Continue to LLM call normally

    def _force_compression(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], usage_pct: float
    ) -> bool:
        """Force compress when token usage exceeds critical threshold. Returns True (continue loop)."""
        inst_name = instance.instance_name

        # Halt other agents (exempt target, compression_agent, and root agent)
        exempt = [inst_name, 'compression_agent']
        if instance.parent_instance:
            exempt.append(instance.parent_instance)
        self.pool.halt_all_instances(except_instances=exempt)

        try:
            logger.info(
                f"Context usage at {usage_pct:.1f}% for {inst_name} — "
                f"forcing compression."
            )

            from agent_cascade.compression.core import compress_context as _compress
            result = _compress(
                agent_pool=self.pool,
                target_agent_name=inst_name,
                fraction=0.5,
                mode='auto',
                force=True,
                justification=f'CRITICAL THRESHOLD ({usage_pct:.1f}%)',
            )

            if result.success:
                # Rebuild working set from compressed pool state
                self._rebuild_working_set(messages, llm_messages, inst_name)
                # Use summary_text directly from CompressResult (P2 fix — no fragile tag parsing)
                instance.compression_summary = result.summary_text
                # Update latest_marker_index to point to the new marker in the conversation (P2 fix)
                conv = self.pool.get_conversation(inst_name)
                if conv:
                    for idx, msg in enumerate(conv):
                        role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
                        c = msg.get('content') if isinstance(msg, dict) else getattr(msg, 'content', '')
                        if isinstance(c, str) and '<context_summary>' in c:
                            instance.latest_marker_index = idx

                    # Item 10: Validate message pool after forced compression
                    if not validate_message_pool(conv, inst_name):
                        logger.error(f"[MSG POOL VALIDATION] Pool invalid after forced compression for '{inst_name}'. Attempting recovery from log...")
                        # Recovery: reload from the logger's history (which is unaffected)
                        try:
                            recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                            if recov and validate_message_pool(recov, inst_name):
                                with instance._compression_lock:  # Thread-safe recovery write
                                    self.pool.instance_conversations[inst_name] = list(recov)
                                logger.info(f"Recovered message pool from log for '{inst_name}' ({len(recov)} messages)")
                                conv = recov
                                # Rebuild working sets from recovered data
                                self._rebuild_working_set(messages, llm_messages, inst_name)
                            else:
                                logger.error("Recovery from log also failed — message pool may be corrupted")
                                self._append_system_notification(
                                    llm_messages, "[SYSTEM NOTIFICATION: Compression corrupted pool",
                                    f"[SYSTEM NOTIFICATION: Forced compression and recovery both failed for {inst_name}. Agent halted to prevent corruption.]"
                                )
                                # Halt this instance to prevent further execution with corrupted state
                                self.pool.halt_instance(inst_name)
                        except Exception as e:
                            logger.error(f"Recovery attempt failed for '{inst_name}': {e}")

                    # Item 11: Sync the logger's internal data["history"] to match pool state
                    # Without this, update_history() will treat pool messages not yet seen by
                    # the logger as "new" and append them, causing duplication.
                    try:
                        log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                        log_inst.update_history(conv)
                    except Exception as e:
                        logger.error(f"Logger sync after forced compression FAILED for '{inst_name}': {e}. "
                                     f"Pool may desync — manual intervention required.")

                notification = (
                    f"[SYSTEM NOTIFICATION: Context exceeded {usage_pct:.1f}%. "
                    f"Forced compression applied.]"
                )
                self._append_system_notification(llm_messages, "[SYSTEM NOTIFICATION: Context exceeded", notification)
            else:  # Compression failed or returned error
                logger.error(f"Forced compression failed for {inst_name}: {result.error}")
                notification = (
                    f"[SYSTEM NOTIFICATION: Context exceeded {usage_pct:.1f}%, "
                    f"but automatic compression failed.]"
                )
                self._append_system_notification(llm_messages, "[SYSTEM NOTIFICATION: Context exceeded", notification)

            return True  # Continue loop — don't make LLM call this turn

        except Exception as e:
            logger.error(f"Forced compression raised exception for {inst_name}: {e}")
            return True

        finally:
            self.pool.resume_all_instances()

    def _inject_compression_warning(
        self, llm_messages: List[Message], usage_pct: float,
        current_tokens: int, max_tokens: int
    ):
        """Inject a warning message when context is approaching limit."""
        warning = (
            f"[SYSTEM WARNING: Context window at {usage_pct:.1f}% capacity "
            f"({current_tokens}/{max_tokens} tokens). "
            f"Consider using compress_context to free space.]"
        )
        self._append_system_notification(llm_messages, "[SYSTEM WARNING: Context", warning)

    def _rebuild_working_set(
        self, messages: List[Message], llm_messages: List[Message], inst_name: str
    ):
        """Rebuild both working sets from pool state after compression.

        With clean-trim model (DESIGN_REWRITE §2.4), the pool is already compact —
        we just replace our references with deepcopies of the current pool content.
        """
        # Rebuild messages (full conversation) using shared helper
        from agent_cascade.compression.helpers import rebuild_working_set as _rws
        _rws(messages, self.pool, inst_name)

        # Rebuild llm_messages (sliced working set) — apply slice_history_for_llm
        conv = self.pool.get_conversation(inst_name)
        if not conv:
            return

        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages.clear()
        llm_messages.extend(list(sliced))  # Already a new list from slice_history_for_llm

    def _call_llm_with_injection(
        self, instance: AgentInstance, llm_messages: List[Message]
    ) -> Iterator[Message]:
        """Phase 3: LLM call with active function injection.

        Makes the actual LLM API call via api_router, handling streaming.
        Checks for stop/halt mid-stream.
        """
        inst_name = instance.instance_name
        template = self.pool.templates.get(instance.agent_class)
        if not template:
            yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: No template for {instance.agent_class}]")
            return

        # Get active functions (tool schemas) from template
        active_functions = template._get_active_functions() if hasattr(template, '_get_active_functions') else []

        # Build the LLM call
        try:
            for output in self._execute_llm_call(instance, template, llm_messages, active_functions):
                # Check stop/halt mid-stream
                if self.pool.stopped or self.pool.is_instance_halted(inst_name):
                    break
                yield output
        except Exception as e:
            logger.error(f"LLM call failed for {inst_name}: {e}")
            yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed — {e}]")

    def _execute_llm_call(self, instance: AgentInstance, template, messages: List[Message], active_functions) -> Iterator[Message]:
        """Execute the actual LLM API call via api_router with failover."""
        if self.pool.api_router and hasattr(self.pool.api_router, 'call_with_fallback'):
            # Route through API router for multi-endpoint failover
            agent_type = instance.agent_class.lower()

            def _do_call(llm_cfg: dict) -> Iterator[Message]:
                merged_cfg = {}
                if hasattr(template.llm, 'generate_cfg'):
                    merged_cfg.update(template.llm.generate_cfg)
                merged_cfg.update(llm_cfg)
                merged_cfg['agent_name'] = template.name

                return template.llm.chat(
                    messages=messages,
                    functions=active_functions,
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=merged_cfg,
                )

            agent_type = instance.agent_class.lower() if hasattr(instance, 'agent_class') else 'agent'
            return self.pool.api_router.call_with_fallback(agent_type, _do_call)
        else:
            # Direct call without router
            return template.llm.chat(
                messages=messages,
                functions=active_functions,
                stream=True,
                delta_stream=False,
            )

    def _process_response(
        self, instance: AgentInstance, turn_output: List[Message],
        messages: List[Message], llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.

        Returns True if processing should continue to next iteration (tool was used or truncated).
        """
        inst_name = instance.instance_name

        # ── Normalize and update history ────────────────────────────────────
        is_truncated = False
        for msg in turn_output:
            # P4: Gemma thought tag normalization — prevent history pollution
            # Check for Gemma-style <|channel>thought tags
            content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
            if not msg.get('reasoning_content') and isinstance(content, str) and '<|channel>thought' in content.lower():
                import re as _re
                # Only strip if at very beginning to avoid matching tags inside file content
                match = _re.search(r'^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<channel\|>|$)', content, _re.IGNORECASE)
                if match:
                    msg['reasoning_content'] = match.group(1).strip()
                    msg['content'] = _re.sub(r'^\s*<\|channel>thought\n?[\s\S]*?(?:\n?<channel\|>|$)', '', content, count=1, flags=_re.IGNORECASE).strip()

            # Strip thinking blocks from reasoning_content to prevent tag pollution in history
            if msg.get('reasoning_content'):
                rc = msg.get('reasoning_content')
                if isinstance(rc, str):
                    msg['reasoning_content'] = self._strip_thinking_blocks(rc)
            
            # Clean thinking blocks from function call arguments (P4 continuation)
            func_call = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
            if func_call:
                if isinstance(func_call, dict) and func_call.get('arguments'):
                    func_call['arguments'] = self._strip_thinking_blocks(func_call['arguments'])
                elif hasattr(func_call, 'arguments'):
                    func_call.arguments = self._strip_thinking_blocks(func_call.arguments)

            # Check for truncation (finish_reason == 'length')
            extra = msg.get('extra') if isinstance(msg, dict) else getattr(msg, 'extra', None)
            if extra and extra.get('finish_reason') == 'length':
                is_truncated = True

        # Append to all working sets
        response.extend(turn_output)
        messages.extend(turn_output)
        llm_messages.extend(turn_output)
        with instance._compression_lock:
            instance.conversation.extend(turn_output)

        # Persist messages to JSONL log file (P1: LoggerManager migration)
        try:
            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
            for msg in turn_output:
                log_inst.log_message(msg)
        except Exception:
            pass  # Logging failures must never break the execution loop

        # ── Auto-continue on truncation ─────────────────────────────────────
        if is_truncated and not self.pool.stopped and not self.pool.is_instance_halted(inst_name):
            logger.info(f"Detected message truncation for {inst_name}. Auto-continuing.")
            cont_msg = Message(
                role=USER,
                content="[SYSTEM]: Your previous response was cut off. Continue from where you left off."
            )
            messages.append(cont_msg)
            llm_messages.append(cont_msg)
            with instance._compression_lock:
                instance.conversation.append(cont_msg)
            return True  # Continue to next LLM call

        # ── Tool detection and execution ────────────────────────────────────
        used_any_tool = False
        for out in turn_output:
            use_tool, tool_name, tool_args, _ = self._detect_tool(out)
            if not use_tool:
                continue

            used_any_tool = True

            # Stop/halt check BEFORE tool execution
            if self.pool.stopped or self.pool.is_instance_halted(inst_name):
                break

            try:
                tool_result = self._execute_tool(instance, tool_name, tool_args, llm_messages)
            except Exception as e:
                logger.error(f"Tool {tool_name} failed for {inst_name}: {e}")
                tool_result = f"Error: {e}"

            # Truncate if needed
            tool_result = self._truncate_tool_result(
                str(tool_result), tool_name, llm_messages, inst_name
            )

            # Track compress_context execution
            if tool_name == 'compress_context':
                self._rebuild_working_set(messages, llm_messages, inst_name)
                # Item 10: Validate message pool after agent-triggered compression
                conv = self.pool.get_conversation(inst_name)
                if conv and not validate_message_pool(conv, inst_name):
                    logger.error(f"[MSG POOL VALIDATION] Pool invalid after agent-triggered compression for '{inst_name}'")

            # Build function result message
            fn_msg = Message(role=FUNCTION, name=tool_name, content=tool_result)
            messages.append(fn_msg)
            llm_messages.append(fn_msg)
            with instance._compression_lock:
                instance.conversation.append(fn_msg)

            # ── Mid-tool urgent injection ───────────────────────────────────
            urgent = self.pool.drain_queue(inst_name)
            if urgent:
                for text in urgent:
                    if text.strip():
                        async_msg = Message(role=USER, content=text)
                        messages.append(async_msg)
                        llm_messages.append(async_msg)
                        with instance._compression_lock:
                            instance.conversation.append(async_msg)
                break  # Stop executing remaining tools

        return used_any_tool or is_truncated  # Continue loop if tool was used

    def _post_turn_checks(self, instance: AgentInstance, messages: List[Message]) -> bool:
        """Phase 5: Check for final answer, wait for parallel agents, drain post-generation queue.

        Returns False when agent has truly completed (break from loop).
        Handles: final answer detection, thinking-only detection, parallel agent wait,
        and post-generation message drain.

        Args:
            instance: The agent being executed.
            messages: Full working set of messages.

        Returns:
            True to continue the turn loop, False to break (agent complete).
        """
        inst_name = instance.instance_name

        # Check if last assistant message had a tool call (still working)
        has_tool_call = False
        for msg in reversed(messages):
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == ASSISTANT:
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                has_tool_call = fc is not None
                break

        # If tool was called — continue the loop (tool result will be in next turn)
        if has_tool_call:
            return True

        # Check for real content vs pure thinking
        last_msgs = [m for m in messages[-3:] if m.get('role') != FUNCTION]
        has_real_content = any(
            extract_text_from_message(m, add_upload_info=False).strip()
            for m in last_msgs
            if (m.get('role') == ASSISTANT or getattr(m, 'role', '') == ASSISTANT)
        )

        has_thinking = any(
            m.get('thought') or m.get('reasoning_content')
            for m in messages[-3:]
        )

        # Pure thinking turn — continue to next turn
        if not has_real_content and has_thinking:
            logger.info(f"Pure reasoning turn detected for {inst_name}. Continuing.")
            return True

        # If no real content at all — likely the agent is done
        if not has_real_content:
            # Wait for parallel agent results
            if self.pool._execution.has_active_tasks(inst_name):
                while (self.pool._execution.has_active_tasks(inst_name) and
                       not self.pool.stopped and
                       not self.pool.is_instance_halted(inst_name)):
                    time.sleep(0.5)

            # Post-generation queue drain
            if self.pool.has_messages(inst_name):
                logger.info(f"Queued messages for {inst_name} after turn completion. Looping back.")
                return True  # Loop back to process injected messages

            # Agent has truly completed
            return False

        return True  # Has real content — continue the loop

    # ═══════════════════════════════════════════════════════════════════════
    #  Tool Execution — unified path for ALL tools including call_agent
    # ═══════════════════════════════════════════════════════════════════════

    def _execute_tool(
        self, instance: AgentInstance, tool_name: str,
        tool_args, messages: List[Message]
    ) -> str:
        """Execute any tool. Including call_agent and dismiss_agent.

        For call_agent/dismiss_agent: delegates to the pool's agent management.
        For all other tools: calls through to the template's function_map.

        Args:
            instance: The agent calling the tool.
            tool_name: Name of the tool to execute.
            tool_args: Arguments for the tool (str or dict).
            messages: Current conversation messages.

        Returns:
            Tool execution result as a string.
        """
        if tool_name == 'call_agent':
            return self._handle_call_agent(tool_args, messages, instance)
        elif tool_name == 'dismiss_agent':
            return self._handle_dismiss_agent(tool_args, instance)
        elif tool_name == 'compress_context':
            return self._handle_compress_context(tool_args, messages, instance.instance_name)
        else:
            # Standard tool execution via template's function_map
            template = self.pool.templates.get(instance.agent_class)
            if not template:
                raise ValueError(f"No template for agent class {instance.agent_class}")

            # Resolve __USE_PREV_ARG__ placeholders
            if isinstance(tool_args, dict):
                from agent_cascade.tool_utils import resolve_prev_arg_placeholders
                tool_args, prev_err = resolve_prev_arg_placeholders(
                    tool_args, instance.instance_name, tool_name, self.pool
                )
                if prev_err:
                    return prev_err

            return template._call_tool(
                tool_name, tool_args,
                agent_instance_name=instance.instance_name,
                agent_obj=self,
                messages=messages,
            )

    def _handle_call_agent(self, args: dict, messages: List[Message], instance: AgentInstance) -> str:
        """Handle call_agent tool call — the unified path replacing _stream_sub_agent_call.

        Works the same for any agent calling another agent. No special paths.

        Args:
            args: Tool arguments (instance_name, agent_class, task, parallel_launch).
            messages: Caller's conversation messages.
            instance: The calling agent instance.

        Returns:
            Result string from the called agent.
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return f'Error: Invalid JSON arguments: {args}'

        instance_name = args.get('instance_name', '')
        agent_class = (args.get('agent_class') or '').strip().lower()

        if not instance_name or not agent_class:
            return "Error: call_agent requires instance_name and agent_class."

        # P2: Recursive self-call cloning — prevent state corruption on self-delegation
        with self.pool._execution._state_lock:
            if instance_name in self.pool._execution.active_stack:
                count = self.pool._execution.active_stack.count(instance_name)
                original_instance = instance_name
                instance_name = f"{instance_name}_child{count}"
                logger.info(f"Recursive self-call detected for '{original_instance}'. Cloning to '{instance_name}'.")

        # P5: Class mismatch detection — clear history if class differs on existing instance
        existing_class = self.pool.instance_classes.get(instance_name)
        if existing_class and agent_class and existing_class != agent_class:
            logger.info(
                f"Class mismatch for '{instance_name}': {existing_class} -> {agent_class}. "
                f"Clearing history to prevent context mix-up."
            )
            self.pool.clear_conversation(instance_name) if hasattr(self.pool, 'clear_conversation') else None
            # Reset logger's internal history tracking to prevent desync (reviewer Issue 3)
            try:
                log_inst = self.pool.get_logger(instance_name, agent_class)
                log_inst.data["history"] = []
            except Exception:
                pass
            # Update the instance's agent_class to the existing one (old behavior: keep existing class template)
            existing_inst = self.pool.get_instance(instance_name)
            if existing_inst:
                existing_inst.agent_class = existing_class
            # Reuse existing class — set agent_class back so we use the existing template
            agent_class = existing_class

        # Check concurrency limits
        is_parallel_allowed = True
        effective_concurrency = 0
        if self.pool.api_router:
            try:
                effective_concurrency = self.pool.api_router.get_effective_concurrency(agent_class)
            except Exception:
                pass

        if effective_concurrency == 0:
            is_parallel_allowed = False
        elif effective_concurrency > 0:
            active_count = self.pool._execution.count_by_class(agent_class)
            if active_count >= effective_concurrency:
                is_parallel_allowed = False

        # Parallel launch path
        if is_parallel_allowed and args.get('parallel_launch') is True:
            return self.pool.submit_parallel(
                agent_class, instance_name, args, messages, instance.instance_name
            )

        # Synchronous execution — the unified path
        return self._execute_agent_sync(agent_class, instance_name, args, messages, instance.instance_name)

    def _handle_dismiss_agent(self, args: dict, instance: AgentInstance) -> str:
        """Handle dismiss_agent tool call — removes another agent from pool.

        Args:
            args: Tool arguments (instance_name).
            instance: The calling agent instance.

        Returns:
            Result string confirming dismissal.
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return f'Error: Invalid JSON arguments: {args}'

        target_name = args.get('instance_name', '')
        if not target_name:
            return "Error: dismiss_agent requires instance_name."

        # Don't allow dismissing self or the root agent
        if target_name == instance.instance_name:
            return f"Error: Cannot dismiss yourself ({target_name})."
        if instance.parent_instance and target_name == instance.parent_instance:
            return f"Error: Cannot dismiss your supervisor ({target_name})."

        self.pool.dismiss_instance(target_name)
        return f"[Agent '{target_name}' dismissed successfully.]"

    # ═══════════════════════════════════════════════════════════════════════
    #  Item 7: /compress manual command handling
    # ═══════════════════════════════════════════════════════════════════════

    def _handle_compress_command(self, instance: AgentInstance, messages: List[Message]) -> bool:
        """Detect and handle /compress [fraction] user command.

        Checks the last USER message for a /compress command. If found:
          1. Parse the fraction (default 0.5)
          2. Generate a preview summary via dry_run compression
          3. Request user approval via operation_manager
          4. Apply compression if approved

        Returns True if the command was handled (whether approved or not).
        """
        inst_name = instance.instance_name

        # Find the last USER message
        last_user = None
        for msg in reversed(messages):
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == USER:
                last_user = msg
                break

        if last_user is None:
            return False

        content = last_user.get('content', '') if isinstance(last_user, dict) else getattr(last_user, 'content', '')
        if not isinstance(content, str):
            return False

        if not content.strip().startswith('/compress'):
            return False

        # Parse fraction from command (default 0.5)
        parts = content.strip().split()
        fraction = 0.5
        if len(parts) > 1:
            try:
                fraction = float(parts[1])
            except ValueError:
                pass

        # Clamp fraction to valid range
        fraction = max(0.1, min(0.9, fraction))

        # Get compress_context tool from template
        template = self.pool.templates.get(instance.agent_class)
        if not template or 'compress_context' not in getattr(template, 'function_map', {}):
            logger.warning(f"/compress command but compress_context tool unavailable for {inst_name}")
            return True

        compress_tool = template.function_map['compress_context']

        # Step 1: Generate preview summary (dry_run)
        try:
            preview_params = json.dumps({
                'fraction': fraction,
                'justification': 'MANUAL USER COMMAND (Preview)',
                'mode': 'auto',
            })
            summary = compress_tool.call(
                preview_params,
                messages=messages,
                agent_instance_name=inst_name,
                agent_obj=instance,  # Pass instance so tool can resolve agent_pool via template
                dry_run=True,  # Don't mutate pool yet
            )
        except Exception as e:
            logger.error(f"Preview compression failed for {inst_name}: {e}")
            summary = None

        if not summary or str(summary).startswith("ERROR"):
            logger.warning(f"/compress preview failed for {inst_name}: {summary}")
            self._append_system_notification(
                messages, "[SYSTEM NOTIFICATION: Compression command failed",
                f"[SYSTEM NOTIFICATION: /compress preview failed for {inst_name}. Cannot compress.]"
            )
            return True

        # Step 2: Request user approval via operation_manager
        approved = False
        rejection_reason = ""
        if self.pool.operation_manager:
            try:
                approved, rejection_reason = self.pool.operation_manager.request_user_approval(
                    agent_name=inst_name,
                    tool_name='compress_context',
                    tool_args={'fraction': fraction, 'summary': summary},
                    description=f"Proposed Compression Summary ({int(fraction*100)}% of history)",
                )
            except Exception as e:
                logger.error(f"User approval request failed for {inst_name}: {e}")
                self._append_system_notification(
                    messages, "[SYSTEM NOTIFICATION: Compression command failed",
                    f"[SYSTEM NOTIFICATION: /compress approval request failed: {e}]"
                )
                return True
        else:
            # No operation_manager — auto-approve (standalone mode)
            approved = True

        if not approved:
            logger.info(f"/compress rejected by user for {inst_name}: {rejection_reason}")
            self._append_system_notification(
                messages, "[SYSTEM NOTIFICATION: Compression cancelled",
                f"[SYSTEM NOTIFICATION: /compress cancelled by user. Reason: {rejection_reason}]"
            )
            return True

        # Step 3: Apply the compression with precomputed summary
        try:
            apply_params = json.dumps({
                'fraction': fraction,
                'justification': 'MANUAL USER COMMAND (Approved)',
                'mode': 'auto',
            })
            result = compress_tool.call(
                apply_params,
                messages=messages,
                agent_instance_name=inst_name,
                agent_obj=instance,  # Pass instance for proper pool resolution
                precomputed_summary=summary,  # Skip LLM summary generation
            )
            logger.info(f"/compress applied for {inst_name}: {result}")

            # Validate message pool after compression (Item 10)
            conv = self.pool.get_conversation(inst_name)
            if conv and not validate_message_pool(conv, inst_name):
                logger.error(f"[MSG POOL VALIDATION] Pool invalid after /compress for '{inst_name}'. Attempting recovery...")
                # Recovery: reload from logger history (same as forced compression path)
                try:
                    recov = self.pool.get_logger(inst_name, instance.agent_class).data.get('history', [])
                    if recov and validate_message_pool(recov, inst_name):
                        with instance._compression_lock:
                            self.pool.instance_conversations[inst_name] = list(recov)
                        logger.info(f"Recovered message pool after /compress for '{inst_name}' ({len(recov)} messages)")
                        # Rebuild working sets from recovered data (matches forced compression path)
                        self._rebuild_working_set(messages, llm_messages, inst_name)
                    else:
                        self._append_system_notification(
                            messages, "[SYSTEM NOTIFICATION: Compression corrupted pool",
                            f"[SYSTEM NOTIFICATION: /compress applied but message pool validation failed and recovery unsuccessful. Agent may behave unexpectedly.]"
                        )
                except Exception as e:
                    logger.error(f"Recovery after /compress failed for '{inst_name}': {e}")

            self._append_system_notification(
                messages, "[SYSTEM NOTIFICATION: Compression applied",
                f"[SYSTEM NOTIFICATION: /compress applied successfully for {inst_name}.]"
            )

        except Exception as e:
            logger.error(f"/compress apply failed for {inst_name}: {e}")
            self._append_system_notification(
                messages, "[SYSTEM NOTIFICATION: Compression command failed",
                f"[SYSTEM NOTIFICATION: /compress apply failed for {inst_name}: {e}]"
            )

        return True

    # ═══════════════════════════════════════════════════════════════════════
    #  Item 10: Message pool validation
    # ═══════════════════════════════════════════════════════════════════════

def validate_message_pool(messages: List[Message], agent_name: str) -> bool:
    """Validate message pool integrity after compression operations.

    Checks:
      - Pool is not empty
      - First message is SYSTEM (if present)
      - No excessive duplicate consecutive messages (>30%)
      - All message roles are valid non-empty strings

    Returns True if the pool is valid, False if corruption detected.
    """
    if not messages:
        logger.error(f"[MSG POOL VALIDATION] Empty message pool for agent '{agent_name}'")
        return False

    # Check first message is SYSTEM
    first = messages[0]
    first_role = first.get('role') if isinstance(first, dict) else getattr(first, 'role', '')
    if first_role != SYSTEM:
        logger.warning(f"[MSG POOL VALIDATION] First message for '{agent_name}' is not SYSTEM (got {first_role})")

    # Check for duplicate consecutive messages (compression can cause this via extend+clear issues)
    prev_content = None
    prev_role = None
    dup_count = 0
    for i, msg in enumerate(messages):
        role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
        # Increase content window from 200 to 500 chars for better precision (Issue 8 fix)
        content_key = str(content)[:500] if content else ''

        if role == prev_role and content_key == prev_content:
            dup_count += 1
            logger.warning(f"[MSG POOL VALIDATION] Duplicate consecutive msg at index {i} for '{agent_name}'")

        prev_role = role
        prev_content = content_key

    # Lower threshold from 30% to 10% — 10% duplicate consecutive msgs is suspicious (Issue 7 fix)
    if len(messages) > 5 and dup_count > len(messages) * 0.1:
        logger.error(f"[MSG POOL VALIDATION] Excessive duplicates ({dup_count}/{len(messages)}) for agent '{agent_name}'")
        return False

    # Check that roles are valid strings (not None or empty after compression)
    invalid_roles = sum(1 for m in messages if not (m.get('role') if isinstance(m, dict) else getattr(m, 'role', '')))
    if invalid_roles:
        logger.error(f"[MSG POOL VALIDATION] {invalid_roles} messages with invalid roles for agent '{agent_name}'")
        return False

    return True

    # ═══════════════════════════════════════════════════════════════════════

    def _handle_compress_context(
        self, args: dict, messages: List[Message], target_agent_name: str
    ) -> str:
        """Handle compress_context tool call — delegates to compression module.

        Args:
            args: Compression arguments (fraction, mode).
            messages: Messages to compress.
            target_agent_name: Name of the agent whose context should be compressed.

        Returns:
            Result string with compression outcome.
        """
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                return f'Error: Invalid JSON arguments: {args}'

        fraction = args.get('fraction', 0.5)
        mode = args.get('mode', 'auto')
        summary_text = args.get('summary_text')
        force = args.get('force', False)

        # Acquire per-agent lock for thread safety
        inst = self.pool.get_instance(target_agent_name)
        if not inst:
            return f"Error: Agent '{target_agent_name}' not found."

        # NOTE: Do NOT wrap compress_context in _compression_lock — it internally
        # calls agent_pool.get_conversation() which acquires the same lock.
        # Holding the outer lock + inner lock = deadlock (non-reentrant Lock).
        from agent_cascade.compression.core import compress_context as _compress
        result = _compress(
            agent_pool=self.pool,
            target_agent_name=target_agent_name,
            fraction=fraction,
            mode=mode,
            summary_text=summary_text,
            force=force,
            justification=args.get('justification', 'Agent-triggered compression'),
        )

        if result.success:
            return (f"Compression successful. Discarded {result.messages_discarded} messages. "
                    f"Tail count: {result.tail_count}.")
        else:
            return f"Compression failed: {result.error}"

    def _create_and_run_agent(
        self, agent_class: str, instance_name: str,
        args: dict, caller: str
    ) -> tuple:
        """Create an AgentInstance and run it through the unified loop.

        Shared helper used by both sync and parallel call_agent paths.
        Creates the instance, builds system + task messages, logs them,
        tracks in active_stack, and runs engine.run(inst).

        Returns:
            Tuple of (AgentInstance, conversation history).
        """
        # Create the instance
        now = time.monotonic()
        inst = AgentInstance(
            instance_name=instance_name,
            agent_class=agent_class,
            conversation=[],
            is_active=False,
            max_turns=None,  # Will be set below via settings propagation (P6)
            parent_instance=caller,
            created_at=now,
            last_activity=now,
            compression_summary=None,
            latest_marker_index=-1,
        )
        self.pool.instances[instance_name] = inst

        # Build system message for new agent
        template = self.pool.templates.get(agent_class)
        if not template:
            raise ValueError(f"No template for agent class {agent_class}")

        sys_content = getattr(template, 'base_system_message',
                              getattr(template, 'system_message', ''))
        lines = sys_content.strip().split('\n') if sys_content else []

        # Replace identity line
        if lines and f" {instance_name}" not in lines[0]:
            lines[0] = f"You are {instance_name}."

        # Insert session metadata
        meta_block = [
            "## Session Metadata",
            f"- Supervisor: {caller}",
        ]
        insert_pos = 2 if len(lines) > 1 and not lines[1].startswith("#") else 1
        for i, ml in enumerate(meta_block):
            lines.insert(insert_pos + i, ml)

        sys_msg = Message(role=SYSTEM, content="\n".join(lines))

        # Build task message
        task_text = args.get('task', '')
        context_text = args.get('context', '')
        if context_text:
            task_text = f"{context_text}\n\n{task_text}"

        # Item 9: Multimodal image propagation — scan caller's conversation for images
        # referenced in the task text and include them as multimodal content
        sub_agent_msg_content: list = [{'type': 'text', 'text': task_text}]
        added_to_sub = set()

        def _safe_get_role(msg):
            return msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')

        def _safe_get_content(msg):
            return msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')

        # Get caller's conversation history to scan for images
        caller_conv = self.pool.get_conversation(caller)
        seen_images = {}
        if caller_conv:
            for msg in caller_conv:
                content = _safe_get_content(msg)
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == 'image':
                            img_url = item_value
                            seen_images[img_url] = img_url

        # Include images that are referenced in the task text
        for img_url in seen_images.values():
            basename = img_url.split('/')[-1].split('?')[0] if '/' in img_url else img_url
            if basename in task_text and img_url not in added_to_sub:
                sub_agent_msg_content.append({'type': 'image', 'value': img_url})
                added_to_sub.add(img_url)

        # Also check the last user message for images even if not referenced in text
        if caller_conv:
            last_user_msg = None
            for m in reversed(caller_conv):
                if _safe_get_role(m) == USER:
                    last_user_msg = m
                    break
            if last_user_msg:
                content = _safe_get_content(last_user_msg)
                if isinstance(content, list):
                    for item in content:
                        item_type = item.get('type') if isinstance(item, dict) else getattr(item, 'type', None)
                        item_value = item.get('value') if isinstance(item, dict) else getattr(item, 'value', None)
                        if item_type == 'image' and item_value not in added_to_sub:
                            sub_agent_msg_content.append({'type': 'image', 'value': item_value})
                            added_to_sub.add(item_value)

        # Use multimodal content list if images found, otherwise plain text
        if len(sub_agent_msg_content) > 1:
            task_msg = Message(role=USER, content=sub_agent_msg_content)
        else:
            task_msg = Message(role=USER, content=task_text)

        # Build conversation: [system, task]
        conv = [sys_msg, task_msg]
        with inst._compression_lock:
            inst.conversation = conv

        # Log initial messages to agent's JSONL file (P1 continuation)
        try:
            log_inst = self.pool.get_logger(instance_name, agent_class)
            log_inst.log_message(sys_msg)
            log_inst.log_message(task_msg)
            # Sync internal tracking to prevent drift if logger instance differs later
            log_inst.data["history"] = [
                log_inst._format_message(sys_msg),
                log_inst._format_message(task_msg),
            ]
        except Exception:
            pass  # Logging failures must never break execution

        # P6: Settings propagation from caller agent
        # Propagate max_turns, auto_continue_enabled, and max_input_tokens
        if hasattr(self.pool, 'api_router') and self.pool.api_router:
            try:
                caller_inst = self.pool.get_instance(caller)
                if caller_inst:
                    caller_template = self.pool.templates.get(caller_inst.agent_class)
                    if caller_template and hasattr(caller_template, 'llm'):
                        llm_cfg = getattr(caller_template.llm, 'generate_cfg', {})

                        # Propagate max_turns from caller's template config
                        caller_max_turns = llm_cfg.get('max_turns') or 50
                        inst.max_turns = caller_max_turns

                        # Propagate max_input_tokens (context window limit) — thread-safe copy-write
                        supervisor_max = llm_cfg.get('max_input_tokens')
                        if supervisor_max:
                            target_template = self.pool.templates.get(agent_class)
                            if target_template and hasattr(target_template, 'llm'):
                                with self.pool._execution._state_lock:
                                    cfg = (target_template.llm.generate_cfg or {}).copy()
                                    cfg['max_input_tokens'] = supervisor_max
                                    target_template.llm.generate_cfg = cfg
            except Exception:
                pass  # Settings propagation failures should not break agent creation

        # P3: Disabled tools propagation to other agents (security/UX) — thread-safe copy-write
        if hasattr(self.pool, 'api_router') and self.pool.api_router:
            try:
                caller_inst = self.pool.get_instance(caller)
                if caller_inst:
                    caller_template = self.pool.templates.get(caller_inst.agent_class)
                    if caller_template and hasattr(caller_template, 'llm'):
                        caller_disabled_tools = getattr(caller_template.llm, 'generate_cfg', {}).get('disabled_tools')
                        if caller_disabled_tools:
                            target_template = self.pool.templates.get(agent_class)
                            if target_template and hasattr(target_template, 'llm'):
                                with self.pool._execution._state_lock:
                                    cfg = (target_template.llm.generate_cfg or {}).copy()
                                    cfg['disabled_tools'] = caller_disabled_tools
                                    target_template.llm.generate_cfg = cfg
                                logger.debug(
                                    f"Propagated disabled_tools to agent '{instance_name}': {caller_disabled_tools}"
                                )
            except Exception as e:
                logger.warning(f"Failed to propagate disabled_tools from {caller} to {instance_name}: {e}")

        # Track in active stack (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append(instance_name)

        # Item 12: Initialize sub-agent WebUI state before execution begins
        try:
            initial_state = {
                'active': True,
                'agent_name': f"{instance_name} ({agent_class})",
                'messages': list(conv),
            }
            with self.pool._execution._state_lock:
                self.pool.sub_agent_state[instance_name] = initial_state
        except Exception:
            pass  # WebUI state updates must never break execution

        try:
            # Execute through unified loop
            final_resp = []
            _update_counter = 0
            for resp in self.run(inst):
                if self.pool.stopped or self.pool.is_instance_halted(instance_name):
                    break
                final_resp = resp

                # Item 12: Throttled sub-agent WebUI state update (every 5 turns)
                _update_counter += 1
                if _update_counter % 5 == 0:
                    try:
                        current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                        state = {
                            'active': True,
                            'agent_name': f"{instance_name} ({agent_class})",
                            'messages': [dict(m) if isinstance(m, dict) else m.model_dump() if hasattr(m, 'model_dump') else m for m in current_conv + list(final_resp)],
                        }
                        with self.pool._execution._state_lock:
                            self.pool.sub_agent_state[instance_name] = state
                    except Exception:
                        pass  # WebUI state updates must never break execution

            conv.extend(final_resp)

            # Item 12: Always emit final sub-agent state after loop completes
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            try:
                current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                final_state = {
                    'active': False,  # Execution complete — agent is no longer active
                    'agent_name': f"{instance_name} ({agent_class})",
                    'messages': [dict(m) if isinstance(m, dict) else m.model_dump() if hasattr(m, 'model_dump') else m for m in current_conv + list(final_resp)],
                }
                with self.pool._execution._state_lock:
                    self.pool.sub_agent_state[instance_name] = final_state
            except Exception:
                pass  # WebUI state updates must never break execution
        finally:
            # Always clean up active stack — even on halt or error
            with self.pool._execution._state_lock:
                if instance_name in self.pool._execution.active_stack:
                    self.pool._execution.active_stack.remove(instance_name)

        return inst, conv

    # ═══════════════════════════════════════════════════════════════════════
    #  Helper methods — token counting, tool detection, truncation
    # ═══════════════════════════════════════════════════════════════════════

    def _execute_agent_sync(
        self, agent_class: str, instance_name: str,
        args: dict, caller_history: List[Message], caller: str
    ) -> str:
        """Execute an agent synchronously through the unified loop. Replaces _stream_sub_agent_call()."""
        from agent_cascade.compression.helpers import extract_sub_agent_feedback

        template = self.pool.templates.get(agent_class)
        if not template:
            return f"Error: Agent class '{agent_class}' not found."

        # Create and run via shared helper
        inst, conv = self._create_and_run_agent(agent_class, instance_name, args, caller)

        # Note: _create_and_run_agent handles active_stack cleanup via its own finally block.
        result_str = extract_sub_agent_feedback(conv, instance_name)
        return f"[{instance_name}\'s output]:\n{result_str}"

    def _get_max_tokens(self, instance: AgentInstance) -> int:
        """Resolve the effective max_input_tokens from LLM config."""
        # 1. Try API Router (handles per-endpoint MIN logic)
        if self.pool.api_router:
            try:
                router_limit = self.pool.api_router.get_effective_max_tokens(instance.agent_class.lower())
                if router_limit > 0:
                    return router_limit
            except Exception:
                pass

        # 2. Try template\'s LLM config
        template = self.pool.templates.get(instance.agent_class)
        if template and hasattr(template, 'llm'):
            llm = template.llm
            cfg = getattr(llm, 'cfg', {})
            agent_max = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens')
            if agent_max:
                return int(agent_max)

        # 3. Fallback to reasonable default
        return 128000

    def _count_history_tokens(self, messages: List[Message]) -> int:
        """Calculate total tokens in a message list."""
        try:
            from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count

            total = 0
            for msg in messages:
                role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
                func_call = (msg.get('function_call') if isinstance(msg, dict)
                             else getattr(msg, 'function_call', None))

                # For assistant with function call, count only the function call string
                if role == ASSISTANT and func_call:
                    total += qwen_count(f'{func_call}')
                    continue

                msg_obj = Message(**msg) if isinstance(msg, dict) else msg
                text = extract_text_from_message(msg_obj, add_upload_info=True)
                total += qwen_count(text)

            return total
        except Exception:
            # Fallback: rough estimate (4 chars per token)
            total_chars = sum(
                len(str(m.get('content', '') if isinstance(m, dict) else getattr(m, 'content', '')))
                for m in messages
            )
            return max(total_chars // 4, 100)

    def _detect_loop(self, messages: List[Message]):
        """Detect repetitive patterns in recent conversation. Returns (reason, pop_count) or None."""
        if len(messages) < 6:
            return None

        # Extract features from non-system messages
        def get_feature(m):
            if hasattr(m, 'model_dump'):
                m = m.model_dump()
            elif not isinstance(m, dict):
                m = {
                    'role': getattr(m, 'role', ''),
                    'content': getattr(m, 'content', ''),
                    'reasoning_content': getattr(m, 'reasoning_content', getattr(m, 'thought', '')),
                    'function_call': getattr(m, 'function_call', None),
                }

            role = m.get('role')
            content = str(m.get('content', '') or '')
            reasoning = str(m.get('reasoning_content', '') or m.get('thought', ''))
            text_feature = f"{reasoning}\n{content}" if reasoning else (content or reasoning)

            fc = m.get('function_call')
            if fc:
                name = fc.get('name') if isinstance(fc, dict) else getattr(fc, 'name', '')
                args = fc.get('arguments') if isinstance(fc, dict) else getattr(fc, 'arguments', '')
                return f"{role}:{name}:{args}"

            return f"{role}:{text_feature[:3000]}"

        # Check last 40 messages for repeated patterns
        window = messages[-40:]
        features = []
        feature_to_window_idx = []
        for i, m in enumerate(window):
            role = m.get('role') if isinstance(m, dict) else getattr(m, 'role', '')
            if role != SYSTEM:
                features.append(get_feature(m))
                feature_to_window_idx.append(i)

        if len(features) < 4:
            return None

        # Generic loop detection: pattern of length L repeating K times
        for L in range(1, 21):
            K = 3 if L < 5 else 2
            if len(features) < L * K:
                continue

            for i in range(len(features) - (L * K), -1, -1):
                pattern = features[i : i + L]
                is_loop = True
                for k in range(1, K):
                    if features[i + k * L : i + (k + 1) * L] != pattern:
                        is_loop = False
                        break

                if is_loop and features[-L:] == pattern:
                    roles = [p.split(':')[0] for p in pattern]
                    # Skip false positives: single-function/single-user patterns
                    if L == 1 and roles[0] in (FUNCTION, USER):
                        continue

                    second_rep_window_idx = feature_to_window_idx[i + L]
                    pop_count = len(window) - second_rep_window_idx
                    reason = f"Detected repeated sequence loop ({', '.join(roles)} repeating {K} times)"
                    return reason, pop_count

        return None

    def _detect_tool(self, message: Message) -> Tuple[bool, str, Any, str]:
        """Detect if a message contains a tool call. Returns (use_tool, tool_name, tool_args, text)."""
        func_call = (message.get('function_call') if isinstance(message, dict)
                     else getattr(message, 'function_call', None))
        text = (message.get('content', '') if isinstance(message, dict)
                else getattr(message, 'content', ''))

        if func_call:
            if isinstance(func_call, dict):
                return True, func_call.get('name'), func_call.get('arguments'), text
            else:
                return True, getattr(func_call, 'name', ''), getattr(func_call, 'arguments', ''), text

        return False, None, None, text or ''

    def _strip_thinking_blocks(self, text: str) -> str:
        """Remove thinking tags from reasoning content."""
        import re
        if not isinstance(text, str):
            return text
        # Remove standard think blocks
        cleaned = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
        # Also remove  blocks (common variant)
        cleaned = re.sub(r'<thought>.*?</thought>', '', cleaned, flags=re.DOTALL)
        return cleaned

    def _append_system_notification(
        self, messages: List[Message], guard_prefix: str, notification_text: str
    ):
        """Append a system notification to the last message, preventing duplicates."""
        if not messages:
            return

        last_msg = messages[-1]
        content = (last_msg.get('content') if isinstance(last_msg, dict)
                   else getattr(last_msg, 'content', None))

        if isinstance(content, str):
            if guard_prefix not in content:
                new_content = content + f"\n\n{notification_text}"
                if isinstance(last_msg, dict):
                    last_msg['content'] = new_content
                else:
                    last_msg.content = new_content
        elif isinstance(content, list):
            has_notification = any(
                (isinstance(item, dict) and guard_prefix in str(item.get('text', '')))
                or (isinstance(item, str) and guard_prefix in item)
                for item in content
            )
            if not has_notification:
                content.append({'type': 'text', 'text': notification_text})

    def _truncate_tool_result(
        self, tool_result: str, tool_name: str,
        messages: List[Message], instance_name: str
    ) -> str:
        """Truncate a tool result if it would push context past 95% capacity."""
        if not isinstance(tool_result, str):
            return tool_result

        if tool_name in ['compress_context']:
            return tool_result

        inst = self.pool.get_instance(instance_name)
        max_tokens = self._get_max_tokens(inst) if inst else 128000
        if max_tokens <= 0:
            return tool_result

        # Inline image content — skip truncation (compact markdown data)
        if '![image/' in tool_result:
            return tool_result

        try:
            from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count

            system_tokens = 0
            non_system_tokens = 0
            for msg in messages:
                role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
                msg_obj = Message(**msg) if isinstance(msg, dict) else msg
                text = extract_text_from_message(msg_obj, add_upload_info=True)
                tokens = qwen_count(text)
                if role == SYSTEM:
                    system_tokens += tokens
                else:
                    non_system_tokens += tokens

            available_tokens = max_tokens - system_tokens
            if available_tokens <= 0:
                available_tokens = max_tokens

            total_threshold = int(available_tokens * 0.95)
            per_tool_threshold = int(available_tokens * 0.25)
            result_tokens = max(1, len(tool_result) // 3)

            # Wild read detection
            wild_read_limit = 10000
            is_wild_read = len(tool_result) > wild_read_limit

            if (result_tokens <= per_tool_threshold and
                    non_system_tokens + result_tokens <= total_threshold and
                    not is_wild_read):
                return tool_result

            # Truncation required
            target_tokens = min(result_tokens, per_tool_threshold) if not is_wild_read else 500
            if non_system_tokens + target_tokens > total_threshold:
                target_tokens = max(200, total_threshold - non_system_tokens)

            truncated = tool_result[:target_tokens * 3]
            return f"{truncated}\n\n[TOOL RESPONSE TRUNCATED — reason: token budget exceeded]"

        except Exception:
            # Fallback: just truncate to a reasonable size
            if len(tool_result) > 8000:
                return f"{tool_result[:8000]}\n\n[TOOL RESPONSE TRUNCATED]"
            return tool_result