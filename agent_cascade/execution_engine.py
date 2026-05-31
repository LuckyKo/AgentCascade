"""
Execution Engine — Phase 1 of the AgentCascade Architecture Rewrite.

Stateless execution coordinator that drives ALL agent instances through a single
unified loop. Replaces both api_server.run_agent_thread() and the old sub-agent
execution path — eliminating the structural duality.

See DESIGN_REWRITE.md §3.1 for design rationale.

Key design principle: Engine is stateless. It receives AgentInstance as a parameter
and orchestrates phases. Each phase method (~20-60 lines) is independently testable.
"""

import asyncio
import copy
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple

from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, SYSTEM, USER, Message,
)
from agent_cascade.log import logger
# Import at module level for build_stream_update_from_pool (Minor #5 from review):
# Python caches module imports in sys.modules, so this is not a performance concern.
# Kept local to _create_and_run_agent only because execution_engine shouldn't have
# hard dependency on api_integration at module scope (cleaner separation of concerns).
from agent_cascade.utils.utils import extract_text_from_message

from .agent_instance import AgentInstance, LoopDetectedError

# Maximum size for spillover files (tool output saved to disk). Prevents disk exhaustion.
MAX_SPILL_SIZE = 50 * 1024 * 1024  # 50MB


def _get_active_functions_from_template(template, instance=None) -> list:
    """
    Build the list of active function schemas from a template's function_map,
    filtering out any tools disabled via the template's LLM generate_cfg.

    Mirrors Agent._get_active_functions() from the main branch so that tool
    filtering works correctly for all agents — including the orchestrator which
    no longer has a class-specific _get_active_functions method.

    Args:
        template: The agent template with function_map and llm.generate_cfg.
        instance: Optional AgentInstance — if provided, its _generate_cfg_override
                  takes precedence over the template config for disabled_tools.
    """
    # Read disabled_tools from instance override first, then fall back to template
    if instance is not None and instance._generate_cfg_override:
        raw_disabled = instance._generate_cfg_override.get('disabled_tools', {})
    else:
        # Defensive: template.llm may be None for templates without LLM config
        llm = getattr(template, 'llm', None)
        raw_disabled = getattr(llm, 'generate_cfg', {}).get('disabled_tools', {})

    # Handle both dict format (per-agent: {"Maine": ["tool1"]}) and flat list format
    if isinstance(raw_disabled, dict):
        disabled_map = raw_disabled
        agent_name = getattr(template, 'name', None)
        disabled = set(disabled_map.get(agent_name, []))  # Mirrors main branch — safe even when name is None
        if agent_name:
            slug = agent_name.lower().replace(' ', '_')
            disabled.update(disabled_map.get(slug, []))

        agent_type = getattr(template, 'agent_type', None)
        if agent_type and agent_type in disabled_map:
            disabled.update(disabled_map[agent_type])
    elif isinstance(raw_disabled, (list, tuple)):
        disabled = set(raw_disabled)
    else:
        disabled = set()

    # Defensive: template.function_map may be None for templates without tools
    func_map = getattr(template, 'function_map', None)
    if not func_map:
        return []
    return [func.function for name, func in func_map.items() if name not in disabled]


def _build_resources_block(pool, template, instance=None) -> str:
    """Build the '--- CURRENT AVAILABLE RESOURCES' block reflecting current disabled_tools.

    This is used both during initial injection and to refresh the block when
    disabled_tools changes at runtime (e.g., user toggles tools via UI).

    Args:
        pool: The AgentPool instance (needed to list available agent types).
        template: The agent template with function_map and llm.generate_cfg.
        instance: Optional AgentInstance — if provided, its _generate_cfg_override
                  takes precedence over the template config for disabled_tools.

    Returns:
        A string containing the full resources block, or empty string if no template.
    """
    if not template or not hasattr(template, 'function_map'):
        return ""

    res = "\n\n--- CURRENT AVAILABLE RESOURCES (Auto-Injected) ---\n"

    # Determine disabled tools for this agent's class
    disabled_tools = []
    # Check instance override first, then fall back to template config
    if instance is not None and instance._generate_cfg_override is not None:
        raw_disabled = instance._generate_cfg_override.get('disabled_tools')
        if raw_disabled is None:
            # Override exists but lacks 'disabled_tools' key — fall back to template
            raw_disabled = getattr(getattr(template, 'llm', None), 'generate_cfg', {}).get('disabled_tools', [])
    else:
        raw_disabled = getattr(getattr(template, 'llm', None), 'generate_cfg', {}).get('disabled_tools', [])
    if isinstance(raw_disabled, dict):
        agent_key = getattr(template, 'agent_class', None) or getattr(template, 'name', '')
        slug = agent_key.lower().replace(' ', '_') if agent_key else ''
        disabled_tools = list(set(
            raw_disabled.get(agent_key, []) + raw_disabled.get(slug, [])
        ))
    elif isinstance(raw_disabled, (list, tuple)):
        disabled_tools = list(set(raw_disabled))

    can_call_agents = 'call_agent' in template.function_map and 'call_agent' not in disabled_tools

    # List available agent types only if this agent can call other agents
    if can_call_agents:
        res += "\nAvailable Agent Types (call via call_agent):\n"
        has_agents = False
        templates_dict = getattr(pool, 'templates', {})
        for name in sorted(templates_dict.keys()):
            if name != getattr(template, 'agent_class', None):
                agent_obj = templates_dict[name]
                tagline = getattr(agent_obj, 'description', 'No description provided')
                res += f"- **{name}**: {tagline}\n"
                has_agents = True
        if not has_agents:
            res += "- None currently available.\n"

    # List enabled tools (excluding disabled ones)
    res += "\nEnabled Tools (can change per interaction):\n"
    for t_name in sorted(template.function_map.keys()):
        if t_name in disabled_tools:
            continue
        desc = getattr(template.function_map[t_name], 'description', 'No description provided')
        res += f"- **{t_name}**: {desc}\n"

    return res


def _build_session_metadata(pool, instance) -> str:
    """Build the '## Session Metadata' section reflecting current workspace state.

    Used both during initial injection and to refresh the block each turn so that
    changes to working_dir / extra_paths are reflected immediately.

    Reads workspace configuration from the operation_manager (the live source of truth)
    rather than logger metadata, which is never updated when UI settings change.
    Falls back to logger metadata if operation_manager is unavailable.

    Args:
        pool: The AgentPool instance (needed to get logger metadata).
        instance: The agent instance whose metadata to build.

    Returns:
        A string containing the full Session Metadata section, or empty string on failure.
    """
    inst_name = instance.instance_name

    meta_lines = ["## Session Metadata"]

    # Root agent only knows its supervisor is the user; sub-agents get their caller as supervisor
    if instance.parent_instance is None:
        meta_lines.append("- Supervisor: User")
    else:
        meta_lines.append(f"- Supervisor: {instance.parent_instance}")

    # Get workspace config from operation_manager (live source of truth), falling back to logger metadata
    working_dir = "Unknown"
    extra_ro: list[str] = []
    extra_rw: list[str] = []
    log_path = "N/A"

    try:
        # Prefer operation_manager — it reflects UI config changes in real-time
        om = getattr(pool, 'operation_manager', None)
        if om is not None:
            working_dir = str(getattr(om, 'base_dir', 'Unknown'))
            extra_ro = [str(p) for p in getattr(om, 'extra_work_folders_ro', [])]
            extra_rw = [str(p) for p in getattr(om, 'extra_work_folders_rw', [])]

        # Get logger instance (needed for log_path; also used as fallback for workspace config)
        try:
            log_inst = pool.get_logger(inst_name, instance.agent_class)
            log_path = getattr(log_inst, 'log_path', 'Unknown')

            # Fallback: if operation_manager unavailable, read from logger metadata (may be stale)
            if om is None:
                working_dir = log_inst.data['metadata'].get('working_dir', 'Unknown')
                extra_ro = log_inst.data['metadata'].get('extra_paths_ro', [])
                extra_rw = log_inst.data['metadata'].get('extra_paths_rw', [])

        except (AttributeError, KeyError) as e:
            from agent_cascade.log import logger
            logger.debug("Logger metadata access failed for %s: %s", inst_name, e)

    except Exception as e:
        from agent_cascade.log import logger
        logger.debug("Session metadata build failed: %s", e)
        working_dir = os.getcwd() if hasattr(os, 'getcwd') else "Unknown"

    meta_lines.append(f"- Working Dir: {working_dir}")
    if extra_ro:
        meta_lines.append(f"- Extra Paths (Read-Only): {', '.join(extra_ro)}")
    if extra_rw:
        meta_lines.append(f"- Extra Paths (Read-Write): {', '.join(extra_rw)}")
    meta_lines.append(f"- Log Path: {log_path}")
    meta_lines.append("Use your logs to recall details from turns that were compressed.")

    return '\n'.join(meta_lines)


