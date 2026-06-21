"""
Execution Engine — Orchestration coordinator for the AgentCascade Architecture.

Coordinates execution of ALL agent instances through a single unified loop.
Replaces both api_server.run_agent_thread() and the old sub-agent execution path,
eliminating the structural duality. Delegates lifecycle, compression, tool dispatch,
and streaming to specialized handler classes.

See DESIGN_REWRITE.md §3.1 for design rationale.

Key design principle: Engine coordinates execution flow and delegates domain logic
to specialized handlers (LifecycleManager, CompressionHandler, ToolDispatcher, 
StreamPublisher). Each phase method (~20-60 lines) is independently testable.
"""

import asyncio
import copy
import json
import os
import re
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Optional, Tuple, Union
from enum import Enum, auto

from agent_cascade.constants import DEFAULT_SECURITY_DISABLED_TOOLS, DEFAULT_COMPRESSOR_DISABLED_TOOLS
from agent_cascade.llm.schema import (
    ASSISTANT, FUNCTION, SYSTEM, USER, Message,
)
from agent_cascade.log import logger
from agent_cascade.tool_utils import (
    MAX_SPILL_SIZE,  # Use shared constant for consistency
    mark_tool_call_truncated,
    clear_truncation_state,
    generate_spillover_filename,
)
# Import at module level for build_stream_update_from_pool (Minor #5 from review):
# Python caches module imports in sys.modules, so this is not a performance concern.
# Kept local to _create_and_run_agent only because execution_engine shouldn't have
# hard dependency on api_integration at module scope (cleaner separation of concerns).
from agent_cascade.utils.utils import extract_text_from_message

# M3: Import validate_message_pool from standalone utils module (Phase 2 Task 2.4)
# Moved to utils/pool_validation.py to break circular import chain with compression module
from .utils.pool_validation import validate_message_pool

from .agent_instance import AgentInstance, LoopDetectedError, AgentState
from .lifecycle_manager import AgentLifecycleManager
from .compression.handler import CompressionHandler
from .tool_dispatcher import ToolDispatcher
from .stream_publisher import StreamPublisher


# ── SleepAction Enum (Phase 3.1) ───────────────────────────────────────────────
class SleepAction(Enum):
    """Actions returned by _handle_sleeping_state() to control the main loop."""
    CONTINUE_LOOP = auto()  # Re-enter while loop (with possible yield)
    BREAK_LOOP = auto()     # Transitioned to COMPLETING/TERMINATED, exit while loop


# ── Message Field Accessor Helper (Phase 2 Task 2.0) ────────────────────────────
def _msg_field(msg, field: str, default=None):
    """Unified field accessor for dict or Message objects.
    
    Handles both dict format (e.g., {'role': 'user', ...}) and Message object
    format with attributes (e.g., msg.role). Used throughout execution_engine
    to avoid repetitive isinstance checks.
    
    Args:
        msg: Message object or dict with message fields
        field: Field name to access ('role', 'content', 'extra', etc.)
        default: Default value if field not found
        
    Returns:
        Field value or default
    """
    return msg.get(field, default) if isinstance(msg, dict) else getattr(msg, field, default)


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
    inst_name = getattr(instance, 'instance_name', 'UNKNOWN') if instance else 'NO_INSTANCE'
    agent_class = getattr(template, 'agent_type', getattr(template, 'name', 'UNKNOWN'))
    
    # Also check instance.agent_class for defense-in-depth
    instance_agent_class = getattr(instance, 'agent_class', None) if instance else None
    
    # logger.debug(f"[{inst_name}] _get_active_functions_from_template: agent_class='{agent_class}', instance.agent_class={instance_agent_class}")
    
    # Read disabled_tools from instance override first, then fall back to template
    if instance is not None and instance._generate_cfg_override is not None:
        raw_disabled = instance._generate_cfg_override.get('disabled_tools')
        if raw_disabled is None:
            # Override exists but lacks 'disabled_tools' key — fall back to template
            llm = getattr(template, 'llm', None)
            if llm is None:
                logger.warning(f"[{inst_name}] template.llm is None, using empty disabled_tools")
                raw_disabled = []
            else:
                raw_disabled = getattr(llm, 'generate_cfg', {}).get('disabled_tools', [])
        # elif not raw_disabled:  # Empty — nothing to log
    else:
        # Defensive: template.llm may be None for templates without LLM config
        llm = getattr(template, 'llm', None)
        if llm is None:
            logger.warning(f"[{inst_name}] template.llm is None, using empty disabled_tools")
            raw_disabled = []
        else:
            raw_disabled = getattr(llm, 'generate_cfg', {}).get('disabled_tools', [])

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

    # ── Agent-class-based default disabled tools (defense-in-depth) ──────────────
    # Regardless of whether upstream code set _generate_cfg_override, always apply
    # the agent-type-specific defaults.  This is the ultimate safety net: even if
    # every other layer fails, Security and Compressor agents will never get more
    # tools than intended.
    
    # Check BOTH template.agent_type AND instance.agent_class for defense-in-depth
    is_security_agent = (agent_class == 'Security' or instance_agent_class == 'Security')
    is_compressor_agent = (agent_class == 'Compressor' or instance_agent_class == 'Compressor')
    
    if is_security_agent:
        disabled = disabled | DEFAULT_SECURITY_DISABLED_TOOLS
        # logger.debug(f"[{inst_name}] Applied Security-agent defaults: {len(DEFAULT_SECURITY_DISABLED_TOOLS)} tools added")
    elif is_compressor_agent:
        disabled = disabled | DEFAULT_COMPRESSOR_DISABLED_TOOLS
        # logger.debug(f"[{inst_name}] Applied Compressor-agent defaults: {len(DEFAULT_COMPRESSOR_DISABLED_TOOLS)} tools added")

    # logger.debug(f"[{inst_name}] _get_active_functions_from_template: disabled_tools={disabled}")

    # Defensive: template.function_map may be None for templates without tools
    func_map = getattr(template, 'function_map', None)
    if not func_map:
        logger.info(f"[{inst_name}] _get_active_functions_from_template: No function_map, returning empty list")
        return []
    
    # Build the active functions list and log what's being filtered out
    all_tool_names = set(func_map.keys())
    filtered_out = all_tool_names & disabled
    active_tool_names = all_tool_names - disabled
    
    # logger.debug(f"[{inst_name}] _get_active_functions_from_template: {len(all_tool_names)} tools total, {len(filtered_out)} filtered, {len(active_tool_names)} active")
    
    # Check specifically for shell_cmd — warn if Security agent has it enabled
    if 'shell_cmd' in all_tool_names and is_security_agent and 'shell_cmd' not in disabled:
        logger.warning(f"[{inst_name}] CRITICAL: shell_cmd is NOT disabled for Security agent!")
    
    return [func.function for name, func in func_map.items() if name not in disabled]


def _make_token_count_callback(instance):
    """Create a callback for capturing token counts from llm/base.py (Force Compression Fix)."""
    def _on_token_count(all_tokens: int, available_token: int, max_tokens: int):
        """Callback invoked by llm/base.py after computing token counts."""
        instance._last_actual_token_count = all_tokens
        # Always update with actual max_tokens from base.py — this is the ground truth
        if max_tokens > 0:  # Defensive validation
            instance._allocated_max_input_tokens = max_tokens
    return _on_token_count


def _invalidate_token_cache(instance):
    """Invalidate all token count caches after conversation mutation."""
    instance._last_actual_token_count = 0
    instance._last_token_count_conversation_length = -1


# ── Token Cache Invalidation Context Manager (Phase 2 Task 2.1) ────────────────
@contextmanager
def token_cache_invalidated(instance):
    """Context manager that ensures token cache is invalidated after conversation mutation.
    
    PR3 Note: Most callers should now use centralized API methods like append_message(),
    trim_tail(), rebuild_conversation() which handle cache invalidation automatically.
    This context manager is useful for legacy code or when multiple mutations need to
    be batched under a single lock with deferred cache invalidation.
    
    Usage (legacy pattern):
        with token_cache_invalidated(instance):
            instance.conversation.append(new_msg)
        # Token cache automatically invalidated here
    
    Usage (PR3 preferred pattern):
        instance.append_message(new_msg)  # Cache handled automatically
        
    Args:
        instance: AgentInstance whose token cache should be invalidated on exit
        
    Yields:
        None (context manager for wrapping conversation mutations)
    """
    try:
        yield
    finally:
        _invalidate_token_cache(instance)


# ── Message Normalization Helpers (Phase 2 Task 2.2) ────────────────────────────
def _normalize_gemma_thought_tags(msg):
    """Normalize Gemma <|channel>thought tags into reasoning_content.
    
    Modifies msg in-place to extract thought content into reasoning_content field,
    preventing history pollution from raw thinking tags.
    
    Args:
        msg: Message dict or object with 'content' and 'reasoning_content' fields
        
    Returns:
        None (modifies msg in-place)
    """
    content = _msg_field(msg, 'content', '')
    if not _msg_field(msg, 'function_call') and isinstance(content, str) and '<|channel>thought' in content.lower():
        match = re.search(r'^\s*<\|channel>thought\n?([\s\S]*?)(?:\n?<\|channel>|$)', content, re.IGNORECASE)
        if match:
            reasoning_text = match.group(1).strip()
            cleaned_content = re.sub(r'^\s*<\|channel>thought\n?[\s\S]*?(?:\n?<\|channel>|$)', '', content, count=1, flags=re.IGNORECASE).strip()
            if isinstance(msg, dict):
                msg['reasoning_content'] = reasoning_text
                msg['content'] = cleaned_content
            else:
                msg.reasoning_content = reasoning_text
                msg.content = cleaned_content


def _normalize_thinking_blocks(text):
    """Strip thinking blocks from text to prevent tag pollution.
    
    Removes <thinking>...</thinking> and <thought>...</thought> tags from text.
    
    Args:
        text: Raw text that may contain thinking tags
        
    Returns:
        Cleaned text with thinking blocks removed
    """
    # Early return for very long texts to avoid expensive regex operations (Issue #5)
    if isinstance(text, str) and len(text) > 1_000_000:
        return text
    if not isinstance(text, str):
        return text
    # Remove standard think blocks
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.DOTALL)
    # Also remove <thought> blocks (common variant)
    cleaned = re.sub(r'<thought>.*?</thought>', '', cleaned, flags=re.DOTALL)
    return cleaned


def _normalize_tool_arguments(func_call):
    """Clean thinking blocks from function call arguments.
    
    Modifies func_call in-place to remove thinking tags that may have leaked
    into the arguments field during LLM generation.
    
    Args:
        func_call: FunctionCall object or dict with 'arguments' field
    """
    if isinstance(func_call, dict) and func_call.get('arguments'):
        func_call['arguments'] = _normalize_thinking_blocks(func_call['arguments'])
    elif hasattr(func_call, 'arguments'):
        func_call.arguments = _normalize_thinking_blocks(func_call.arguments)


