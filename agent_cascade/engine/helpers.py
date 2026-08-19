"""Module-level pure/near-pure helper functions for the execution engine.

Moved VERBATIM from ``agent_cascade/execution_engine.py`` (Phase 1 of the
module-split refactor). These are free functions / a small enum that were at
module scope in the original file; they keep their names and bodies unchanged.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from enum import Enum, auto

from agent_cascade.agent_instance import ArgumentCachePool
from agent_cascade.settings import (
    AUTO_SKILL_ENABLED,
    AUTO_SKILL_EXTRA_TURNS,
    CHARS_PER_TOKEN_ESTIMATE,
    DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE, LOAD_SKILL_AUTO,
)
from agent_cascade.llm.schema import ASSISTANT, FUNCTION, SYSTEM, USER, Message
from agent_cascade.log import logger
from agent_cascade.utils.utils import msg_field, msg_set

# ── Constants (shared engine constants; true home is this module) ──────────────
# These were module-level in the original execution_engine.py. They are used by
# the helper functions below (_normalize_thinking_blocks, _is_incomplete_state),
# so their natural home is here. core.py re-imports them from this module (core
# already imports helpers, so no circular import is introduced).
MAX_TEXT_LENGTH_FOR_REGEX = 1_000_000    # Threshold to skip expensive regex ops
MIN_OUTPUT_LENGTH = 200                  # Minimum output length for broken-json detection

# ── SleepAction Enum (Phase 3.1)
# ───────────────────────────────────────────────
class SleepAction(Enum):
    """Actions returned by _handle_sleeping_state() to control the main loop."""
    CONTINUE_LOOP = auto()  # Re-enter while loop (with possible yield)
    BREAK_LOOP = auto()     # Transitioned to COMPLETING/TERMINATED, exit while loop


def _get_active_functions_from_template(template, instance=None, pool=None) -> list:
    """
    Build the list of active function schemas from a template's function_map,
    filtering out any tools disabled via the template's LLM generate_cfg.

    Uses the centralized disabled_tools resolver for all resolution logic.
    Defense-in-depth (Security/Compressor defaults) is built into the resolver.

    Also reads from agent_pool._ui_disabled_tools for real-time tool assignment
    updates from the UI settings panel when pool is provided.

    Args:
        template: The agent template with function_map and llm.generate_cfg.
        instance: Optional AgentInstance — if provided, its _generate_cfg_override
                  takes precedence over the template config for disabled_tools.
        pool: Optional AgentPool — if provided, live UI disabled_tools are read
              from it for real-time tool assignment updates (highest priority).

    Returns:
        List of active function schema dicts (tool definitions for the LLM).
    """
    # Centralized disabled_tools resolution — see
    # agent_cascade.utils.disabled_tools
    from agent_cascade.utils.disabled_tools import resolve_disabled_tools_for_agent

    inst_name = getattr(instance, 'instance_name', 'UNKNOWN') if instance else 'NO_INSTANCE'

    # Gather inputs for the centralized resolver
    instance_override = (getattr(instance, '_generate_cfg_override', None)
                        if instance is not None else None)
    template_cfg = (getattr(template.llm, 'generate_cfg', None) or {}
                    if getattr(template, 'llm', None) is not None else {})

    # Extract disabled_tools value for logging to avoid complex f-string
    # expressions
    disabled_tools_value = None
    if isinstance(instance_override, dict) and instance_override:
        disabled_tools_value = instance_override.get('disabled_tools')
    elif instance_override is not None:
        disabled_tools_value = instance_override

    agent_name = getattr(template, 'name', '') or ''
    agent_type = getattr(template, 'agent_type', '') or ''

    # Also check instance.agent_class for defense-in-depth: the resolver uses
    # agent_type for class-default enforcement.  If template lacks agent_type
    # but the instance has a known type (security/compressor), pass it through.
    if not agent_type and instance is not None:
        iac = getattr(instance, 'agent_class', None)
        if iac:
            agent_type = iac

    disabled = resolve_disabled_tools_for_agent(
        instance_override=instance_override,
        template_cfg=template_cfg,
        agent_name=agent_name,
        agent_type=agent_type,
    )

    # Check live pool config for real-time tool updates.
    if pool is not None and hasattr(pool, 'get_ui_disabled_tools_for_agent'):
        live_disabled = pool.get_ui_disabled_tools_for_agent(agent_name, agent_type)
        disabled |= set(live_disabled or [])

    # Defensive: template.function_map may be None for templates without tools
    func_map = getattr(template, 'function_map', None)
    if not func_map:
        logger.info(f"[{inst_name}] _get_active_functions_from_template: No function_map, returning empty list")
        return []

    # Sort by name to ensure deterministic output across retries (KV cache prefix)
    # Handle MCP tools sentinel: if __all_mcp_tools__ is in disabled, block all registered MCP tools
    disable_all_mcp = '__all_mcp_tools__' in disabled
    if disable_all_mcp:
        from agent_cascade.tools.mcp_manager import MCPManager
        registered_names = MCPManager._registered_tool_names.copy()
        disabled |= {name for name in func_map.keys() if name in registered_names}

    return [func.function for name, func in sorted(func_map.items()) if name not in disabled]


def _make_token_count_callback(instance):
    """Create a callback for capturing token counts from llm/base.py (Force Compression Fix)."""
    def _on_token_count(all_tokens: int, available_token: int, max_tokens: int):
        """Callback invoked by llm/base.py after computing token counts."""
        instance._last_actual_token_count = all_tokens
        # Always update with actual max_tokens from base.py — this is the
        # ground truth
        if max_tokens > 0:  # Defensive validation
            instance._allocated_max_input_tokens = max_tokens
    return _on_token_count


def _make_usage_callback(instance, telemetry_collector):
    """Create a callback for capturing response token usage from LLM streaming layer."""
    def _on_usage(prompt_tokens: int, completion_tokens: int, details=None):
        """Called by streaming layer when usage data arrives from API."""
        # Update compression tracking with ground-truth prompt tokens
        instance._last_actual_token_count = prompt_tokens
        
        # Record in telemetry (non-blocking) — use same defensive pattern as _telemetry() helper
        if telemetry_collector is not None:
            try:
                tel_name = instance.instance_name
                telemetry_collector.record_token_usage(tel_name, prompt_tokens, completion_tokens, details)
            except Exception as e:
                from agent_cascade.log import logger
                logger.debug("Telemetry usage callback error for %s: %s", instance.instance_name, e)
    return _on_usage


def _invalidate_token_cache(instance):
    """Invalidate all token count caches after conversation mutation."""
    instance._last_actual_token_count = 0
    instance._last_token_count_conversation_length = -1


# ── Message Normalization Helpers (Phase 2 Task 2.2)
# ────────────────────────────
def _normalize_gemma_thought_tags(msg):
    """Normalize Gemma <|channel>thought tags into reasoning_content.

    Modifies msg in-place to extract thought content into reasoning_content field,
    preventing history pollution from raw thinking tags.

    Args:
        msg: Message dict or object with 'content' and 'reasoning_content' fields

    Returns:
        None (modifies msg in-place)
    """
    content = msg_field(msg, 'content', '')
    if not msg_field(msg, 'function_call') and isinstance(content, str) and '<|channel>thought' in content.lower():
        match = re.search(r'^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<\|channel>|$)', content, re.IGNORECASE)
        if match:
            reasoning_text = match.group(1).strip()
            cleaned_content = re.sub(r'^\s*<\|channel>thought\n?[\s\S]*?(?:\n?<\|channel>|$)', '', content, count=1, flags=re.IGNORECASE).strip()
            msg_set(msg, 'reasoning_content', reasoning_text)
            msg_set(msg, 'content', cleaned_content)


def _normalize_thinking_blocks(text):
    """Strip thinking blocks from the START of text to prevent tag pollution.

    Removes <thinking>...</thinking> and <thought>...</thought> tags that appear
    at the beginning of the text. Uses start-anchored regex to avoid corrupting
    content inside JSON string values (e.g., tool arguments containing tag-like text).

    TODO: Consider consolidating with strip_thinking_blocks from thinking_block.py.
    Currently kept separate as it's used for reasoning_content normalization and
    _strip_thinking_blocks method, both of which need start-anchored behavior only.

    Args:
        text: Raw text that may contain thinking tags at the start

    Returns:
        Cleaned text with leading thinking blocks removed
    """
    # Early return for very long texts to avoid expensive regex operations
    # (Issue
    if isinstance(text, str) and len(text) > MAX_TEXT_LENGTH_FOR_REGEX:
        return text
    if not isinstance(text, str):
        return text
    # Remove standard think blocks at start only (anchored with ^)
    cleaned = re.sub(r'^\s*<thinking>.*?</thinking>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Also remove <thought> blocks (common variant)
    cleaned = re.sub(r'^\s*<thought>.*?</thought>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned


# ── Embedded Tool Call Detection Constants ────────────────────────────
# LLMs sometimes leak tool call formats into reasoning/content text.
# These patterns detect and extract them before they cause infinite loops.
# Qwen format: ✿FUNCTION✿: name ... ✿ARGS✿: args
# Lookahead stops at next Qwen markers, PEG <function= tags, and </function>
_EMBEDDED_TOOL_QWEN_RE = re.compile(
    r'✿FUNCTION✿\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\n?\s*'
    r'✿ARGS✿\s*:\s*([\s\S]*?)(?=\s*✿FUNCTION✿|\s*✿RETURN✿|\s*<function=|\s*$)',
    re.IGNORECASE
)

# PEG-native format: <function=name>...<parameter>args</parameter>...</function>
_EMBEDDED_TOOL_PEG_RE = re.compile(
    r'<function=(\w+)>([\s\S]*?)</function>',
    re.IGNORECASE
)

_EMBEDDED_TOOL_PEG_PARAM_RE = re.compile(
    r'<parameter>([\s\S]*?)</parameter>',
    re.IGNORECASE
)


def _extract_tool_calls_from_text(text):
    """Extract tool calls from reasoning/content text.

    LLMs sometimes embed tool calls inside reasoning blocks instead of
    using proper function_call attributes. This extracts them.

    Returns all matches found. Callers (e.g. _detect_tool, _check_for_tool_calls_in_output)
    typically only use the first match (calls[0]) to execute a single tool per turn.

    Args:
        text: Raw text that may contain embedded tool call markers

    Returns:
        List of (name, args) tuples, empty if none found.
    """
    if not isinstance(text, str) or not text:
        return []
    results = []
    # Try Qwen format first
    for m in _EMBEDDED_TOOL_QWEN_RE.finditer(text):
        name, args = m.group(1).strip(), m.group(2).strip()
        if name:
            results.append((name, args))
    if results:
        return results
    # Fall back to peg-native format
    for m in _EMBEDDED_TOOL_PEG_RE.finditer(text):
        name = m.group(1).strip()
        body = m.group(2).strip()
        # Extract <parameter> content if present, otherwise use full body
        pm = _EMBEDDED_TOOL_PEG_PARAM_RE.search(body)
        args = pm.group(1).strip() if pm else body
        # Skip if args contain nested <function= tags (incomplete extraction)
        if name and args and '<function=' not in args:
            results.append((name, args))
    return results


def _check_message_truncation(msg):
    """Check if message was truncated (finish_reason == 'length').

    Args:
        msg: Message to check

    Returns:
        True if message indicates truncation, False otherwise
    """
    extra = msg_field(msg, 'extra')
    # Type safety check: ensure extra is a dict before calling .get() (Issue
    return extra is not None and isinstance(extra, dict) and extra.get('finish_reason') == 'length'


def _is_incomplete_state(turn_output: List[Message]) -> str | None:
    """Check if the latest LLM response indicates a malformed/incomplete output.

    Detects three cases of malformed messages (dump and continue):
    1. Reasoning-only block: has reasoning but no content and no tool calls
    2. Incomplete tool call: broken JSON arguments (bracket/brace mismatch)
    3. Empty output: no reasoning, no content, no tool calls

    Only checks the last assistant message (the one sent back to the caller).

    Args:
        turn_output: Messages from the latest LLM response

    Returns:
        A string describing the case ("reasoning-only", "broken-json", "empty-output")
        if incomplete, or None if the output looks complete.
    """

    # Find the last assistant message in reverse order
    for msg in reversed(turn_output):
        role = msg_field(msg, 'role', '')
        if role != ASSISTANT:
            continue

        # Check reasoning/thinking content
        reasoning = (msg_field(msg, 'reasoning_content') or
                     msg_field(msg, 'thought') or '')
        has_reasoning = isinstance(reasoning, str) and len(reasoning.strip()) > 1

        # Check tool calls
        func_call = msg_field(msg, 'function_call')
        has_tool_call = bool(func_call)

        # Check text content
        content = msg_field(msg, 'content', '') or ''
        if isinstance(content, list):
            text_parts = [item.get('text', '') for item in content
                         if isinstance(item, dict) and item.get('type') == 'text']
            content = ' '.join(text_parts).strip()
        elif not isinstance(content, str):
            content = str(content).strip()

        # Malformed message detection — any of these means incomplete output:
        # 1. Reasoning-only block: has reasoning but no content and no tool calls
        if has_reasoning and not content.strip() and not has_tool_call:
            return "reasoning-only"

        # 2. Incomplete tool call: has tool call with broken JSON arguments
        if has_tool_call:
            args = func_call.get('arguments', '') if isinstance(func_call, dict) else ''
            if args and isinstance(args, str):
                stripped = args.strip()
                # Count all bracket types for robustness
                open_braces = stripped.count('{')
                close_braces = stripped.count('}')
                open_brackets = stripped.count('[')
                close_brackets = stripped.count(']')
                has_mismatch = (open_braces > close_braces) or (open_brackets > close_brackets)
                # Only flag if mismatch exists, or ends with comma/quote AND has some content
                if has_mismatch or (stripped and (stripped[-1] in ',\'"') and len(stripped) < MIN_OUTPUT_LENGTH):
                    return "broken-json"

        # 3. Empty output: no reasoning, no content, no tool calls
        if not has_reasoning and not content.strip() and not has_tool_call:
            return "empty-output"

        break  # Only check the last assistant message

    return None


def _build_resources_block(pool, template, instance=None) -> str:
    """Build the '## AVAILABLE AGENTS' block reflecting current disabled_tools.

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

    content_parts = []

    # Get active functions — single source of truth for disabled_tools
    # resolution.
    # _get_active_functions_from_template reads instance override, template
    # config,
    # AND live pool config for real-time tool assignment updates.
    active_functions = _get_active_functions_from_template(template, instance, pool=pool)
    disabled_tools = set(template.function_map.keys()) - {f['name'] for f in active_functions}

    can_call_agents = 'call_agent' in template.function_map and 'call_agent' not in disabled_tools

    # List available agent types only if this agent can call other agents
    if can_call_agents:
        content_parts.append("\nAvailable Agent Types (call via call_agent):\n")
        has_agents = False
        templates_dict = getattr(pool, 'templates', {})
        for name in sorted(templates_dict.keys()):
            if name != getattr(template, 'agent_class', None):
                agent_obj = templates_dict[name]
                tagline = getattr(agent_obj, 'description', 'No description provided')
                content_parts.append(f"- **{name}**: {tagline}\n")
                has_agents = True
        if not has_agents:
            content_parts.append("- None currently available.\n")

    # Append Argument Caching Pool instructions only if the feature is enabled
    cache_enabled = getattr(getattr(pool, 'settings', None), 'cache_pool_enabled', True)
    if cache_enabled:
        content_parts.append(
            "\n### Advanced Feature: Argument Caching Pool\n"
            "The system maintains a rolling cache of tool arguments and large outputs (>1000 chars).\n"
            "Each cached entry is assigned a sequential index N. You can insert any cached entry by using\n"
            'the placeholder "{USE_CACHED_ENTRY_N}" inside any tool argument value, where N is the cache index.\n'
            "A single argument value can contain multiple placeholders, e.g.\n"
            '  content: "I found {USE_CACHED_ENTRY_12} from X and {USE_CACHED_ENTRY_23} from Y."\n'
            "Each placeholder is independently resolved and replaced with its cached value.\n"
            "Use system_info to view the current cache pool state. When entries are cached, you will see a [CACHE INFO] notification."
        )

    if not content_parts:
        return ""

    return "\n\n## AVAILABLE AGENTS\n" + "".join(content_parts)