def _replace_section(m0_content: str, heading_prefix: str, new_section: str) -> str:
    """Replace a section in m0 content starting with a given heading up to the next heading or end.

    Generic version of _replace_resources_block that works for any ## heading or --- horizontal rule.

    Args:
        m0_content: The current system message content.
        heading_prefix: The raw heading text to search for (e.g., "## Session Metadata").
            This is NOT a regex — it gets escaped internally.
        new_section: The freshly built section to insert.

    Returns:
        The updated m0_content with the section replaced.
    """
    if not new_section:
        return m0_content  # Guard: don't delete the section if replacement is empty
    # Build pattern that matches from the heading through everything until next blank-line + heading/--- or end
    # NOTE: Use string concatenation for regex to avoid f-string {1,6} quantifier being evaluated as Python expression
    escaped = re.escape(heading_prefix)
    pattern = escaped + r'.*?(?=\n\n(?:#{1,6}|---)|\Z)'
    # Use lambda for replacement to prevent re.sub from interpreting backslashes in the text as regex escapes
    # (critical on Windows where paths like N:\work\... contain \w which would crash)
    return re.sub(pattern, lambda m: new_section.rstrip(), m0_content, count=1, flags=re.DOTALL)


def _replace_resources_block(m0_content: str, new_block: str) -> str:
    """Replace the existing '--- CURRENT AVAILABLE RESOURCES' block in m0 content.

    Uses a regex to find and replace the entire block (from the header line
    through to just before the next heading or --- rule or end of string).
    Delegates to _replace_section() for the actual replacement logic.

    Args:
        m0_content: The current system message content.
        new_block: The freshly built resources block to insert.

    Returns:
        The updated m0_content with the resources block replaced.
    """
    return _replace_section(m0_content, "--- CURRENT AVAILABLE RESOURCES", new_block)


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
        logger.debug(
            f"[CALL_AGENT_DEBUG] engine.run() ENTRY — instance={instance.instance_name}, "
            f"class={instance.agent_class}, nest_depth={getattr(instance, '_nest_depth', 'N/A')}"
        )
        instance.is_active = True  # Mark active before execution starts
        self._current_instance = instance  # Fix #2: set for token count cache lookups
        try:
            # ── Phase 1: Setup ─────────────────────────────────────────────
            messages, llm_messages, response = self._setup_turn(instance)
            if not messages:
                logger.warning(
                    f"[CALL_AGENT_DEBUG] engine.run() — early exit for instance={instance.instance_name}, "
                    f"reason=_setup_turn returned None (empty conversation or error)"
                )
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
                    logger.debug(
                        f"[CALL_AGENT_DEBUG] engine.run() — halted/stopped for instance={instance.instance_name}, "
                        f"pool_stopped={self.pool.stopped}"
                    )
                    yield response
                    continue

                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    logger.debug(
                        f"[CALL_AGENT_DEBUG] engine.run() — tool used by instance={instance.instance_name}, "
                        f"looping for next turn (turns_available={turns_available})"
                    )
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
            logger.error(
                f"[CALL_AGENT_DEBUG] engine.run() EXCEPTION for instance={instance.instance_name}: "
                f"error_type={type(e).__name__}, error={e}"
            )
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — mark inactive regardless of how we exit
            instance.is_active = False
            logger.debug(
                f"[CALL_AGENT_DEBUG] engine.run() EXIT — instance={instance.instance_name}, "
                f"is_active=False (cleanup in finally)"
            )

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
        logger.debug(
            f"[CALL_AGENT_DEBUG] _setup_turn ENTRY — instance={instance.instance_name}, "
            f"agent_class={instance.agent_class}"
        )
        inst_name = instance.instance_name

        # Load conversation from pool (single source of truth)
        with instance._compression_lock:
            conv = list(instance.conversation)
        if not conv:
            logger.warning(
                f"[CALL_AGENT_DEBUG] _setup_turn — empty conversation for instance={inst_name}, "
                f"early exit returning None"
            )
            return None, None, None

        # Load template to get system message if needed
        template = self.pool.templates.get(instance.agent_class)

        # P7: System prompt injection for ALL agents (not just root)
        # Inject identity, session metadata, available resources, and argument reuse instructions
        if len(conv) > 0:
            m0 = conv[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')
            
            # If no system message at start, inject it from template
            if m0_role != SYSTEM and template and getattr(template, 'system_message', None):
                sys_msg = Message(role=SYSTEM, content=template.system_message)
                conv.insert(0, sys_msg)
                with instance._compression_lock:
                    instance.conversation.insert(0, sys_msg)
                instance._last_token_count_conversation_length = -1
                m0 = sys_msg
                m0_role = SYSTEM

            if m0_role == SYSTEM:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str):
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are\s+\w+\."
                    if re.search(pattern, m0_content):
                        m0_content = re.sub(pattern, f"You are {inst_name}.", m0_content, count=1)
                    
                    # 2. Inject/update Session Metadata section — always rebuild each turn so changes are reflected immediately
                    meta_block = _build_session_metadata(self.pool, instance)
                    if meta_block:
                        if '## Session Metadata' in m0_content:
                            # Replace the existing block with fresh data
                            m0_content = _replace_section(m0_content, "## Session Metadata", meta_block)
                        else:
                            # First injection — insert after the identity line (same position as before)
                            content_lines = m0_content.split('\n')
                            insert_pos = 2 if len(content_lines) > 1 and not content_lines[1].startswith("#") else 1
                            for i, ml in enumerate(meta_block.split('\n')):
                                content_lines.insert(insert_pos + i, ml)
                            m0_content = '\n'.join(content_lines)
                    
                    # 3. Inject/update available resources (enabled tools always; agent types only if call_agent is available)
                    # Always rebuild this block each turn so that changes to disabled_tools are reflected immediately
                    template = self.pool.templates.get(instance.agent_class)
                    new_block = _build_resources_block(self.pool, template, instance)
                    if new_block:
                        if '--- CURRENT AVAILABLE RESOURCES' in m0_content:
                            # Replace the existing block with fresh data (handles dynamic tool changes)
                            m0_content = _replace_resources_block(m0_content, new_block)
                        else:
                            # First injection — append to end
                            m0_content += new_block
                    
                    # 4. Inject Argument Reuse instructions (static version for caching) — all agents
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
                # Fix #2: Invalidate token count cache — conversation mutated
                instance._last_token_count_conversation_length = -1
            return True  # Yield and continue loop to process new messages

        # ── /compress manual command handling (Item 7) ──────────────────────
        if self._handle_compress_command(instance, messages):
            return True  # Command handled — yield and continue

        # ── COMPRESSION CHECK (critical for long-running agents) ────────────
        max_tokens = self._get_max_tokens(instance)
        current_tokens = self._count_history_tokens(llm_messages, instance)
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
                # Fix #2: Invalidate token count cache — conversation was rebuilt by compression
                instance._last_token_count_conversation_length = -1
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
                                # Phase 3: Write directly to instance.conversation instead of via bridge
                                with instance._compression_lock:  # Thread-safe recovery write
                                    instance.conversation = list(recov)
                                # Invalidate token count cache — conversation was replaced (Fix #2)
                                instance._last_token_count_conversation_length = -1
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
        # Mirrors Agent._get_active_functions(): filter out disabled tools from function_map
        active_functions = _get_active_functions_from_template(template, instance)

        # Build the LLM call — with delta_stream=False each yielded item is a List[Message]
        # (the accumulated response so far). We iterate through all items (or until stop/halt),
        # keeping the latest accumulated result, then yield individual messages from it.
        try:
            last_output = None
            for output in self._execute_llm_call(instance, template, llm_messages, active_functions):
                last_output = output  # keep latest accumulated response FIRST
                # Check stop/halt mid-stream (after capturing the current result)
                if self.pool.stopped or self.pool.is_instance_halted(inst_name):
                    break

            if not last_output or (isinstance(last_output, list) and len(last_output) == 0):
                yield Message(role=ASSISTANT, content="[SYSTEM ERROR: Empty LLM response]")
            else:
                for msg in last_output:
                    yield msg
        except Exception as e:
            logger.error(f"LLM call failed for {inst_name}: {e}")
            yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed — {e}]")

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
            agent_type = instance.agent_class.lower()

            def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
                merged_cfg = {}
                # Use per-instance override if present, otherwise fall back to template config
                if instance._generate_cfg_override is not None:
                    merged_cfg.update(instance._generate_cfg_override)
                elif hasattr(llm, 'generate_cfg'):
                    merged_cfg.update(llm.generate_cfg)
                merged_cfg.update(llm_cfg)
                merged_cfg['agent_name'] = template.name

                return llm.chat(
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
            return llm.chat(
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

        # Fix #2: Invalidate token count cache — conversation length changed
        instance._last_token_count_conversation_length = -1  # Force cache miss on next call

        # Persist messages to JSONL log file (P1: LoggerManager migration)
        try:
            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
            for msg in turn_output:
                log_inst.log_message(msg)
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")

        # ── Auto-continue on truncation (only if user has enabled the setting) ──
        if is_truncated and not self.pool.stopped and not self.pool.is_instance_halted(inst_name) and self.pool.settings.auto_continue:
            logger.info(f"Detected message truncation for {inst_name}. Auto-continuing.")
            cont_msg = Message(
                role=USER,
                content="[SYSTEM]: Your previous response was cut off. Continue from where you left off."
            )
            messages.append(cont_msg)
            llm_messages.append(cont_msg)
            with instance._compression_lock:
                instance.conversation.append(cont_msg)
            # Fix #2: Invalidate token count cache — conversation mutated
            instance._last_token_count_conversation_length = -1
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

            # Track tool success/failure — needed for function_id matching and frontend isToolFailure()
            _tool_success = True
            _tool_error = ""

            # Telemetry: record tool call start (non-blocking)
            try:
                if hasattr(self.pool, 'telemetry'):
                    self.pool.telemetry.record_tool_call_start(inst_name, tool_name)
            except Exception:
                pass

            try:
                try:
                    tool_result = self._execute_tool(instance, tool_name, tool_args, llm_messages)
                except Exception as e:
                    logger.error(f"Tool {tool_name} failed for {inst_name}: {e}")
                    tool_result = f"Error: {e}"
                    _tool_success = False
                    _tool_error = str(e)
                    # Re-raise loop detection errors so the turn loop stops as intended
                    if isinstance(e, LoopDetectedError):
                        # tool_result already set above, so finally-block telemetry will fire before propagate
                        raise

                # Truncate if needed — track whether truncation actually occurred.
                # Non-string tool results bypass truncation and always report truncated=False.
                _was_truncated = False
                if isinstance(tool_result, str):
                    _pre_trunc_len = len(tool_result)
                    tool_result = self._truncate_tool_result(
                        tool_result, tool_name, llm_messages, inst_name
                    )
                    _was_truncated = len(tool_result) < _pre_trunc_len

                # ── Post-execution success detection ────────────────────────────────
                # Many tools return an error message as a string instead of raising an exception.
                # NOTE: This uses first-line heuristics — false positives are possible for tools
                # that return structured error-like output. Only affects telemetry metrics and
                # frontend tool status display, not execution flow.
                if _tool_success and isinstance(tool_result, str):
                    first_line = ''
                    for line in tool_result.split('\n'):
                        stripped = line.strip()
                        if stripped:
                            first_line = stripped.lower()
                            break
                    error_indicators = [
                        'error:', 'rejected by user:', 'failed:', 'invalid:',
                        'permission denied:', 'an error occurred', 'does not exist'
                    ]
                    if any(first_line.startswith(ind) for ind in error_indicators) or 'failed to' in first_line:
                        _tool_success = False
                        _tool_error = tool_result[:500]

            finally:
                # Telemetry: record tool call end (non-blocking, always called)
                try:
                    if hasattr(self.pool, 'telemetry'):
                        self.pool.telemetry.record_tool_call_end(
                            inst_name, tool_name,
                            success=_tool_success,
                            result_chars=len(tool_result) if isinstance(tool_result, str) else 0,
                            truncated=_was_truncated,
                            error=_tool_error,
                        )
                except Exception:
                    pass

            # Extract function_id from the assistant message that had the tool call
            # This is critical — without it, the LLM API can't match tool results to tool calls
            extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})

            # Track compress_context execution
            if tool_name == 'compress_context':
                self._rebuild_working_set(messages, llm_messages, inst_name)
                # Item 10: Validate message pool after agent-triggered compression
                conv = self.pool.get_conversation(inst_name)
                if conv and not validate_message_pool(conv, inst_name):
                    logger.error(f"[MSG POOL VALIDATION] Pool invalid after agent-triggered compression for '{inst_name}'")

            # Build function result message — include function_id and tool_success per OpenAI spec
            fn_msg = Message(
                role=FUNCTION,
                name=tool_name,
                content=tool_result,
                extra={
                    'function_id': extra_data.get('function_id', '1'),
                    'tool_success': _tool_success,
                },
            )
            messages.append(fn_msg)
            llm_messages.append(fn_msg)
            response.append(fn_msg)  # Stream tool result to UI (was missing)
            with instance._compression_lock:
                instance.conversation.append(fn_msg)
            # Fix #2: Invalidate token count cache — conversation mutated
            instance._last_token_count_conversation_length = -1

            # Log the function result to JSONL (was missing)
            try:
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                log_inst.log_message(fn_msg)
            except Exception:
                pass  # Logging must never block tool execution

            # ── Mid-tool urgent injection ───────────────────────────────────
            urgent = self.pool.drain_queue(inst_name)
            if urgent:
                for text in urgent:
                    if text.strip():
                        async_msg = Message(role=USER, content=text)
                        messages.append(async_msg)
                        llm_messages.append(async_msg)
                        response.append(async_msg)  # Stream urgent message to UI (was missing)
                        with instance._compression_lock:
                            instance.conversation.append(async_msg)
                # Fix #2: Invalidate token count cache — conversation mutated by urgent injection
                instance._last_token_count_conversation_length = -1
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

        # Wait for parallel agent results even if it has real content
        if self.pool._execution.has_active_tasks(inst_name):
            while (self.pool._execution.has_active_tasks(inst_name) and
                   not self.pool.stopped and
                   not self.pool.is_instance_halted(inst_name)):
                time.sleep(0.5)
                
        # Post-generation queue drain
        if self.pool.has_messages(inst_name):
            logger.info(f"Queued messages for {inst_name} after turn completion. Looping back.")
            return True  # Loop back to process injected messages

        return False  # Has real content but NO tool calls — agent is done

    # ═══════════════════════════════════════════════════════════════════════
    #  Tool Execution — unified path for ALL tools including call_agent
    # ═══════════════════════════════════════════════════════════════════════

    def _cache_tool_args(self, instance_name: str, tool_name: str, tool_args: Any) -> None:
        """Store resolved tool arguments in the per-instance cache for __USE_PREV_ARG__ reuse.

        Args are deep-copied to prevent later mutation of cached values.
        Each argument key is stored under the instance scope so that any
        subsequent tool call can reuse it by name — regardless of which tool
        originally provided it, or whether the previous call succeeded.

        Args:
            instance_name: The agent instance name (scope key).
            tool_name: Name of the tool whose args are being cached.
            tool_args: Resolved arguments (after placeholder substitution).
        """
        if not isinstance(tool_args, dict):
            return  # Nothing to cache for non-dict args

        try:
            scope = self.pool.last_tool_args.setdefault(instance_name, {})
            # Per-tool cache: most recent resolved args for this exact tool
            scope[tool_name] = copy.deepcopy(tool_args)
            # Global arg cache: union of all argument keys seen in this instance
            global_cache = scope.setdefault("__GLOBAL__", {})
            global_cache.update(copy.deepcopy(tool_args))
        except (AttributeError, TypeError):
            # Defensive: pool may not have last_tool_args in unusual setups
            logger.warning(f"Failed to cache args for tool '{tool_name}' on instance '{instance_name}': deepcopy failed")

    def _resolve_placeholders(self, tool_args: Any, instance_name: str,
                              tool_name: str) -> Optional[dict]:
        """Resolve __USE_PREV_ARG__ placeholders in tool arguments.

        If *tool_args* is a JSON string it is parsed first, then resolved.
        Resolution looks up each argument name in the global cache (which
        aggregates args from all previous tool calls in this instance).
        Unresolvable placeholders are left as-is — no error is raised so
        that regular tool use is unaffected.

        Args:
            tool_args: Raw tool arguments (dict or JSON string).
            instance_name: Agent instance name (scope key).
            tool_name: Name of the tool being called.

        Returns:
            Resolved dict on success (with or without placeholder resolution),
            or None on JSON parse failure / unexpected type.
        """
        # ── Step 1: ensure we have a dict to work with ──────────────────────
        if isinstance(tool_args, dict):
            parsed = tool_args
        elif isinstance(tool_args, str):
            try:
                parsed = json.loads(tool_args)
            except json.JSONDecodeError:
                logger.debug(
                    f"[CALL_AGENT_DEBUG] _resolve_placeholders — JSON parse failure for instance={instance_name}, "
                    f"tool={tool_name}, args_preview={str(tool_args)[:200]}"
                )
                return None  # JSON parse failure — signal error to caller
            if not isinstance(parsed, dict):
                logger.debug(
                    f"[CALL_AGENT_DEBUG] _resolve_placeholders — parsed to non-dict for instance={instance_name}, "
                    f"tool={tool_name}, type={type(parsed).__name__}"
                )
                return None  # Parsed to non-dict — signal error
        else:
            logger.debug(
                f"[CALL_AGENT_DEBUG] _resolve_placeholders — unexpected type for instance={instance_name}, "
                f"tool={tool_name}, type={type(tool_args).__name__}"
            )
            return None  # Unexpected type — signal error

        # ── Step 2: scan for placeholders (whitespace-tolerant) ────────────────
        placeholders_found = [k for k, v in parsed.items()
                              if isinstance(v, str) and v.strip() == "__USE_PREV_ARG__"]
        if not placeholders_found:
            return parsed  # No placeholders — nothing to do

        resolved_args = copy.deepcopy(parsed)

        # ── Step 3: look up each placeholder by arg name ────────────────────
        scope_cache = getattr(self.pool, 'last_tool_args', {}).get(instance_name, {})

        prev_args = scope_cache.get(tool_name)           # tool-specific fallback
        global_args = scope_cache.get("__GLOBAL__", {})  # primary lookup (all tools)

        for arg_key in placeholders_found:
            # Try global cache first (any previous tool), then tool-specific
            if arg_key in global_args:
                resolved_args[arg_key] = copy.deepcopy(global_args[arg_key])
            elif prev_args and arg_key in prev_args:
                resolved_args[arg_key] = copy.deepcopy(prev_args[arg_key])
            # else: leave the placeholder as-is — no error, just pass through

        return resolved_args

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
            # ── CALL_AGENT DEBUG: Entry point ──
            logger.debug(
                f"[CALL_AGENT_DEBUG] _execute_tool ENTRY — instance={instance.instance_name}, "
                f"tool_args_type={type(tool_args).__name__}, tool_args_preview={str(tool_args)[:200]}"
            )
            resolved = self._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            logger.debug(
                f"[CALL_AGENT_DEBUG] _resolve_placeholders returned — resolved_type={type(resolved).__name__}, "
                f"resolved_preview={str(resolved)[:200]}"
            )
            if resolved is None:
                logger.warning(
                    f"[CALL_AGENT_DEBUG] _resolve_placeholders returned None for instance {instance.instance_name} — "
                    f"this means JSON parsing failed in tool args: {str(tool_args)[:300]}"
                )
            result = self._handle_call_agent(resolved, messages, instance)
            logger.debug(
                f"[CALL_AGENT_DEBUG] _handle_call_agent returned — result_type={type(result).__name__}, "
                f"result_preview={str(result)[:200]}"
            )
            self._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        elif tool_name == 'dismiss_agent':
            resolved = self._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            result = self._handle_dismiss_agent(resolved, instance)
            self._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        elif tool_name == 'compress_context':
            resolved = self._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            result = self._handle_compress_context(resolved, messages, instance.instance_name)
            self._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result
        else:
            # Standard tool execution via template's function_map
            template = self.pool.templates.get(instance.agent_class)
            if not template:
                raise ValueError(f"No template for agent class {instance.agent_class}")

            resolved = self._resolve_placeholders(tool_args, instance.instance_name, tool_name)
            if resolved is None:
                return f"Error: Invalid JSON arguments for tool '{tool_name}'."

            result = template._call_tool(
                tool_name, resolved,
                agent_instance_name=instance.instance_name,
                agent_obj=self,
                messages=messages,
            )
            self._cache_tool_args(instance.instance_name, tool_name, resolved)
            return result

    def _handle_call_agent(self, args: Any, messages: List[Message], instance: AgentInstance) -> str:
        """Handle call_agent tool call — the unified path replacing the old sub-agent execution.

        Works the same for any agent calling another agent. No special paths.

        Args:
            args: Tool arguments (instance_name, agent_class, task, parallel_launch).
            messages: Caller's conversation messages.
            instance: The calling agent instance.

        Returns:
            Result string from the called agent.
        """
        caller_name = instance.instance_name
        logger.debug(
            f"[CALL_AGENT_DEBUG] _handle_call_agent ENTRY — caller={caller_name}, "
            f"args_type={type(args).__name__}, args_preview={str(args)[:300]}"
        )

        if args is None:
            # JSON parsing failed in _resolve_placeholders — return error
            logger.warning(f"[CALL_AGENT_DEBUG] EXIT (early) — caller={caller_name}, reason=args_is_None")
            return 'Error: Invalid JSON arguments.'

        instance_name = args.get('instance_name', '')
        agent_class = (args.get('agent_class') or '').strip().lower()

        if not instance_name or not agent_class:
            logger.warning(
                f"[CALL_AGENT_DEBUG] EXIT (early) — caller={caller_name}, "
                f"reason=missing_instance_name_or_agent_class, instance_name='{instance_name}', agent_class='{agent_class}'"
            )
            return "Error: call_agent requires instance_name and agent_class."

        logger.debug(
            f"[CALL_AGENT_DEBUG] _handle_call_agent — caller={caller_name}, target={instance_name}, class={agent_class}"
        )

        # P2: Recursive self-call cloning — prevent state corruption on self-delegation
        with self.pool._execution._state_lock:
            if any(n == instance_name for n, _depth in self.pool._execution.active_stack):
                count = sum(1 for n, _depth in self.pool._execution.active_stack if n == instance_name)
                original_instance = instance_name
                instance_name = f"{instance_name}_child{count}"
                logger.debug(f"[CALL_AGENT_DEBUG] Recursive self-call detected for '{original_instance}'. Cloning to '{instance_name}'.")
                logger.debug(
                    f"[CALL_AGENT_DEBUG] EXIT (self-call clone) — caller={caller_name}, "
                    f"original={original_instance}, cloned={instance_name}"
                )

        # P5: Class mismatch detection — clear history if class differs on existing instance
        existing_class = self.pool.instance_classes.get(instance_name)
        if existing_class and agent_class and existing_class != agent_class:
            logger.warning(
                f"[CALL_AGENT_DEBUG] EXIT (early) — caller={caller_name}, reason=class_mismatch, "
                f"target={instance_name}, existing={existing_class}, requested={agent_class}"
            )
            return (f"Error: Agent '{instance_name}' already exists as '{existing_class}'. "
                    f"Cannot create as '{agent_class}'. Use a different instance name.")

        # Nesting depth check — prevent infinite agent chains
        caller_depth = 0
        if caller_inst := self.pool.get_instance(instance.instance_name):
            caller_depth = getattr(caller_inst, '_nest_depth', 0)
        child_depth = caller_depth + 1
        max_depth = self.pool.settings.max_nesting_depth if hasattr(self.pool, 'settings') else 10
        logger.debug(
            f"[CALL_AGENT_DEBUG] Nesting depth — caller={caller_name}, caller_depth={caller_depth}, "
            f"child_depth={child_depth}, max_depth={max_depth}"
        )
        if child_depth > max_depth:
            logger.warning(
                f"[CALL_AGENT_DEBUG] EXIT (early) — caller={caller_name}, reason=nesting_depth_exceeded, "
                f"child_depth={child_depth}, max_depth={max_depth}"
            )
            return (f"Error: Nesting depth limit ({max_depth}) exceeded. "
                    f"The caller '{instance.instance_name}' is at depth {caller_depth}. "
                    f"Cannot create agent '{instance_name}' at depth {child_depth}.")

        # Check concurrency limits
        is_parallel_allowed = True
        effective_concurrency = 0
        if self.pool.api_router:
            try:
                effective_concurrency = self.pool.api_router.get_effective_concurrency(agent_class)
            except Exception as e:
                logger.debug(f"Concurrency lookup failed for {agent_class} (using default): {e}")

        if effective_concurrency == 0:
            is_parallel_allowed = False
        elif effective_concurrency > 0:
            active_count = self.pool._execution.count_by_class(agent_class)
            if active_count >= effective_concurrency:
                is_parallel_allowed = False

        logger.debug(
            f"[CALL_AGENT_DEBUG] Concurrency — is_parallel_allowed={is_parallel_allowed}, "
            f"effective_concurrency={effective_concurrency}, parallel_launch_arg={args.get('parallel_launch')}"
        )

        # Parallel launch path
        if is_parallel_allowed and args.get('parallel_launch') is True:
            logger.debug(
                f"[CALL_AGENT_DEBUG] Taking PARALLEL path — caller={caller_name}, target={instance_name}, "
                f"class={agent_class}, child_depth={child_depth}"
            )
            result = self.pool.submit_parallel(
                agent_class, instance_name, args, messages, instance.instance_name, child_depth
            )
            logger.debug(f"[CALL_AGENT_DEBUG] EXIT (parallel) — caller={caller_name}, target={instance_name}, result_preview={str(result)[:200]}")
            return result

        # Synchronous execution — the unified path
        logger.debug(
            f"[CALL_AGENT_DEBUG] Taking SYNC path — caller={caller_name}, target={instance_name}, "
            f"class={agent_class}, child_depth={child_depth}"
        )
        result = self._execute_agent_sync(agent_class, instance_name, args, messages, instance.instance_name, child_depth)
        logger.debug(f"[CALL_AGENT_DEBUG] EXIT (sync) — caller={caller_name}, target={instance_name}, result_preview={str(result)[:200]}")
        return result

    def _handle_dismiss_agent(self, args: Any, instance: AgentInstance) -> str:
        """Handle dismiss_agent tool call — removes another agent from pool.

        Args:
            args: Tool arguments (instance_name).
            instance: The calling agent instance.

        Returns:
            Result string confirming dismissal.
        """
        if args is None:
            # JSON parsing failed in _resolve_placeholders — return error
            return 'Error: Invalid JSON arguments.'

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
    #  compress_context tool handler
    # ═══════════════════════════════════════════════════════════════════════

    def _handle_compress_context(
        self, args: Any, messages: List[Message], target_agent_name: str
    ) -> str:
        """Handle compress_context tool call — delegates to compression module.

        Args:
            args: Compression arguments (fraction, mode).
            messages: Messages to compress.
            target_agent_name: Name of the agent whose context should be compressed.

        Returns:
            Result string with compression outcome.
        """
        if args is None:
            # JSON parsing failed in _resolve_placeholders — return error
            return 'Error: Invalid JSON arguments.'

        # Fix #7: Validate fraction to prevent extreme values
        fraction = max(0.1, min(0.9, args.get('fraction', 0.5)))
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
            # Fix #2: Invalidate token count cache — conversation was rebuilt by compression
            inst._last_token_count_conversation_length = -1

            # Sync logger's internal data["history"] to match pool state (Item 11)
            # Without this, update_history() will treat pool messages not yet seen by
            # the logger as "new" and append them, causing duplication.
            try:
                conv = self.pool.get_conversation(target_agent_name)
                log_inst = self.pool.get_logger(target_agent_name, inst.agent_class)
                log_inst.update_history(conv)
            except Exception as e:
                logger.error(f"Logger sync after compress_context tool FAILED for '{target_agent_name}': {e}")

            return (f"Compression successful. Discarded {result.messages_discarded} messages. "
                    f"Tail count: {result.tail_count}.")
        else:
            return f"Compression failed: {result.error}"

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
            except ValueError as e:
                logger.warning(f"Invalid fraction in /compress command for {inst_name}: {e}")

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
                        # Phase 3: Write directly to instance.conversation instead of via bridge
                        with instance._compression_lock:
                            instance.conversation = list(recov)
                        # Invalidate token count cache — conversation was replaced (Fix #2)
                        instance._last_token_count_conversation_length = -1
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

            # Fix #2: Invalidate token count cache — conversation was rebuilt by compression
            instance._last_token_count_conversation_length = -1

            # Sync logger's internal data["history"] to match pool state (Item 11)
            # Without this, update_history() will treat pool messages not yet seen by
            # the logger as "new" and append them, causing duplication.
            try:
                conv = self.pool.get_conversation(inst_name)
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                log_inst.update_history(conv)
            except Exception as e:
                logger.error(f"Logger sync after /compress FAILED for '{inst_name}': {e}")

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

    def _create_and_run_agent(
        self, agent_class: str, instance_name: str,
        args: dict, caller: str, nest_depth: int = 0
    ) -> tuple:
        """Create an AgentInstance and run it through the unified loop.

        Shared helper used by both sync and parallel call_agent paths.
        Creates the instance, builds system + task messages, logs them,
        tracks in active_stack, and runs engine.run(inst).

        Returns:
            Tuple of (AgentInstance, conversation history).

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
        """
        logger.debug(
            f"[CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target={instance_name}, class={agent_class}, "
            f"caller={caller}, nest_depth={nest_depth}"
        )
        self._create_completed = False  # Reset for this execution cycle

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
            _nest_depth=nest_depth,
        )

        # Fix #6: Warn if reusing an instance name that already exists with the same class
        existing = self.pool.instances.get(instance_name)
        if existing is not None:
            old_active = getattr(existing, 'is_active', False)
            logger.warning(
                f"[INSTANCE REUSE] '{instance_name}' ({agent_class}) is being reused. "
                f"Previous instance was {'active' if old_active else 'inactive'} — conversation will be replaced."
            )

        self.pool.instances[instance_name] = inst
        logger.debug(f"[CALL_AGENT_DEBUG] _create_and_run_agent — instance registered in pool for {instance_name}")

        # Build system message for new agent
        template = self.pool.templates.get(agent_class)
        if not template:
            logger.error(
                f"[CALL_AGENT_DEBUG] _create_and_run_agent — NO TEMPLATE for agent_class={agent_class}, "
                f"target={instance_name}, caller={caller}"
            )
            raise ValueError(f"No template for agent class {agent_class}")

        sys_content = getattr(template, 'base_system_message',
                              getattr(template, 'system_message', ''))
        lines = sys_content.strip().split('\n') if sys_content else []

        # Replace identity line
        if lines and f" {instance_name}" not in lines[0]:
            lines[0] = f"You are {instance_name}."

        # Session metadata injection is handled by P7 in _setup_turn for all agents uniformly.
        # Do NOT pre-inject here — it would cause P7's idempotency guard to skip full metadata
        # (Working Dir, Log Path, Extra Paths) for sub-agents.

        sys_msg = Message(role=SYSTEM, content="\n".join(lines))

        # Build task message
        task_text = args.get('task', '')
        context_text = args.get('context', '')
        if context_text:
            task_text = f"{context_text}\n\n{task_text}"

        # Item 9: Multimodal image propagation — scan caller's conversation for images
        # referenced in the task text and include them as multimodal content
        agent_msg_content: list = [{'type': 'text', 'text': task_text}]
        added_to_inst = set()

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
            if basename in task_text and img_url not in added_to_inst:
                agent_msg_content.append({'type': 'image', 'value': img_url})
                added_to_inst.add(img_url)

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
                        if item_type == 'image' and item_value not in added_to_inst:
                            agent_msg_content.append({'type': 'image', 'value': item_value})
                            added_to_inst.add(item_value)

        # Use multimodal content list if images found, otherwise plain text
        if len(agent_msg_content) > 1:
            task_msg = Message(role=USER, content=agent_msg_content)
        else:
            task_msg = Message(role=USER, content=task_text)

        # Build conversation: [system, task]
        conv = [sys_msg, task_msg]
        with inst._compression_lock:
            inst.conversation = conv
            # Invalidate token count cache — conversation replaced
            inst._last_token_count_conversation_length = -1

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
            except Exception as e:
                logger.debug(f"Logging initial messages for {instance_name} failed (non-critical): {e}")

        # P6+P3: Settings propagation from caller agent — merged under single lock scope
        # to prevent a race window where another thread reads partial state.
        # Propagates max_turns, max_input_tokens, and disabled_tools.
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

                        target_template = self.pool.templates.get(agent_class)
                        if not target_template or not getattr(target_template, 'llm', None):
                            # Target template has no LLM — skip settings propagation but continue execution
                            logger.warning(
                                f"Target agent '{instance_name}' ({agent_class}) template has no LLM config — "
                                f"skipping settings propagation (max_input_tokens and disabled_tools)"
                            )
                        else:
                            with self.pool._execution._state_lock:
                                # Propagate max_input_tokens (context window limit) — store on instance, NOT template
                                supervisor_max = llm_cfg.get('max_input_tokens')
                                if supervisor_max:
                                    cfg = (target_template.llm.generate_cfg or {}).copy()
                                    cfg['max_input_tokens'] = supervisor_max
                                    inst._generate_cfg_override = cfg

                                # Propagate disabled_tools — merge with any existing instance override (not overwrite)
                                caller_disabled_tools = llm_cfg.get('disabled_tools')
                                if caller_disabled_tools:
                                    cfg = dict(inst._generate_cfg_override or {}) if inst._generate_cfg_override else (target_template.llm.generate_cfg or {}).copy()
                                    existing_disabled = cfg.get('disabled_tools', [])
                                    if isinstance(existing_disabled, list):
                                        # Merge: combine existing with caller's disabled tools (deduplicated)
                                        cfg['disabled_tools'] = list(set(existing_disabled + list(caller_disabled_tools)))
                                    else:
                                        cfg['disabled_tools'] = list(caller_disabled_tools)
                                    inst._generate_cfg_override = cfg
                                    logger.debug(
                                        f"Propagated disabled_tools to agent '{instance_name}': {caller_disabled_tools}"
                                    )
            except Exception as e:
                logger.debug(f"Settings propagation from {caller} to {instance_name} failed (non-critical): {e}")

        # Track in active stack with depth info (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append((instance_name, inst._nest_depth))

        # Item 12: Initialize sub-agent WebUI state before execution begins (Fix #3: lighter snapshot)
        try:
            initial_state = {
                'active': True,
                'agent_name': f"{instance_name} ({agent_class})",
                'message_count': len(conv),
                'latest_message_summary': '',
                'conversation_length_tokens': getattr(inst, '_cached_token_count', 0),
            }
            with self.pool._execution._state_lock:
                self.pool.instance_state[instance_name] = initial_state
        except Exception as e:
            logger.debug(f"WebUI initial state update for {instance_name} failed (non-critical): {e}")

        # ── Push immediate stream_update so the tab appears without delay ──
        # Without this, the frontend won't see the new instance until the first
        # engine yield + throttle cycle (up to 300-500ms later during LLM cold start).
        try:
            ws_queue = getattr(self.pool, '_ws_send_queue', None)
            ws_loop = getattr(self.pool, '_ws_loop', None)
            if ws_queue and ws_loop and not ws_loop.is_closed():
                from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
                su = build_stream_update_from_pool(
                    pool=self.pool,
                    instance_name=caller,  # Root instance for header stats
                    responses=None,        # Reads full conversations from pool
                )
                if su is not None:
                    # Use shared _put_stream_update helper to safely handle QueueFull
                    asyncio.run_coroutine_threadsafe(
                        _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                        ws_loop,
                    )
        except Exception as e:
            logger.debug(f"Immediate sub-agent tab stream_update failed (non-critical): {e}")

        try:
            # Execute through unified loop — push stream_update events so the
            # frontend sees sub-agent tab updates independently of main agent flow.
            # Without this, the main streaming loop is blocked during tool execution
            # and no WebSocket events arrive until the sub-agent finishes.
            logger.debug(
                f"[CALL_AGENT_DEBUG] _create_and_run_agent — starting engine.run() for {instance_name}"
            )
            final_resp = []
            _update_counter = 0
            _last_sub_send = 0.0
            _sub_send_interval = 0.15  # Match main loop throttle (run_agent_unified.py line 154)
            _ws_error_count = 0       # Track consecutive WebSocket push failures
            _stream_pushing_disabled = False  # Set True after 3 consecutive failures

            for resp in self.run(inst):
                if self.pool.stopped or self.pool.is_instance_halted(instance_name):
                    break
                final_resp = resp

                # Item 12: Throttled sub-agent WebUI state update (every 5 turns) — Fix #3: lighter snapshot
                _update_counter += 1
                if _update_counter % 5 == 0:
                    try:
                        current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                        # Build a lightweight summary of the latest message instead of full dump
                        latest_summary = ''
                        if final_resp:
                            last_msg = final_resp[-1]
                            content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                            latest_summary = str(content)[:500] if content else ''
                        state = {
                            'active': True,
                            'agent_name': f"{instance_name} ({agent_class})",
                            'message_count': len(current_conv),  # inst.conversation already includes final_resp
                            'latest_message_summary': latest_summary,
                            'conversation_length_tokens': getattr(inst, '_cached_token_count', 0),
                        }
                        with self.pool._execution._state_lock:
                            self.pool.instance_state[instance_name] = state
                    except Exception as e:
                        logger.debug(f"WebUI state update for {instance_name} failed (non-critical): {e}")

                # ── Push stream_update to frontend during sub-agent execution ──
                # This is the key fix: without this, the main agent's streaming loop
                # is blocked and no WebSocket events reach the frontend. The frontend
                # relies on stream_update to call renderSubAgents() every ~200ms.
                now = time.time()  # Use time.time() for consistency with run_agent_unified.py:135
                if now - _last_sub_send >= _sub_send_interval and not _stream_pushing_disabled:
                    try:
                        ws_queue = getattr(self.pool, '_ws_send_queue', None)
                        ws_loop = getattr(self.pool, '_ws_loop', None)
                        if ws_queue and ws_loop and not ws_loop.is_closed():
                            # Import here to avoid circular import at module level
                            from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
                            # Pass root agent name (caller) for token stats — the function
                            # iterates over ALL instances in pool.instances anyway.
                            su = build_stream_update_from_pool(
                                pool=self.pool,
                                instance_name=caller,  # Root/primary instance for header stats
                                responses=None,        # Use None — reads full conversations from pool
                            )
                            if su is not None:
                                # Use shared _put_stream_update helper — QueueFull handled inside event loop
                                asyncio.run_coroutine_threadsafe(
                                    _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                                    ws_loop,
                                )
                            _last_sub_send = now
                            _ws_error_count = 0  # Reset error counter on success
                    except RuntimeError as e:
                        # RuntimeError from run_coroutine_threadsafe means event loop was closed
                        _ws_error_count += 1
                        logger.debug(f"Sub-agent stream_update push failed — event loop closed (non-critical): {e}")
                        if _ws_error_count >= 3:
                            logger.warning(
                                f"WebSocket send failed {_ws_error_count} times consecutively — "
                                f"disabling sub-agent streaming for {instance_name}"
                            )
                            _stream_pushing_disabled = True
                    except Exception as e:
                        _ws_error_count += 1
                        logger.debug(f"Sub-agent stream_update push failed (non-critical): {e}")
                        if _ws_error_count >= 3:
                            logger.warning(
                                f"WebSocket send failed {_ws_error_count} times consecutively — "
                                f"disabling sub-agent streaming for {instance_name}"
                            )
                            _stream_pushing_disabled = True

            conv.extend(final_resp)
            self._create_completed = True  # Mark for finally-block EXIT log reason tracking

            # Item 12: Always emit final sub-agent state after loop completes (Fix #3: lighter snapshot)
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            try:
                current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                latest_summary = ''
                if final_resp:
                    last_msg = final_resp[-1]
                    content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                    latest_summary = str(content)[:500] if content else ''
                final_state = {
                    'active': False,  # Execution complete — agent is no longer active
                    'agent_name': f"{instance_name} ({agent_class})",
                    'message_count': len(current_conv),  # inst.conversation already includes final_resp
                    'latest_message_summary': latest_summary,
                    'conversation_length_tokens': getattr(inst, '_cached_token_count', 0),
                }
                with self.pool._execution._state_lock:
                    self.pool.instance_state[instance_name] = final_state

                # ── Push final stream_update after sub-agent completes ──
                if not _stream_pushing_disabled:
                    try:
                        ws_queue = getattr(self.pool, '_ws_send_queue', None)
                        ws_loop = getattr(self.pool, '_ws_loop', None)
                        if ws_queue and ws_loop and not ws_loop.is_closed():
                            from agent_cascade.api_integration import build_stream_update_from_pool, _put_stream_update
                            su = build_stream_update_from_pool(
                                pool=self.pool,
                                instance_name=caller,
                                responses=None,
                            )
                            if su is not None:
                                # Use shared _put_stream_update helper — QueueFull handled inside event loop
                                asyncio.run_coroutine_threadsafe(
                                    _put_stream_update(ws_queue, {'type': 'stream_update', **su}),
                                    ws_loop,
                                )
                    except Exception as e:
                        logger.debug(f"Sub-agent final stream_update push failed (non-critical): {e}")
            except Exception as e:
                logger.debug(f"WebUI final state update for {instance_name} failed (non-critical): {e}")
        finally:
            # Always clean up active stack — even on halt or error
            with self.pool._execution._state_lock:
                for i, (name, _depth) in enumerate(self.pool._execution.active_stack):
                    if name == instance_name:
                        self.pool._execution.active_stack.pop(i)
                        break

            # Determine exit reason for debugging
            _completed = getattr(self, '_create_completed', False)
            logger.debug(
                f"[CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target={instance_name}, "
                f"reason={'completed' if _completed else 'aborted'}, "
                f"inst_type={type(inst).__name__}, conv_len={len(conv)}, "
                f"final_resp_len={len(final_resp)}"
            )

        return inst, conv

    # ═══════════════════════════════════════════════════════════════════════
    #  Helper methods — token counting, tool detection, truncation
    # ═══════════════════════════════════════════════════════════════════════

    def _execute_agent_sync(
        self, agent_class: str, instance_name: str,
        args: dict, caller_history: List[Message], caller: str, nest_depth: int = 0
    ) -> str:
        """Execute an agent synchronously through the unified loop. Replaces _stream_agent_instance_call().

        Fix #5: Acquires endpoint scheduling slot before execution (matching parallel tasks in submit_task).

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
        """
        from agent_cascade.compression.helpers import extract_instance_output

        logger.debug(
            f"[CALL_AGENT_DEBUG] _execute_agent_sync ENTRY — target={instance_name}, class={agent_class}, "
            f"caller={caller}, nest_depth={nest_depth}"
        )

        template = self.pool.templates.get(agent_class)
        if not template:
            logger.error(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync EXIT (early) — target={instance_name}, "
                f"reason=no_template, class={agent_class}"
            )
            return f"Error: Agent class '{agent_class}' not found."

        # ── Endpoint scheduling: acquire slot for root agents, skip for nested ──
        # Root agents (nest_depth=0) acquire a slot via _acquire_slot.
        # Nested agents (nest_depth>0) skip — the parent already holds the slot.
        # This prevents deadlock where both parent and child try to acquire the same slot.
        # Known trade-off: nested sync agents bypass per-endpoint concurrency enforcement.
        # This is acceptable because nested calls are serialized by the caller anyway,
        # and top-level concurrency control (via parallel task slots) limits total capacity.
        endpoint_release = None
        if nest_depth > 0:
            logger.debug(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync — SKIPPING endpoint slot acquisition for {instance_name} "
                f"(nest_depth={nest_depth}, caller already holds the slot)"
            )
        else:
            # Root-level call: acquire a slot (or log why we can't)
            if not hasattr(self.pool, '_execution') or not hasattr(self.pool._execution, '_acquire_slot'):
                logger.error(
                    f"[CALL_AGENT_DEBUG] _execute_agent_sync — fatal: pool missing _ExecutionManager "
                    f"or _acquire_slot for {instance_name}. Endpoint scheduling is unavailable."
                )
                return f"Error: Endpoint scheduling not available for '{instance_name}'."
            try:
                endpoint_release = self.pool._execution._acquire_slot(agent_class, instance_name)
                logger.debug(f"[CALL_AGENT_DEBUG] _execute_agent_sync — acquired endpoint slot for {instance_name}")
            except Exception as e:
                logger.warning(
                    f"[CALL_AGENT_DEBUG] _execute_agent_sync — warning: failed to acquire endpoint slot for {instance_name}, "
                    f"continuing without slot constraint (error={e})"
                )

        try:
            # Create and run via shared helper
            logger.debug(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync — calling _create_and_run_agent for {instance_name}"
            )
            inst, conv = self._create_and_run_agent(agent_class, instance_name, args, caller, nest_depth)

            # Check return values — Bug #1 check: ensure _create_and_run_agent didn't return None
            if inst is None or conv is None:
                logger.error(
                    f"[CALL_AGENT_DEBUG] BUG DETECTED — _create_and_run_agent returned None for {instance_name}: "
                    f"inst={inst}, conv_type={type(conv).__name__}"
                )
                return f"Error running agent '{instance_name}': Internal error — agent creation returned None."

            logger.debug(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync — _create_and_run_agent returned for {instance_name}: "
                f"inst_type={type(inst).__name__}, conv_len={len(conv)}"
            )

            # Note: _create_and_run_agent handles active_stack cleanup via its own finally block.
            result_str = extract_instance_output(conv, instance_name)
            if not result_str:
                logger.warning(
                    f"[CALL_AGENT_DEBUG] _execute_agent_sync — extract_instance_output returned empty for {instance_name}"
                )
            logger.debug(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync — extract_instance_output for {instance_name}: "
                f"result_preview={str(result_str)[:200]}"
            )
            return f"[{instance_name}\'s output]:\n{result_str}"
        except Exception as e:
            # Catch exceptions from agent creation/execution and return as clean tool error
            logger.error(
                f"[CALL_AGENT_DEBUG] _execute_agent_sync EXIT (exception) — target={instance_name}, "
                f"error_type={type(e).__name__}, error={e}"
            )
            return f"Error running agent '{instance_name}': {e}"
        finally:
            # Release endpoint slot when sync execution completes (only for root-level calls)
            if endpoint_release is not None:
                try:
                    endpoint_release()
                    logger.debug(f"[CALL_AGENT_DEBUG] _execute_agent_sync — released endpoint slot for {instance_name}")
                except Exception as e:
                    logger.warning(f"Failed to release endpoint slot for {instance_name}: {e}")

    def _get_max_tokens(self, instance: AgentInstance) -> int:
        """Resolve the effective max_input_tokens from LLM config."""
        # 1. Try API Router (handles per-endpoint MIN logic)
        if self.pool.api_router:
            try:
                router_limit = self.pool.api_router.get_effective_max_tokens(instance.agent_class.lower())
                if router_limit > 0:
                    return router_limit
            except Exception as e:
                logger.debug(f"API router max_tokens lookup failed for {instance.agent_class} (using fallback): {e}")

        # 2. Try per-instance override first (from settings propagation)
        if instance._generate_cfg_override and 'max_input_tokens' in instance._generate_cfg_override:
            return int(instance._generate_cfg_override['max_input_tokens'])

        # 3. Try template's LLM config
        template = self.pool.templates.get(instance.agent_class)
        if template and hasattr(template, 'llm'):
            llm = template.llm
            cfg = getattr(llm, 'cfg', {})
            agent_max = cfg.get('generate_cfg', {}).get('max_input_tokens') or cfg.get('max_input_tokens')
            if agent_max:
                return int(agent_max)

        # 3. Fallback to reasonable default
        return 128000

    def _count_history_tokens(self, messages: List[Message], instance: AgentInstance = None) -> int:
        """Calculate total tokens in a message list (with caching — Fix #2)."""
        try:
            from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count

            # Check cache: if conversation length hasn't changed, reuse the cached count
            inst = instance or getattr(self, '_current_instance', None)  # Prefer explicit param (thread-safe)
            if inst and inst._last_token_count_conversation_length >= 0 and len(messages) == inst._last_token_count_conversation_length:
                return inst._cached_token_count

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

            # Update cache
            if inst:
                inst._cached_token_count = total
                inst._last_token_count_conversation_length = len(messages)

            return total
        except Exception as e:
            logger.debug(f"Token counting failed (using rough estimate): {e}")
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
        """Truncate a tool result if it would push context past 95% capacity.

        Writes the full original content to a spillover file on disk when truncation occurs,
        and appends the spillover path to the truncation notice so the agent can read it back.

        call_agent is exempt from wild-read detection (the 10K char limit) — sub-agent outputs
        are structured responses, not raw data dumps.
        """
        if not isinstance(tool_result, str):
            return tool_result

        # Exempt tools with short, structured output where truncation could confuse the agent
        if tool_name in ['compress_context', 'read_file', 'write_file', 'edit_file', 'delete_file', 'copy_file', 'move_file']:
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

            # Wild read detection — exempt call_agent (sub-agent outputs are not raw data dumps)
            # Tier 1: Start with settings constant (env-var configurable via QWEN_AGENT_TOOL_RESULT_MAX_CHARS)
            from agent_cascade.settings import DEFAULT_TOOL_RESULT_MAX_CHARS
            wild_read_limit = DEFAULT_TOOL_RESULT_MAX_CHARS
            # Tier 2: Override from pool runtime config if set by UI slider (takes immediate effect)
            if hasattr(self.pool, 'llm_cfg') and self.pool.llm_cfg:
                wild_read_limit = self.pool.llm_cfg.get('tool_result_max_chars', wild_read_limit)
            is_wild_read = (len(tool_result) > wild_read_limit and tool_name != 'call_agent')

            if (result_tokens <= per_tool_threshold and
                    non_system_tokens + result_tokens <= total_threshold and
                    not is_wild_read):
                return tool_result

            # ── Truncation required — write spillover file first ─────────────────────
            spill_rel = self._write_spillover_file(tool_result, tool_name, instance_name)

            target_tokens = min(result_tokens, per_tool_threshold) if not is_wild_read else 500
            if non_system_tokens + target_tokens > total_threshold:
                target_tokens = max(200, total_threshold - non_system_tokens)

            truncated = tool_result[:target_tokens * 3]

            # Build truncation notice with spillover path — matches format used by
            # operation_manager.py and code_interpreter.py across the unified branch
            if spill_rel:
                return (
                    f"{truncated}\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. "
                    f"Full output ({len(tool_result)} chars) saved to: {spill_rel}"
                    f"\nYou can read it with read_file if needed. "
                    f"Consider compressing context before continuing.]"
                )
            else:
                return (
                    f"{truncated}\n\n[TOOL RESPONSE TRUNCATED — Character limit exceeded. "
                    f"Spillover file could not be saved (disk error). "
                    f"Consider compressing context before continuing.]"
                )

        except Exception as e:
            logger.debug(f"Tool result truncation calculation failed (using fallback): {e}")
            # Fallback: just truncate to a reasonable size
            if len(tool_result) > 8000:
                spill_rel = self._write_spillover_file(tool_result, tool_name, instance_name)
                if spill_rel:
                    return (
                        f"{tool_result[:8000]}\n\n[TOOL RESPONSE TRUNCATED — fallback path. "
                        f"Full output ({len(tool_result)} chars) saved to: {spill_rel}"
                        f"\nYou can read it with read_file if needed. "
                        f"Consider compressing context before continuing.]"
                    )
                return f"{tool_result[:8000]}\n\n[TOOL RESPONSE TRUNCATED — fallback path. Spillover file could not be saved (disk error).]"
            return tool_result

    def _write_spillover_file(
        self, tool_result: str, tool_name: str, instance_name: str
    ) -> Optional[str]:
        """Write the full tool result to a spillover file on disk.

        Returns a workspace-relative path string that the agent can use with read_file,
        or None if writing failed.

        Follows the pattern from the old branch (agent_orchestrator.py):
            - Files go to <workspace>/logs/spillover/
            - Filenames are {instance}_{tool}_{timestamp}.txt
            - Paths are normalized to forward slashes for cross-platform compatibility
            - Output is capped at MAX_SPILL_SIZE (50MB) to prevent disk exhaustion
        """
        try:
            # Resolve workspace directory via the logger manager (defensive guard)
            if not hasattr(self.pool, '_logger') or self.pool._logger is None:
                return None
            workspace_dir = self.pool._logger.workspace_dir
            log_dir = workspace_dir / 'logs' / 'spillover'
            log_dir.mkdir(parents=True, exist_ok=True)

            # Cap output to prevent disk exhaustion from massive tool results
            if len(tool_result) > MAX_SPILL_SIZE:
                tool_result = tool_result[:MAX_SPILL_SIZE] + "\n\n[SPILL FILE TRUNCATED — exceeded maximum size]"

            # Use microsecond-precision timestamp to reduce collision risk
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            safe_tool = re.sub(r'[^a-zA-Z0-9_-]', '_', tool_name)
            safe_instance = re.sub(r'[^a-zA-Z0-9_-]', '_', instance_name)
            spill_filename = f"{safe_instance}_{safe_tool}_{timestamp}.txt"
            spill_path = log_dir / spill_filename

            # Duplicate filename detection — append counter if file already exists
            counter = 1
            while spill_path.exists():
                spill_filename = f"{safe_instance}_{safe_tool}_{timestamp}_{counter}.txt"
                spill_path = log_dir / spill_filename
                counter += 1

            spill_path.write_text(tool_result, encoding='utf-8')

            # Convert to workspace-relative path so agent can use it with read_file
            try:
                spill_rel = str(spill_path.relative_to(workspace_dir)).replace('\\', '/')
            except ValueError:
                # Fallback if spill_path is outside workspace_dir
                spill_rel = str(spill_path).replace('\\', '/')

            logger.info(
                f"Wrote spillover file for '{tool_name}' result of {instance_name}: "
                f"{len(tool_result)} chars -> {spill_rel}"
            )
            return spill_rel

        except Exception as e:
            logger.error(f"Failed to write spillover file for {instance_name}/{tool_name}: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════
#  Item 10: Message pool validation (module-level utility)
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