def _check_message_truncation(msg):
    """Check if message was truncated (finish_reason == 'length').
    
    Args:
        msg: Message to check
        
    Returns:
        True if message indicates truncation, False otherwise
    """
    extra = _msg_field(msg, 'extra')
    # Type safety check: ensure extra is a dict before calling .get() (Issue #3)
    return extra is not None and isinstance(extra, dict) and extra.get('finish_reason') == 'length'


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
        agent_key = getattr(template, 'agent_type', None) or getattr(template, 'name', '')
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
    # Descriptions are omitted — LLM already knows them via native function schemas
    active_functions = _get_active_functions_from_template(template, instance)
    enabled_tools = sorted(f['name'] for f in active_functions)
    if enabled_tools:
        res += "\nEnabled Tools (can change per interaction): " + ", ".join(enabled_tools) + "\n"

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
    """Core execution coordinator — delegates to specialized handlers.
    
    Responsibilities:
    - Main turn loop orchestration
    - Phase dispatch (setup → pre-check → LLM → process → post-check)
    - State machine transitions
    - Delegation to LifecycleManager, CompressionHandler, ToolDispatcher, StreamPublisher
    
    After Phase 4 refactoring: ExecutionEngine class is ~2,400 lines (down from original ~3,727).
    Total file size: ~2,800 lines (includes module-level helper functions and delegation wrappers).
    
    Uses two-phase initialization:
    1. __init__() creates handlers with pool only (no engine reference)
    2. initialize() sets cross-references after all objects constructed
    
    Every agent (including the root/top-level agent) goes through this same engine.
    There is no separate execution path for any agent type.
    """

    def __init__(self, pool):
        """Initialize with a reference to the AgentPool.

        Two-phase initialization:
        1. Creates all handlers with pool reference
        2. Calls initialize() to set engine references (breaks circular dependencies)

        Args:
            pool: The AgentPool instance that manages all agent state.
        """
        self.pool = pool
        # Phase 4.1: Initialize lifecycle manager with lazy engine reference
        self.lifecycle = AgentLifecycleManager(pool)
        # Phase 4.2: Initialize compression handler with lazy engine reference
        self.compression_handler = CompressionHandler(pool)
        # Phase 4.3: Initialize tool dispatcher with lazy engine reference
        self.tool_dispatcher = ToolDispatcher(pool)
        # Phase 4.4: Initialize stream publisher with lazy engine reference
        self.stream_publisher = StreamPublisher(pool)
        
        # Two-phase initialization: set engine references after all handlers created
        self.initialize()
    
    def initialize(self) -> None:
        """Complete initialization after __init__.
        
        Sets the engine reference on handlers that need it (lifecycle, compression, tool dispatcher)
        to break circular dependencies. StreamPublisher does not require an engine reference.
        
        Called automatically from __init__ for transparent two-phase initialization.
        """
        self.lifecycle.set_engine(self)
        self.compression_handler.set_engine(self)
        self.tool_dispatcher.set_engine(self)
        # stream_publisher doesn't need engine reference (per refactor plan line 2190)

    # ── Slot acquisition helper (fixes 3x duplication) ─────────────────────

    def _acquire_slot_with_logging(self, instance: AgentInstance, context: str = "initial") -> None:
        """Acquire concurrency slot with debug logging.

        Args:
            instance: The agent instance acquiring the slot
            context: Description of acquisition context ("initial", "after_async_wakeup", etc.)
        """
        if not hasattr(self.pool, '_acquire_slot'):
            return

        try:
            logger.debug(
                f"[SLOT_ACQUIRE] {context} - instance={instance.instance_name}, "
                f"class={instance.agent_class}"
            )
            instance._slot_release = self.pool._acquire_slot(
                instance.agent_class, instance.instance_name
            )
            logger.debug(
                f"[SLOT_ACQUIRED] {context} - instance={instance.instance_name}, "
                f"has_callback={instance._slot_release is not None}"
            )
        except Exception as e:
            logger.error(f"[SLOT_ACQUIRE_FAILED] {context} for {instance.instance_name}: {e}")
            raise

    # ── Unified injection helpers ──────────────────────────────────────────

    def _drain_and_inject(
        self,
        instance: AgentInstance,
        inst_name: str,
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message],
        *,  # Everything below is keyword-only — prevents positional confusion
        drain_fn: Optional[Callable[[str], Any]] = None,   # Drain mode: callable that takes inst_name and returns data
        items: Optional[Any] = None,                        # Items mode: already-drained data to inject
        factory: Callable[[Any], Message],                  # Converts raw item → Message
        log_level: str = "debug",                           # Most injection points are debug-level
    ) -> bool:
        """Drain a queue/buffer and inject results as USER messages into all working lists.

        Messages are appended atomically to all working lists (messages, llm_messages,
        response, instance.conversation) under instance._compression_lock, ensuring
        no length mismatches between cached lists and conversation. Wrapped in
        token_cache_invalidated() context manager to ensure FULL cache invalidation.

        Exactly one of drain_fn or items must be provided.

        Returns True if any messages were injected, False otherwise.
        """
        # Get data from either mode
        if items is not None:
            raw_data = items
        elif drain_fn is not None:
            raw_data = drain_fn(inst_name)
        else:
            return False

        if not raw_data:
            return False

        level = logger.info if log_level == "info" else logger.debug
        level(f"Draining {len(raw_data)} item(s) for {inst_name}.")

        # Pre-process all items into messages to avoid calling factory() twice
        processed_messages = []
        for item in raw_data:
            msg = factory(item)
            if msg.content.strip():  # Skip empty messages
                processed_messages.append(msg)

        if not processed_messages:
            return True

        with instance._compression_lock:
            for msg in processed_messages:
                # Append to all working lists atomically under the compression_lock.
                # This ensures cached lists and instance.conversation stay in sync,
                # preventing silent cache rebuilds on next turn (Fix 1).
                messages.append(msg)
                llm_messages.append(msg)
                response.append(msg)
                instance.append_message(msg)  # PR2: centralized mutation API handles cache sync
                
                # Mark activity since we're bypassing pool.add_message() which used to do this (Fix 4)
                self.pool._mark_activity(inst_name)

            # Log messages outside the lock to minimize hold time (logging can be slow)
            for msg in processed_messages:
                try:
                    log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                    log_inst.log_message(msg)
                except Exception as e:
                    logger.debug(f"Logging failed for {inst_name} (non-critical): {e}")

        return True

    @staticmethod
    def _make_user_message(text: str) -> Message:
        """Create a USER message from raw text."""
        return Message(role=USER, content=text)

    @staticmethod
    def _make_async_result_message(tuple_data: Tuple[str, Optional[str]]) -> Message:
        """Create a USER message from an async result tuple (content, function_id)."""
        result_content, function_id = tuple_data
        prefix = f"[BACKGROUND TOOL RESULT for {function_id}]" if function_id else "[BACKGROUND TOOL RESULT]"
        return Message(role=USER, content=f"{prefix}: {result_content}")

    # ═══════════════════════════════════════════════════════════════════════
    #  Main Execution Loop — Core turn loop orchestration and phase dispatch
    # ═══════════════════════════════════════════════════════════════════════

    def run(self, instance: AgentInstance) -> Iterator[Union[List[Message], tuple[List[Message], bool]]]:
        """Execute the agent's turn loop as a generator yielding state updates.

        This is THE execution entry point for ALL agents. No separate paths
        for any agent type. The root agent is just the first instance
        created in the pool.

        Args:
            instance: The AgentInstance to execute.

        Yields:
            Union[List[Message], tuple[List[Message], bool]]: Either a list of messages,
                or a tuple of (messages_list, is_streaming_bool) during LLM streaming phases.
                Consumers should unpack tuples before extending conversations to avoid bool leaks.
        """
        logger.debug("engine.run() ENTRY - instance=%s", instance.instance_name)
        # Transition to RUNNING state (replaces is_active=True)
        with instance._state_lock:
            if instance.state == AgentState.IDLE:
                instance._transition(AgentState.RUNNING)
            else:
                # Safety net: If we reach here, the L1 session_lock guard in api_server.py
                # failed to prevent a race condition. Raise to surface the bug instead of
                # silent return.
                raise RuntimeError(
                    f"[BUG] {instance.instance_name} entered engine.run() in state "
                    f"{instance.state.name} — should be IDLE. L1 race guard failed!"
                )
        self._current_instance = instance  # Fix #2: set for token count cache lookups
        
        # Clear truncation state at the start of each agent turn to prevent stale markers
        clear_truncation_state()
        
        # Initialize variables before try block to handle exceptions during _setup_turn
        messages = None
        llm_messages = None
        response = None
        
        # ── Acquire concurrency slot for this agent's endpoint ───────────────
        # On sequential endpoints (concurrency_limit=0), only one agent should 
        # be making API calls at a time. The parent acquires the slot, then releases
        # it when transitioning to SLEEPING so children can proceed.
        
        # SLOT_BYPASS FIX: Skip slot acquisition if _skip_slot_acquire is set.
        # This allows nested agents (Security, Compressor) to run without acquiring
        # their own slot when invoked within an existing turn. The caller holds the
        # slot throughout, preventing deadlock from release→nested→reacquire cycles.
        # Cache this once at the top since it never changes during engine.run().
        skip_slot_acquire = getattr(instance, '_skip_slot_acquire', False)

        if not skip_slot_acquire:
            instance._slot_release = None  # Initialize for proper cleanup in finally block
            self._acquire_slot_with_logging(instance, "initial")
        else:
            # Bypass mode — nested agent (Security/Compressor) running within existing turn
            logger.debug(
                f"[SLOT_BYPASS] Skipping slot acquire - instance={instance.instance_name}, "
                f"class={instance.agent_class} (nested invocation)"
            )
        
        try:
            # ── Phase 1: Setup ─────────────────────────────────────────────
            messages, llm_messages, response = self._setup_turn(instance)
            if not messages:
                # Safety: drain any queued user messages before exiting, so they aren't lost.
                # Note: Using pool.add_message() here is safe because the engine returns immediately
                # after this block (line 676), so cached lists are never used again for this instance.
                # This prevents reintroducing the silent cache rebuild bug from Fix 1.
                inst_name = instance.instance_name
                queued = self.pool.drain_queue(inst_name)
                for item in queued:
                    msg = self._make_user_message(item)
                    if msg.content.strip():
                        self.pool.add_message(inst_name, msg)
                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            log_inst.log_message(msg)
                        except Exception:
                            pass
                logger.debug("early exit - %s (_setup_turn returned empty)", instance.instance_name)
                return  # Manual command handled or error

            max_turns = instance.max_turns or 50
            turns_available = max_turns
            inst_name = instance.instance_name

            while turns_available > 0:
                # ── SLEEPING STATE GUARD ────────────────────────────────────
                # Agents wake ONLY for async tool results, NOT user messages alone.
                # User messages accumulate in queue and are drained alongside async results.
                if instance.state == AgentState.SLEEPING:
                    action, yield_value = self._handle_sleeping_state(
                        instance, messages, llm_messages, response, skip_slot_acquire
                    )
                    if yield_value is not None:
                        yield yield_value
                        if action == SleepAction.CONTINUE_LOOP:
                            time.sleep(0.1)  # Prevent tight loop when no results available yet
                            continue
                    if action == SleepAction.BREAK_LOOP:
                        break
                    # Otherwise CONTINUE_LOOP — continue to next iteration

                # ── Phase 2: Pre-LLM Checks ────────────────────────────────
                # Stop/halt checks, async message injection, compression check/force, loop detection
                if self._pre_llm_checks(instance, messages, llm_messages, response, turns_available):
                    yield response
                    continue

                turns_available -= 1

                # ── Phase 3: LLM Call with Injection Points ────────────────
                turn_output = []
                for msg in self._call_llm_with_injection(instance, llm_messages):
                    if msg is None:
                        # Yield current partial conversation state to trigger streaming broadcast in run_agent_thread_unified.
                        # We combine persisted history (response), committed turn messages (turn_output),
                        # and currently streaming partial messages (instance._streaming_responses)
                        # to provide a complete "current view" for activity banners and UI rendering.
                        with instance._compression_lock:
                            partial_msgs = list(instance._streaming_responses)
                        yield (response + turn_output + partial_msgs, True)
                        continue
                    # FIX BOOL_LEAK: Validate message type before appending to prevent bool/list leak
                    if isinstance(msg, (Message, dict)):
                        # Endpoint recovery: [RETRYING] messages are transient UI notifications only — don't add to conversation history
                        content = msg.get('content', '') if isinstance(msg, dict) else getattr(msg, 'content', '')
                        is_retrying_msg = isinstance(content, str) and content.startswith("[RETRYING]")
                        
                        # Yield for UI visibility (even transient messages)
                        # Retry notifications show only the retry message; normal messages show with streaming state
                        if is_retrying_msg:
                            yield (response + turn_output + [msg], True)
                        else:
                            yield (response + turn_output + partial_msgs, True)
                        
                        # Only append to turn_output if it's a real message (not transient retry notification)
                        if not is_retrying_msg:
                            turn_output.append(msg)
                    else:
                        logger.warning(f"[MSG_VALIDATION] Skipping non-Message in LLM response for {instance.instance_name}: type={type(msg).__name__}, value={str(msg)[:100]}")

                if self.pool.stopped or self.pool.is_instance_halted(instance.instance_name) or self.pool.is_instance_terminated(instance.instance_name):
                    logger.debug("halted/stopped - %s", instance.instance_name)
                    # Sleep to prevent tight loop when halted (Issue #6 fix)
                    time.sleep(0.5)
                    yield response
                    continue

                # ── Phase 4: Response Processing and Tool Execution ─────────
                if self._process_response(instance, turn_output, messages, llm_messages, response):
                    # logger.debug("tool used - %s looping", instance.instance_name)
                    yield response
                    continue

                # ── Phase 5: Post-Turn Checks ───────────────────────────────
                if not self._post_turn_checks(instance, messages, llm_messages, response):
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
            logger.error("EXCEPTION - %s: %s: %s", instance.instance_name, type(e).__name__, e)
            error_msg = Message(role=ASSISTANT, content=f"[SYSTEM ERROR: {e}]")
            yield [error_msg]

        finally:
            # C4 fix: Always clean up — transition to IDLE regardless of how we exit
            
            # SLOT_TIMEOUT FIX: Log slot state before release for debugging
            if hasattr(instance, '_slot_release'):
                logger.debug(
                    f"[SLOT_FINAL] Before finally release - instance={instance.instance_name}, "
                    f"slot_held={instance._slot_release is not None}"
                )
            
            # Release concurrency slot on exit if still held (using helper method FIX Mi3)
            self._release_slot(instance, instance.instance_name)
            
            # SLOT_TIMEOUT FIX: Verify release happened
            if hasattr(instance, '_slot_release'):
                logger.debug(
                    f"[SLOT_FINAL] After finally release - instance={instance.instance_name}, "
                    f"slot_still_held={instance._slot_release is not None}"
                )
            
            # FIX LogAppendFixer: Final sync to ensure all messages in conversation are logged
            # This catches any injected messages that weren't followed by an LLM call triggering _process_response() sync
            try:
                inst_name = instance.instance_name
                agent_class = instance.agent_class
                log_inst = self.pool.get_logger(inst_name, agent_class)
                
                already_logged_count = len(log_inst.data.get("history", []))
                with instance._compression_lock:
                    conv_len = len(instance.conversation)
                
                # Only sync if there's a mismatch (defensive check to avoid redundant logging)
                if already_logged_count < conv_len:
                    logger.debug(
                        f"[FINAL_SYNC] {inst_name}: Catching up {conv_len - already_logged_count} unlogged messages "
                        f"(logged={already_logged_count}, conversation={conv_len})"
                    )
                    with instance._compression_lock:
                        for msg in instance.conversation[already_logged_count:]:
                            if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                                log_inst.log_message(msg)
            except Exception as e:
                logger.debug(f"Final sync to JSONL failed for {getattr(instance, 'instance_name', 'unknown')} (non-critical): {e}")
            
            with instance._state_lock:
                current_state = instance.state
                if current_state in (AgentState.RUNNING, AgentState.SLEEPING, AgentState.COMPLETING):
                    # Mark activity at task completion so idle timer starts from when agent becomes idle, not from creation
                    self.pool._mark_activity(instance.instance_name)
                    instance._transition(AgentState.IDLE)
                    logger.debug("EXIT - %s %s→IDLE", instance.instance_name, current_state.name)
                elif current_state == AgentState.TERMINATED:
                    logger.debug("EXIT - %s already TERMINATED", instance.instance_name)
                else:
                    logger.debug("EXIT - %s in %s state", instance.instance_name, current_state.name)

    # ═══════════════════════════════════════════════════════════════════════
    #  Phase Methods — each ~20-60 lines, independently testable
    # ═══════════════════════════════════════════════════════════════════════

    def _setup_turn(self, instance: AgentInstance) -> tuple:
        """Phase 1: Prepare messages and LLM input for the turn loop.

        Builds the system message from template (for main agent), loads conversation history,
        applies slice_history_for_llm to get working set, and sets up the response accumulator.

        Simple caching model: if config unchanged and cache exists, extend with new messages;
        otherwise rebuild from pool. The LLM API handles prefix caching automatically.
        
        Returns:
            Tuple of (messages, llm_messages, response) or (None, None, None) on error.
        """
        inst_name = instance.instance_name

        # Load conversation from pool (single source of truth)
        with instance._compression_lock:
            conv = list(instance.conversation)
        
        if not conv:
            logger.warning("empty conversation for %s - early exit", inst_name)
            return None, None, None

        # Simple cache check: use cached working set if config hasn't changed
        can_use_cache = (
            instance._last_config_version == self.pool._config_version and
            instance._cached_messages and
            instance._cached_llm_messages
        )
        
        if can_use_cache:
            # Extend cached lists with any new messages appended since last turn
            with instance._compression_lock:
                cached_len = len(instance._cached_messages)
                current_len = len(instance.conversation)
                
                # Fix 2: Cache sanity check - detect mismatches that would cause silent rebuilds
                if cached_len != current_len:
                    if current_len > cached_len:
                        # Normal case: new messages were appended - extend the cache
                        logger.debug(
                            f"[CACHE_EXTEND] Extending cached working set for {inst_name} "
                            f"by {current_len - cached_len} message(s)"
                        )
                        new_messages = list(instance.conversation[cached_len:])
                        instance._cached_messages.extend(new_messages)
                        # Re-slice to ensure marker correctness after extension
                        sliced = self.pool.slice_history_for_llm(instance._cached_messages)
                        instance._cached_llm_messages = list(sliced) if sliced else list(instance._cached_messages)
                    else:
                        # cached_len > current_len indicates a regression in Fix 1 — atomic updates should prevent this.
                        # Force rebuild to resync, log at INFO level for visibility (Fix 2 + Fix 3).
                        logger.info(
                            f"[CACHE_MISMATCH] {inst_name}: conv={current_len}, cached={cached_len} "
                            f"— forcing rebuild to resync"
                        )
                        can_use_cache = False
                
                if can_use_cache:
                    return instance._cached_messages, instance._cached_llm_messages, []

        # Cache miss or config change - rebuild from pool (Fix 3: promoted to INFO for visibility)
        logger.info(f"[CACHE_REBUILD] Rebuilding working set for {inst_name}")

        # Load template to get system message if needed
        template = self.pool.get_template(instance.agent_class)

        # P7: System prompt injection for ALL agents (not just root)
        # Inject identity, session metadata, available resources, and argument reuse instructions
        if len(conv) > 0:
            m0 = conv[0]
            m0_role = m0.get('role') if isinstance(m0, dict) else getattr(m0, 'role', '')
            
            # If no system message at start, inject it from template
            if m0_role != SYSTEM and template and getattr(template, 'system_message', None):
                sys_msg = Message(role=SYSTEM, content=template.system_message)
                
                # Preserve old first message's timestamp so update_history() can match it as an update
                # rather than appending a duplicate. The logger uses timestamps as identity markers.
                if len(conv) > 0 and hasattr(conv[0], 'timestamp') and conv[0].timestamp:
                    try:
                        sys_msg.timestamp = conv[0].timestamp
                    except AttributeError:
                        pass
                
                conv.insert(0, sys_msg)
                instance.insert_message_at_head(sys_msg)  # PR2: centralized API handles cache sync and clear
                m0 = sys_msg
                m0_role = SYSTEM
                
                # Note: No update_history() call here - _log_messages_to_jsonl() already handles first-time logging.
                # Calling update_history() with fresh timestamp causes dedup to fail and create duplicates.

            if m0_role == SYSTEM:
                m0_content = m0.get('content', '') if isinstance(m0, dict) else getattr(m0, 'content', '')
                if isinstance(m0_content, str):
                    original_content = m0_content
                    # 1. Update identity "You are [instance]."
                    pattern = rf"(?i)You are\s+\w+\."
                    if re.search(pattern, m0_content):
                        m0_content = re.sub(pattern, f"You are {inst_name}.", m0_content, count=1)
                    
                    # 2. Inject/update Session Metadata section
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
                    # Note: Using already-resolved template, no need for re-lookup
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
                    
                    # Update the message ONLY if content actually changed (preserves LLM prefix caching)
                    if m0_content != original_content:
                        if isinstance(m0, dict):
                            m0['content'] = m0_content
                        else:
                            m0.content = m0_content
                        logger.debug(f"[CACHE_REBUILD] System prompt content CHANGED for {inst_name}")
                        
                        # Persist updated system message to file so it survives restarts
                        try:
                            log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                            with instance._compression_lock:
                                conv_snapshot = list(instance.conversation)
                            log_inst.update_history(conv_snapshot)
                        except Exception as e:
                            logger.warning(f"Failed to persist system message update for {inst_name}: {e}")
                    else:
                        logger.debug(f"[CACHE_REBUILD] System prompt for {inst_name} textually identical — skipping pool update")

        # messages = full working set; llm_messages = what actually goes to LLM
        # Apply slice to extract system + post-marker tail if markers exist
        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages = list(sliced) if sliced else list(conv)
        
        # Sync caches — simple extend-or-rebuild model
        instance._cached_messages = conv
        instance._cached_llm_messages = llm_messages
        instance._last_config_version = self.pool._config_version
        
        response: List[Message] = []
        return conv, llm_messages, response

    def _check_stop_conditions(self, instance: AgentInstance) -> bool:
        """Check for pool stopped, instance halted, or terminated states.
        
        Extracted from _pre_llm_checks() - Phase 3.8
        
        Args:
            instance: Current agent instance
            
        Returns:
            True if any stop condition met (skip LLM call), False otherwise.
        """
        inst_name = instance.instance_name
        return (self.pool.stopped or 
                self.pool.is_instance_halted(inst_name) or 
                self.pool.is_instance_terminated(inst_name))

    def _inject_async_messages(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Drain and inject async results that arrived during LLM call.
        
        Extracted from _pre_llm_checks() - Phase 3.8
        
        Also invalidates LLM preprocessing cache after queue injection for fresh processing.
        
        Args:
            instance: Current agent instance
            messages, llm_messages, response: Working message sets
            
        Returns:
            True if any messages were injected (need to re-process), False otherwise.
        """
        inst_name = instance.instance_name
        
        if self._drain_and_inject(
            instance, inst_name, messages, llm_messages, response,
            drain_fn=self.pool.drain_queue,
            factory=self._make_user_message,
        ):
            # Invalidate LLM preprocessing cache after queue injection for fresh processing
            template = self.pool.get_template(instance.agent_class)
            if template and hasattr(template, 'llm') and template.llm:
                try:
                    template.llm._clear_preprocess_cache()
                except Exception as e:
                    logger.debug(f"Failed to clear LLM preprocess cache for {inst_name}: {e}")

            return True
        
        return False

    def _check_and_trigger_compression(
        self,
        instance: AgentInstance,
        messages: List[Message],
        llm_messages: List[Message],
        response: Optional[List[Message]] = None
    ) -> bool:
        """Calculate usage percentage and trigger force compression if needed.
        
        Extracted from _pre_llm_checks() - Phase 3.8
        
        Also injects warning at lower thresholds. Uses ground-truth token counts
        from last LLM call when available for accurate compression triggering.
        
        Args:
            instance: Current agent instance
            messages, llm_messages: Working message sets
            response: Optional list to append notifications for yielding (fixes compress feedback bug)
            
        Returns:
            True if compression was triggered (skip LLM call), False otherwise.
        """
        max_tokens = self._get_max_tokens(instance)
        
        # Ground-truth token counts from last LLM call (fixes force compression loop bug)
        # This fixes the force compression loop bug where manual counting was ~5x higher than actual
        # Atomic snapshot pattern: read both values into locals before check (thread-safe under GIL)
        actual_tokens = instance._last_actual_token_count
        allocated_max = instance._allocated_max_input_tokens
        
        if actual_tokens > 0 and allocated_max > 0:
            # Use cached ground-truth values from last LLM API call
            current_tokens = actual_tokens
            max_tokens_for_check = allocated_max
        else:
            # Fallback to manual counting when ground-truth not available (e.g., first turn)
            # CRITICAL FIX: Count tokens on FULL conversation (messages), not sliced llm_messages
            # llm_messages is already trimmed by slice_history_for_llm(), so it doesn't reflect
            # actual context accumulation. We need to measure the full working set to determine
            # when compression should trigger.
            current_tokens = self._count_history_tokens(messages, instance)
            max_tokens_for_check = max_tokens
        
        usage_pct = (current_tokens / max_tokens_for_check * 100) if max_tokens_for_check > 0 else 0

        # Forced compression at >95% — halts other agents, compresses, rebuilds
        if usage_pct > self.pool.settings.compression_force_threshold:
            return self._force_compression(instance, messages, llm_messages, usage_pct, response)

        # Warning injection at >85% (warning is inline hint, not yielded to UI)
        if usage_pct > self.pool.settings.compression_warning_threshold:
            self._inject_compression_warning(llm_messages, usage_pct, current_tokens, max_tokens_for_check)
            
        return False

    # ═══════════════════════════════════════════════════════════════════════
    #  Compression Checks — Pre-LLM and Post-Turn compression handling
    # ═══════════════════════════════════════════════════════════════════════

    def _pre_llm_checks(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], response: List[Message], turns_available: int
    ) -> bool:
        """Phase 2: Stop/halt checks, async injection, compression check, loop detection.

        Returns True if processing should continue to next iteration (yield + continue).
        Handles: stop/halt guard, async message drain, forced compression with rebuild,
        and loop detection (raises LoopDetectedError if found).
        """
        inst_name = instance.instance_name
        
        # 1. Stop/halt checks
        if self._check_stop_conditions(instance):
            return True  # Skip LLM call, yield and continue loop
        
        # 2. Async message injection
        if self._inject_async_messages(instance, messages, llm_messages, response):
            return True  # Yield and continue loop to process new messages
        
        # 3. Rollback command check (delegated to compression_handler)
        # Pass response so notification messages get yielded (fixes compress feedback bug)
        if self.compression_handler.handle_rollback_command(instance, messages, llm_messages, response):
            return True  # Command handled — yield and continue
        
        # 4. Compress command check (Phase 4.2: delegated to compression_handler)
        # Pass response so notification messages get yielded (fixes compress feedback bug)
        if self.compression_handler.handle_compress_command(instance, messages, llm_messages, response):
            return True  # Command handled — yield and continue
        
        # 5. Compression trigger (pass response for notification feedback)
        if self._check_and_trigger_compression(instance, messages, llm_messages, response):
            return True  # Compression triggered — yield and continue
        
        # 6. Loop detection (with post-compression cooldown) ───────────────────
        # After compression, the conversation state has concentrated patterns that
        # can trigger false-positive loop detection. Skip detection on the turn
        # immediately following compression via _suppress_loop_detection_next_turn flag.
        # Thread safety: Python GIL ensures atomic reads/writes for simple boolean attributes.
        if not getattr(instance, '_suppress_loop_detection_next_turn', False):
            loop_info = self._detect_loop(messages)
            if loop_info:
                reason, pop_count = loop_info
                logger.warning(f"Loop detected for {inst_name}: {reason}")
                raise LoopDetectedError(reason=reason, pop_count=pop_count)
        else:
            # Clear the cooldown flag now that we've skipped loop detection this turn.
            # Next turn will run normal loop detection (no more suppression).
            instance._suppress_loop_detection_next_turn = False

        return False  # Continue to LLM call normally

    def _force_compression(
        self, instance: AgentInstance, messages: List[Message],
        llm_messages: List[Message], usage_pct: float,
        response: Optional[List[Message]] = None
    ) -> bool:
        """Force compress when token usage exceeds critical threshold. Returns True (continue loop)."""
        inst_name = instance.instance_name
        
        # Phase 4.2: Delegate to compression_handler (pass response for notification feedback)
        if self.compression_handler.check_cooldown(instance, llm_messages, usage_pct):
            return True
        
        if self.compression_handler.check_overfeeding(instance, llm_messages, response):
            return True
        
        return self.compression_handler.execute_force_compression(instance, messages, llm_messages, usage_pct, response)

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
        
        # Rebuild llm_messages (sliced working set) — apply slice_history_for_llm
        conv = self.pool.get_conversation(inst_name)
        if not conv:
            return
        
        sliced = self.pool.slice_history_for_llm(conv)
        llm_messages.clear()
        llm_messages.extend(list(sliced))  # Already a new list from slice_history_for_llm
        
        # ── Cache Invalidation (after rebuild) ────────────────────────────────────
        # Invalidate token count cache so next _count_history_tokens does fresh count
        if inst:
            inst._cached_token_count = 0
            _invalidate_token_cache(inst)  # Critical: invalidates ALL cache fields including _last_actual_token_count
        
        # Invalidate LLM preprocessing cache for this instance's template
        template = self.pool.get_template(inst.agent_class) if inst else None
        if template and hasattr(template, 'llm') and template.llm:
            try:
                template.llm._clear_preprocess_cache()
            except Exception as e:
                logger.debug(f"Failed to clear LLM preprocess cache for {inst_name}: {e}")
        
        # Sync the instance caches (Fix LLM Reprocessing)
        if inst:
            inst._cached_messages = messages
            inst._cached_llm_messages = llm_messages
            inst._last_config_version = self.pool._config_version
        
        logger.debug(
            f"Rebuilt working sets for {inst_name}: "
            f"messages={len(messages)}, llm_messages={len(llm_messages)}"
        )

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
                # FIX: Also check reasoning_content and function_call to catch all changes
                if (getattr(old_msg, 'content', None) != getattr(new_msg, 'content', None) or
                    getattr(old_msg, 'reasoning_content', None) != getattr(new_msg, 'reasoning_content', None) or
                    getattr(old_msg, 'function_call', None) != getattr(new_msg, 'function_call', None)):
                    needs_update = True
                    break
        
        if needs_update:
            instance._streaming_responses = copy.deepcopy(last_output)

    def _classify_llm_error(self, error: Exception) -> str:
        """Classify LLM error as 'retryable', 'fatal', or 'unknown'.
        
        Extracted from _call_llm_with_injection() - Phase 3.6
        
        Args:
            error: The exception that occurred
            
        Returns:
            Error classification string
        """
        error_str = str(error).lower()
        
        # Retryable errors (transient)
        retryable_errors = (
            'connection', 'timeout', 'timed out', 'ssl', 
            'broken pipe', 'disconnected', 'eof', 
            'reset by peer', 'refused',
            '503', '502', '504', '429',  # Server errors + rate limiting
            'network unreachable', 'dns', 'resolution failed',  # Network/DNS issues
            'temporary', 'overloaded', 'service unavailable'  # Transient server states
        )
        
        # Explicitly non-retryable patterns (billing, auth, config)
        non_retryable_errors = (
            'insufficient_quota', 'billing_error', 'account_not_active',
            'invalid_api_key', 'authentication', 'unauthorized',
            'forbidden', 'permission denied',
            'model_not_found', 'invalid_model',
            'invalid_request', 'validation'
        )
        
        is_non_retryable = any(err in error_str for err in non_retryable_errors)
        has_retryable_pattern = any(err in error_str for err in retryable_errors)
        
        if is_non_retryable:
            return 'fatal'
        elif has_retryable_pattern:
            return 'retryable'
        else:
            # Unknown error — default to retryable for transient issues we haven't categorized
            return 'unknown'

    def _make_retrying_message(
        self,
        instance: AgentInstance,
        attempt: int,
        max_retries: int,
        delay: float
    ) -> Message:
        """Create [RETRYING] notification message for UI.
        
        Extracted from _call_llm_with_injection() - Phase 3.6
        
        Args:
            instance: Agent instance
            attempt: Current retry attempt number
            max_retries: Maximum retries allowed
            delay: Seconds until next retry
            
        Returns:
            Transient Message object (not added to conversation history)
        """
        return Message(
            role=ASSISTANT,
            content=f"[RETRYING] Connection lost, retrying ({attempt}/{max_retries}) in {delay:.1f}s..."
        )

    def _make_error_message(self, instance: AgentInstance, error_msg: str) -> Message:
        """Create [ERROR] notification message for UI.
        
        Extracted from _call_llm_with_injection() - Phase 3.6
        
        Args:
            instance: Agent instance
            error_msg: Error message to display
            
        Returns:
            Transient Message object (not added to conversation history)
        """
        return Message(
            role=ASSISTANT,
            content=f"[ERROR {instance.instance_name}: {error_msg}]"
        )

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
        MAX_RETRIES = 1
        BASE_DELAY = 1.0
        
        last_output = None
        retry_count = 0
        error_already_yielded = False
        
        while retry_count <= MAX_RETRIES:
            try:
                # Streaming UI Content Update Fix: Track partial LLM content for UI updates every ~100ms
                last_streaming_update_time = time.monotonic()
                
                for output in self._execute_llm_call(instance, template, llm_messages, active_functions):
                    last_output = output
                    
                    # Update _streaming_responses every ~100ms with deep copy of partial content
                    current_time = time.monotonic()
                    if current_time - last_streaming_update_time >= 0.1:
                        with instance._compression_lock:
                            self._update_streaming_responses(instance, last_output)
                            last_streaming_update_time = current_time
                        yield None
                    
                    # Check stop/halt mid-stream
                    if self.pool.stopped or self.pool.is_instance_halted(inst_name) or self.pool.is_instance_terminated(inst_name):
                        with instance._compression_lock:
                            instance._streaming_responses = []
                        break
                
                if last_output is not None:
                    break
                
            except Exception as e:
                with instance._compression_lock:
                    instance._streaming_responses = []
                
                if retry_count >= MAX_RETRIES:
                    error_msg = str(e).split('\n')[0] if e else "Unknown error"
                    logger.error(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} after {MAX_RETRIES} retries: {e}")
                    yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed after {MAX_RETRIES} retries — {error_msg}]")
                    error_already_yielded = True
                    break
                
                retry_count += 1
                backoff = min(BASE_DELAY * (2 ** (retry_count - 1)), 5.0)
                
                # Classify error type
                error_type = self._classify_llm_error(e)
                
                if error_type == 'fatal':
                    error_msg = str(e).split('\n')[0] if e else "Unknown error"
                    logger.warning(f"[ENDPOINT_RETRY] LLM call failed for {inst_name} with non-retryable error: {e}")
                    yield Message(role=ASSISTANT, content=f"[SYSTEM ERROR: LLM call failed — {error_msg}]")
                    error_already_yielded = True
                    break
                
                logger.warning(
                    f"[ENDPOINT_RETRY] LLM call failed for {inst_name}, retry {retry_count}/{MAX_RETRIES}. "
                    f"Retrying in {backoff:.1f}s with new endpoint... Error: {e}"
                )
                
                # Signal retry to UI before blocking on sleep
                yield self._make_retrying_message(instance, retry_count, MAX_RETRIES, backoff)
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

    # ═══════════════════════════════════════════════════════════════════════
    #  LLM Calling — Core LLM call with retry logic and error handling
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
        active_functions = _get_active_functions_from_template(template, instance)

        # Delegate to extracted method - Phase 3.6
        yield from self._execute_llm_call_with_retry(instance, llm_messages, template, active_functions)

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
            
            # Dynamic endpoint selection based on agent's actual token requirements
            allocated_tokens = None
            if instance._generate_cfg_override is not None and 'max_input_tokens' in instance._generate_cfg_override:
                val = instance._generate_cfg_override['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    allocated_tokens = val
            elif hasattr(llm, 'generate_cfg') and 'max_input_tokens' in llm.generate_cfg:
                val = llm.generate_cfg['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    allocated_tokens = val

            def _do_call(llm_cfg: dict) -> Iterator[List[Message]]:
                merged_cfg = {}
                # Use per-instance override if present, otherwise fall back to template config
                if instance._generate_cfg_override is not None:
                    merged_cfg.update(instance._generate_cfg_override)
                elif hasattr(llm, 'generate_cfg'):
                    merged_cfg.update(llm.generate_cfg)
                # Endpoint config (from router) overwrites — including max_input_tokens.
                # This allows user's General Settings / endpoint limit to take effect.
                merged_cfg.update(llm_cfg)
                merged_cfg['agent_name'] = template.name
                
                # Store allocated max_input_tokens in instance for compression check (ground-truth tracking)
                # Validate to ensure it's a positive integer before storing
                if 'max_input_tokens' in merged_cfg:
                    val = merged_cfg['max_input_tokens']
                    if isinstance(val, int) and val > 0:
                        instance._allocated_max_input_tokens = val
                
                # Register token count callback to capture actual token usage from LLM (ground-truth tracking)
                merged_cfg['_on_token_count'] = _make_token_count_callback(instance)

                return llm.chat(
                    messages=messages,
                    functions=active_functions,
                    stream=True,
                    delta_stream=False,
                    extra_generate_cfg=merged_cfg,
                )

            agent_type = instance.agent_class.lower() if hasattr(instance, 'agent_class') else 'agent'
            return self.pool.api_router.call_with_fallback(agent_type, _do_call, allocated_tokens=allocated_tokens)
        else:
            # Direct call without router — still respect instance override for max_input_tokens etc.
            merged_cfg = {}
            if instance._generate_cfg_override is not None:
                merged_cfg.update(instance._generate_cfg_override)
            elif hasattr(llm, 'generate_cfg'):
                merged_cfg.update(llm.generate_cfg)
            merged_cfg['agent_name'] = template.name
            
            # Store allocated max_input_tokens in instance for compression check (ground-truth tracking)
            # Validate to ensure it's a positive integer before storing
            if 'max_input_tokens' in merged_cfg:
                val = merged_cfg['max_input_tokens']
                if isinstance(val, int) and val > 0:
                    instance._allocated_max_input_tokens = val
            
            # Register token count callback to capture actual token usage from LLM (ground-truth tracking)
            merged_cfg['_on_token_count'] = _make_token_count_callback(instance)

            return llm.chat(
                messages=messages,
                functions=active_functions,
                stream=True,
                delta_stream=False,
                extra_generate_cfg=merged_cfg,
            )

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
            
            # Strip thinking blocks from reasoning_content to prevent tag pollution in history
            reasoning_content = _msg_field(msg, 'reasoning_content')
            if isinstance(reasoning_content, str):
                if isinstance(msg, dict):
                    msg['reasoning_content'] = _normalize_thinking_blocks(reasoning_content)
                else:
                    msg.reasoning_content = _normalize_thinking_blocks(reasoning_content)
            
            # Clean thinking blocks from function call arguments (P4 continuation)
            func_call = _msg_field(msg, 'function_call')
            if func_call:
                _normalize_tool_arguments(func_call)

    def _log_messages_to_jsonl(
        self,
        instance: AgentInstance,
        inst_name: str,
        turn_output: List[Message]
    ) -> None:
        """Persist messages to JSONL log file.
        
        Logs pre-existing messages that weren't logged yet, then logs turn_output messages.
        This must happen BEFORE instance.conversation.extend(turn_output) so the initial
        sync only sees messages that existed before this LLM call (system + user).
        
        Args:
            instance: AgentInstance for logger lookup and conversation access
            inst_name: Instance name for logging
            turn_output: Messages from LLM to log
        """
        # Persist pre-existing messages to JSONL before appending turn_output
        log_inst = self.pool.get_logger(inst_name, instance.agent_class)

        already_logged_count = len(log_inst.data.get("history", []))
        with instance._compression_lock:
            conv = instance.conversation
        
        if already_logged_count == 0 and conv:
            # First time logging — log all pre-existing messages (system + user).
            # turn_output has NOT been appended yet, so no risk of duplication.
            for msg in conv:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    log_inst.log_message(msg)
        elif already_logged_count < len(conv):
            # Partial sync — log only messages added since last sync (e.g., user messages
            # from add_message() on subsequent turns that weren't logged to JSONL).
            # turn_output has NOT been appended yet, so no risk of duplicating it.
            for msg in conv[already_logged_count:]:
                if isinstance(msg, Message) or (isinstance(msg, dict) and 'role' in msg):
                    log_inst.log_message(msg)

        # Persist turn_output messages to JSONL log file (P1: LoggerManager migration)
        try:
            # Log turn_output messages from this LLM call
            for msg in turn_output:
                log_inst.log_message(msg)
        except Exception as e:
            logger.debug(f"Logging message to file failed for {inst_name} (non-critical): {e}")

    def _check_and_handle_truncation(
        self,
        is_truncated: bool,
        turn_output: List[Message],
        instance: AgentInstance,
        inst_name: str,
        messages: List[Message],
        llm_messages: List[Message]
    ) -> bool:
        """Inject continue message if truncation detected and auto_continue enabled.
        
        If any message in turn_output is truncated (finish_reason == 'length') and
        auto_continue setting is enabled, injects a continue message to all working sets
        and returns True to continue to next LLM call.
        
        Args:
            is_truncated: Pre-computed truncation flag from caller (FIX #2: avoid double-check)
            turn_output: Messages from LLM (for logging context only)
            instance: AgentInstance for conversation access and token cache
            inst_name: Instance name for logging and halt checks
            messages: Full message set to append continue message
            llm_messages: LLM-formatted message set to append continue message
            
        Returns:
            True if truncation detected and continue message injected, False otherwise
            
        Note:
            is_truncated is pre-computed by caller to avoid checking truncation twice
            (once in normalization, once here). This method only handles the injection logic.
        """
        # Auto-continue on truncation (only if user has enabled the setting)
        # FIX #2: Use pre-computed is_truncated from caller instead of re-checking
        if (is_truncated and not self.pool.stopped and 
            not self.pool.is_instance_halted(inst_name) and 
            not self.pool.is_instance_terminated(inst_name) and 
            self.pool.settings.auto_continue):
            logger.info(f"Detected message truncation for {inst_name}. Auto-continuing.")
            cont_msg = Message(
                role=USER,
                content="[SYSTEM]: Your previous response was cut off. Continue from where you left off."
            )
            messages.append(cont_msg)
            llm_messages.append(cont_msg)
            instance.append_message(cont_msg)  # PR2: centralized mutation API handles cache sync
            return True
        
        return False

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
        
        # ── Hoist template lookup outside loop (performance optimization) ────────
        # Template and disabled tool list don't change during the loop.
        # Also check instance._generate_cfg_override for defense-in-depth.
        _primary_template = self.pool.get_template(instance.agent_class)
        _primary_disabled_tools = set()
        _primary_function_map = {}
        if _primary_template:
            # Start with template's disabled tools
            _primary_disabled_tools = _primary_template._get_disabled_tool_names()
            
            # Also check instance override for defense-in-depth
            # This mirrors the logic in _get_active_functions_from_template()
            if hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
                inst_disabled = instance._generate_cfg_override.get('disabled_tools', {})
                if isinstance(inst_disabled, dict):
                    agent_name = getattr(_primary_template, 'name', None)
                    if agent_name and agent_name in inst_disabled:
                        _primary_disabled_tools.update(inst_disabled[agent_name])
                    # Also check slugified name for robustness
                    if agent_name:
                        slug = agent_name.lower().replace(' ', '_')
                        if slug in inst_disabled:
                            _primary_disabled_tools.update(inst_disabled[slug])
                    agent_type = getattr(_primary_template, 'agent_type', None)
                    if agent_type and agent_type in inst_disabled:
                        _primary_disabled_tools.update(inst_disabled[agent_type])
                elif isinstance(inst_disabled, (list, tuple)):
                    _primary_disabled_tools.update(inst_disabled)
            
            _primary_function_map = getattr(_primary_template, 'function_map', {})
        
        for out in turn_output:
            use_tool, tool_name, tool_args, _ = self._detect_tool(out)
            if not use_tool:
                continue

            # Stop/halt check BEFORE tool execution (check before setting used_any_tool)
            if self.pool.stopped or self.pool.is_instance_halted(inst_name) or self.pool.is_instance_terminated(inst_name):
                break
            
            # ── Disabled/Inexistent Tool Auto-Deny ────────────────────────────
            # Defense-in-depth: check if tool is disabled BEFORE execution.
            # Disabled tools are still in function_map (only filtered from active functions sent to LLM).
            # If an agent generates a call for a disabled tool, it gets auto-denied here.
            if _primary_template and (tool_name in _primary_disabled_tools or tool_name not in _primary_function_map):
                # Determine deny reason with same logic as legacy implementation
                if tool_name in _primary_disabled_tools and tool_name not in _primary_function_map:
                    deny_reason = "disabled and does not exist"
                elif tool_name in _primary_disabled_tools:
                    deny_reason = "disabled"
                else:
                    deny_reason = "does not exist"
                
                logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")
                tool_result = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."
                
                # Extract function_id from the assistant message that had the tool call
                extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                function_id = extra_data.get('function_id')
                
                # Telemetry: record tool call start and end for auto-denied tools
                try:
                    if hasattr(self.pool, 'telemetry'):
                        self.pool.telemetry.record_tool_call_start(inst_name, tool_name)
                        self.pool.telemetry.record_tool_call_end(
                            inst_name, tool_name,
                            success=False,
                            result_chars=len(tool_result),
                            truncated=False,
                            error=f"Tool {deny_reason}",
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
                messages.append(fn_msg)
                llm_messages.append(fn_msg)
                response.append(fn_msg)  # Stream denial to UI
                
                instance.append_message(fn_msg)  # PR2: centralized mutation API handles cache sync
                
                # Track as executed for orphan handling (it was processed, just denied)
                executed_tools.append(tool_name)
                
                # Log the function result to JSONL
                try:
                    log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                    log_inst.log_message(fn_msg)
                except Exception:
                    pass  # Logging must never block tool execution
                
                used_any_tool = True
                continue  # Skip actual tool execution
            
            used_any_tool = True

            # Track tool success/failure — needed for function_id matching and frontend isToolFailure()
            _tool_success = True
            _tool_error = ""

            # Telemetry: record tool call start (non-blocking)
            try:
                if hasattr(self.pool, 'telemetry'):
                    self.pool.telemetry.record_tool_call_start(inst_name, tool_name)
            except Exception:
                pass

            # Extract function_id from the assistant message that had the tool call BEFORE executing
            # This is critical — without it, the LLM API can't match tool results to tool calls
            extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
            function_id = extra_data.get('function_id')

            try:
                try:
                    # Phase 4.3: Delegate to ToolDispatcher
                    tool_result = self.tool_dispatcher.execute_tool(
                        instance, tool_name, tool_args, llm_messages, function_id=function_id
                    )
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
                    # Phase 4.3: Delegate to ToolDispatcher
                    tool_result = self.tool_dispatcher.truncate_tool_result(
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

            # Track compress_context execution
            if tool_name == 'compress_context':
                inst = self.pool.get_instance(inst_name)
                if inst:
                    self._rebuild_working_set(messages, llm_messages, inst_name)
                # Item 10: Validate message pool after agent-triggered compression
                conv = self.pool.get_conversation(inst_name)
                if conv and not validate_message_pool(conv, inst_name):
                    logger.error(f"[MSG POOL VALIDATION] Pool invalid after agent-triggered compression for '{inst_name}'")

            # Build function result message — include function_id and tool_success per OpenAI spec
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
            messages.append(fn_msg)
            llm_messages.append(fn_msg)
            response.append(fn_msg)  # Stream tool result to UI (was missing)
            instance.append_message(fn_msg)  # PR2: centralized mutation API
            
            # Track executed tool for orphan handling
            executed_tools.append(tool_name)

            # Log the function result to JSONL (was missing)
            try:
                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                log_inst.log_message(fn_msg)
            except Exception:
                pass  # Logging must never block tool execution

        # ── Handle orphaned tool calls from early break ───────────────────────────────
        # If halt/stop was detected mid-loop, remaining tools in turn_output don't have FUNCTION results.
        # Add placeholder FUNCTION messages to prevent API Error 400 (orphaned tool_call_id's).
        if self.pool.stopped or self.pool.is_instance_halted(inst_name) or self.pool.is_instance_terminated(inst_name):
            executed_set = set(executed_tools)  # Convert to set for O(1) lookup
            tools_processed = 0
            
            # ── Hoist template lookup outside compression lock ──────────────────
            # Template and disabled tool list don't change during the loop.
            _orphan_template = self.pool.get_template(instance.agent_class)
            _orphan_disabled_tools = set()
            _orphan_function_map = {}
            if _orphan_template:
                # Start with template's disabled tools
                _orphan_disabled_tools = _orphan_template._get_disabled_tool_names()
                
                # Also check instance override for defense-in-depth
                # This mirrors the logic in _get_active_functions_from_template()
                if hasattr(instance, '_generate_cfg_override') and instance._generate_cfg_override:
                    inst_disabled = instance._generate_cfg_override.get('disabled_tools', {})
                    if isinstance(inst_disabled, dict):
                        agent_name = getattr(_orphan_template, 'name', None)
                        if agent_name and agent_name in inst_disabled:
                            _orphan_disabled_tools.update(inst_disabled[agent_name])
                        # Also check slugified name for robustness
                        if agent_name:
                            slug = agent_name.lower().replace(' ', '_')
                            if slug in inst_disabled:
                                _orphan_disabled_tools.update(inst_disabled[slug])
                        agent_type = getattr(_orphan_template, 'agent_type', None)
                        if agent_type and agent_type in inst_disabled:
                            _orphan_disabled_tools.update(inst_disabled[agent_type])
                    elif isinstance(inst_disabled, (list, tuple)):
                        _orphan_disabled_tools.update(inst_disabled)
                
                _orphan_function_map = getattr(_orphan_template, 'function_map', {})
            
            with token_cache_invalidated(instance):
                with instance._compression_lock:  # FIX #1: Batch lock acquisition for all placeholder appends
                        
                        for out in turn_output:
                            use_tool, tool_name, tool_args, _ = self._detect_tool(out)
                            if not use_tool:
                                continue
                            
                            # Only add placeholder for tools that were NOT executed
                            if tool_name in executed_set:
                                continue
                            
                            # ── Disabled/Inexistent Tool Auto-Deny (orphan handling) ────────
                            # For unexecuted tools due to halt, check if they're disabled/inexistent.
                            # If so, give proper denial message instead of generic "skipped" message.
                            deny_reason = None
                            # Template guard for consistency with primary loop
                            if _orphan_template and (tool_name in _orphan_disabled_tools or tool_name not in _orphan_function_map):
                                # Determine deny reason with same logic as legacy implementation
                                if tool_name in _orphan_disabled_tools and tool_name not in _orphan_function_map:
                                    deny_reason = "disabled and does not exist"
                                elif tool_name in _orphan_disabled_tools:
                                    deny_reason = "disabled"
                                else:
                                    deny_reason = "does not exist"
                                
                                # Log the denial (matching primary loop pattern)
                                logger.info(f"Auto-denying tool '{tool_name}' for agent {inst_name} — tool is {deny_reason}.")
                            
                            # Extract function_id from the assistant message that had the tool call
                            extra_data = out.get('extra', {}) if isinstance(out, dict) else (getattr(out, 'extra', None) or {})
                            _orphan_function_id = extra_data.get('function_id')  # Use same pattern as primary loop
                            
                            # Add placeholder FUNCTION result for unexecuted tool
                            # Use denial message if tool is disabled/inexistent, otherwise use skip message
                            if deny_reason:
                                fn_content = f"Tool '{tool_name}' was auto-denied because it is {deny_reason} for this agent. This tool cannot be used."
                            else:
                                fn_content = f"Tool execution skipped: instance {inst_name} was halted/stopped"
                            
                            # Telemetry: record tool call start and end (matching primary loop pattern)
                            try:
                                if hasattr(self.pool, 'telemetry'):
                                    self.pool.telemetry.record_tool_call_start(inst_name, tool_name)
                                    self.pool.telemetry.record_tool_call_end(
                                        inst_name, tool_name,
                                        success=False,
                                        result_chars=len(fn_content),
                                        truncated=False,
                                        error=f"Tool {deny_reason}" if deny_reason else "Skipped (halt/stop)",
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
                            messages.append(fn_msg)
                            llm_messages.append(fn_msg)
                            response.append(fn_msg)  # Stream to UI
                            
                            instance.append_message(fn_msg)  # PR2: centralized mutation API handles cache sync
                            
                            # Track as executed for consistency (matching primary loop pattern)
                            executed_tools.append(tool_name)
                            
                            # Log the function result to JSONL (matching primary loop pattern)
                            try:
                                log_inst = self.pool.get_logger(inst_name, instance.agent_class)
                                log_inst.log_message(fn_msg)
                            except Exception:
                                pass  # Logging must never block tool execution
                            
                            tools_processed += 1
                        
                        if tools_processed > 0:
                            logger.warning(f"Added {tools_processed} placeholder FUNCTION messages for unexecuted tools in {inst_name}")

        return used_any_tool

    def _process_response(
        self, instance: AgentInstance, turn_output: List[Message],
        messages: List[Message], llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Phase 4: Normalize response, handle auto-continue on truncation, execute tools.

        Returns True if processing should continue to next iteration (tool was used or truncated).
        """
        inst_name = instance.instance_name

        # FIX #2 & #3: Compute truncation flag BEFORE normalization to avoid double-check
        is_truncated = any(_check_message_truncation(msg) for msg in turn_output)
        
        # Extracted to _normalize_turn_output() - Phase 3.3 (FIX #3: now only normalizes)
        self._normalize_turn_output(turn_output)

        # Extracted to _log_messages_to_jsonl() - Phase 3.3
        self._log_messages_to_jsonl(instance, inst_name, turn_output)

        # Append to all working sets  
        response.extend(turn_output)
        messages.extend(turn_output)
        llm_messages.extend(turn_output)
        instance.append_messages(turn_output)  # PR2: centralized mutation API handles cache sync
        # Streaming UI Content Update Fix: Clear _streaming_responses after Phase 4 commits messages
        instance._streaming_responses = []
        
        # Extract ground-truth usage info from LLM response (ground-truth token tracking)
        # This replaces manual token counting with actual API-reported values
        for msg in turn_output:
            extra = msg.get('extra') if isinstance(msg, dict) else getattr(msg, 'extra', None)
            if extra and isinstance(extra, dict) and 'usage' in extra:
                usage = extra['usage']
                if isinstance(usage, dict):
                    # Update ground-truth token counts from LLM API response
                    if 'prompt_tokens' in usage:
                        instance._last_actual_token_count = usage['prompt_tokens']
                    if 'total_tokens' in usage and 'prompt_tokens' not in usage:
                        # Some APIs only return total_tokens
                        instance._last_actual_token_count = usage['total_tokens']
                    break  # Only need to extract from first message with usage info

        # Extracted to _check_and_handle_truncation() - Phase 3.3
        if self._check_and_handle_truncation(is_truncated, turn_output, instance, inst_name, messages, llm_messages):
            return True  # Continue to next LLM call

        # Extracted to _execute_detected_tools() - Phase 3.3
        used_any_tool = self._execute_detected_tools(instance, inst_name, turn_output, messages, llm_messages, response)

        # ── Post-tool urgent injection ───────────────────────────────────
        # Inject urgent messages AFTER all tools complete to avoid orphaned tool_call_id's
        if self._drain_and_inject(
            instance, inst_name, messages, llm_messages, response,
            drain_fn=self.pool.drain_queue,
            factory=self._make_user_message,
        ):
            return True  # Continue to next LLM call for urgent message processing

        # FIX #4: Only loop if tool was used. If truncation detected but auto_continue disabled,
        # the turn is complete (no continue message injected), so return False to avoid infinite loop.
        return used_any_tool


    def _check_for_tool_calls_in_output(
        self,
        instance: AgentInstance,
        response: List[Message]
    ) -> bool:
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
        for msg in reversed(response):
            role = msg.get('role') if isinstance(msg, dict) else getattr(msg, 'role', '')
            if role == ASSISTANT:
                fc = msg.get('function_call') if isinstance(msg, dict) else getattr(msg, 'function_call', None)
                has_tool_call = fc is not None
                break
        
        return has_tool_call

    def _detect_pure_thinking_turn(
        self,
        instance: AgentInstance,
        response: List[Message]
    ) -> bool:
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
            if (m.get('role') == ASSISTANT or getattr(m, 'role', '') == ASSISTANT)
        )

        has_thinking = any(
            m.get('thought') or m.get('reasoning_content')
            for m in response[-3:]
        )

        # Pure thinking turn — continue to next turn
        if not has_real_content and has_thinking:
            logger.info(f"Pure reasoning turn detected for {inst_name}. Continuing.")
            return True
        
        return False

    def _transition_to_sleeping_if_pending(
        self,
        instance: AgentInstance,
        inst_name: str
    ) -> bool:
        """Handle SLEEPING state transition when async tools are pending.
        
        Extracted from _post_turn_checks() - Phase 3.9
        
        If there are pending background tools and no more tool calls to execute,
        transition the agent to SLEEPING state while waiting for results.
        
        Args:
            instance: Current agent instance
            inst_name: Instance name for logging
            
        Returns:
            True if transitioned (continue looping), False if not sleeping.
        """
        # Check for pending async tool calls (including call_agent) before completing
        # This applies regardless of whether agent has real content or not
        if self.pool.has_pending(inst_name):
            logger.debug(f"Pending async tools for {inst_name}. Transitioning to SLEEPING.")
            self._transition_to_sleeping(instance)
            return True  # Continue loop → hits SLEEPING guard at top
        
        return False

    def _drain_post_generation_messages(
        self,
        instance: AgentInstance,
        inst_name: str,
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
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
        
        # Safety drain: catch any results from fast-completing children that completed
        # between register_async_call() and the has_pending() check above
        try:
            if self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_async_results,
                factory=self._make_async_result_message,
            ):
                return True  # Continue loop to process drained results
        except Exception as e:
            logger.error("Safety drain failed for %s: %s", inst_name, e)
        
        return False

    def _post_turn_checks(
        self, 
        instance: AgentInstance, 
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message]
    ) -> bool:
        """Phase 5: Check for final answer, wait for parallel agents, drain post-generation queue.

        Returns False when agent has truly completed (break from loop).
        Handles: final answer detection, thinking-only detection, parallel agent wait,
        and post-generation message drain.

        Args:
            instance: The agent being executed.
            messages: Full working set of messages.
            llm_messages: Messages formatted for LLM API.
            response: Response list to yield back to caller.

        Returns:
            True to continue the turn loop, False to break (agent complete).
        """
        inst_name = instance.instance_name
        
        # 1. Check for unexecuted tool calls
        if self._check_for_tool_calls_in_output(instance, response):
            return True  # Tool was called — continue the loop (tool result will be in next turn)
        
        # 2. Detect pure thinking turn (stalled agent)
        if self._detect_pure_thinking_turn(instance, response):
            return False  # Pure reasoning detected — break out of loop (agent stalled)
        
        # 3. Transition to SLEEPING if async tools pending
        if self._transition_to_sleeping_if_pending(instance, inst_name):
            return True  # Continue loop → hits SLEEPING guard at top
        
        # 4. Drain post-generation messages (safety drain)
        if self._drain_post_generation_messages(
            instance, inst_name, messages, llm_messages, response
        ):
            return True  # Messages drained — continue looping
        
        return False  # Agent has truly completed


    @staticmethod
    def _release_slot(slot_holder: Any, holder_name: str, context: str = "cleanup") -> None:
        """Release a concurrency slot from a slot holder with error handling.
        
        FIX Mi3: Extracted helper to eliminate code duplication across three locations.
        Encapsulates the capture-nullify-release-log pattern for slot release.
        
        Note: This is intentionally static and accesses logger from module scope.
        Defensive check ensures robustness even if called without hasattr guards.
        
        Args:
            slot_holder: Object with _slot_release attribute (AgentInstance or similar)
            holder_name: Name of the holder for logging purposes
            context: Optional context description for logging (e.g., "sleep transition", "sync child")
        """
        # Defensive guard: handle objects without _slot_release attribute
        if not hasattr(slot_holder, '_slot_release'):
            suffix = f" during {context}" if context else ""
            logger.debug(f"[SLOT_RELEASE] No _slot_release attr for {holder_name}{suffix}")
            return
        
        context_suffix = f" during {context}" if context else ""
        if slot_holder._slot_release is not None:
            release_callback = slot_holder._slot_release
            slot_holder._slot_release = None  # Capture ref first, then nullify to prevent double-release
            try:
                release_callback()
                logger.debug(f"[SLOT_RELEASE] Successfully released for {holder_name}{context_suffix}")
            except Exception as e:
                logger.error(
                    f"[SLOT_RELEASE_ERROR] Failed to release slot for {holder_name}{context_suffix}: {e}",
                    exc_info=True
                )
        else:
            logger.debug(f"[SLOT_RELEASE] _slot_release already None for {holder_name}{context_suffix}")

    def _transition_to_sleeping(self, instance: 'AgentInstance') -> None:
        """Transition an agent instance to SLEEPING state.

        Helper method to reduce code duplication in _post_turn_checks.
        Sets the appropriate timestamps and transitions state atomically.
        Also releases the concurrency slot so children can proceed.

        Args:
            instance: The agent instance to transition.
        """
        # Note: This is safe even when _skip_slot_acquire=True because _release_slot checks
        # for None before releasing. No need to guard with skip_slot_acquire check.
        # Release concurrency slot when sleeping — allows children to proceed (using helper method FIX Mi3)
        self._release_slot(instance, instance.instance_name, "sleep transition")
        
        with instance._state_lock:
            if instance.state == AgentState.RUNNING:
                # Mark activity before transitioning to SLEEPING so idle timer is updated
                self.pool._mark_activity(instance.instance_name)
                instance._transition(AgentState.SLEEPING)
                instance.sleeping_since = time.monotonic()
                instance._last_wakeup_log = time.monotonic()
            else:
                # Log warning when transition is skipped to help identify bugs where
                # _transition_to_sleeping is called on agents not in RUNNING state.
                # This indicates a logic bug in the caller — the agent should be
                # in RUNNING state before attempting to sleep it.
                logger.warning(
                    f"_transition_to_sleeping skipped for {instance.instance_name}: "
                    f"current state={instance.state.name} (expected RUNNING)"
                )

    # ═══════════════════════════════════════════════════════════════════════
    #  State Handling — SLEEPING state extraction (Phase 3.1)
    # ═══════════════════════════════════════════════════════════════════════

    def _handle_sleeping_state(
        self,
        instance: 'AgentInstance',
        messages: List[Message],
        llm_messages: List[Message],
        response: List[Message],
        skip_slot_acquire: bool
    ) -> Tuple[SleepAction, Optional[List[Message]]]:
        """Handle SLEEPING state wakeup logic.

        Extracted from run() as part of Phase 3.1 refactoring to reduce method size
        and improve testability. This method handles all the branching logic for
        waking a sleeping agent based on async tool results, user messages, and timeouts.

        Args:
            instance: Current agent instance in SLEEPING state.
            messages: Working list of all messages (user + assistant).
            llm_messages: Messages formatted for LLM consumption.
            response: Response messages being built for this turn.
            skip_slot_acquire: Whether to skip slot re-acquisition (for nested agents).

        Returns:
            Tuple of (action, optional_yield_value):
            - action = CONTINUE_LOOP means re-enter the while loop
            - action = BREAK_LOOP means exit the while loop
            - yield_value=None means no special yield needed before continuing/breaking
            - yield_value=[] means yield empty list (signals waiting state)
        """
        inst_name = instance.instance_name

        # Drain async tool results ONLY — user messages do NOT wake sleeping agents
        async_results = self.pool.drain_async_results(inst_name)

        if async_results:
            # Async results arrived — wake up and inject
            with instance._state_lock:
                # CHECK TERMINATED BEFORE TRANSITION — prevents resuming terminated agent
                if instance.state == AgentState.TERMINATED:
                    logger.debug("TERMINATED while SLEEPING for %s - breaking", inst_name)
                    return SleepAction.BREAK_LOOP, None
                instance._transition(AgentState.RUNNING)
                instance.sleeping_since = None
                instance._last_wakeup_log = time.monotonic()
                logger.debug("RESUMED from SLEEPING - %s async results, user msgs=%s", 
                             len(async_results), self.pool.has_messages(inst_name))

            # Inject async results FIRST (items mode — already drained)
            self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                items=async_results,
                factory=self._make_async_result_message,
            )
            
            # THEN drain any queued user messages (they were waiting while agent was sleeping)
            self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_queue,
                factory=self._make_user_message,
            )

            # Re-acquire concurrency slot after waking from SLEEPING
            if not skip_slot_acquire:
                self._acquire_slot_with_logging(instance, "after_async_wakeup")

            # Continue to normal LLM processing below (in RUNNING state now)
            return SleepAction.CONTINUE_LOOP, None

        elif self.pool.has_pending(inst_name):
            # Still waiting for background tools — NO user wakeup (preserves tool_call → tool_response flow)
            # User messages stay in queue until async results arrive
            if instance.state == AgentState.TERMINATED:
                return SleepAction.BREAK_LOOP, None
            
            current_time = time.monotonic()
            sleeping_duration = 0.0
            if instance.sleeping_since is not None:
                sleeping_duration = current_time - instance.sleeping_since
            
            # Get settings with defaults
            wakeup_interval = getattr(self.pool.settings, 'sleeping_wakeup_interval', 5.0)
            sleeping_timeout = getattr(self.pool.settings, 'sleeping_timeout', 300.0)
            
            # Check for timeout first
            if sleeping_duration >= sleeping_timeout:
                logger.warning("SLEEPING TIMEOUT - %s waited %.1fs (timeout=%ss)", 
                                inst_name, sleeping_duration, sleeping_timeout)
                # Final drain before giving up — prevents data loss of late-arriving results
                final_results = self.pool.drain_async_results(inst_name)
                self._drain_and_inject(
                    instance, inst_name, messages, llm_messages, response,
                    items=final_results,
                    factory=self._make_async_result_message,
                )
                with instance._state_lock:
                    instance._transition(AgentState.COMPLETING)
                    instance.sleeping_since = None
                return SleepAction.BREAK_LOOP, None
            
            # Log wakeup message periodically
            if (current_time - instance._last_wakeup_log) >= wakeup_interval:
                logger.info("SLEEPING - %s waiting %.1fs for background tools", 
                            inst_name, sleeping_duration)
                instance._last_wakeup_log = current_time
            
            logger.debug("WAITING for background tools - %s (%.1fs)", inst_name, sleeping_duration)
            # Yield empty list signals waiting state without consuming turn
            return SleepAction.CONTINUE_LOOP, []

        else:
            # No pending tools and no immediate results
            # Stable-state drain — keep draining until no more results arrive
            results_found = False
            while self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_async_results,
                factory=self._make_async_result_message,
            ):
                results_found = True
            
            # Final safety drain — catches race conditions
            if self._drain_and_inject(
                instance, inst_name, messages, llm_messages, response,
                drain_fn=self.pool.drain_async_results,
                factory=self._make_async_result_message,
            ):
                results_found = True
            
            # If any results were found, transition to RUNNING so LLM processes them
            if results_found:
                with instance._state_lock:
                    if instance.state == AgentState.TERMINATED:
                        return SleepAction.BREAK_LOOP, None
                    instance._transition(AgentState.RUNNING)
                    instance.sleeping_since = None
                    instance._last_wakeup_log = time.monotonic()

                # Re-acquire concurrency slot after waking from SLEEPING
                if not skip_slot_acquire:
                    self._acquire_slot_with_logging(instance, "after_stable_drain")

                # Loop back; now in RUNNING state → LLM processes injected results
                return SleepAction.CONTINUE_LOOP, []  # Bridge signal for UI update before LLM processing
            
            # No results found — safe to transition to COMPLETING
            with instance._state_lock:
                if instance.state == AgentState.TERMINATED:
                    return SleepAction.BREAK_LOOP, None
                instance._transition(AgentState.COMPLETING)
                instance.sleeping_since = None
            logger.debug("COMPLETING - %s (no pending tools)", inst_name)
            return SleepAction.BREAK_LOOP, None

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
                logger.debug("JSON parse failure for %s/%s", instance_name, tool_name)
                return None  # JSON parse failure — signal error to caller
            if not isinstance(parsed, dict):
                logger.debug("parsed to non-dict for %s/%s: %s", instance_name, tool_name, type(parsed).__name__)
                return None  # Parsed to non-dict — signal error
        else:
            logger.debug("unexpected type for %s/%s: %s", instance_name, tool_name, type(tool_args).__name__)
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

    def _create_and_run_agent(
        self, agent_class: str, instance_name: str,
        args: dict, caller: str, nest_depth: int = 0, force_fresh: bool = False
    ) -> tuple:
        """Create an AgentInstance and run it through the unified loop.

        Shared helper used by both sync and parallel call_agent paths.
        Creates the instance, builds system + task messages, logs them,
        tracks in active_stack, and runs engine.run(inst).

        Returns:
            Tuple of (AgentInstance, conversation history).

        Args:
            nest_depth: Depth in the agent call chain (0 = root). Used to enforce max_nesting_depth.
            force_fresh: If True, always create new instance even if inactive one exists.
                        Used for Security/Compressor agents that should start fresh each time.
        """
        self._create_completed = False  # Reset for this execution cycle
        
        logger.debug(
            "[CALL_AGENT_DEBUG] _create_and_run_agent ENTRY — target=%s, class=%s, caller=%s, "
            "nest_depth=%d, force_fresh=%s",
            instance_name, agent_class, caller, nest_depth, force_fresh
        )
        
        # Phase 4.1: Delegate to lifecycle manager for instance creation/reuse
        inst, is_reuse = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth, force_fresh
        )

        # Phase 4.1: Delegate to lifecycle manager for system message building
        sys_msg = self.lifecycle.build_system_message(agent_class, instance_name)

        # Phase 4.1: Delegate to lifecycle manager for task message building
        task_msg = self.lifecycle.build_task_message(args, caller)

        # Phase 4.1: Delegate to lifecycle manager for conversation initialization
        conv = self.lifecycle.initialize_conversation(
            inst, sys_msg, task_msg, is_reuse, instance_name, agent_class
        )

        # Phase 4.1: Delegate to lifecycle manager for settings propagation
        self.lifecycle.propagate_settings(inst, caller, agent_class)

        # Track in active stack with depth info (thread-safe via RLock)
        with self.pool._execution._state_lock:
            self.pool._execution.active_stack.append((instance_name, inst._nest_depth))

        # Item 12: Initialize sub-agent WebUI state before execution begins (Fix #3: lighter snapshot)
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, agent_class, inst, conv, final_resp=[], is_initial=True)

        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)

        try:
            # Execute through unified loop — push stream_update events so the
            # frontend sees sub-agent tab updates independently of main agent flow.
            # Without this, the main streaming loop is blocked during tool execution
            # and no WebSocket events arrive until the sub-agent finishes.
            logger.debug("starting engine.run() for %s", instance_name)
            final_resp = []
            _update_counter = 0
            _last_sub_send = 0.0
            _sub_send_interval = 0.15  # Match main loop throttle (run_agent_unified.py line 154)

            # Bug #43 Fix: Pre-execution check — don't start if this instance was terminated while waiting
            if self.pool.is_instance_terminated(instance_name):
                logger.info("instance %s terminated before execution - skipping", instance_name)
                # Clear leftover queued messages to prevent accumulation
                q = self.pool.message_queues.get(instance_name)
                if q:
                    q.clear()
                return inst, []

            for resp in self.run(inst):
                if self.pool.stopped or self.pool.is_instance_halted(instance_name) or self.pool.is_instance_terminated(instance_name):
                    break
                
                # FIX BOOL_LEAK: Unpack (messages, is_streaming) tuple from engine.run()
                # engine.run() yields tuples like (List[Message], bool), but we only need the message list
                if isinstance(resp, tuple) and len(resp) == 2:
                    final_resp = resp[0]  # Extract just the message list
                else:
                    final_resp = resp

                # Item 12: Throttled sub-agent WebUI state update (every 5 turns) — Fix #3: lighter snapshot
                _update_counter += 1
                if _update_counter % 5 == 0:
                    # Issue Y2: Use shared helper method instead of duplicated logic
                    current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
                    self._update_webui_state(instance_name, agent_class, inst, current_conv, final_resp)

                # ── Push stream_update to frontend during sub-agent execution ──
                # This is the key fix: without this, the main agent's streaming loop
                # is blocked and no WebSocket events reach the frontend. The frontend
                # relies on stream_update to call renderSubAgents() every ~200ms.
                now = time.time()  # Use time.time() for consistency with run_agent_unified.py:135
                if now - _last_sub_send >= _sub_send_interval:
                    self.stream_publisher.push_periodic_update(caller)
                    _last_sub_send = now

            # FIX MSG_COUNT_BUG: Removed conv.extend(final_resp) to prevent duplicate messages.
            # Reason: conv and instance.conversation are the same object reference (from lifecycle_manager.initialize_conversation).
            # Messages are already added to instance.conversation during engine.run() via _process_response() at line 1935.
            # Extending again here caused each LLM response to be duplicated in the conversation.
            # See: .agent_lessons/lessons_msg_count_bug.md for detailed analysis.
            self._create_completed = True  # Mark for finally-block EXIT log reason tracking

            # Item 12: Always emit final sub-agent state after loop completes (Fix #3: lighter snapshot)
            # Ensures even short-lived agents (<5 turns) appear in the WebUI
            # Issue Y2: Use shared helper method instead of duplicated logic
            current_conv = list(inst.conversation) if hasattr(inst, 'conversation') else conv
            self._update_webui_state(instance_name, agent_class, inst, current_conv, final_resp)

            # ── Push final stream_update after sub-agent completes ──
            self.stream_publisher.push_final_state(inst, caller)
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
                "[CALL_AGENT_DEBUG] _create_and_run_agent EXIT — target=%s, reason=%s, "
                "inst_type=%s, conv_len=%d, final_resp_len=%d",
                instance_name, 'completed' if _completed else 'aborted', type(inst).__name__,
                len(conv), len(final_resp) if 'final_resp' in locals() else 0
            )

        return inst, conv

    def _create_system_agent(
        self, agent_class: str, instance_name: str,
        task: str, caller: str, context: str = ""
    ) -> 'AgentInstance':
        """Create a fresh AgentInstance for system-invoked agents (Security, Compressor).
        
        Unlike _create_and_run_agent(), this always creates a NEW instance even if one
        with the same name exists. This is needed for agents that should start fresh
        each time they're invoked (no conversation history carryover).
        
        This method:
        - Creates new AgentInstance (never reuses existing)
        - Adds to pool.instances
        - Initializes pool.instance_state for UI visibility
        - Sets up active_stack tracking
        - Returns the instance ready for engine.run() execution
        
        Args:
            agent_class: The agent class name (e.g., 'Security', 'Compressor')
            instance_name: The instance name (usually same as agent_class for system agents)
            task: The task prompt to give the agent
            caller: The parent/caller instance name
            context: Optional context to prepend to task
            
        Returns:
            AgentInstance ready for execution via engine.run()
        """
        # FIX #1 (reviewer): Delegate to lifecycle manager instead of duplicating logic
        
        # Build args dict for lifecycle manager's build_task_message
        args = {'task': task, 'context': context}
        
        # Use lifecycle manager with force_fresh=True for system agents
        inst, is_reuse = self.lifecycle.find_or_create_instance(
            agent_class, instance_name, caller, nest_depth=0, force_fresh=True
        )
        
        # Build system message using lifecycle manager
        sys_msg = self.lifecycle.build_system_message(agent_class, instance_name)
        
        # Build task message using lifecycle manager
        task_msg = self.lifecycle.build_task_message(args, caller)
        
        # Initialize conversation using lifecycle manager (pass actual is_reuse value)
        conv = self.lifecycle.initialize_conversation(
            inst, sys_msg, task_msg, is_reuse=is_reuse, instance_name=instance_name, agent_class=agent_class
        )
        
        # Phase 4.1: Propagate settings from caller to system agent
        self.lifecycle.propagate_settings(inst, caller, agent_class)
        
        # Track in active stack (thread-safe)
        self.pool.active_stack_append(instance_name, 0)
        
        # Initialize WebUI state for immediate tab visibility
        # Issue Y2: Use shared helper method instead of duplicated logic
        self._update_webui_state(instance_name, agent_class, inst, conv, final_resp=[], is_initial=True)
        
        # Phase 4.4: Delegate to StreamPublisher for WebSocket push
        self.stream_publisher.push_initial_state(inst, caller)
        
        return inst

    # ═══════════════════════════════════════════════════════════════════════
    #  WebUI State Update Helpers (Issue Y2: Extract duplicated logic)
    # ═══════════════════════════════════════════════════════════════════════

    def _update_webui_state(
        self, instance_name: str, agent_class: str, inst: AgentInstance,
        conv: list, final_resp: list = None, is_initial: bool = False
    ) -> None:
        """Update WebUI state for an agent instance (shared helper to eliminate duplication).
        
        Extracted from duplicated logic in _create_and_run_agent and _create_system_agent.
        Handles initial, periodic, and final state updates with thread-safe operations.
        
        Args:
            instance_name: The agent instance name
            agent_class: The agent class type
            inst: The AgentInstance object
            conv: Current conversation list
            final_resp: Optional final response for message summary extraction
            is_initial: If True, use empty summary (initial state); otherwise extract from final_resp
        """
        try:
            # Build lightweight summary of latest message (or empty for initial state)
            latest_summary = ''
            if not is_initial and final_resp:
                last_msg = final_resp[-1]
                content = last_msg.get('content', '') if isinstance(last_msg, dict) else getattr(last_msg, 'content', '')
                latest_summary = str(content)[:500] if content else ''
            
            # Thread-safe state read - snapshot under lock before building dict
            with inst._state_lock:
                current_state = inst.state
            
            state = {
                'active': current_state in (AgentState.RUNNING, AgentState.SLEEPING),
                'agent_state': current_state.name,  # Send actual state name for activity indicator coloring
                'agent_name': f"{instance_name} ({agent_class})",
                'message_count': len(conv),
                'latest_message_summary': latest_summary,
                'conversation_length_tokens': getattr(inst, '_cached_token_count', 0),
            }
            
            # Update pool state (thread-safe)
            with self.pool._execution._state_lock:
                self.pool.instance_state[instance_name] = state
                
        except Exception as e:
            logger.debug(f"WebUI state update for {instance_name} failed (non-critical): {e}")

    # ═══════════════════════════════════════════════════════════════════════
    #  Helper methods — token counting, tool detection, truncation
    # ═══════════════════════════════════════════════════════════════════════

    def _get_max_tokens(self, instance: AgentInstance) -> int:
        """Resolve the effective max_input_tokens from LLM config.

        Delegates to shared helper _resolve_max_tokens from api_integration
        to eliminate code duplication and fix OAI detection read-path bug.
        """
        from agent_cascade.api_integration import _resolve_max_tokens
        return _resolve_max_tokens(self.pool, instance)

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
                # Explicit type guard for known message types (dict or Message object)
                if isinstance(msg, list):
                    # Skip unexpected list objects to prevent incorrect processing
                    continue
                
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
                for m in messages if not isinstance(m, list)
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
        """Remove thinking tags from reasoning content.
        
        Delegates to module-level helper for consistency.
        """
        return _normalize_thinking_blocks(text)

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

    # Note: _truncate_tool_result removed during refactoring.
    # Callers should use self.tool_dispatcher.truncate_tool_result() directly.