def _build_skills_block(loaded_skills: list) -> str:
    """Build the skills section for system prompt injection.

    Formats a markdown block containing full instructions from loaded skills.
    Each skill's instructions are wrapped in a clearly labeled section so the
    agent knows which expertise applies to its current task.

    Args:
        loaded_skills: List of instruction strings (from SkillManager.resolve_load_skill).

    Returns:
        Formatted markdown block, or empty string if no skills loaded.
    """
    if not loaded_skills:
        return ""

    parts = ["\n\n## Active Skills"]
    for idx, instructions in enumerate(loaded_skills, 1):
        parts.append(f"\n### Skill {idx}\n{instructions}")

    logger.debug("[SKILLS] Built skills block with %d skill(s), total ~%d chars",
                 len(loaded_skills), sum(len(s) for s in loaded_skills))
    return '\n'.join(parts)


def _inject_skills_to_system_message(pool, instance_or_sysmsg, skills_to_inject=None):
    """Inject given skills into a system message.

    General-purpose helper used by both main agent and sub-agent paths to inject
    skill instructions into a system message. Checks for existing '## Active Skills'
    block to avoid duplicate injection.

    Accepts either:
      - an AgentInstance with conversation[0] as the system message, or
      - a Message object directly (used during instance creation before conversation exists).

    Args:
        pool: AgentPool providing skill_manager and settings.
        instance_or_sysmsg: Either an AgentInstance or a Message object whose content is modified in-place.
        skills_to_inject: Optional list of instruction strings to inject.
            If None or empty, nothing is injected (returns False).

    Returns:
        True if skills were injected, False otherwise.
        Safe to call multiple times (checks for existing '## Active Skills' block).
    """
    if not skills_to_inject:
        return False

    # Support both AgentInstance and direct Message object
    from agent_cascade.agent_instance import AgentInstance
    if isinstance(instance_or_sysmsg, AgentInstance):
        if not instance_or_sysmsg.conversation:
            return False
        sys_msg = instance_or_sysmsg.conversation[0]
    else:
        sys_msg = instance_or_sysmsg

    # Idempotency guard: skip injection if '## Active Skills' already exists.
    # This is acceptable for self-augmentation because it's a static skill — once injected
    # into a session's system message, its content doesn't change, so re-injection is redundant.
    if sys_msg.role != SYSTEM or "## Active Skills" in sys_msg.content:
        return False

    skills_block = _build_skills_block(skills_to_inject)

    # Ensure consistent order: AVAILABLE AGENTS → Active Skills.
    # If AVAILABLE AGENTS block exists, insert skills after it; otherwise append at end.
    if "## AVAILABLE AGENTS" in sys_msg.content:
        # Find the end of the AVAILABLE AGENTS block (next ## heading or end).
        # Pattern captures from the heading through content to next section boundary.
        pattern = r'## AVAILABLE AGENTS\s*(.*?)(?=\n\n##|\Z)'
        match = re.search(pattern, sys_msg.content, flags=re.DOTALL)
        if match:
            insert_pos = match.end()
            sys_msg.content = sys_msg.content[:insert_pos] + skills_block + sys_msg.content[insert_pos:]
        else:
            # Fallback: append at end if regex fails
            sys_msg.content += skills_block
    else:
        sys_msg.content += skills_block

    # Log with appropriate identifier
    if isinstance(instance_or_sysmsg, AgentInstance):
        logger.info(f"[SKILLS] Injected {len(skills_to_inject)} skill(s) into instance '{instance_or_sysmsg.instance_name}' system message")
    else:
        logger.info(f"[SKILLS] Injected {len(skills_to_inject)} skill(s) into system message")

    return True


def _inject_self_augmentation_skill(pool, instance) -> bool:
    """Inject self-augmentation skill into instance's system message when skills are enabled.

    Self-augmentation is the foundational root skill that teaches agents how to
    discover and load specialized skills at runtime. It must be injected into every
    main agent instance so they can bootstrap their own capability expansion, regardless
    of whether load_skill is AUTO or an explicit list. On session restore, only
    self-augmentation is injected (no AUTO matching against stale history); new agents
    spawned with fresh tasks get full AUTO matching.

    Args:
        pool: AgentPool instance providing skill_manager and settings.
        instance: AgentInstance whose system message may be modified in-place.

    Returns:
        True if self-augmentation skill was injected, False otherwise.
        Safe to call multiple times (checks for existing '## Active Skills' block).
    """
    from agent_cascade.settings import DEFAULT_LOAD_SKILL_MODE, LOAD_SKILL_NONE

    load_skill_value = getattr(pool.settings, 'default_load_skill_mode', DEFAULT_LOAD_SKILL_MODE)
    if isinstance(load_skill_value, str):
        load_skill_value_upper = load_skill_value.strip().upper()
    else:
        load_skill_value_upper = "AUTO"

    skill_manager = getattr(pool, 'skill_manager', None)
    skills_to_inject = []
    if not skill_manager:
        logger.debug("[SKILLS] _inject_self_augmentation_skill: no skill_manager on pool, skipping")
        return False
    if load_skill_value_upper == LOAD_SKILL_NONE:
        logger.debug("[SKILLS] _inject_self_augmentation_skill: default_load_skill_mode is NONE (skills disabled), skipping")
        return False

    # Self-augmentation is injected for any enabled mode (AUTO or explicit list).
    # It's the meta-skill that enables runtime discovery, so it must always be present.

    skill_manager._ensure_discovered()
    self_augmentation_instructions = skill_manager.load_full_instructions("self-augmentation")
    if not self_augmentation_instructions:
        logger.warning("[SKILLS] _inject_self_augmentation_skill: 'self-augmentation' skill not found in registry")
        return False

    skills_to_inject.append(self_augmentation_instructions)

    return _inject_skills_to_system_message(pool, instance, skills_to_inject)


def _get_supervisor_log_filename(pool: Any, supervisor_name: str) -> Optional[str]:
    """Get the log filename for a supervisor instance by name.

    Returns None if any step fails; never raises exceptions.
    """
    loggers = getattr(pool, 'instance_loggers', None)
    if not loggers:
        return None
    logger_inst = loggers.get(supervisor_name)
    if not logger_inst:
        return None
    log_path = getattr(logger_inst, 'log_path', None)
    if not log_path:
        return None
    return os.path.basename(str(log_path))


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

    # Root agent only knows its supervisor is the user; sub-agents get their
    # caller as supervisor
    if instance.parent_instance is None:
        meta_lines.append("- Supervisor: User")
    else:
        supervisor = instance.parent_instance
        log_filename = _get_supervisor_log_filename(pool, supervisor)
        if log_filename:
            meta_lines.append(f"- Supervisor: {supervisor} ({log_filename})")
        else:
            meta_lines.append(f"- Supervisor: {supervisor}")

    # Get workspace config from operation_manager (live source of truth),
    # falling back to logger metadata
    working_dir = "Unknown"
    extra_ro: list[str] = []
    extra_rw: list[str] = []
    log_path = "N/A"

    try:
        # Prefer operation_manager — it reflects UI config changes in real-time
        om = getattr(pool, 'operation_manager', None)
        if om is not None:
            working_dir = str(getattr(om, 'base_dir', 'Unknown'))
            # Sort paths for deterministic output (KV cache prefix across retries)
            extra_ro = sorted([str(p) for p in getattr(om, 'extra_work_folders_ro', [])])
            extra_rw = sorted([str(p) for p in getattr(om, 'extra_work_folders_rw', [])])

        # Get logger instance (needed for log_path; also used as fallback for
        # workspace config)
        try:
            log_inst = pool.get_logger(inst_name, instance.agent_class)
            log_path = getattr(log_inst, 'log_path', 'Unknown')

            # Fallback: if operation_manager unavailable, read from logger
            # metadata (may be stale)
            if om is None:
                working_dir = log_inst.data['metadata'].get('working_dir', 'Unknown')
                extra_ro = sorted(log_inst.data['metadata'].get('extra_paths_ro', []))
                extra_rw = sorted(log_inst.data['metadata'].get('extra_paths_rw', []))

        except (AttributeError, KeyError) as e:
            logger.debug("Logger metadata access failed for %s: %s", inst_name, e)

    except Exception as e:
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

    # NOTE: Use string concatenation for regex to avoid f-string {1,6}
    # quantifier being evaluated as Python expression
    escaped = re.escape(heading_prefix)
    pattern = escaped + r'.*?(?=\n\n(?:#{1,6}|---)|\Z)'

    # Find existing section to compare — if logically identical, skip
    # replacement entirely to preserve byte-identical output for KV cache
    # prefix identity across retries.
    existing_match = re.search(pattern, m0_content, flags=re.DOTALL)
    if existing_match:
        existing_content = existing_match.group(0).strip()
        new_content = new_section.strip()
        if existing_content == new_content:
            return m0_content  # No logical change — preserve exact bytes

    # Use lambda for replacement to prevent re.sub from interpreting
    # backslashes in the text as regex escapes (critical on Windows where
    # paths contain \w etc.)
    return re.sub(pattern, lambda m: new_section, m0_content, count=1, flags=re.DOTALL)


def _replace_resources_block(m0_content: str, new_block: str) -> str:
    """Replace the existing '## AVAILABLE AGENTS' block in m0 content.

    Uses a regex to find and replace the entire block (from the header line
    through to just before the next heading or --- rule or end of string).
    Delegates to _replace_section() for the actual replacement logic.

    Args:
        m0_content: The current system message content.
        new_block: The freshly built resources block to insert.

    Returns:
        The updated m0_content with the resources block replaced.
    """
    return _replace_section(m0_content, "## AVAILABLE AGENTS", new_block)
